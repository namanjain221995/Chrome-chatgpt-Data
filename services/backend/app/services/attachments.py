"""Attachment lifecycle: init -> direct S3 upload -> complete -> finalize.

State machine
-------------
``pending``     presigned URL issued, nothing uploaded yet
``quarantine``  object exists in S3 and declared size/checksum match the head
``clean``       worker re-hashed the bytes, magic-bytes matched the MIME type,
                and the object was copied under ``attachments/clean/``
``rejected``    a verification step failed; the object stays in quarantine for
                forensics and is expired by lifecycle policy
``metadata_only`` the page never exposed the bytes (historical or generated
                files); only metadata is recorded
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.crypto import safe_filename, utcnow
from app.core.errors import NotFoundError, PolicyError, ValidationError
from app.core.logging import get_logger
from app.models.attachment import Attachment, MessageAttachment
from app.models.conversation import Conversation, Message
from app.models.enums import AttachmentState, CaptureCompleteness, JobKind
from app.schemas.attachments import AttachmentCompleteIn, AttachmentInitIn
from app.services import jobs as jobs_service
from app.services.ingest import IngestContext, get_or_create_conversation
from app.services.policy import assert_attachment_capture_allowed
from app.services.storage import StorageService, attachment_key, get_storage

logger = get_logger(__name__)

#: Server-side allowlist. The browser-declared MIME type is only a hint; the
#: worker re-checks magic bytes before anything is promoted to ``clean``.
ALLOWED_MIME_TYPES: dict[str, tuple[str, ...]] = {
    "image/png": (".png",),
    "image/jpeg": (".jpg", ".jpeg"),
    "image/webp": (".webp",),
    "image/gif": (".gif",),
    "application/pdf": (".pdf",),
    "text/plain": (".txt", ".log", ".md"),
    "text/csv": (".csv",),
    "text/markdown": (".md",),
    "application/json": (".json",),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": (".docx",),
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": (".xlsx",),
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": (".pptx",),
}

#: Magic-byte prefixes checked by the worker, keyed by MIME type.
MAGIC_BYTES: dict[str, tuple[bytes, ...]] = {
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/gif": (b"GIF87a", b"GIF89a"),
    "image/webp": (b"RIFF",),
    "application/pdf": (b"%PDF-",),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": (b"PK\x03\x04",),
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": (b"PK\x03\x04",),
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": (b"PK\x03\x04",),
}

#: Types validated as decodable UTF-8 rather than by magic bytes.
TEXTUAL_MIME_TYPES = frozenset({"text/plain", "text/csv", "text/markdown", "application/json"})


def allowed_extensions() -> list[str]:
    return sorted({ext for exts in ALLOWED_MIME_TYPES.values() for ext in exts})


@dataclass(frozen=True)
class PresignedUpload:
    attachment: Attachment
    upload_url: str | None
    headers: dict[str, str]
    expires_at: datetime | None
    duplicate: bool


def validate_attachment_metadata(payload: AttachmentInitIn, settings: Settings) -> str:
    """Validate MIME, extension and size. Returns the sanitised filename."""
    if payload.byte_size > settings.max_attachment_bytes:
        raise ValidationError(
            "Attachment exceeds the configured maximum size",
            code="attachment_too_large",
            details={"max_bytes": settings.max_attachment_bytes},
        )
    if payload.mime_type not in ALLOWED_MIME_TYPES:
        raise ValidationError(
            "Attachment MIME type is not allowed",
            code="mime_type_not_allowed",
            details={"allowed": sorted(ALLOWED_MIME_TYPES)},
        )
    safe = safe_filename(payload.filename)
    suffix = ("." + safe.rsplit(".", 1)[-1].lower()) if "." in safe else ""
    if suffix not in ALLOWED_MIME_TYPES[payload.mime_type]:
        raise ValidationError(
            "Filename extension does not match the declared MIME type",
            code="extension_mismatch",
            details={"expected": list(ALLOWED_MIME_TYPES[payload.mime_type])},
        )
    return safe


async def init_attachment(
    session: AsyncSession,
    ctx: IngestContext,
    payload: AttachmentInitIn,
    *,
    storage: StorageService | None = None,
) -> PresignedUpload:
    settings = ctx.settings
    assert_attachment_capture_allowed(settings)
    storage = storage or get_storage()
    safe_name = validate_attachment_metadata(payload, settings)

    conversation, _ = await get_or_create_conversation(
        session,
        ctx,
        source_conversation_id=payload.source_conversation_id,
        completeness=CaptureCompleteness.LIVE_ONLY,
    )

    existing = (
        await session.execute(
            select(Attachment).where(
                Attachment.conversation_id == conversation.id,
                Attachment.client_attachment_id == payload.client_attachment_id,
            )
        )
    ).scalar_one_or_none()

    if existing is not None and existing.state in (
        AttachmentState.CLEAN,
        AttachmentState.QUARANTINE,
        AttachmentState.METADATA_ONLY,
    ):
        return PresignedUpload(existing, None, {}, None, duplicate=True)

    attachment = existing or Attachment(
        id=uuid.uuid4(),
        organization_id=ctx.organization.id,
        workspace_id=ctx.workspace.id,
        conversation_id=conversation.id,
        user_id=ctx.user.id,
        device_id=ctx.device_id,
        client_attachment_id=payload.client_attachment_id,
    )
    attachment.original_filename = payload.filename[:512]
    attachment.safe_filename = safe_name
    attachment.declared_mime_type = payload.mime_type
    attachment.extension = (
        ("." + safe_name.rsplit(".", 1)[-1].lower()) if "." in safe_name else None
    )
    attachment.byte_size = payload.byte_size
    attachment.sha256 = payload.sha256
    attachment.source_file_id = payload.source_file_id
    attachment.metadata_only = payload.metadata_only

    if payload.metadata_only:
        # Generated images and historical attachments frequently do not expose
        # their original bytes to the page. Record what is visible, honestly.
        attachment.state = AttachmentState.METADATA_ONLY
        attachment.metadata_json = {
            **(attachment.metadata_json or {}),
            "reason": "bytes_not_available_in_page",
            "relation": payload.relation,
        }
        session.add(attachment)
        await session.flush()
        return PresignedUpload(attachment, None, {}, None, duplicate=False)

    key = attachment_key(
        stage="quarantine",
        workspace_hash=ctx.workspace_hash,
        conversation_id=conversation.source_conversation_id,
        attachment_id=str(attachment.id),
        filename=safe_name,
    )
    url, headers, expires_at = storage.presign_put(
        key=key,
        content_type=payload.mime_type,
        content_length=payload.byte_size,
        sha256_hex_digest=payload.sha256,
    )
    attachment.quarantine_s3_key = key
    attachment.state = AttachmentState.PENDING
    attachment.presign_issued_at = utcnow()
    attachment.presign_expires_at = expires_at
    session.add(attachment)
    await session.flush()
    return PresignedUpload(attachment, url, headers, expires_at, duplicate=False)


async def complete_attachment(
    session: AsyncSession,
    ctx: IngestContext,
    payload: AttachmentCompleteIn,
    *,
    storage: StorageService | None = None,
) -> tuple[Attachment, bool, str | None]:
    """Verify the uploaded object and queue byte-level finalization."""
    storage = storage or get_storage()
    attachment = (
        await session.execute(
            select(Attachment).where(
                Attachment.id == payload.attachment_id,
                Attachment.organization_id == ctx.organization.id,
            )
        )
    ).scalar_one_or_none()
    if attachment is None:
        raise NotFoundError("Unknown attachment", code="attachment_not_found")

    # An attachment may only be completed by the employee who initiated it.
    if attachment.user_id is not None and attachment.user_id != ctx.user.id:
        raise PolicyError(
            "Attachment does not belong to the authenticated employee",
            code="attachment_owner_mismatch",
        )

    if attachment.state == AttachmentState.CLEAN:
        return attachment, True, "already_clean"

    if attachment.sha256 != payload.sha256 or attachment.byte_size != payload.byte_size:
        attachment.state = AttachmentState.REJECTED
        attachment.rejection_reason = "declared checksum or size changed after init"
        return attachment, False, "checksum_or_size_mismatch"

    if attachment.presign_expires_at and attachment.presign_expires_at < utcnow() - timedelta(
        minutes=15
    ):
        # The upload window plus a generous grace period has passed.
        attachment.state = AttachmentState.EXPIRED
        attachment.rejection_reason = "upload window expired"
        return attachment, False, "upload_expired"

    if not attachment.quarantine_s3_key:
        return attachment, False, "no_upload_key"

    head = await storage.head_object(attachment.quarantine_s3_key)
    if not head.exists:
        return attachment, False, "object_missing"
    if head.byte_size != attachment.byte_size:
        attachment.state = AttachmentState.REJECTED
        attachment.rejection_reason = "stored object size does not match declared size"
        return attachment, False, "size_mismatch"

    attachment.state = AttachmentState.QUARANTINE
    attachment.uploaded_at = utcnow()
    attachment.verified_byte_size = head.byte_size
    attachment.s3_version_id = head.version_id

    linked_message_id = await _link_attachment(
        session, ctx, attachment=attachment, source_message_id=payload.source_message_id
    )

    await jobs_service.enqueue_job(
        session,
        kind=JobKind.FINALIZE_ATTACHMENT,
        organization_id=ctx.organization.id,
        priority=150,
        dedupe_key=f"finalize_attachment:{attachment.id}",
        payload={"attachment_id": str(attachment.id), "workspace_hash": ctx.workspace_hash},
    )
    return attachment, True, None if linked_message_id else "not_linked_yet"


async def _link_attachment(
    session: AsyncSession,
    ctx: IngestContext,
    *,
    attachment: Attachment,
    source_message_id: str | None,
) -> uuid.UUID | None:
    """Bind the attachment to its committed message, if one is known yet."""
    message: Message | None = None
    if source_message_id:
        message = (
            await session.execute(
                select(Message).where(
                    Message.conversation_id == attachment.conversation_id,
                    Message.source_message_id == source_message_id,
                )
            )
        ).scalar_one_or_none()

    if message is None:
        # Fall back to the most recent user message in this conversation: the
        # employee attached the file to the turn they were composing.
        message = (
            (
                await session.execute(
                    select(Message)
                    .where(
                        Message.conversation_id == attachment.conversation_id,
                        Message.role == "user",
                    )
                    .order_by(Message.sequence_index.desc())
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )

    if message is None:
        return None

    if message.organization_id != ctx.organization.id:
        raise PolicyError("Cross-organization attachment linkage refused", code="linkage_denied")

    existing_link = (
        await session.execute(
            select(MessageAttachment).where(
                MessageAttachment.attachment_id == attachment.id,
                MessageAttachment.message_id == message.id,
            )
        )
    ).scalar_one_or_none()
    if existing_link is None:
        session.add(
            MessageAttachment(
                id=uuid.uuid4(),
                attachment_id=attachment.id,
                message_id=message.id,
                message_version_id=message.current_version_id,
                conversation_id=attachment.conversation_id,
                position=0,
                relation="uploaded_by_user",
            )
        )
    message.has_attachments = True
    return message.id


def detect_mime_from_bytes(data: bytes, declared: str | None) -> str | None:
    """Magic-byte sniffing for the allowlisted formats."""
    head = data[:16]
    for mime, prefixes in MAGIC_BYTES.items():
        for prefix in prefixes:
            if head.startswith(prefix):
                if mime == "image/webp":
                    # RIFF....WEBP
                    if len(data) >= 12 and data[8:12] == b"WEBP":
                        return "image/webp"
                    continue
                return mime
    if declared in TEXTUAL_MIME_TYPES:
        try:
            data[: min(len(data), 65536)].decode("utf-8")
            return declared
        except UnicodeDecodeError:
            return None
    return None


async def pending_attachment_count(session: AsyncSession, organization_id: uuid.UUID | None) -> int:
    from sqlalchemy import func

    stmt = (
        select(func.count())
        .select_from(Attachment)
        .where(Attachment.state.in_([AttachmentState.PENDING, AttachmentState.QUARANTINE]))
    )
    if organization_id:
        stmt = stmt.where(Attachment.organization_id == organization_id)
    return int((await session.execute(stmt)).scalar_one())


async def expire_stale_pending(session: AsyncSession, *, older_than_minutes: int = 60) -> int:
    """Mark presigned-but-never-uploaded attachments as expired."""
    cutoff = utcnow() - timedelta(minutes=older_than_minutes)
    rows = (
        (
            await session.execute(
                select(Attachment).where(
                    Attachment.state == AttachmentState.PENDING,
                    Attachment.presign_expires_at < cutoff,
                )
            )
        )
        .scalars()
        .all()
    )
    for attachment in rows:
        attachment.state = AttachmentState.EXPIRED
        attachment.rejection_reason = "never uploaded before the presigned URL expired"
    return len(rows)


async def conversation_for_attachment(
    session: AsyncSession, attachment: Attachment
) -> Conversation | None:
    return await session.get(Conversation, attachment.conversation_id)
