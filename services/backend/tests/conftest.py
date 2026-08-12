"""Shared test fixtures.

Unit tests run with no external services. Integration tests (marked
``@pytest.mark.integration``) need PostgreSQL; they are skipped with a clear
message when ``TEST_DATABASE_URL`` is not set, so ``make test-backend`` works on
a laptop with nothing running and ``make test-integration`` exercises the full
stack.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("DEV_AUTH_ENABLED", "true")
os.environ.setdefault("BROWSER_CONTENT_CAPTURE_ENABLED", "true")
os.environ.setdefault("OPENAI_WRITTEN_AUTHORIZATION_CONFIRMED", "true")
os.environ.setdefault("MANAGED_WORKSPACE_LABEL", "TechSara's Workspace")
os.environ.setdefault("ALLOWED_EMAIL_DOMAINS", "example.com")
os.environ.setdefault("OIDC_CLIENT_ID", "test-client-id")
os.environ.setdefault("OIDC_ISSUER", "https://accounts.google.com")
os.environ.setdefault("OIDC_REQUIRED_HD", "example.com")
os.environ.setdefault("S3_BUCKET", "test-bucket")
os.environ.setdefault("LOG_LEVEL", "WARNING")

from app.core.config import get_settings, reset_settings_cache
from app.core.security import Role
from app.db.session import reset_engine_for_tests
from app.schemas.common import ClientContext, WorkspaceRef
from tests.fakes import FakeStorageService

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "")

integration = pytest.mark.integration

pytestmark_requires_db = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL is not set; run `make test-integration`",
)


def pytest_collection_modifyitems(config: Any, items: list[Any]) -> None:
    """Auto-skip integration tests when no database is configured."""
    if TEST_DATABASE_URL:
        return
    skip = pytest.mark.skip(reason="TEST_DATABASE_URL not set; run `make test-integration`")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _reset_caches() -> Generator[None, None, None]:
    from app.api.deps import reset_rate_limiter

    reset_settings_cache()
    reset_rate_limiter()
    yield
    reset_settings_cache()
    reset_rate_limiter()


@pytest.fixture
def settings():  # type: ignore[no-untyped-def]
    return get_settings()


@pytest.fixture
def fake_storage() -> Generator[FakeStorageService, None, None]:
    from app.services import storage as storage_module

    fake = FakeStorageService()
    storage_module.reset_storage(fake)  # type: ignore[arg-type]
    yield fake
    storage_module.reset_storage(None)


# ---------------------------------------------------------------------------
# Database fixtures (integration)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db_engine():  # type: ignore[no-untyped-def]
    """Function-scoped engine.

    Deliberately not session-scoped: pytest-asyncio gives each test its own
    event loop, and an asyncpg pool bound to a closed loop is a classic source
    of flaky suites. NullPool keeps the per-test cost to a single connection.
    """
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL is not set")
    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_engine) -> AsyncGenerator[AsyncSession, None]:  # type: ignore[no-untyped-def]
    """A session wrapped in a transaction that is always rolled back."""
    connection = await db_engine.connect()
    transaction = await connection.begin()
    factory = async_sessionmaker(bind=connection, expire_on_commit=False, autoflush=False)
    session = factory()
    try:
        yield session
    finally:
        await session.close()
        await transaction.rollback()
        await connection.close()


@pytest_asyncio.fixture
async def api_client(db_engine, fake_storage) -> AsyncGenerator[AsyncClient, None]:  # type: ignore[no-untyped-def]
    """HTTP client bound to a rolled-back transaction for full-stack tests."""
    from app.db.session import get_db_session
    from app.main import create_app

    connection = await db_engine.connect()
    transaction = await connection.begin()
    factory = async_sessionmaker(bind=connection, expire_on_commit=False, autoflush=False)

    app = create_app()

    async def _override() -> AsyncGenerator[AsyncSession, None]:
        session = factory()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    app.dependency_overrides[get_db_session] = _override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        # Tests that need to mutate rows (e.g. grant a role) must use the same
        # connection as the app, or they would not see each other's writes.
        client.app_session_factory = factory  # type: ignore[attr-defined]
        yield client

    await transaction.rollback()
    await connection.close()
    reset_engine_for_tests(None)


# ---------------------------------------------------------------------------
# Domain helpers
# ---------------------------------------------------------------------------


def utc_now() -> datetime:
    return datetime.now(UTC)


def make_client_context(**overrides: Any) -> ClientContext:
    payload: dict[str, Any] = {
        "extension_version": "1.0.0",
        "adapter_version": "2024.1",
        "schema_version": "1.0",
        "device_fingerprint": "a" * 32,
        "captured_at": utc_now(),
    }
    payload.update(overrides)
    return ClientContext(**payload)


def managed_workspace_ref(**overrides: Any) -> WorkspaceRef:
    payload: dict[str, Any] = {
        "kind": "managed_company",
        "verified": True,
        "label": "TechSara's Workspace",
        "verification_signals": ["workspace_label_match"],
    }
    payload.update(overrides)
    return WorkspaceRef(**payload)


def new_idempotency_key(prefix: str = "test") -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


@pytest_asyncio.fixture
async def seeded_org(db_session: AsyncSession):  # type: ignore[no-untyped-def]
    """Organization + employee + device, ready for ingestion tests."""
    from app.services import accounts
    from app.services.retention import ensure_default_policy

    organization = await accounts.get_or_create_organization(db_session)
    from app.core.security import VerifiedIdentity

    identity = VerifiedIdentity(
        subject=f"sub-{uuid.uuid4().hex}",
        issuer="https://accounts.google.com",
        email=f"employee-{uuid.uuid4().hex[:8]}@example.com",
        email_verified=True,
        hosted_domain="example.com",
        name="Test Employee",
    )
    user = await accounts.get_or_create_user(
        db_session, organization=organization, identity=identity
    )
    device = await accounts.get_or_create_device(
        db_session,
        user=user,
        organization=organization,
        device_fingerprint=uuid.uuid4().hex,
        extension_version="1.0.0",
        adapter_version="2024.1",
    )
    await ensure_default_policy(db_session, organization_id=organization.id)
    await db_session.flush()
    return organization, user, device


@pytest_asyncio.fixture
async def ingest_ctx(db_session: AsyncSession, seeded_org, fake_storage):  # type: ignore[no-untyped-def]
    from app.services.ingest import build_context

    organization, user, device = seeded_org
    return await build_context(
        db_session,
        organization=organization,
        user=user,
        device=device,
        workspace_ref=managed_workspace_ref(),
    )


@pytest.fixture
def admin_roles() -> list[Role]:
    return [Role.COMPLIANCE_ADMIN, Role.DATA_CURATOR]
