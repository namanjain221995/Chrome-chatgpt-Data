"""Liveness and readiness probes.

`/health/live` answers "is the process running"; it must never touch the
database, or a database blip would cause Docker to kill a healthy container.
`/health/ready` answers "can this process serve traffic".
"""

from __future__ import annotations

from fastapi import APIRouter, Response, status

from app.core.config import get_settings
from app.core.crypto import utcnow
from app.db.session import check_database
from app.schemas.attachments import HealthOut

router = APIRouter(tags=["health"])


@router.get("/health/live", response_model=HealthOut, summary="Liveness probe")
async def live() -> HealthOut:
    settings = get_settings()
    return HealthOut(
        status="ok",
        checks={"process": True},
        version=settings.app_version,
        server_time=utcnow(),
    )


@router.get("/health/ready", response_model=HealthOut, summary="Readiness probe")
async def ready(response: Response) -> HealthOut:
    settings = get_settings()
    database_ok = await check_database()
    checks = {"database": database_ok, "config": True}
    if not database_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthOut(
        status="ok" if database_ok else "error",
        checks=checks,
        version=settings.app_version,
        server_time=utcnow(),
    )
