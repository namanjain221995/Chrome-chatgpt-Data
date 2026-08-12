"""HTTP middleware: correlation ids, body limits, security headers, timing."""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.core.config import get_settings
from app.core.logging import actor_var, correlation_id_var, get_logger

logger = get_logger(__name__)

CORRELATION_HEADER = "X-Correlation-Id"

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=(), interest-cohort=()",
    # The API returns JSON only; a maximally strict CSP is appropriate.
    "Content-Security-Policy": (
        "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    ),
    "Cache-Control": "no-store",
}


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Bind a correlation id to the request context and echo it back."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        incoming = request.headers.get(CORRELATION_HEADER, "")
        # Never trust a client-supplied value verbatim: it lands in logs.
        correlation_id = (
            incoming
            if incoming.isascii() and incoming.replace("-", "").isalnum() and len(incoming) <= 64
            else uuid.uuid4().hex
        )
        token = correlation_id_var.set(correlation_id)
        actor_token = actor_var.set(None)
        start = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            duration_ms = (time.perf_counter() - start) * 1000
            correlation_id_var.reset(token)
            actor_var.reset(actor_token)
        response.headers[CORRELATION_HEADER] = correlation_id
        response.headers["X-Response-Time-Ms"] = f"{duration_ms:.1f}"
        # Path only: query strings and bodies may carry sensitive material.
        logger.info(
            "http_request",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            duration_ms=round(duration_ms, 1),
        )
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        for header, value in SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        if get_settings().is_production:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=63072000; includeSubDomains"
            )
        return response


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject oversized bodies before they are parsed.

    Cloudflare supplies the outer edge limits; this is the authoritative
    application limit at the origin.
    """

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        max_bytes = get_settings().max_request_bytes
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > max_bytes:
            return JSONResponse(
                status_code=413,
                content={
                    "error": {
                        "code": "payload_too_large",
                        "message": "Request body exceeds the configured maximum",
                        "details": {"max_bytes": max_bytes},
                        "correlation_id": correlation_id_var.get(),
                    }
                },
            )
        return await call_next(request)
