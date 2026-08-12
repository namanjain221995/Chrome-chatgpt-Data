"""Curated export to JSONL on S3.

Hard rules (enforced here, not just documented):
  * ``TRAINING_EXPORT_ENABLED`` must be true for curated training exports;
  * only conversations with ``training_approvals.status='approved'`` are eligible;
  * legal-hold records are excluded unless the export kind is a legal-hold bundle;
  * splitting is by *whole conversation*, so a train/test leak cannot occur;
  * nothing here ever starts model training.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.crypto import canonical_json_sha256, sha256_hex, utcnow
from app.core.errors import PolicyError
from app.core.logging import get_logger
from app.models.attachment import Attachment, MessageAttachment
from app.models.conversation import Conversation, Message, MessageVersion
from app.models.enums import ApprovalStatus, AttachmentState, ExportKind, ExportStatus
from app.models.governance import Export, TrainingApproval
from app.services.storage import (
    StorageService,
    export_manifest_key,
    export_part_key,
)

logger = get_logger(__name__)

RECORDS_PER_PART = 5000


def assert_export_allowed(kind: ExportKind, settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    if kind == ExportKind.CURATED_TRAINING_JSONL and not settings.training_export_enabled:
        raise PolicyError(
            "Curated training export is disabled (TRAINING_EXPORT_ENABLED=false)",
            code="training_export_disabled",
        )


def split_for_conversation(conversation_id: uuid.UUID, ratios: dict[str, float]) -> str:
    """Deterministic whole-conversation split.

    Hashing the conversation id means the same conversation always lands in the
    same split, so no message of a conversation can leak across splits.
    """
    if not ratios:
        return "train"
    bucket = int(sha256_hex(str(conversation_id))[:8], 16) / 0xFFFFFFFF
    cumulative = 0.0
    for name in sorted(ratios):
        cumulative += ratios[name]
        if bucket < cumulative:
            return name
    return sorted(ratios)[-1]


async def eligible_conversation_ids(
    session: AsyncSession,
    *,
    export: Export,
    settings: Settings | None = None,
) -> list[uuid.UUID]:
    settings = settings or get_settings()
    filters = export.filters or {}

    stmt = select(Conversation.id).where(
        Conversation.organization_id == export.organization_id,
        Conversation.deleted_at.is_(None),
    )

    if export.kind != ExportKind.LEGAL_HOLD_BUNDLE:
        stmt = stmt.where(Conversation.legal_hold.is_(False))

    if export.kind == ExportKind.CURATED_TRAINING_JSONL:
        approved = (
            select(TrainingApproval.conversation_id)
            .where(
                TrainingApproval.organization_id == export.organization_id,
                TrainingApproval.status == ApprovalStatus.APPROVED.value,
                TrainingApproval.message_id.is_(None),
                TrainingApproval.contains_secrets.is_(False),
            )
            .scalar_subquery()
        )
        stmt = stmt.where(Conversation.id.in_(approved))
        # Never export a conversation we cannot vouch for.
        stmt = stmt.where(
            Conversation.capture_completeness.in_(
                ["complete_current_page", "reconciled", "compliance_verified"]
            )
        )

    if filters.get("conversation_ids"):
        stmt = stmt.where(Conversation.id.in_([uuid.UUID(c) for c in filters["conversation_ids"]]))
    if filters.get("workspace_id"):
        stmt = stmt.where(Conversation.workspace_id == uuid.UUID(filters["workspace_id"]))
    if filters.get("from_time"):
        stmt = stmt.where(Conversation.created_at >= filters["from_time"])
    if filters.get("to_time"):
        stmt = stmt.where(Conversation.created_at <= filters["to_time"])

    return list((await session.execute(stmt.order_by(Conversation.id))).scalars().all())


async def build_conversation_record(
    session: AsyncSession, conversation_id: uuid.UUID, *, include_attachments: bool
) -> dict[str, Any] | None:
    conversation = await session.get(Conversation, conversation_id)
    if conversation is None or conversation.deleted_at is not None:
        return None

    messages = (
        (
            await session.execute(
                select(Message)
                .where(Message.conversation_id == conversation.id, Message.deleted_at.is_(None))
                .order_by(Message.sequence_index.asc(), Message.created_at.asc())
            )
        )
        .scalars()
        .all()
    )

    current_ids = [m.current_version_id for m in messages if m.current_version_id]
    versions: dict[uuid.UUID, MessageVersion] = {}
    if current_ids:
        rows = (
            (
                await session.execute(
                    select(MessageVersion).where(MessageVersion.id.in_(current_ids))
                )
            )
            .scalars()
            .all()
        )
        versions = {v.id: v for v in rows}

    turns: list[dict[str, Any]] = []
    for message in messages:
        version = versions.get(message.current_version_id) if message.current_version_id else None
        if version is None:
            continue
        turns.append(
            {
                "role": str(message.role),
                "text": version.plain_text,
                "sequence_index": message.sequence_index,
                "source_message_id": message.source_message_id,
                "content_sha256": version.content_sha256,
                "completion_status": str(version.completion_status),
            }
        )

    attachments: list[dict[str, Any]] = []
    if include_attachments:
        attachment_rows = (
            (
                await session.execute(
                    select(Attachment)
                    .join(MessageAttachment, MessageAttachment.attachment_id == Attachment.id)
                    .where(MessageAttachment.conversation_id == conversation.id)
                )
            )
            .scalars()
            .all()
        )
        attachments = [
            {
                "attachment_id": str(a.id),
                "filename": a.safe_filename,
                "mime_type": a.detected_mime_type or a.declared_mime_type,
                "sha256": a.verified_sha256 or a.sha256,
                "s3_key": a.clean_s3_key,
                "state": str(a.state),
            }
            for a in attachment_rows
            if a.state == AttachmentState.CLEAN
        ]

    pairs = _prompt_answer_pairs(turns)
    record = {
        "conversation_id": str(conversation.id),
        "source_conversation_id": conversation.source_conversation_id,
        "title": conversation.title,
        "capture_completeness": str(conversation.capture_completeness),
        "message_count": len(turns),
        "messages": turns,
        "pairs": pairs,
        "attachments": attachments,
        "snapshot_sha256": conversation.snapshot_sha256,
    }
    record["record_sha256"] = canonical_json_sha256(record)
    return record


def _prompt_answer_pairs(turns: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Adjacent user->assistant pairs, in order. Never reordered or invented."""
    pairs: list[dict[str, str]] = []
    for index, turn in enumerate(turns[:-1]):
        following = turns[index + 1]
        if turn["role"] == "user" and following["role"] == "assistant":
            pairs.append({"prompt": turn["text"], "answer": following["text"]})
    return pairs


