"""Durable PostgreSQL job queue.

Claiming uses ``SELECT ... FOR UPDATE SKIP LOCKED`` inside a transaction, so
multiple worker processes on the same instance never hand the same job to two
handlers. Stale locks (a worker killed mid-job) are recovered by comparing
``locked_at`` against ``WORKER_STALE_LOCK_SECONDS``.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import JobKind, JobStatus
from app.models.identity import _enum

MAX_ATTEMPTS_DEFAULT = 8


class Job(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "jobs"

    organization_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    kind: Mapped[JobKind] = mapped_column(_enum(JobKind, "job_kind"), nullable=False)
    status: Mapped[JobStatus] = mapped_column(
        _enum(JobStatus, "job_status"), nullable=False, default=JobStatus.PENDING
    )
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    dedupe_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=MAX_ATTEMPTS_DEFAULT)
    run_after: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lock_token: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    result: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    attempts_log: Mapped[list[JobAttempt]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )

    __table_args__ = (
        # Only one live job per dedupe key; completed jobs may repeat the key.
        Index(
            "uq_jobs_dedupe_active",
            "dedupe_key",
            unique=True,
            postgresql_where=text("dedupe_key IS NOT NULL AND status IN ('pending','running')"),
        ),
        # The claim query's covering index: status + run_after + priority.
        Index(
            "ix_jobs_claim",
            "status",
            "run_after",
            "priority",
            "created_at",
            postgresql_where=text("status = 'pending'"),
        ),
        Index(
            "ix_jobs_stale_locks",
            "locked_at",
            postgresql_where=text("status = 'running'"),
        ),
        Index("ix_jobs_kind_status", "kind", "status"),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        CheckConstraint("max_attempts >= 1", name="max_attempts_positive"),
        CheckConstraint("priority BETWEEN 0 AND 1000", name="priority_in_range"),
    )


class JobAttempt(Base, UUIDPrimaryKeyMixin):
    """One execution attempt; kept for operational forensics."""

    __tablename__ = "job_attempts"

    job_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    worker_id: Mapped[str] = mapped_column(String(128), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    succeeded: Mapped[bool | None] = mapped_column(nullable=True)
    error_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    recovered_stale_lock: Mapped[bool] = mapped_column(nullable=False, default=False)

    job: Mapped[Job] = relationship(back_populates="attempts_log")

    __table_args__ = (
        UniqueConstraint("job_id", "attempt_number", name="uq_job_attempts_job_number"),
        Index("ix_job_attempts_started", "started_at"),
        CheckConstraint("attempt_number >= 1", name="attempt_number_positive"),
    )
