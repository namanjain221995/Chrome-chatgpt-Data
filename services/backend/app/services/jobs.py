"""Durable PostgreSQL job queue.

Claim protocol
--------------
```sql
SELECT id FROM jobs
 WHERE status = 'pending' AND run_after <= now()
 ORDER BY priority, run_after, created_at
 LIMIT :n
 FOR UPDATE SKIP LOCKED;
```
The row lock is held for the duration of the claiming transaction only; the
job is then marked ``running`` with ``locked_at``/``locked_by``/``lock_token``.
Completion is guarded by ``lock_token`` so a worker that lost its lock to stale
recovery cannot overwrite the result of the worker that took over.
"""

from __future__ import annotations

import random
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.crypto import utcnow
from app.core.logging import get_logger
from app.models.enums import JobKind, JobStatus
from app.models.jobs import Job, JobAttempt

logger = get_logger(__name__)

BASE_BACKOFF_SECONDS = 5
MAX_BACKOFF_SECONDS = 3600


def backoff_delay(attempts: int, *, jitter: bool = True) -> float:
    """Exponential backoff with full jitter, capped at one hour."""
    exponent = min(attempts, 12)
    ceiling = min(BASE_BACKOFF_SECONDS * (2**exponent), MAX_BACKOFF_SECONDS)
    if not jitter:
        return float(ceiling)
    return random.uniform(BASE_BACKOFF_SECONDS, float(ceiling))  # noqa: S311 - not cryptographic


async def enqueue_job(
    session: AsyncSession,
    *,
    kind: JobKind,
    payload: dict[str, Any],
    organization_id: uuid.UUID | None = None,
    dedupe_key: str | None = None,
    priority: int = 100,
    run_after: datetime | None = None,
    max_attempts: int = 8,
) -> Job | None:
    """Insert a job, honouring the partial unique index on live dedupe keys.

    Returns ``None`` when an identical job is already pending or running —
    that is a successful no-op, not an error.
    """
    values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "kind": kind.value,
        "status": JobStatus.PENDING.value,
        "priority": priority,
        "payload": payload,
        "dedupe_key": dedupe_key,
        "organization_id": organization_id,
        "max_attempts": max_attempts,
        "run_after": run_after or utcnow(),
    }
    insert_stmt = pg_insert(Job).values(**values)
    if dedupe_key:
        # The unique index is partial (`status IN ('pending','running')`), so
        # ON CONFLICT must name that index predicate explicitly.
        insert_stmt = insert_stmt.on_conflict_do_nothing(
            index_elements=[Job.dedupe_key],
            index_where=text("dedupe_key IS NOT NULL AND status IN ('pending','running')"),
        )
    result = await session.execute(insert_stmt.returning(Job.id))
    row = result.first()
    if row is None:
        return None
    return await session.get(Job, row[0])


