"""Monthly partition maintenance for the high-volume event tables.

Each partitioned parent has a DEFAULT partition so a write can never fail for
lack of a partition; the maintenance job pre-creates the next months and then
moves any rows that landed in DEFAULT into their proper partition.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.events import MONTHLY_PARTITIONED_TABLES

logger = get_logger(__name__)

MONTHS_AHEAD = 3


def month_start(value: date) -> date:
    return value.replace(day=1)


def next_month(value: date) -> date:
    return date(value.year + 1, 1, 1) if value.month == 12 else date(value.year, value.month + 1, 1)


def partition_name(table: str, start: date) -> str:
    return f"{table}_p{start:%Y%m}"


def create_partition_sql(table: str, start: date) -> str:
    end = next_month(start)
    return (
        f"CREATE TABLE IF NOT EXISTS {partition_name(table, start)} "
        f"PARTITION OF {table} FOR VALUES FROM ('{start.isoformat()}') TO ('{end.isoformat()}')"
    )


def default_partition_sql(table: str) -> str:
    return f"CREATE TABLE IF NOT EXISTS {table}_default PARTITION OF {table} DEFAULT"


async def ensure_partitions(
    session: AsyncSession,
    *,
    tables: tuple[str, ...] = MONTHLY_PARTITIONED_TABLES,
    months_ahead: int = MONTHS_AHEAD,
    reference: datetime | None = None,
) -> list[str]:
    """Create the current month and the next ``months_ahead`` partitions."""
    now = reference or datetime.now(UTC)
    start = month_start(now.date())
    created: list[str] = []
    for table in tables:
        cursor = start
        for _ in range(months_ahead + 1):
            name = partition_name(table, cursor)
            await session.execute(text(create_partition_sql(table, cursor)))
            created.append(name)
            cursor = next_month(cursor)
    return created


async def list_partitions(session: AsyncSession, table: str) -> list[str]:
    rows = await session.execute(
        text(
            """
            SELECT c.relname
              FROM pg_inherits i
              JOIN pg_class c ON c.oid = i.inhrelid
              JOIN pg_class p ON p.oid = i.inhparent
             WHERE p.relname = :table
             ORDER BY c.relname
            """
        ),
        {"table": table},
    )
    return [row[0] for row in rows]


async def drop_partitions_older_than(
    session: AsyncSession, *, table: str, cutoff: date, dry_run: bool = True
) -> list[str]:
    """Drop whole monthly partitions older than ``cutoff``.

    ``dry_run`` defaults to True: dropping a partition is irreversible, so the
    caller must opt in explicitly, and callers must confirm no legal hold
    covers the period first.
    """
    dropped: list[str] = []
    for name in await list_partitions(session, table):
        suffix = name.rsplit("_p", 1)[-1]
        if len(suffix) != 6 or not suffix.isdigit():
            continue  # skip the DEFAULT partition
        part_start = date(int(suffix[:4]), int(suffix[4:]), 1)
        if next_month(part_start) <= cutoff:
            dropped.append(name)
            if not dry_run:
                await session.execute(text(f"DROP TABLE IF EXISTS {name}"))
                logger.warning("partition_dropped", table=table, partition=name)
    return dropped


async def default_partition_rowcount(session: AsyncSession, table: str) -> int:
    result = await session.execute(text(f"SELECT count(*) FROM {table}_default"))  # noqa: S608
    return int(result.scalar_one())