async def run_export(
    session: AsyncSession,
    export: Export,
    *,
    storage: StorageService,
    settings: Settings | None = None,
) -> Export:
    settings = settings or get_settings()
    assert_export_allowed(ExportKind(export.kind), settings)

    export.status = ExportStatus.RUNNING
    export.started_at = utcnow()
    await session.flush()

    conversation_ids = await eligible_conversation_ids(session, export=export, settings=settings)
    ratios = dict(export.split_ratios or {"train": 1.0})
    use_split = export.split_strategy == "conversation_hash"

    buckets: dict[str, list[dict[str, Any]]] = {}
    for conversation_id in conversation_ids:
        record = await build_conversation_record(
            session, conversation_id, include_attachments=export.include_attachments
        )
        if record is None:
            continue
        split = split_for_conversation(conversation_id, ratios) if use_split else "all"
        record["split"] = split
        buckets.setdefault(split, []).append(record)

    parts: list[dict[str, Any]] = []
    total_records = 0
    total_bytes = 0
    part_number = 0

    for split in sorted(buckets):
        for chunk in _chunks(buckets[split], RECORDS_PER_PART):
            part_number += 1
            key = export_part_key(
                export_id=str(export.id),
                part_number=part_number,
                split=split if use_split else None,
            )
            result = await storage.put_jsonl_gz(
                key, chunk, metadata={"export-id": str(export.id), "split": split}
            )
            parts.append(
                {
                    "part": part_number,
                    "split": split,
                    "s3_key": key,
                    "record_count": len(chunk),
                    "byte_size": result.byte_size,
                    "sha256": result.sha256,
                }
            )
            total_records += len(chunk)
            total_bytes += result.byte_size

    manifest = {
        "export_id": str(export.id),
        "kind": str(export.kind),
        "generated_at": utcnow().isoformat(),
        "conversation_count": len(conversation_ids),
        "record_count": total_records,
        "split_strategy": export.split_strategy,
        "split_ratios": ratios,
        "parts": parts,
        "notes": [
            "Only conversations with an explicit approved training_approvals row are included.",
            "Splitting is by whole conversation; no conversation spans two splits.",
            "This export does not start any model training.",
        ],
    }
    manifest_key = export_manifest_key(export_id=str(export.id))
    manifest_result = await storage.put_json(manifest_key, manifest)

    export.status = ExportStatus.COMPLETED
    export.completed_at = utcnow()
    export.conversation_count = len(conversation_ids)
    export.record_count = total_records
    export.byte_size = total_bytes
    export.parts = parts
    export.s3_prefix = f"exports/jsonl/{export.id}/"
    export.manifest_s3_key = manifest_key
    export.manifest_sha256 = manifest_result.sha256
    logger.info(
        "export_completed",
        export_id=str(export.id),
        conversations=len(conversation_ids),
        records=total_records,
    )
    return export


def _chunks(items: list[Any], size: int) -> Iterable[list[Any]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]