async def claim_jobs(
    session: AsyncSession,
    *,
    worker_id: str,
    limit: int = 1,
    kinds: Sequence[JobKind] | None = None,
) -> list[tuple[Job, uuid.UUID]]:
    """Atomically claim up to ``limit`` runnable jobs.

    Returns `(job, lock_token)` pairs. The caller must commit.
    """
    now = utcnow()
    conditions = [Job.status == JobStatus.PENDING.value, Job.run_after <= now]
    if kinds:
        conditions.append(Job.kind.in_([k.value for k in kinds]))

    select_stmt = (
        select(Job.id)
        .where(and_(*conditions))
        .order_by(Job.priority.asc(), Job.run_after.asc(), Job.created_at.asc())
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    ids = list((await session.execute(select_stmt)).scalars().all())
    if not ids:
        return []

    claimed: list[tuple[Job, uuid.UUID]] = []
    for job_id in ids:
        lock_token = uuid.uuid4()
        upd = (
            update(Job)
            .where(Job.id == job_id, Job.status == JobStatus.PENDING.value)
            .values(
                status=JobStatus.RUNNING.value,
                locked_at=now,
                locked_by=worker_id,
                lock_token=lock_token,
                started_at=now,
                attempts=Job.attempts + 1,
            )
            .returning(Job.id)
        )
        if (await session.execute(upd)).first() is None:
            continue
        job = await session.get(Job, job_id)
        if job is not None:
            claimed.append((job, lock_token))
    return claimed


async def record_attempt_start(
    session: AsyncSession, *, job: Job, worker_id: str, recovered_stale_lock: bool = False
) -> JobAttempt:
    attempt = JobAttempt(
        job_id=job.id,
        attempt_number=job.attempts,
        worker_id=worker_id,
        started_at=utcnow(),
        recovered_stale_lock=recovered_stale_lock,
    )
    session.add(attempt)
    return attempt


async def complete_job(
    session: AsyncSession,
    *,
    job_id: uuid.UUID,
    lock_token: uuid.UUID,
    result: dict[str, Any] | None = None,
) -> bool:
    """Mark a job succeeded. Returns False if the lock was stolen meanwhile."""
    now = utcnow()
    stmt = (
        update(Job)
        .where(Job.id == job_id, Job.lock_token == lock_token)
        .values(
            status=JobStatus.SUCCEEDED.value,
            finished_at=now,
            locked_at=None,
            locked_by=None,
            lock_token=None,
            error_summary=None,
            result=result or {},
        )
        .returning(Job.id)
    )
    return (await session.execute(stmt)).first() is not None


async def fail_job(
    session: AsyncSession,
    *,
    job_id: uuid.UUID,
    lock_token: uuid.UUID,
    error_summary: str,
    retryable: bool = True,
) -> bool:
    """Reschedule with backoff, or bury the job once attempts are exhausted."""
    job = await session.get(Job, job_id)
    if job is None or job.lock_token != lock_token:
        return False

    now = utcnow()
    summary = error_summary[:2000]
    if retryable and job.attempts < job.max_attempts:
        delay = backoff_delay(job.attempts)
        job.status = JobStatus.PENDING
        job.run_after = now + timedelta(seconds=delay)
    else:
        job.status = JobStatus.DEAD if not retryable else JobStatus.FAILED
        job.finished_at = now
    job.locked_at = None
    job.locked_by = None
    job.lock_token = None
    job.error_summary = summary
    return True


async def recover_stale_jobs(session: AsyncSession, *, stale_seconds: int | None = None) -> int:
    """Return jobs whose worker died back to the pending queue.

    A job is stale when it is ``running`` and ``locked_at`` is older than the
    threshold. Recovery clears the lock token, which also invalidates any late
    completion attempt from the original worker.
    """
    settings = get_settings()
    threshold = stale_seconds if stale_seconds is not None else settings.worker_stale_lock_seconds
    cutoff = datetime.now(UTC) - timedelta(seconds=threshold)

    stmt = (
        update(Job)
        .where(
            Job.status == JobStatus.RUNNING.value,
            or_(Job.locked_at.is_(None), Job.locked_at < cutoff),
        )
        .values(
            status=JobStatus.PENDING.value,
            locked_at=None,
            locked_by=None,
            lock_token=None,
            run_after=utcnow(),
            error_summary="recovered from stale lock",
        )
        .returning(Job.id)
    )
    recovered = list((await session.execute(stmt)).scalars().all())
    if recovered:
        logger.warning("stale_jobs_recovered", count=len(recovered))
    return len(recovered)


async def queue_stats(session: AsyncSession) -> dict[str, Any]:
    rows = (await session.execute(select(Job.status, func.count()).group_by(Job.status))).all()
    counts = {str(status): int(count) for status, count in rows}

    oldest = (
        await session.execute(
            select(func.min(Job.run_after)).where(Job.status == JobStatus.PENDING.value)
        )
    ).scalar()

    settings = get_settings()
    stale_cutoff = datetime.now(UTC) - timedelta(seconds=settings.worker_stale_lock_seconds)
    stale = (
        await session.execute(
            select(func.count()).where(
                Job.status == JobStatus.RUNNING.value, Job.locked_at < stale_cutoff
            )
        )
    ).scalar_one()

    pending = counts.get(JobStatus.PENDING.value, 0)
    oldest_age = (utcnow() - oldest).total_seconds() if oldest else None
    return {
        "pending": pending,
        "running": counts.get(JobStatus.RUNNING.value, 0),
        "succeeded": counts.get(JobStatus.SUCCEEDED.value, 0),
        "failed": counts.get(JobStatus.FAILED.value, 0),
        "dead": counts.get(JobStatus.DEAD.value, 0),
        "oldest_pending_age_seconds": oldest_age,
        "stale_locks": int(stale or 0),
        "backpressure": pending >= settings.job_queue_backpressure_threshold,
    }


async def pending_job_count(session: AsyncSession) -> int:
    return int(
        (
            await session.execute(select(func.count()).where(Job.status == JobStatus.PENDING.value))
        ).scalar_one()
    )
