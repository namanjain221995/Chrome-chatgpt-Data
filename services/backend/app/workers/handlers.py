"""Job handlers.

Every handler is idempotent: a job may run twice after a crash, and the second
run must be a safe no-op. Raw JSON always reaches S3 *before* the job that
produced it is marked complete.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.crypto import sha256_hex, utcnow
from app.core.logging import get_logger
from app.models.attachment import Attachment
from app.models.conversation import Conversation, Message, MessageVersion
from app.models.enums import (
    AttachmentState,
    CompletionStatus,
    ExportStatus,
    JobKind,
)
from app.models.events import CaptureEvent
from app.models.governance import Export, RetentionPolicy
from app.models.identity import Organization, Workspace
from app.models.jobs import Job
from app.services import attachments as attachment_service
from app.services import partitions as partition_service
from app.services import retention as retention_service
from app.services.exif import strip_metadata
from app.services.exports import run_export
from app.services.snapshots import write_snapshot
from app.services.storage import StorageService, attachment_key, get_storage

logger = get_logger(__name__)

Handler = Callable[[AsyncSession, Job], Awaitable[dict[str, Any]]]


class NonRetryableError(RuntimeError):
    """Raised when retrying cannot possibly help (bad payload, missing row)."""


# ---------------------------------------------------------------------------
# Raw event archiving
# ---------------------------------------------------------------------------


async def handle_archive_raw_event(session: AsyncSession, job: Job) -> dict[str, Any]:
    storage = get_storage()
    payload = job.payload or {}
    try:
        event_id = uuid.UUID(payload["capture_event_id"])
    except (KeyError, ValueError) as exc:
        raise NonRetryableError("capture_event_id missing or malformed") from exc

    event = (
        (await session.execute(select(CaptureEvent).where(CaptureEvent.id == event_id)))
        .scalars()
        .first()
    )
    if event is None:
        raise NonRetryableError(f"capture event {event_id} no longer exists")

    if event.archived_at is not None and event.raw_s3_key:
        return {"skipped": True, "reason": "already_archived", "s3_key": event.raw_s3_key}

    key = payload.get("s3_key")
    if not key:
        raise NonRetryableError("s3_key missing from job payload")

    document = {
        "schema_version": event.schema_version,
        "capture_event_id": str(event.id),
        "organization_id": str(event.organization_id),
        "workspace_id": str(event.workspace_id) if event.workspace_id else None,
        "conversation_id": str(event.conversation_id) if event.conversation_id else None,
        "message_id": str(event.message_id) if event.message_id else None,
        "kind": str(event.kind),
        "idempotency_key": event.idempotency_key,
        "adapter_version": event.adapter_version,
        "extension_version": event.extension_version,
        "client_captured_at": event.client_captured_at.isoformat()
        if event.client_captured_at
        else None,
        "server_received_at": event.created_at.isoformat(),
        "payload": event.payload,
        "integrity": {"payload_sha256": event.payload_sha256},
    }

    # S3 write happens first; only then is the row marked archived.
    result = await storage.put_json(key, document, metadata={"capture-event-id": str(event.id)})
    event.raw_s3_key = result.key
    event.raw_s3_version_id = result.version_id
    event.archived_at = utcnow()

    if event.message_id:
        await session.execute(
            update(MessageVersion)
            .where(MessageVersion.capture_event_id == event.id)
            .values(raw_s3_key=result.key, raw_s3_version_id=result.version_id)
        )
    return {"s3_key": result.key, "version_id": result.version_id, "sha256": result.sha256}


# ---------------------------------------------------------------------------
# Conversation snapshots
# ---------------------------------------------------------------------------


async def handle_build_snapshot(session: AsyncSession, job: Job) -> dict[str, Any]:
    storage = get_storage()
    payload = job.payload or {}
    try:
        conversation_id = uuid.UUID(payload["conversation_id"])
    except (KeyError, ValueError) as exc:
        raise NonRetryableError("conversation_id missing or malformed") from exc

    conversation = await session.get(Conversation, conversation_id)
    if conversation is None:
        raise NonRetryableError(f"conversation {conversation_id} no longer exists")
    if conversation.deleted_at is not None:
        return {"skipped": True, "reason": "conversation_soft_deleted"}

    workspace_hash = payload.get("workspace_hash")
    if not workspace_hash:
        workspace = await session.get(Workspace, conversation.workspace_id)
        workspace_hash = workspace.workspace_hash if workspace else "unknown"

    key, digest = await write_snapshot(
        session, conversation, workspace_hash=workspace_hash, storage=storage
    )
    return {"s3_key": key, "sha256": digest, "snapshot_version": conversation.snapshot_version}


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------


async def handle_finalize_attachment(session: AsyncSession, job: Job) -> dict[str, Any]:
    storage = get_storage()
    settings = get_settings()
    payload = job.payload or {}
    try:
        attachment_id = uuid.UUID(payload["attachment_id"])
    except (KeyError, ValueError) as exc:
        raise NonRetryableError("attachment_id missing or malformed") from exc

    attachment = await session.get(Attachment, attachment_id)
    if attachment is None:
        raise NonRetryableError(f"attachment {attachment_id} no longer exists")
    if attachment.state == AttachmentState.CLEAN:
        return {"skipped": True, "reason": "already_clean"}
    if attachment.state != AttachmentState.QUARANTINE or not attachment.quarantine_s3_key:
        raise NonRetryableError(f"attachment is in state {attachment.state}, not quarantine")

    data = await storage.get_bytes(
        attachment.quarantine_s3_key, max_bytes=settings.max_attachment_bytes
    )

    if len(data) != attachment.byte_size:
        return _reject_attachment(attachment, "stored size does not match declared size")

    digest = sha256_hex(data)
    if digest != attachment.sha256:
        return _reject_attachment(attachment, "sha256 mismatch between declared and stored bytes")

    detected = attachment_service.detect_mime_from_bytes(data, attachment.declared_mime_type)
    if detected is None:
        return _reject_attachment(attachment, "content does not match any allowed file type")
    if detected != attachment.declared_mime_type:
        # Office formats all begin with the ZIP magic, so an exact match is not
        # always possible; a shared container is acceptable, anything else is not.
        zip_family = {
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        }
        if not (detected in zip_family and attachment.declared_mime_type in zip_family):
            return _reject_attachment(
                attachment,
                f"declared type {attachment.declared_mime_type} does not match content",
            )

    conversation = await session.get(Conversation, attachment.conversation_id)
    workspace = await session.get(Workspace, attachment.workspace_id)
    workspace_hash = payload.get("workspace_hash") or (
        workspace.workspace_hash if workspace else "unknown"
    )
    source_conversation_id = (
        conversation.source_conversation_id if conversation else str(attachment.conversation_id)
    )

    clean_key = attachment_key(
        stage="clean",
        workspace_hash=workspace_hash,
        conversation_id=source_conversation_id,
        attachment_id=str(attachment.id),
        filename=attachment.safe_filename,
    )
    version_id = await storage.copy_object(
        source_key=attachment.quarantine_s3_key, dest_key=clean_key
    )

    curated_key: str | None = None
    curated, stripped = strip_metadata(data, detected)
    if stripped:
        curated_key = attachment_key(
            stage="curated",
            workspace_hash=workspace_hash,
            conversation_id=source_conversation_id,
            attachment_id=str(attachment.id),
            filename=attachment.safe_filename,
        )
        await storage.put_bytes(curated_key, curated, content_type=detected)

    attachment.state = AttachmentState.CLEAN
    attachment.clean_s3_key = clean_key
    attachment.curated_s3_key = curated_key
    attachment.exif_stripped = stripped
    attachment.detected_mime_type = detected
    attachment.verified_sha256 = digest
    attachment.verified_byte_size = len(data)
    attachment.verified_at = utcnow()
    attachment.s3_version_id = version_id or attachment.s3_version_id
    attachment.scan_status = "not_scanned"
    attachment.scan_result = {
        "note": "ClamAV profile is optional; enable the `scan` compose profile to scan uploads."
    }
    return {
        "state": "clean",
        "clean_s3_key": clean_key,
        "curated_s3_key": curated_key,
        "exif_stripped": stripped,
    }


def _reject_attachment(attachment: Attachment, reason: str) -> dict[str, Any]:
    attachment.state = AttachmentState.REJECTED
    attachment.rejection_reason = reason[:1000]
    attachment.verified_at = utcnow()
    logger.warning("attachment_rejected", attachment_id=str(attachment.id), reason=reason)
    return {"state": "rejected", "reason": reason}


# ---------------------------------------------------------------------------
# Partial reconciliation
# ---------------------------------------------------------------------------


async def handle_reconcile_partial(session: AsyncSession, job: Job) -> dict[str, Any]:
    """Promote a partial message once a complete version exists for it."""
    payload = job.payload or {}
    try:
        message_id = uuid.UUID(payload["message_id"])
    except (KeyError, ValueError) as exc:
        raise NonRetryableError("message_id missing or malformed") from exc

    message = await session.get(Message, message_id)
    if message is None:
        raise NonRetryableError("message no longer exists")
    if message.completion_status == CompletionStatus.COMPLETE:
        return {"skipped": True, "reason": "already_complete"}

    versions = (
        (
            await session.execute(
                select(MessageVersion)
                .where(MessageVersion.message_id == message.id)
                .order_by(MessageVersion.version_number.desc())
            )
        )
        .scalars()
        .all()
    )
    complete = next((v for v in versions if v.completion_status == CompletionStatus.COMPLETE), None)
    if complete is None:
        # Nothing to do yet; the conversation has not been reopened.
        return {"skipped": True, "reason": "no_complete_version_yet"}

    message.completion_status = CompletionStatus.COMPLETE
    message.current_version_id = complete.id
    for version in versions:
        if version.completion_status == CompletionStatus.PARTIAL:
            version.completion_status = CompletionStatus.RECONCILED
            version.metadata_json = {
                **(version.metadata_json or {}),
                "superseded_by": str(complete.id),
                "reconciled_at": utcnow().isoformat(),
            }
    conversation = await session.get(Conversation, message.conversation_id)
    if conversation is not None:
        conversation.snapshot_stale = True
    return {"reconciled": True, "current_version_id": str(complete.id)}


# ---------------------------------------------------------------------------
# Exports, retention, maintenance
# ---------------------------------------------------------------------------


async def handle_run_export(session: AsyncSession, job: Job) -> dict[str, Any]:
    storage = get_storage()
    payload = job.payload or {}
    try:
        export_id = uuid.UUID(payload["export_id"])
    except (KeyError, ValueError) as exc:
        raise NonRetryableError("export_id missing or malformed") from exc

    export = await session.get(Export, export_id)
    if export is None:
        raise NonRetryableError("export no longer exists")
    if export.status == ExportStatus.COMPLETED:
        return {"skipped": True, "reason": "already_completed"}

    try:
        await run_export(session, export, storage=storage)
    except Exception as exc:
        export.status = ExportStatus.FAILED
        export.error_summary = f"{type(exc).__name__}: {exc}"[:2000]
        raise
    return {
        "conversation_count": export.conversation_count,
        "record_count": export.record_count,
        "manifest_s3_key": export.manifest_s3_key,
    }


async def handle_retention_sweep(session: AsyncSession, job: Job) -> dict[str, Any]:
    settings = get_settings()
    results: dict[str, Any] = {"soft_deleted": 0, "hard_deleted": 0, "keys_pruned": 0}

    policies = (
        (await session.execute(select(RetentionPolicy).where(RetentionPolicy.is_active.is_(True))))
        .scalars()
        .all()
    )
    for policy in policies:
        results["soft_deleted"] += await retention_service.soft_delete_expired(
            session, policy=policy
        )
        results["hard_deleted"] += await retention_service.hard_delete_grace_expired(
            session, policy=policy
        )

    results["keys_pruned"] = await retention_service.prune_idempotency_keys(
        session, settings=settings
    )
    results["attachments_expired"] = await attachment_service.expire_stale_pending(session)
    return results


async def handle_maintain_partitions(session: AsyncSession, job: Job) -> dict[str, Any]:
    created = await partition_service.ensure_partitions(session)
    default_rows = {}
    for table in ("capture_events", "source_events", "audit_events"):
        default_rows[table] = await partition_service.default_partition_rowcount(session, table)
    return {"partitions_ensured": len(created), "default_partition_rows": default_rows}


async def handle_cleanup_stale(session: AsyncSession, job: Job) -> dict[str, Any]:
    """Housekeeping: prune finished jobs and their attempt history."""
    cutoff = utcnow() - timedelta(days=14)
    result = await session.execute(
        text("DELETE FROM jobs WHERE status = 'succeeded' AND finished_at < :cutoff"),
        {"cutoff": cutoff},
    )
    return {"jobs_pruned": int(getattr(result, "rowcount", 0) or 0)}


async def handle_compliance_sync(session: AsyncSession, job: Job) -> dict[str, Any]:
    """Import already-persisted source events into the conversation model."""
    from app.services.compliance_import import import_pending_source_events

    organization_id = job.organization_id
    if organization_id is None:
        org = (await session.execute(select(Organization).limit(1))).scalars().first()
        if org is None:
            raise NonRetryableError("no organization configured")
        organization_id = org.id
    imported = await import_pending_source_events(session, organization_id=organization_id)
    return {"imported": imported}


HANDLERS: dict[JobKind, Handler] = {
    JobKind.ARCHIVE_RAW_EVENT: handle_archive_raw_event,
    JobKind.BUILD_CONVERSATION_SNAPSHOT: handle_build_snapshot,
    JobKind.FINALIZE_ATTACHMENT: handle_finalize_attachment,
    JobKind.RECONCILE_PARTIAL: handle_reconcile_partial,
    JobKind.RUN_EXPORT: handle_run_export,
    JobKind.RETENTION_SWEEP: handle_retention_sweep,
    JobKind.MAINTAIN_PARTITIONS: handle_maintain_partitions,
    JobKind.CLEANUP_STALE: handle_cleanup_stale,
    JobKind.COMPLIANCE_SYNC: handle_compliance_sync,
}


def get_handler(kind: JobKind | str) -> Handler | None:
    if isinstance(kind, str):
        try:
            kind = JobKind(kind)
        except ValueError:
            return None
    return HANDLERS.get(kind)


__all__ = ["HANDLERS", "Handler", "NonRetryableError", "StorageService", "get_handler"]
