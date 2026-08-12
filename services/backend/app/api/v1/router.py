"""Aggregates every versioned router under a single prefix."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import admin, attachments, auth, ingest

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(ingest.router)
api_router.include_router(attachments.router)
api_router.include_router(admin.router)

__all__ = ["api_router"]
