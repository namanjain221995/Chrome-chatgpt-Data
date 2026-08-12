"""Alembic environment.

Runs against the async engine so a single DATABASE_URL (asyncpg) serves both
the application and migrations — no second driver to install or configure.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.core.config import get_settings
from app.models import Base  # noqa: F401 - registers every table on the metadata

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.database_url)

target_metadata = Base.metadata

#: Objects created by raw SQL in migrations that autogenerate must ignore.
IGNORED_TABLE_SUFFIXES = ("_default",)


def include_object(obj, name, type_, reflected, compare_to):  # type: ignore[no-untyped-def]
    """Skip partition children: they are managed by partition maintenance."""
    if type_ == "table" and reflected:
        if name.endswith(IGNORED_TABLE_SUFFIXES):
            return False
        # Monthly partitions look like capture_events_p202601.
        parts = name.rsplit("_p", 1)
        if len(parts) == 2 and len(parts[1]) == 6 and parts[1].isdigit():
            return False
    if type_ == "index" and reflected:
        parent = getattr(obj, "table", None)
        table_name = getattr(parent, "name", "") or ""
        parts = table_name.rsplit("_p", 1)
        if table_name.endswith(IGNORED_TABLE_SUFFIXES) or (
            len(parts) == 2 and len(parts[1]) == 6 and parts[1].isdigit()
        ):
            return False
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        include_object=include_object,
        include_schemas=False,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
