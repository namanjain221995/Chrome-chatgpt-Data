"""Async engine and session management.

Connection pooling lives inside each API process (no RDS Proxy / PgBouncer in
version 1). Pool sizing is documented in docs/SCALING_250_USERS.md:

    total_connections = API_WORKERS * (DATABASE_POOL_SIZE + DATABASE_MAX_OVERFLOW)
                      + WORKER_CONCURRENCY + compliance poller (1)

which must stay below PostgreSQL `max_connections`.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings, get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def build_engine(settings: Settings | None = None, **overrides: Any) -> AsyncEngine:
    settings = settings or get_settings()
    connect_args: dict[str, Any] = {
        "server_settings": {
            "application_name": settings.app_name,
            "statement_timeout": str(settings.database_statement_timeout_ms),
            "idle_in_transaction_session_timeout": "60000",
            "timezone": "UTC",
        },
        # asyncpg caches prepared statements per connection; keep it modest so
        # a restarted pool does not thrash the server-side cache.
        "statement_cache_size": 256,
    }
    kwargs: dict[str, Any] = {
        "echo": False,
        "pool_pre_ping": True,
        "pool_size": settings.database_pool_size,
        "max_overflow": settings.database_max_overflow,
        "pool_recycle": 1800,
        "pool_timeout": 30,
        "connect_args": connect_args,
    }
    kwargs.update(overrides)
    return create_async_engine(settings.database_url, **kwargs)


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = build_engine()
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            bind=get_engine(),
            expire_on_commit=False,
            autoflush=False,
            class_=AsyncSession,
        )
    return _sessionmaker


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency. Commits on success, rolls back on error."""
    factory = get_sessionmaker()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def session_scope() -> AsyncGenerator[AsyncSession, None]:
    """Context-managed session for workers and scripts."""
    factory = get_sessionmaker()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def check_database(timeout_seconds: float = 5.0) -> bool:
    """Readiness probe: a real round trip, not just pool state."""
    import asyncio

    try:
        async with asyncio.timeout(timeout_seconds):
            async with get_engine().connect() as conn:
                await conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:
        logger.warning("database_check_failed", error_type=type(exc).__name__)
        return False


async def dispose_engine() -> None:
    """Drain connections on graceful shutdown."""
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


def reset_engine_for_tests(engine: AsyncEngine | None = None) -> None:
    global _engine, _sessionmaker
    _engine = engine
    _sessionmaker = (
        async_sessionmaker(
            bind=engine, expire_on_commit=False, autoflush=False, class_=AsyncSession
        )
        if engine is not None
        else None
    )
