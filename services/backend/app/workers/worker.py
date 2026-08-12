"""Background worker process.

Runs the same image as the API. Bounded concurrency (``WORKER_CONCURRENCY``)
claims jobs with ``FOR UPDATE SKIP LOCKED``, recovers stale locks left behind by
a killed worker, and shuts down gracefully so an in-flight job either finishes
or is safely retried.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import socket
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.crypto import utcnow
from app.core.logging import configure_logging, correlation_id_var, get_logger
from app.db.session import dispose_engine, session_scope
from app.models.enums import JobKind
from app.models.jobs import Job
from app.services import jobs as jobs_service
from app.workers.handlers import NonRetryableError, get_handler

logger = get_logger(__name__)

#: Periodic jobs the worker keeps scheduled without an external cron.
PERIODIC_JOBS: tuple[tuple[JobKind, int, int], ...] = (
    # (kind, interval_seconds, priority)
    (JobKind.MAINTAIN_PARTITIONS, 6 * 3600, 800),
    (JobKind.RETENTION_SWEEP, 24 * 3600, 900),
    (JobKind.CLEANUP_STALE, 12 * 3600, 950),
)


def worker_identity() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


class Worker:
    def __init__(self, *, worker_id: str | None = None, concurrency: int | None = None) -> None:
        settings = get_settings()
        self.worker_id = worker_id or worker_identity()
        self.concurrency = concurrency or max(1, settings.worker_concurrency)
        self.poll_interval = settings.worker_poll_interval_seconds
        self._stopping = asyncio.Event()
        self._last_periodic: dict[JobKind, float] = {}
        self.processed = 0
        self.failed = 0

    def request_stop(self) -> None:
        logger.info("worker_stop_requested", worker_id=self.worker_id)
        self._stopping.set()

    @property
    def stopping(self) -> bool:
        return self._stopping.is_set()

    async def run_once(self, session: AsyncSession) -> int:
        """Claim and execute up to ``concurrency`` jobs. Returns count run."""
        claimed = await jobs_service.claim_jobs(
            session, worker_id=self.worker_id, limit=self.concurrency
        )
        if not claimed:
            return 0
        await session.commit()

        for job, lock_token in claimed:
            await self._execute(session, job, lock_token)
        return len(claimed)

    async def _execute(self, session: AsyncSession, job: Job, lock_token: uuid.UUID) -> None:
        token = correlation_id_var.set(f"job-{job.id.hex[:16]}")
        attempt = await jobs_service.record_attempt_start(
            session, job=job, worker_id=self.worker_id
        )
        started = utcnow()
        handler = get_handler(job.kind)
        try:
            if handler is None:
                raise NonRetryableError(f"no handler registered for job kind {job.kind}")
            result = await handler(session, job)
            await jobs_service.complete_job(
                session, job_id=job.id, lock_token=lock_token, result=result
            )
            attempt.succeeded = True
            self.processed += 1
            logger.info(
                "job_succeeded",
                job_id=str(job.id),
                kind=str(job.kind),
                attempt=job.attempts,
                duration_ms=int((utcnow() - started).total_seconds() * 1000),
            )
        except NonRetryableError as exc:
            attempt.succeeded = False
            attempt.error_type = type(exc).__name__
            attempt.error_summary = str(exc)[:2000]
            await jobs_service.fail_job(
                session,
                job_id=job.id,
                lock_token=lock_token,
                error_summary=str(exc),
                retryable=False,
            )
            self.failed += 1
            logger.error("job_dead", job_id=str(job.id), kind=str(job.kind), reason=str(exc)[:200])
        except Exception as exc:
            attempt.succeeded = False
            attempt.error_type = type(exc).__name__
            attempt.error_summary = f"{type(exc).__name__}: {exc}"[:2000]
            await jobs_service.fail_job(
                session,
                job_id=job.id,
                lock_token=lock_token,
                error_summary=f"{type(exc).__name__}: {exc}",
                retryable=True,
            )
            self.failed += 1
            logger.exception("job_failed", job_id=str(job.id), kind=str(job.kind))
        finally:
            attempt.finished_at = utcnow()
            attempt.duration_ms = int((attempt.finished_at - started).total_seconds() * 1000)
            with contextlib.suppress(Exception):
                await session.commit()
            correlation_id_var.reset(token)

    async def ensure_periodic_jobs(self, session: AsyncSession) -> None:
        loop_now = asyncio.get_running_loop().time()
        for kind, interval, priority in PERIODIC_JOBS:
            last = self._last_periodic.get(kind)
            if last is not None and loop_now - last < interval:
                continue
            self._last_periodic[kind] = loop_now
            # The dedupe key coalesces with any pending copy of the same job.
            await jobs_service.enqueue_job(
                session,
                kind=kind,
                payload={"scheduled_by": self.worker_id},
                priority=priority,
                dedupe_key=f"periodic:{kind.value}",
            )
        await session.commit()

    async def run_forever(self) -> None:
        logger.info("worker_started", worker_id=self.worker_id, concurrency=self.concurrency)
        idle_cycles = 0
        while not self.stopping:
            try:
                async with session_scope() as session:
                    await self.ensure_periodic_jobs(session)
                    recovered = await jobs_service.recover_stale_jobs(session)
                    if recovered:
                        await session.commit()
                    executed = await self.run_once(session)
            except Exception:
                logger.exception("worker_cycle_failed", worker_id=self.worker_id)
                executed = 0

            if executed:
                idle_cycles = 0
            else:
                idle_cycles = min(idle_cycles + 1, 10)
                # Back off gently when the queue is empty to spare the database.
                delay = self.poll_interval * (1 + idle_cycles * 0.25)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stopping.wait(), timeout=delay)

        logger.info(
            "worker_stopped",
            worker_id=self.worker_id,
            processed=self.processed,
            failed=self.failed,
        )


async def async_main() -> None:
    configure_logging()
    settings = get_settings()
    worker = Worker()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, worker.request_stop)

    logger.info("worker_boot", environment=settings.environment, version=settings.app_version)
    try:
        await worker.run_forever()
    finally:
        await dispose_engine()


def main() -> Any:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
