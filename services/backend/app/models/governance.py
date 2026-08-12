"""Feedback, training approvals, exports, retention policies and legal holds."""

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
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import (
    ApprovalStatus,
    ExportKind,
    ExportStatus,
    FeedbackKind,
    RetentionAction,
)
from app.models.identity import _enum


class Feedback(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Employee or reviewer feedback attached to a message or conversation."""

    __tablename__ = "feedback"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("messages.id", ondelete="CASCADE"), nullable=True
    )
    message_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("message_versions.id", ondelete="SET NULL"), nullable=True
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    kind: Mapped[FeedbackKind] = mapped_column(_enum(FeedbackKind, "feedback_kind"), nullable=False)
    rating: Mapped[int | None] = mapped_column(Integer, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    client_feedback_id: Mapped[str] = mapped_column(String(128), nullable=False)
    metadata_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    __table_args__ = (
        UniqueConstraint("organization_id", "client_feedback_id", name="uq_feedback_org_client_id"),
        Index("ix_feedback_conversation", "conversation_id"),
        Index("ix_feedback_message", "message_id"),
        CheckConstraint("rating IS NULL OR rating BETWEEN 1 AND 5", name="rating_in_range"),
    )


class TrainingApproval(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Explicit human approval gate for curated export eligibility.

    A conversation is exportable only when a row exists here with
    ``status='approved'`` — the default is never to export.
    """

    __tablename__ = "training_approvals"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("messages.id", ondelete="CASCADE"), nullable=True
    )
    status: Mapped[ApprovalStatus] = mapped_column(
        _enum(ApprovalStatus, "approval_status"), nullable=False, default=ApprovalStatus.PENDING
    )
    reviewed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    contains_secrets: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    contains_personal_data: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    policy_version: Mapped[str | None] = mapped_column(String(32), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "conversation_id", "message_id", name="uq_training_approvals_conversation_message"
        ),
        Index(
            "ix_training_approvals_approved",
            "organization_id",
            "status",
            postgresql_where=text("status = 'approved'"),
        ),
    )


class Export(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "exports"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    requested_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    kind: Mapped[ExportKind] = mapped_column(_enum(ExportKind, "export_kind"), nullable=False)
    status: Mapped[ExportStatus] = mapped_column(
        _enum(ExportStatus, "export_status"), nullable=False, default=ExportStatus.PENDING
    )
    filters: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    split_strategy: Mapped[str] = mapped_column(
        String(32), nullable=False, default="conversation_hash"
    )
    split_ratios: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=lambda: {"train": 0.8, "validation": 0.1, "test": 0.1}
    )
    include_attachments: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    conversation_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    record_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    s3_prefix: Mapped[str | None] = mapped_column(Text, nullable=True)
    manifest_s3_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    manifest_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    parts: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_exports_org_status", "organization_id", "status"),
        CheckConstraint("record_count >= 0", name="record_count_non_negative"),
        CheckConstraint(
            "split_strategy in ('conversation_hash','none')", name="split_strategy_allowed"
        ),
    )


class RetentionPolicy(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Policy-driven retention: soft delete first, audited hard delete later."""

    __tablename__ = "retention_policies"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    applies_to: Mapped[str] = mapped_column(String(64), nullable=False, default="conversations")
    retain_days: Mapped[int] = mapped_column(Integer, nullable=False, default=365)
    grace_days: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    action: Mapped[RetentionAction] = mapped_column(
        _enum(RetentionAction, "retention_action"),
        nullable=False,
        default=RetentionAction.SOFT_DELETE,
    )
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    legal_hold_exempt: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("organization_id", "name", name="uq_retention_policies_org_name"),
        Index(
            "ix_retention_policies_default",
            "organization_id",
            "is_default",
            unique=True,
            postgresql_where=text("is_default = true"),
        ),
        CheckConstraint("retain_days >= 1", name="retain_days_positive"),
        CheckConstraint("grace_days >= 0", name="grace_days_non_negative"),
        CheckConstraint(
            "applies_to in ('conversations','capture_events','source_events','audit_events',"
            "'attachments','exports')",
            name="applies_to_allowed",
        ),
        # A legal-hold-exempt policy may never hard delete.
        CheckConstraint(
            "NOT (legal_hold_exempt = true AND action = 'hard_delete')",
            name="legal_hold_exempt_cannot_hard_delete",
        ),
    )


class LegalHold(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Named legal hold; while active, matching records cannot be deleted."""

    __tablename__ = "legal_holds"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    matter_reference: Mapped[str | None] = mapped_column(String(255), nullable=True)
    scope: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    released_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("organization_id", "name", name="uq_legal_holds_org_name"),
        Index(
            "ix_legal_holds_active",
            "organization_id",
            postgresql_where=text("released_at IS NULL"),
        ),
    )
