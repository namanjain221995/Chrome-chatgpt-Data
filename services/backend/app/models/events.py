"""High-volume event tables, sync checkpoints and the audit trail.

``capture_events``, ``source_events`` and ``audit_events`` are declared as
monthly RANGE-partitioned tables (partition key ``created_at``) because they
grow at roughly the message rate — 100k rows/day at the stress target. Monthly
partitions make retention a metadata-only ``DROP TABLE`` instead of a mass
DELETE, and keep index maintenance bounded.

Consequence: a UNIQUE constraint on a partitioned table must include the
partition key, which would make idempotency month-local. To keep idempotency
globally correct, :class:`IdempotencyKey` is a small *non-partitioned* table
holding one row per accepted idempotency key.
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
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import AuditAction, CaptureEventKind, IngestStatus, SourceEventKind
from app.models.identity import _enum

MONTHLY_PARTITIONED_TABLES: tuple[str, ...] = (
    "capture_events",
    "source_events",
    "audit_events",
)


class CaptureEvent(Base):
    """Immutable record of one accepted client capture event.

    The raw client payload is stored verbatim in JSONB and is also written to
    S3 by the worker before the archive job is marked complete.
    """

    __tablename__ = "capture_events"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), default=uuid.uuid4, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    device_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    message_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)

    kind: Mapped[CaptureEventKind] = mapped_column(
        _enum(CaptureEventKind, "capture_event_kind"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[IngestStatus] = mapped_column(
        _enum(IngestStatus, "ingest_status"), nullable=False, default=IngestStatus.ACCEPTED
    )
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False, default="1.0")
    adapter_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    extension_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    client_captured_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    raw_s3_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_s3_version_id: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        PrimaryKeyConstraint("id", "created_at", name="pk_capture_events"),
        Index("ix_capture_events_org_created", "organization_id", "created_at"),
        Index("ix_capture_events_conversation", "conversation_id", "created_at"),
        Index("ix_capture_events_idempotency", "idempotency_key"),
        Index("ix_capture_events_unarchived", "archived_at", "created_at"),
        CheckConstraint("payload_bytes >= 0", name="payload_bytes_non_negative"),
        {"postgresql_partition_by": "RANGE (created_at)"},
    )


class IdempotencyKey(Base):
    """Global, non-partitioned uniqueness for client idempotency keys.

    Rows older than the client retry horizon are pruned by the retention job
    (``OFFLINE_QUEUE_MAX_AGE_DAYS`` bounds how long a client may retry).
    """

    __tablename__ = "idempotency_keys"

    organization_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    capture_event_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    capture_event_created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    result_ref: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        PrimaryKeyConstraint("organization_id", "idempotency_key", name="pk_idempotency_keys"),
        Index("ix_idempotency_keys_created_at", "created_at"),
    )


class SourceEvent(Base):
    """Raw event from an authorized upstream source (compliance API).

    Written to S3 *before* the sync checkpoint advances, so a crash can only
    ever cause reprocessing, never loss.
    """

    __tablename__ = "source_events"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), default=uuid.uuid4, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="openai_compliance")
    source_event_id: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[SourceEventKind] = mapped_column(
        _enum(SourceEventKind, "source_event_kind"), nullable=False, default=SourceEventKind.UNKNOWN
    )
    source_conversation_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_workspace_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    actor_email_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_deletion: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_s3_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_s3_version_id: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        PrimaryKeyConstraint("id", "created_at", name="pk_source_events"),
        Index("ix_source_events_source_event_id", "source", "source_event_id"),
        Index("ix_source_events_org_event_time", "organization_id", "event_time"),
        Index("ix_source_events_conversation", "source_conversation_id"),
        {"postgresql_partition_by": "RANGE (created_at)"},
    )


class SourceEventKeyIndex(Base):
    """Global dedupe index for upstream event ids (non-partitioned)."""

    __tablename__ = "source_event_keys"

    organization_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    source_event_id: Mapped[str] = mapped_column(String(255), nullable=False)
    source_event_created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        PrimaryKeyConstraint(
            "organization_id", "source", "source_event_id", name="pk_source_event_keys"
        ),
        Index("ix_source_event_keys_created_at", "created_at"),
    )


class SyncCheckpoint(Base):
    """Durable cursor for an upstream poller.

    ``cursor_value``/``window_start`` are only advanced after every event in the
    window has been persisted to PostgreSQL *and* S3.
    """

    __tablename__ = "sync_checkpoints"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    stream: Mapped[str] = mapped_column(String(64), nullable=False, default="default")
    cursor_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    window_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    window_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_event_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consecutive_errors: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_events: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    state: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("organization_id", "source", "stream", name="uq_sync_checkpoints_stream"),
        CheckConstraint("consecutive_errors >= 0", name="consecutive_errors_non_negative"),
    )


class AuditEvent(Base):
    """Append-only audit trail for administrative and privileged actions."""

    __tablename__ = "audit_events"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), default=uuid.uuid4, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    organization_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    actor_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    actor_roles: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    device_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    action: Mapped[AuditAction] = mapped_column(_enum(AuditAction, "audit_action"), nullable=False)
    resource_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resource_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False, default="success")
    ip_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_ip: Mapped[str | None] = mapped_column(INET, nullable=True)
    user_agent_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    details: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    __table_args__ = (
        PrimaryKeyConstraint("id", "created_at", name="pk_audit_events"),
        Index("ix_audit_events_org_created", "organization_id", "created_at"),
        Index("ix_audit_events_actor", "actor_user_id", "created_at"),
        Index("ix_audit_events_action", "action", "created_at"),
        Index("ix_audit_events_resource", "resource_type", "resource_id"),
        CheckConstraint("outcome in ('success','failure','denied')", name="outcome_allowed"),
        {"postgresql_partition_by": "RANGE (created_at)"},
    )
