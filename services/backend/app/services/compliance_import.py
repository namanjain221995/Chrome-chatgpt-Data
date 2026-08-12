"""Persist compliance events and fold them into the conversation model.

Ordering guarantee: an event is written to PostgreSQL *and* to S3 before the
checkpoint that would skip it is advanced. A crash therefore causes at most
reprocessing, never data loss, and reprocessing is safe because the global
``source_event_keys`` table makes ingestion idempotent.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.openai_compliance import ComplianceEvent
from app.core.crypto import pseudonymize, utcnow
from app.core.logging import get_logger
from app.models.conversation import Conversation
from app.models.enums import CaptureCompleteness, SourceEventKind, WorkspaceKind
from app.models.events import SourceEvent, SourceEventKeyIndex, SyncCheckpoint
from app.models.identity import Organization, Workspace
from app.services.storage import StorageService, compliance_raw_key

logger = get_logger(__name__)

COMPLIANCE_SOURCE = "openai_compliance"


async def get_or_create_checkpoint(
    session: AsyncSession, *, organization_id: uuid.UUID, stream: str = "default"
) -> SyncCheckpoint:
    checkpoint = (
        await session.execute(
            select(SyncCheckpoint).where(
                SyncCheckpoint.organization_id == organization_id,
                SyncCheckpoint.source == COMPLIANCE_SOURCE,
                SyncCheckpoint.stream == stream,
            )
        )
    ).scalar_one_or_none()
    if checkpoint is not None:
        return checkpoint
    checkpoint = SyncCheckpoint(
        id=uuid.uuid4(),
        organization_id=organization_id,
        source=COMPLIANCE_SOURCE,
        stream=stream,
        window_start=utcnow() - timedelta(days=1),
        state={},
    )
    session.add(checkpoint)
    await session.flush()
    return checkpoint


async def get_or_create_compliance_workspace(
    session: AsyncSession, *, organization: Organization, source_workspace_id: str | None
) -> Workspace:
    ws_hash = pseudonymize(f"{organization.slug}|{source_workspace_id or 'compliance'}")[:32]
    workspace = (
        await session.execute(
            select(Workspace).where(
                Workspace.organization_id == organization.id,
                Workspace.workspace_hash == ws_hash,
            )
        )
    ).scalar_one_or_none()
    if workspace is not None:
        return workspace
    workspace = Workspace(
        id=uuid.uuid4(),
        organization_id=organization.id,
        source_workspace_id=source_workspace_id,
        label=None,
        workspace_hash=ws_hash,
        # The compliance interface only exposes the company's own workspace.
        kind=WorkspaceKind.MANAGED_COMPANY,
        verified_at=utcnow(),
        capture_enabled=True,
    )
    session.add(workspace)
    await session.flush()
    return workspace


async def persist_event(
    session: AsyncSession,
    *,
    organization: Organization,
    event: ComplianceEvent,
    storage: StorageService,
) -> bool:
    """Write one upstream event. Returns True when it was new.

    The S3 object is written before the row is committed by the caller, which
    is what makes "raw before checkpoint" hold even under a crash.
    """
    claim = (
        pg_insert(SourceEventKeyIndex)
        .values(
            organization_id=organization.id,
            source=COMPLIANCE_SOURCE,
            source_event_id=event.source_event_id,
            source_event_created_at=utcnow(),
        )
        .on_conflict_do_nothing(index_elements=["organization_id", "source", "source_event_id"])
        .returning(SourceEventKeyIndex.source_event_id)
    )
    if (await session.execute(claim)).first() is None:
        return False  # already imported

    workspace = await get_or_create_compliance_workspace(
        session, organization=organization, source_workspace_id=event.workspace_id
    )

    now = utcnow()
    key = compliance_raw_key(
        workspace_hash=workspace.workspace_hash,
        source_event_id=event.source_event_id,
        when=event.event_time or now,
    )
    document = {
        "schema_version": "1.0",
        "source": COMPLIANCE_SOURCE,
        "source_event_id": event.source_event_id,
        "event_time": event.event_time.isoformat() if event.event_time else None,
        "kind": event.kind,
        "is_deletion": event.is_deletion,
        "received_at": now.isoformat(),
        "payload": event.raw,
        "integrity": {"payload_sha256": event.payload_sha256},
    }
    result = await storage.put_json(key, document, metadata={"source": COMPLIANCE_SOURCE})

    session.add(
        SourceEvent(
            id=uuid.uuid4(),
            created_at=now,
            organization_id=organization.id,
            source=COMPLIANCE_SOURCE,
            source_event_id=event.source_event_id,
            kind=SourceEventKind(event.kind)
            if event.kind in SourceEventKind.values()
            else SourceEventKind.UNKNOWN,
            source_conversation_id=event.conversation_id,
            source_message_id=event.message_id,
            source_workspace_id=event.workspace_id,
            actor_email_hash=pseudonymize(event.actor_email) if event.actor_email else None,
            event_time=event.event_time,
            is_deletion=event.is_deletion,
            payload=event.raw,
            payload_sha256=event.payload_sha256,
            raw_s3_key=result.key,
            raw_s3_version_id=result.version_id,
        )
    )
    # Flush so the row is visible to the caller's later reads even when the
    # session has autoflush disabled.
    await session.flush()
    return True


async def import_pending_source_events(
    session: AsyncSession, *, organization_id: uuid.UUID, limit: int = 500
) -> int:
    """Fold unprocessed source events into the conversation model.

    Compliance data is the only source permitted to assert
    ``compliance_verified`` completeness. Deletion events are preserved as
    tombstones rather than silently dropping archived rows.
    """
    rows = (
        (
            await session.execute(
                select(SourceEvent)
                .where(
                    SourceEvent.organization_id == organization_id,
                    SourceEvent.processed_at.is_(None),
                )
                .order_by(SourceEvent.event_time.asc().nullslast())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return 0

    organization = await session.get(Organization, organization_id)
    if organization is None:
        return 0

    processed = 0
    for event in rows:
        if event.source_conversation_id:
            workspace = await get_or_create_compliance_workspace(
                session, organization=organization, source_workspace_id=event.source_workspace_id
            )
            conversation = (
                await session.execute(
                    select(Conversation).where(
                        Conversation.organization_id == organization_id,
                        Conversation.workspace_id == workspace.id,
                        Conversation.source_conversation_id == event.source_conversation_id,
                    )
                )
            ).scalar_one_or_none()

            if conversation is None:
                conversation = Conversation(
                    id=uuid.uuid4(),
                    organization_id=organization_id,
                    workspace_id=workspace.id,
                    source_conversation_id=event.source_conversation_id,
                    capture_sources=["compliance_api"],
                    capture_completeness=CaptureCompleteness.COMPLIANCE_VERIFIED,
                    employee_id_hash=event.actor_email_hash,
                    source_updated_at=event.event_time,
                    first_captured_at=utcnow(),
                    last_captured_at=utcnow(),
                    snapshot_stale=True,
                )
                session.add(conversation)
            else:
                sources = list(conversation.capture_sources or [])
                if "compliance_api" not in sources:
                    sources.append("compliance_api")
                    conversation.capture_sources = sources
                conversation.capture_completeness = CaptureCompleteness.COMPLIANCE_VERIFIED
                conversation.snapshot_stale = True

            if event.is_deletion:
                # Tombstone, not a deletion: the upstream record is gone but the
                # archive keeps evidence that it existed and was removed.
                conversation.metadata_json = {
                    **(conversation.metadata_json or {}),
                    "upstream_deleted": True,
                    "upstream_deleted_at": (
                        event.event_time.isoformat() if event.event_time else None
                    ),
                    "upstream_deletion_event_id": event.source_event_id,
                }

        event.processed_at = utcnow()
        processed += 1

    await session.flush()
    return processed


async def advance_checkpoint(
    checkpoint: SyncCheckpoint,
    *,
    window_end: datetime,
    cursor: str | None,
    last_event_time: datetime | None,
    events_seen: int,
) -> None:
    """Advance only after every event in the window is durably stored."""
    checkpoint.window_start = window_end
    checkpoint.window_end = window_end
    checkpoint.cursor_value = cursor
    checkpoint.last_success_at = utcnow()
    checkpoint.last_attempt_at = utcnow()
    checkpoint.consecutive_errors = 0
    checkpoint.last_error = None
    checkpoint.total_events += events_seen
    if last_event_time and (
        checkpoint.last_event_time is None or last_event_time > checkpoint.last_event_time
    ):
        checkpoint.last_event_time = last_event_time


def record_checkpoint_failure(checkpoint: SyncCheckpoint, error: Exception) -> None:
    checkpoint.last_attempt_at = utcnow()
    checkpoint.consecutive_errors += 1
    # Type name only: an upstream message could echo sensitive request details.
    checkpoint.last_error = type(error).__name__


def checkpoint_health(checkpoint: SyncCheckpoint | None) -> dict[str, Any]:
    if checkpoint is None:
        return {"cursor_healthy": False, "reason": "no checkpoint yet"}
    now = utcnow()
    lag = (now - checkpoint.last_event_time).total_seconds() if checkpoint.last_event_time else None
    return {
        "cursor_healthy": checkpoint.consecutive_errors < 5,
        "consecutive_errors": checkpoint.consecutive_errors,
        "lag_seconds": lag,
        "last_success_at": checkpoint.last_success_at,
        "total_events": checkpoint.total_events,
    }
