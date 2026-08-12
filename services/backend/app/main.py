"""FastAPI application factory.

Started in production by Gunicorn with Uvicorn workers:

    gunicorn app.main:app -k uvicorn.workers.UvicornWorker -w $API_WORKERS
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncGenerator
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.api.middleware import (
    BodySizeLimitMiddleware,
    CorrelationIdMiddleware,
    SecurityHeadersMiddleware,
)
from app.api.v1.health import router as health_router
from app.api.v1.router import api_router
from app.core.config import Settings, get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.db.session import check_database, dispose_engine

logger = get_logger(__name__)

DESCRIPTION = """
Backend for the TechSara Managed ChatGPT Session Archive.

Archives approved company-workspace ChatGPT conversations captured by a managed
Chrome extension and, optionally, by an authorized enterprise compliance feed.
No unsent drafts, no keystrokes, no cookies, no session tokens, and no
personal-workspace content are ever accepted.
""".strip()


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    settings = get_settings()
    configure_logging()
    logger.info(
        "api_starting",
        environment=settings.environment,
        version=settings.app_version,
        capture_active=settings.browser_capture_active,
        compliance_poll_enabled=settings.compliance_poll_enabled,
    )
    if not settings.browser_capture_active:
        logger.warning(
            "capture_gates_closed",
            browser_content_capture_enabled=settings.browser_content_capture_enabled,
            openai_written_authorization_confirmed=(
                settings.openai_written_authorization_confirmed
            ),
            kill_switch=settings.kill_switch_enabled,
        )
    await check_database()
    try:
        yield
    finally:
        # Drain the pool so PostgreSQL does not keep orphaned backends around.
        await dispose_engine()
        logger.info("api_stopped")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging()

    app = FastAPI(
        title="TechSara Managed ChatGPT Session Archive",
        description=DESCRIPTION,
        version=settings.app_version,
        lifespan=lifespan,
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
        openapi_url=None if settings.is_production else "/openapi.json",
    )

    # Order matters: the outermost middleware is registered last.
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(BodySizeLimitMiddleware)
    app.add_middleware(CorrelationIdMiddleware)
    if settings.is_production:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=[settings.archive_hostname])

    origins = settings.allowed_origins
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=False,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "X-Correlation-Id"],
            expose_headers=["X-Correlation-Id", "Retry-After"],
            max_age=600,
        )
    else:
        # No extension id configured yet: browsers get no cross-origin access.
        logger.warning("cors_allowlist_empty", hint="Set EXTENSION_IDS after the first build")

    register_exception_handlers(app)

    app.include_router(health_router)
    app.include_router(api_router, prefix=settings.api_base_path)

    @app.get("/", include_in_schema=False)
    async def root() -> dict[str, Any]:
        return {
            "name": settings.app_name,
            "version": settings.app_version,
            "api_base_path": settings.api_base_path,
            "docs": None if settings.is_production else "/docs",
        }

    return app


app = create_app()
