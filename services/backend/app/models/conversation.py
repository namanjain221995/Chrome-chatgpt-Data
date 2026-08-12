"""Conversations, branches, messages, versions and structured parts.

Versioning model
----------------
``messages`` is the stable identity of a turn inside a conversation.
``message_versions`` is append-only: an edited prompt or a regenerated answer
creates a new version row and moves ``messages.current_version_id``. Nothing is
ever overwritten, so an auditor can reconstruct exactly what the employee saw
and when.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import (
    CaptureCompleteness,
    CompletionStatus,
    MessageRole,
    PartKind,
)
from app.models.identity import _enum


class Conversation(Base, UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "conversations"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    source_conversation_id: Mapped[str] = mapped_column(String(255), nullable=False)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    model_slug: Mapped[str | None] = mapped_column(String(128), nullable=True)
    capture_sources: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    capture_completeness: Mapped[CaptureCompleteness] = mapped_column(
        _enum(CaptureCompleteness, "capture_completeness"),
        nullable=False,
        default=CaptureCompleteness.UNKNOWN,
    )
    employee_id_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    source_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    first_captured_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_captured_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    message_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    snapshot_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    snapshot_s3_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    snapshot_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    snapshot_stale: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    retention_policy_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("retention_policies.id", ondelete="SET NULL"),
        nullable=True,
    )
    metadata_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    messages: Mapped[list[Message]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan"
    )
    branches: Mapped[list[ConversationBranch]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "workspace_id",
            "source_conversation_id",
            name="uq_conversations_org_workspace_source",
        ),
        Index("ix_conversations_workspace_updated", "workspace_id", "source_updated_at"),
        Index("ix_conversations_user_updated", "user_id", "last_captured_at"),
        Index(
            "ix_conversations_snapshot_stale",
            "snapshot_stale",
            postgresql_where=text("snapshot_stale = true AND deleted_at IS NULL"),
        ),
        Index(
            "ix_conversations_legal_hold",
            "legal_hold",
            postgresql_where=text("legal_hold = true"),
        ),
        CheckConstraint("message_count >= 0", name="message_count_non_negative"),
        CheckConstraint(
            "NOT (legal_hold = true AND deleted_at IS NOT NULL)",
            name="legal_hold_blocks_soft_delete",
        ),
    )


class ConversationBranch(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A detected alternative path through a conversation tree."""

    __tablename__ = "conversation_branches"

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    parent_branch_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("conversation_branches.id", ondelete="SET NULL"),
        nullable=True,
    )
    source_branch_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    branch_key: Mapped[str] = mapped_column(String(64), nullable=False)
    branch_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_selected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    detected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    conversation: Mapped[Conversation] = relationship(back_populates="branches")

    __table_args__ = (
        UniqueConstraint("conversation_id", "branch_key", name="uq_branches_conversation_key"),
        Index("ix_branches_conversation_selected", "conversation_id", "is_selected"),
    )


class Message(Base, UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin):
    """Stable identity of one turn. Content lives in ``message_versions``."""

    __tablename__ = "messages"

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    branch_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("conversation_branches.id", ondelete="SET NULL"),
        nullable=True,
    )
    parent_message_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("messages.id", ondelete="SET NULL"), nullable=True
    )
    source_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    role: Mapped[MessageRole] = mapped_column(_enum(MessageRole, "message_role"), nullable=False)
    author_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    sequence_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source_created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    version_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_status: Mapped[CompletionStatus] = mapped_column(
        _enum(CompletionStatus, "completion_status"),
        nullable=False,
        default=CompletionStatus.UNKNOWN,
    )
    has_attachments: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")
    versions: Mapped[list[MessageVersion]] = relationship(
        back_populates="message",
        cascade="all, delete-orphan",
        foreign_keys="MessageVersion.message_id",
    )

    __table_args__ = (
        # Primary identity when ChatGPT exposes a source message id.
        Index(
            "uq_messages_conversation_source_id",
            "conversation_id",
            "source_message_id",
            unique=True,
            postgresql_where=text("source_message_id IS NOT NULL"),
        ),
        # Fallback identity: deterministic fingerprint.
        UniqueConstraint("conversation_id", "fingerprint", name="uq_messages_conversation_fp"),
        Index("ix_messages_conversation_sequence", "conversation_id", "sequence_index"),
        Index("ix_messages_org_created", "organization_id", "created_at"),
        Index(
            "ix_messages_partial_completion",
            "completion_status",
            postgresql_where=text("completion_status = 'partial'"),
        ),
        CheckConstraint("sequence_index >= 0", name="sequence_index_non_negative"),
        CheckConstraint("version_count >= 0", name="version_count_non_negative"),
        CheckConstraint(
            "NOT (legal_hold = true AND deleted_at IS NOT NULL)",
            name="legal_hold_blocks_soft_delete",
        ),
    )


class MessageVersion(Base, UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin):
    """Append-only content of a message: edits and regenerations add rows."""

    __tablename__ = "message_versions"

    message_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_edit: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_regeneration: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    completion_status: Mapped[CompletionStatus] = mapped_column(
        _enum(CompletionStatus, "completion_status"),
        nullable=False,
        default=CompletionStatus.COMPLETE,
    )
    # Full normalised text kept in PostgreSQL for authorized operational search.
    plain_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    sanitized_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    normalized_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    char_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    token_estimate: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    capture_source: Mapped[str] = mapped_column(
        String(32), nullable=False, default="chrome_extension"
    )
    device_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("devices.id", ondelete="SET NULL"), nullable=True
    )
    capture_event_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    raw_s3_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_s3_version_id: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    citations: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    metadata_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # Full-text search for *authorized administrative* use only. There is no
    # public search endpoint; see docs/SECURITY.md.
    search_tsv: Mapped[Any | None] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('simple', plain_text)", persisted=True),
        nullable=True,
    )

    message: Mapped[Message] = relationship(back_populates="versions", foreign_keys=[message_id])
    parts: Mapped[list[MessagePart]] = relationship(
        back_populates="version", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("message_id", "version_number", name="uq_message_versions_message_number"),
        UniqueConstraint(
            "message_id", "normalized_sha256", name="uq_message_versions_message_content"
        ),
        Index("ix_message_versions_conversation", "conversation_id", "created_at"),
        Index("ix_message_versions_org_captured", "organization_id", "captured_at"),
        # Content-identity fallback: lets ingestion recognise a re-captured
        # message whose sequence index shifted because older turns loaded.
        Index("ix_message_versions_conv_norm", "conversation_id", "normalized_sha256"),
        Index("ix_message_versions_search_tsv", "search_tsv", postgresql_using="gin"),
        CheckConstraint("version_number >= 1", name="version_number_positive"),
        CheckConstraint("char_count >= 0", name="char_count_non_negative"),
    )


class MessagePart(Base, UUIDPrimaryKeyMixin):
    """Structured decomposition of a message version (code, tables, lists...)."""

    __tablename__ = "message_parts"

    message_version_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("message_versions.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    part_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    kind: Mapped[PartKind] = mapped_column(_enum(PartKind, "part_kind"), nullable=False)
    language: Mapped[str | None] = mapped_column(String(64), nullable=True)
    text_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    structured: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    version: Mapped[MessageVersion] = relationship(back_populates="parts")

    __table_args__ = (
        UniqueConstraint("message_version_id", "part_index", name="uq_message_parts_version_index"),
        Index("ix_message_parts_kind", "kind"),
        CheckConstraint("part_index >= 0", name="part_index_non_negative"),
    )
