"""Retention: policy-driven soft delete, then audited physical deletion.

Legal hold always wins. A record under legal hold is never soft-deleted, never
hard-deleted, and never dropped by partition maintenance.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.crypto import utcnow
from app.core.logging import get_logger
from app.models.conversation import Conversation, Message, MessageVersion
from app.models.enums import AuditAction, RetentionAction
from app.models.events import IdempotencyKey, SourceEventKeyIndex
from app.models.governance import LegalHold, RetentionPolicy
from app.services.audit import record_audit

logger = get_logger(__name__)

#: Idempotency keys only need to outlive the client's retry horizon.
IDEMPOTENCY_RETENTION_MULTIPLIER = 4


async def apply_legal_hold(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    conversation_ids: list[uuid.UUID],
    hold_name: str,
    actor_user_id: uuid.UUID | None,
    matter_reference: str | None = None,
) -> LegalHold:
    hold = (
        await session.execute(
            select(LegalHold).where(
                LegalHold.organization_id == organization_id, LegalHold.name == hold_name
            )
        )
    ).scalar_one_or_none()
    if hold is None:
        hold = LegalHold(
            id=uuid.uuid4(),
            organization_id=organization_id,
            name=hold_name,
            matter_reference=matter_reference,
            scope={"conversation_ids": [str(c) for c in conversation_ids]},
            created_by_user_id=actor_user_id,
        )
        session.add(hold)
    else:
        scope = dict(hold.scope or {})
        existing = set(scope.get("conversation_ids", []))
        existing.update(str(c) for c in conversation_ids)
        scope["conversation_ids"] = sorted(existing)
        hold.scope = scope
        hold.released_at = None

    if conversation_ids:
        await session.execute(
            update(Conversation)
            .where(
                Conversation.organization_id == organization_id,
                Conversation.id.in_(conversation_ids),
            )
            .values(legal_hold=True)
        )
        await session.execute(
            update(Message)
            .where(Message.conversation_id.in_(conversation_ids))
            .values(legal_hold=True)
        )
    await record_audit(
        session,
        action=AuditAction.LEGAL_HOLD_SET,
        organization_id=organization_id,
        actor_user_id=actor_user_id,
        resource_type="legal_hold",
        resource_id=hold_name,
        details={"conversation_count": len(conversation_ids)},
    )
    return hold


async def soft_delete_expired(
    session: AsyncSession, *, policy: RetentionPolicy, limit: int = 1000
) -> int:
    """Soft-delete conversations older than the policy window."""
    cutoff = utcnow() - timedelta(days=policy.retain_days)
    ids = list(
        (
            await session.execute(
                select(Conversation.id)
                .where(
                    Conversation.organization_id == policy.organization_id,
                    Conversation.deleted_at.is_(None),
                    Conversation.legal_hold.is_(False),
                    Conversation.created_at < cutoff,
                )
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    if not ids:
        return 0

    now = utcnow()
    reason = f"retention_policy:{policy.name}"
    await session.execute(
        update(Conversation)
        .where(Conversation.id.in_(ids), Conversation.legal_hold.is_(False))
        .values(deleted_at=now, deletion_reason=reason)
    )
    await session.execute(
        update(Message)
        .where(Message.conversation_id.in_(ids), Message.legal_hold.is_(False))
        .values(deleted_at=now, deletion_reason=reason)
    )
    await session.execute(
        update(MessageVersion)
        .where(MessageVersion.conversation_id.in_(ids), MessageVersion.legal_hold.is_(False))
        .values(deleted_at=now, deletion_reason=reason)
    )
    await record_audit(
        session,
        action=AuditAction.RETENTION_SOFT_DELETE,
        organization_id=policy.organization_id,
        resource_type="conversation",
        resource_id=f"batch:{len(ids)}",
        details={"policy": policy.name, "count": len(ids), "retain_days": policy.retain_days},
    )
    policy.last_run_at = now
    return len(ids)


async def hard_delete_grace_expired(
    session: AsyncSession, *, policy: RetentionPolicy, limit: int = 200
) -> int:
    """Physically delete records whose grace period has elapsed."""
    if policy.action != RetentionAction.HARD_DELETE:
        return 0
    cutoff = utcnow() - timedelta(days=policy.grace_days)
    ids = list(
        (
            await session.execute(
                select(Conversation.id)
                .where(
                    Conversation.organization_id == policy.organization_id,
                    Conversation.deleted_at.is_not(None),
                    Conversation.deleted_at < cutoff,
                    Conversation.legal_hold.is_(False),
                )
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    if not ids:
        return 0

    # Cascades remove messages, versions, parts, attachments and links.
    await session.execute(
        delete(Conversation).where(Conversation.id.in_(ids), Conversation.legal_hold.is_(False))
    )
    await record_audit(
        session,
        action=AuditAction.RETENTION_HARD_DELETE,
        organization_id=policy.organization_id,
        resource_type="conversation",
        resource_id=f"batch:{len(ids)}",
        details={"policy": policy.name, "count": len(ids), "grace_days": policy.grace_days},
    )
    logger.warning("hard_delete_executed", policy=policy.name, count=len(ids))
    return len(ids)


async def prune_idempotency_keys(session: AsyncSession, *, settings: Settings | None = None) -> int:
    settings = settings or get_settings()
    horizon_days = max(settings.offline_queue_max_age_days * IDEMPOTENCY_RETENTION_MULTIPLIER, 30)
    cutoff = utcnow() - timedelta(days=horizon_days)
    result = await session.execute(delete(IdempotencyKey).where(IdempotencyKey.created_at < cutoff))
    result2 = await session.execute(
        delete(SourceEventKeyIndex).where(SourceEventKeyIndex.created_at < cutoff)
    )
    return int(result.rowcount or 0) + int(result2.rowcount or 0)


async def ensure_default_policy(
    session: AsyncSession, *, organization_id: uuid.UUID, settings: Settings | None = None
) -> RetentionPolicy:
    settings = settings or get_settings()
    policy = (
        await session.execute(
            select(RetentionPolicy).where(
                RetentionPolicy.organization_id == organization_id,
                RetentionPolicy.is_default.is_(True),
            )
        )
    ).scalar_one_or_none()
    if policy is not None:
        return policy
    policy = RetentionPolicy(
        id=uuid.uuid4(),
        organization_id=organization_id,
        name="default",
        description="Default retention: soft delete after RAW_RETENTION_DAYS, no automatic purge.",
        applies_to="conversations",
        retain_days=settings.raw_retention_days,
        grace_days=30,
        action=RetentionAction.SOFT_DELETE,
        is_default=True,
        is_active=True,
    )
    session.add(policy)
    await session.flush()
    return policy


async def retention_summary(session: AsyncSession) -> dict[str, Any]:
    soft_deleted = (
        await session.execute(
            select(func.count())
            .select_from(Conversation)
            .where(Conversation.deleted_at.is_not(None))
        )
    ).scalar_one()
    held = (
        await session.execute(
            select(func.count()).select_from(Conversation).where(Conversation.legal_hold.is_(True))
        )
    ).scalar_one()
    return {"soft_deleted_conversations": int(soft_deleted), "legal_hold_conversations": int(held)}
