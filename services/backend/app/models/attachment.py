"""Attachments and their linkage to messages.

Bytes never pass through FastAPI: the extension uploads directly to S3 with a
short-lived presigned PUT, and the worker verifies size, checksum and magic
bytes before an object is promoted from ``quarantine`` to ``clean``.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import AttachmentState
from app.models.identity import _enum


class Attachment(Base, UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "attachments"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    device_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("devices.id", ondelete="SET NULL"), nullable=True
    )

    client_attachment_id: Mapped[str] = mapped_column(String(128), nullable=False)
    original_filename: Mapped[str | None] = mapped_column(Text, nullable=True)
    safe_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    declared_mime_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    detected_mime_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    extension: Mapped[str | None] = mapped_column(String(32), nullable=True)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    verified_byte_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    verified_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)

    state: Mapped[AttachmentState] = mapped_column(
        _enum(AttachmentState, "attachment_state"), nullable=False, default=AttachmentState.PENDING
    )
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    source_file_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    quarantine_s3_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    clean_s3_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    curated_s3_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    s3_version_id: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    presign_issued_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    presign_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    scan_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    scan_result: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    exif_stripped: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    metadata_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    links: Mapped[list[MessageAttachment]] = relationship(
        back_populates="attachment", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint(
            "conversation_id", "client_attachment_id", name="uq_attachments_conversation_client_id"
        ),
        Index("ix_attachments_state_created", "state", "created_at"),
        Index("ix_attachments_org_sha", "organization_id", "sha256"),
        Index(
            "ix_attachments_pending_expiry",
            "presign_expires_at",
            postgresql_where=text("state = 'pending'"),
        ),
        CheckConstraint("byte_size >= 0", name="byte_size_non_negative"),
        CheckConstraint("char_length(sha256) = 64", name="sha256_is_hex64"),
        CheckConstraint(
            "state <> 'clean' OR clean_s3_key IS NOT NULL OR metadata_only = true",
            name="clean_requires_key",
        ),
    )


class MessageAttachment(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Join table binding a verified attachment to the message that carried it."""

    __tablename__ = "message_attachments"

    attachment_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("attachments.id", ondelete="CASCADE"), nullable=False
    )
    message_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    message_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("message_versions.id", ondelete="SET NULL"), nullable=True
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    relation: Mapped[str] = mapped_column(String(32), nullable=False, default="uploaded_by_user")

    attachment: Mapped[Attachment] = relationship(back_populates="links")

    __table_args__ = (
        UniqueConstraint("attachment_id", "message_id", name="uq_message_attachments_pair"),
        Index("ix_message_attachments_message", "message_id"),
        Index("ix_message_attachments_conversation", "conversation_id"),
        CheckConstraint(
            "relation in ('uploaded_by_user','generated_by_assistant','referenced_historical')",
            name="relation_allowed",
        ),
    )
