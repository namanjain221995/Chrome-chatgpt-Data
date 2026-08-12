"""Adapter boundary for the OpenAI Enterprise Compliance interface.

This module deliberately contains **no invented endpoint paths**. Base URL,
log path, files path, pagination style and response field names are all supplied
by configuration taken from the authorized API documentation the company
receives with its Enterprise agreement. Until those values are set, the adapter
reports itself unconfigured and the poller stays idle.

Everything endpoint-specific lives behind :class:`ComplianceAdapter`, so
adapting to a documented change is a configuration edit plus, at most, a change
in this one file.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from app.core.config import Settings, get_settings
from app.core.crypto import canonical_json_sha256
from app.core.errors import UpstreamError
from app.core.logging import get_logger

logger = get_logger(__name__)

DEFAULT_TIMEOUT_SECONDS = 30
MAX_RETRIES = 5


@dataclass(frozen=True)
class FieldMap:
    """Where the interesting values live inside an upstream response.

    Each entry is a dotted path evaluated against the JSON document. Override
    with the ``OPENAI_COMPLIANCE_FIELD_MAP`` environment variable (JSON object)
    once the authorized documentation is available.
    """

    items: str = "data"
    next_cursor: str = "next_cursor"
    has_more: str = "has_more"
    event_id: str = "id"
    event_time: str = "created_at"
    event_type: str = "type"
    conversation_id: str = "conversation_id"
    message_id: str = "message_id"
    workspace_id: str = "workspace_id"
    actor_email: str = "user.email"
    deleted_flag: str = "deleted"

    @classmethod
    def from_env(cls) -> FieldMap:
        raw = os.getenv("OPENAI_COMPLIANCE_FIELD_MAP", "").strip()
        if not raw:
            return cls()
        try:
            overrides = json.loads(raw)
        except json.JSONDecodeError:
            logger.error("compliance_field_map_invalid_json")
            return cls()
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in overrides.items() if k in known})


@dataclass
class ComplianceEvent:
    """One normalised upstream event, plus the untouched original payload."""

    source_event_id: str
    event_time: datetime | None
    kind: str
    conversation_id: str | None
    message_id: str | None
    workspace_id: str | None
    actor_email: str | None
    is_deletion: bool
    raw: dict[str, Any]

    @property
    def payload_sha256(self) -> str:
        return canonical_json_sha256(self.raw)


@dataclass
class CompliancePage:
    events: list[ComplianceEvent] = field(default_factory=list)
    next_cursor: str | None = None
    has_more: bool = False


def _dig(document: Any, path: str) -> Any:
    current = document
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
        if current is None:
            return None
    return current


def _parse_time(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, int | float):
        # Heuristic: values past year 5138 in seconds are milliseconds.
        seconds = value / 1000 if value > 1e11 else value
        return datetime.fromtimestamp(seconds, tz=UTC)
    if isinstance(value, str):
        text = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


class ComplianceAdapter:
    """HTTP client for the authorized compliance interface."""

    def __init__(self, settings: Settings | None = None, field_map: FieldMap | None = None) -> None:
        self._settings = settings or get_settings()
        self.field_map = field_map or FieldMap.from_env()

    # -- configuration ----------------------------------------------------
    @property
    def is_configured(self) -> bool:
        s = self._settings
        return bool(
            s.openai_compliance_base_url
            and s.openai_compliance_log_path
            and s.openai_compliance_api_key
        )

    @property
    def is_enabled(self) -> bool:
        return self._settings.compliance_poll_enabled and self.is_configured

    def describe(self) -> dict[str, Any]:
        """Non-secret description used by the admin status endpoint."""
        return {
            "enabled": self._settings.compliance_poll_enabled,
            "configured": self.is_configured,
            "base_url_set": bool(self._settings.openai_compliance_base_url),
            "log_path_set": bool(self._settings.openai_compliance_log_path),
            "files_path_set": bool(self._settings.openai_compliance_files_path),
            "field_map": self.field_map.__dict__,
        }

    # -- fetching ---------------------------------------------------------
    def _url(self, path: str) -> str:
        base = (self._settings.openai_compliance_base_url or "").rstrip("/")
        return f"{base}/{path.lstrip('/')}"

    def _headers(self) -> dict[str, str]:
        key = self._settings.openai_compliance_api_key or ""
        return {
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
            "User-Agent": f"{self._settings.app_name}/{self._settings.app_version}",
        }

    async def fetch_log_page(
        self,
        *,
        window_start: datetime,
        window_end: datetime,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> CompliancePage:
        """Fetch one page of compliance log events.

        Query parameter names follow the documented interface and are the only
        assumption made here; adjust this method (not the callers) if the
        authorized documentation differs.
        """
        if not self.is_configured:
            raise UpstreamError("Compliance adapter is not configured", code="not_configured")

        params: dict[str, Any] = {
            "since": window_start.astimezone(UTC).isoformat(),
            "until": window_end.astimezone(UTC).isoformat(),
            "limit": limit or self._settings.compliance_page_size,
        }
        if cursor:
            params["cursor"] = cursor

        url = self._url(self._settings.openai_compliance_log_path or "")
        document = await self._request(url, params)
        return self._parse_page(document)

    async def fetch_file(self, file_reference: str) -> bytes:
        """Download an authorized export artifact, when a files path is set."""
        if not self._settings.openai_compliance_files_path:
            raise UpstreamError("No compliance files path is configured", code="not_configured")
        url = self._url(
            self._settings.openai_compliance_files_path.replace("{file_id}", file_reference)
        )
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS) as client:
            response = await client.get(url, headers=self._headers())
        if response.status_code >= 400:
            raise UpstreamError(f"Compliance file fetch failed with {response.status_code}")
        return response.content

    async def _request(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        """GET with exponential backoff and full jitter.

        Neither the API key nor the response body is ever logged.
        """
        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS) as client:
                    response = await client.get(url, params=params, headers=self._headers())
                if response.status_code == 429 or response.status_code >= 500:
                    raise UpstreamError(f"Compliance API returned {response.status_code}")
                if response.status_code >= 400:
                    # 4xx other than 429 will not succeed on retry.
                    raise UpstreamError(
                        f"Compliance API rejected the request ({response.status_code})",
                        code="compliance_client_error",
                    )
                return dict(response.json())
            except UpstreamError as exc:
                if exc.code == "compliance_client_error":
                    raise
                last_error = exc
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc

            delay = min(2**attempt, 30) * (0.5 + random.random())  # noqa: S311 - jitter only
            logger.warning(
                "compliance_request_retry", attempt=attempt + 1, delay_seconds=round(delay, 2)
            )
            await asyncio.sleep(delay)

        raise UpstreamError(
            f"Compliance API unavailable after {MAX_RETRIES} attempts: {type(last_error).__name__}"
        )

    # -- parsing ----------------------------------------------------------
    def _parse_page(self, document: dict[str, Any]) -> CompliancePage:
        fm = self.field_map
        raw_items = _dig(document, fm.items)
        items: list[dict[str, Any]] = raw_items if isinstance(raw_items, list) else []
        events: list[ComplianceEvent] = []
        for item in items:
            parsed = self.parse_event(item)
            if parsed is not None:
                events.append(parsed)
        return CompliancePage(
            events=events,
            next_cursor=_dig(document, fm.next_cursor),
            has_more=bool(_dig(document, fm.has_more)),
        )

    def parse_event(self, item: dict[str, Any]) -> ComplianceEvent | None:
        fm = self.field_map
        event_id = _dig(item, fm.event_id)
        if not event_id:
            logger.warning("compliance_event_missing_id")
            return None
        event_type = str(_dig(item, fm.event_type) or "unknown")
        return ComplianceEvent(
            source_event_id=str(event_id),
            event_time=_parse_time(_dig(item, fm.event_time)),
            kind=_classify(event_type),
            conversation_id=_as_str(_dig(item, fm.conversation_id)),
            message_id=_as_str(_dig(item, fm.message_id)),
            workspace_id=_as_str(_dig(item, fm.workspace_id)),
            actor_email=_as_str(_dig(item, fm.actor_email)),
            is_deletion=bool(_dig(item, fm.deleted_flag)) or "delet" in event_type.lower(),
            raw=item,
        )


def _as_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _classify(event_type: str) -> str:
    lowered = event_type.lower()
    if "delet" in lowered or "purge" in lowered:
        return "deletion"
    if "message" in lowered:
        return "message"
    if "conversation" in lowered or "chat" in lowered:
        return "conversation"
    if "file" in lowered or "attachment" in lowered:
        return "file"
    return "unknown"
