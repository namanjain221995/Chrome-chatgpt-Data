"""Normalized conversation snapshots written to S3.

The snapshot is the canonical, self-describing archive record for a
conversation. It never embeds file bytes — attachments are referenced by S3
key and checksum — and its ``capture_completeness`` field states honestly how
much of the conversation the system actually observed.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import canonical_json_sha256, utcnow
from app.models.attachment import Attachment, MessageAttachment
from app.models.conversation import Conversation, Message, MessagePart, MessageVersion
from app.models.identity import Workspace
from app.services.storage import StorageService, snapshot_key

SNAPSHOT_SCHEMA_VERSION = "1.0"


async def build_snapshot(
    session: AsyncSession, conversation: Conversation, *, include_html: bool = False
) -> dict[str, Any]:
    """Assemble the full normalized JSON document for a conversation."""
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

    version_rows = (
        (
            await session.execute(
                select(MessageVersion)
                .where(
                    MessageVersion.conversation_id == conversation.id,
                    MessageVersion.deleted_at.is_(None),
                )
                .order_by(MessageVersion.message_id, MessageVersion.version_number)
            )
        )
        .scalars()
        .all()
    )
    versions_by_message: dict[uuid.UUID, list[MessageVersion]] = {}
    for version in version_rows:
        versions_by_message.setdefault(version.message_id, []).append(version)

    part_rows = (
        (
            await session.execute(
                select(MessagePart)
                .where(MessagePart.conversation_id == conversation.id)
                .order_by(MessagePart.part_index)
            )
        )
        .scalars()
        .all()
    )
    parts_by_version: dict[uuid.UUID, list[MessagePart]] = {}
    for part in part_rows:
        parts_by_version.setdefault(part.message_version_id, []).append(part)

    link_rows = (
        await session.execute(
            select(MessageAttachment, Attachment)
            .join(Attachment, Attachment.id == MessageAttachment.attachment_id)
            .where(MessageAttachment.conversation_id == conversation.id)
        )
    ).all()
    attachments_by_message: dict[uuid.UUID, list[dict[str, Any]]] = {}
    attachment_records: list[dict[str, Any]] = []
    seen_attachments: set[uuid.UUID] = set()
    for link, attachment in link_rows:
        record = _attachment_record(attachment)
        attachments_by_message.setdefault(link.message_id, []).append(
            {"attachment_id": str(attachment.id), "relation": link.relation}
        )
        if attachment.id not in seen_attachments:
            attachment_records.append(record)
            seen_attachments.add(attachment.id)

    workspace = await session.get(Workspace, conversation.workspace_id)

    message_docs: list[dict[str, Any]] = []
    for message in messages:
        versions = versions_by_message.get(message.id, [])
        message_docs.append(
            {
                "message_id": str(message.id),
                "source_message_id": message.source_message_id,
                "fingerprint": message.fingerprint,
                "role": str(message.role),
                "author_name": message.author_name,
                "sequence_index": message.sequence_index,
                "branch_id": str(message.branch_id) if message.branch_id else None,
                "parent_message_id": (
                    str(message.parent_message_id) if message.parent_message_id else None
                ),
                "completion_status": str(message.completion_status),
                "source_created_at": _iso(message.source_created_at),
                "current_version_id": (
                    str(message.current_version_id) if message.current_version_id else None
                ),
                "versions": [
                    _version_doc(v, parts_by_version.get(v.id, []), include_html=include_html)
                    for v in versions
                ],
                "attachments": attachments_by_message.get(message.id, []),
            }
        )

    document: dict[str, Any] = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "organization_id": str(conversation.organization_id),
        "workspace_id": str(conversation.workspace_id),
        "workspace_hash": workspace.workspace_hash if workspace else None,
        "conversation_id": str(conversation.id),
        "source_conversation_id": conversation.source_conversation_id,
        "source_url": conversation.source_url,
        "title": conversation.title,
        "model_slug": conversation.model_slug,
        "capture_sources": list(conversation.capture_sources or []),
        "capture_completeness": str(conversation.capture_completeness),
        "employee_id_hash": conversation.employee_id_hash,
        "created_at": _iso(conversation.created_at),
        "updated_at": _iso(conversation.source_updated_at or conversation.last_captured_at),
        "snapshot_version": conversation.snapshot_version + 1,
        "generated_at": _iso(utcnow()),
        "message_count": len(message_docs),
        "messages": message_docs,
        "attachments": attachment_records,
    }
    # The integrity hash covers everything above it.
    document["integrity"] = {"sha256": canonical_json_sha256(document), "algorithm": "sha256"}
    return document


def _version_doc(
    version: MessageVersion, parts: list[MessagePart], *, include_html: bool
) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "message_version_id": str(version.id),
        "version_number": version.version_number,
        "is_edit": version.is_edit,
        "is_regeneration": version.is_regeneration,
        "completion_status": str(version.completion_status),
        "text": version.plain_text,
        "content_sha256": version.content_sha256,
        "normalized_sha256": version.normalized_sha256,
        "char_count": version.char_count,
        "token_estimate": version.token_estimate,
        "captured_at": _iso(version.captured_at),
        "source_created_at": _iso(version.source_created_at),
        "capture_source": version.capture_source,
        "citations": list(version.citations or []),
        "raw_s3_key": version.raw_s3_key,
        "parts": [
            {
                "index": part.part_index,
                "kind": str(part.kind),
                "language": part.language,
                "text": part.text_content,
                "structured": part.structured,
            }
            for part in sorted(parts, key=lambda p: p.part_index)
        ],
    }
    if include_html and version.sanitized_html:
        doc["sanitized_html"] = version.sanitized_html
    return doc


def _attachment_record(attachment: Attachment) -> dict[str, Any]:
    return {
        "attachment_id": str(attachment.id),
        "filename": attachment.safe_filename,
        "original_filename": attachment.original_filename,
        "mime_type": attachment.detected_mime_type or attachment.declared_mime_type,
        "byte_size": attachment.byte_size,
        "sha256": attachment.verified_sha256 or attachment.sha256,
        "state": str(attachment.state),
        "metadata_only": attachment.metadata_only,
        # Keys only: bytes never appear inside conversation JSON.
        "s3_key": attachment.clean_s3_key or attachment.quarantine_s3_key,
        "s3_version_id": attachment.s3_version_id,
    }


def _iso(value: Any) -> str | None:
    return value.isoformat() if value else None


async def write_snapshot(
    session: AsyncSession,
    conversation: Conversation,
    *,
    workspace_hash: str,
    storage: StorageService,
) -> tuple[str, str]:
    """Build, upload and record the snapshot.

    Returns ``(s3_key, integrity_sha256)`` where the digest is the document's
    own ``integrity.sha256`` — the value an auditor recomputes to prove the
    archive was not altered.
    """
    document = await build_snapshot(session, conversation)
    version = conversation.snapshot_version + 1
    key = snapshot_key(
        workspace_hash=workspace_hash,
        conversation_id=conversation.source_conversation_id,
        version=version,
        when=utcnow(),
    )
    integrity_sha256 = document["integrity"]["sha256"]
    await storage.put_json(
        key,
        document,
        metadata={
            "conversation-id": str(conversation.id),
            "snapshot-version": str(version),
            "integrity-sha256": integrity_sha256,
        },
    )
    conversation.snapshot_version = version
    conversation.snapshot_s3_key = key
    conversation.snapshot_sha256 = integrity_sha256
    conversation.snapshot_stale = False
    return key, integrity_sha256
