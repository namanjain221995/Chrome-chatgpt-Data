"""Structured logging with aggressive redaction.

Rules enforced here:
  * message text, attachment bytes and credentials never reach ordinary logs;
  * every log line carries the request correlation id when one is bound;
  * a final scrubbing processor removes anything that looks like a bearer
    token, an AWS key, a cookie header or a presigned URL signature.
"""

from __future__ import annotations

import contextvars
import logging
import re
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog

from app.core.config import get_settings

correlation_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "correlation_id", default=None
)
actor_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("actor", default=None)

# Keys whose values are never emitted, regardless of nesting.
_SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "password",
        "postgres_password",
        "secret",
        "client_secret",
        "api_key",
        "apikey",
        "access_token",
        "refresh_token",
        "id_token",
        "token",
        "jwt",
        "session",
        "aws_secret_access_key",
        "aws_access_key_id",
        "presigned_url",
        "upload_url",
        "download_url",
        "signature",
        "x-amz-signature",
        "code_verifier",
        "private_key",
    }
)

# Keys that hold conversation content; suppressed unless explicitly enabled.
_CONTENT_KEYS = frozenset(
    {
        "text",
        "content",
        "message",
        "body",
        "html",
        "sanitized_html",
        "plain_text",
        "parts",
        "payload",
        "raw_payload",
        "title",
        "prompt",
        "answer",
    }
)

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/-]+=*"), "Bearer [REDACTED]"),
    (re.compile(r"(?i)\bAKIA[0-9A-Z]{16}\b"), "[REDACTED_AWS_KEY]"),
    (re.compile(r"(?i)(X-Amz-Signature=)[0-9a-f]+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(X-Amz-Credential=)[^&\s]+"), r"\1[REDACTED]"),
    (re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), "[REDACTED_JWT]"),
)

_REDACTED = "[REDACTED]"
_SUPPRESSED = "[CONTENT_SUPPRESSED]"
_MAX_DEPTH = 6


def _scrub_string(value: str) -> str:
    for pattern, replacement in _PATTERNS:
        value = pattern.sub(replacement, value)
    return value


def _scrub(value: Any, *, allow_content: bool, depth: int = 0) -> Any:
    if depth > _MAX_DEPTH:
        return "[TRUNCATED]"
    if isinstance(value, str):
        return _scrub_string(value)
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in _SENSITIVE_KEYS:
                out[key] = _REDACTED
            elif lowered in _CONTENT_KEYS and not allow_content:
                out[key] = _SUPPRESSED
            else:
                out[key] = _scrub(item, allow_content=allow_content, depth=depth + 1)
        return out
    if isinstance(value, list | tuple):
        return [_scrub(item, allow_content=allow_content, depth=depth + 1) for item in value]
    return value


def redaction_processor(
    _logger: Any, _name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    allow_content = get_settings().log_message_content
    scrubbed = _scrub(dict(event_dict), allow_content=allow_content, depth=0)
    assert isinstance(scrubbed, dict)
    return scrubbed


def correlation_processor(
    _logger: Any, _name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    cid = correlation_id_var.get()
    if cid:
        event_dict.setdefault("correlation_id", cid)
    actor = actor_var.get()
    if actor:
        event_dict.setdefault("actor", actor)
    return event_dict


def configure_logging() -> None:
    settings = get_settings()
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level, force=True)
    for noisy in ("uvicorn.access", "botocore", "boto3", "urllib3", "s3transfer", "aiobotocore"):
        logging.getLogger(noisy).setLevel(max(level, logging.WARNING))

    renderer: Any = (
        structlog.processors.JSONRenderer()
        if settings.log_format == "json"
        else structlog.dev.ConsoleRenderer(colors=False)
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            correlation_processor,
            redaction_processor,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
