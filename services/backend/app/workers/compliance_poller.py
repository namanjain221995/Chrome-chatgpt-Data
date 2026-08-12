"""Compliance poller process.

Disabled unless ``COMPLIANCE_POLL_ENABLED=true`` *and* the adapter is fully
configured. It polls with overlapping time windows so an event that arrives
slightly out of order is still collected, and it never advances the durable
checkpoint until every event in the window is stored in PostgreSQL and S3.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from datetime import timedelta
from typing import Any

from sqlalchemy import select

from app.adapters.openai_compliance import ComplianceAdapter
from app.core.config import get_settings
from app.core.crypto import utcnow
from app.core.logging import configure_logging, get_logger
from app.db.session import dispose_engine, session_scope
from app.models.enums import JobKind
from app.models.identity import Organization
from app.services import jobs as jobs_service
from app.services.compliance_import import (
    advance_checkpoint,
    get_or_create_checkpoint,
    persist_event,
    record_checkpoint_failure,
)
from app.services.storage import get_storage

logger = get_logger(__name__)


class CompliancePoller:
    def __init__(self, adapter: ComplianceAdapter | None = None) -> None:
        self.settings = get_settings()
        self.adapter = adapter or ComplianceAdapter(self.settings)
        self._stopping = asyncio.Event()
        self.cycles = 0

    def request_stop(self) -> None:
        self._stopping.set()

    @property
    def stopping(self) -> bool:
        return self._stopping.is_set()

    async def run_cycle(self) -> dict[str, Any]:
        """One polling cycle. Returns a small summary for logs and tests."""
        if not self.adapter.is_enabled:
            return {"skipped": True, "reason": "disabled_or_unconfigured"}

        storage = get_storage()
        summary: dict[str, Any] = {"events": 0, "new": 0, "pages": 0}

        async with session_scope() as session:
            organization = (await session.execute(select(Organization).limit(1))).scalars().first()
            if organization is None:
                return {"skipped": True, "reason": "no_organization"}

            checkpoint = await get_or_create_checkpoint(session, organization_id=organization.id)
            now = utcnow()
            overlap = timedelta(seconds=self.settings.compliance_overlap_seconds)
            window_start = (checkpoint.window_start or now - timedelta(days=1)) - overlap
            window_end = now

            cursor = checkpoint.cursor_value
            last_event_time = checkpoint.last_event_time
            try:
                for _page in range(self.settings.compliance_max_pages_per_cycle):
                    page = await self.adapter.fetch_log_page(
                        window_start=window_start, window_end=window_end, cursor=cursor
                    )
                    summary["pages"] += 1
                    summary["events"] += len(page.events)

                    for event in page.events:
                        # S3 write happens inside persist_event, before commit.
                        if await persist_event(
                            session,
                            organization=organization,
                            event=event,
                            storage=storage,
                        ):
                            summary["new"] += 1
                        if event.event_time and (
                            last_event_time is None or event.event_time > last_event_time
                        ):
                            last_event_time = event.event_time

                    cursor = page.next_cursor
                    if not page.has_more or not page.events:
                        break

                await advance_checkpoint(
                    checkpoint,
                    window_end=window_end,
                    cursor=None if not page.has_more else cursor,
                    last_event_time=last_event_time,
                    events_seen=summary["events"],
                )
                if summary["new"]:
                    await jobs_service.enqueue_job(
                        session,
                        kind=JobKind.COMPLIANCE_SYNC,
                        organization_id=organization.id,
                        priority=250,
                        dedupe_key="compliance_sync",
                        payload={"trigger": "poller"},
                    )
            except Exception as exc:
                record_checkpoint_failure(checkpoint, exc)
                logger.error(
                    "compliance_poll_failed",
                    error_type=type(exc).__name__,
                    consecutive_errors=checkpoint.consecutive_errors,
                )
                summary["error"] = type(exc).__name__

        self.cycles += 1
        return summary

    async def run_forever(self) -> None:
        if not self.settings.compliance_poll_enabled:
            logger.warning(
                "compliance_poller_disabled",
                hint="Set COMPLIANCE_POLL_ENABLED=true after authorization and configuration",
            )
        elif not self.adapter.is_configured:
            logger.error(
                "compliance_poller_unconfigured",
                hint="OPENAI_COMPLIANCE_BASE_URL / LOG_PATH / API key must all be set",
            )

        interval = max(30, self.settings.compliance_poll_interval_seconds)
        while not self.stopping:
            summary = await self.run_cycle()
            if not summary.get("skipped"):
                logger.info("compliance_cycle", **summary)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=interval)
        logger.info("compliance_poller_stopped", cycles=self.cycles)


async def async_main() -> None:
    configure_logging()
    poller = CompliancePoller()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, poller.request_stop)
    try:
        await poller.run_forever()
    finally:
        await dispose_engine()


def main() -> Any:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
