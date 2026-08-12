"""Shared schema primitives and constrained types.

These Pydantic models are the single source of truth for the wire contract.
`scripts/generate_schemas.py` renders them into `packages/schemas/schemas/*.json`,
which the Chrome extension validates against in CI, so TypeScript and Python
cannot drift apart silently.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

SCHEMA_VERSION: Final[Literal["1.0"]] = "1.0"

Sha256Hex = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$", description="Lowercase SHA-256 hex")]
IdempotencyKey = Annotated[str, Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")]
# Source identifiers come straight from the page and end up in S3 object keys
# and database values. Constrain the charset so a control character, a newline
# or a path-looking segment can never reach either.
SourceId = Annotated[
    str,
    Field(min_length=1, max_length=255, pattern=r"^[A-Za-z0-9._:@\-]+$"),
]
ShortText = Annotated[str, Field(max_length=1024)]
SemVerish = Annotated[str, Field(max_length=32, pattern=r"^[A-Za-z0-9._+-]+$")]

MAX_TEXT_BYTES = 1_000_000
MAX_HTML_BYTES = 2_000_000


class StrictModel(BaseModel):
    """Reject unknown fields: an unexpected key is a contract violation."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=False,
        validate_assignment=True,
        populate_by_name=True,
        ser_json_timedelta="float",
    )


class ClientContext(StrictModel):
    """Non-sensitive client provenance attached to every ingest request."""

    extension_version: SemVerish = Field(description="Extension version that produced the payload")
    adapter_version: SemVerish = Field(description="DOM adapter version used for extraction")
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    device_fingerprint: Annotated[str, Field(min_length=16, max_length=128)] | None = None
    page_locale: Annotated[str, Field(max_length=16)] | None = None
    captured_at: datetime = Field(description="Client clock at capture time (UTC)")

    @field_validator("captured_at")
    @classmethod
    def _must_be_tz_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("captured_at must be timezone-aware (UTC)")
        return v


class WorkspaceRef(StrictModel):
    """How the extension identifies the workspace it observed.

    ``verified`` is asserted by the client but re-checked server-side; the
    server never stores content for an unverified or personal workspace.
    """

    source_workspace_id: SourceId | None = None
    label: Annotated[str, Field(max_length=255)] | None = None
    kind: Literal["managed_company", "personal", "unverified"] = "unverified"
    verified: bool = False
    verification_signals: list[Annotated[str, Field(max_length=64)]] = Field(
        default_factory=list, max_length=20
    )


class ItemResult(StrictModel):
    """Per-item outcome inside a batch response."""

    index: int = Field(ge=0)
    idempotency_key: str | None = None
    status: Literal["accepted", "duplicate", "rejected", "retryable"]
    id: uuid.UUID | None = None
    conversation_id: uuid.UUID | None = None
    message_id: uuid.UUID | None = None
    message_version_id: uuid.UUID | None = None
    reason_code: Annotated[str, Field(max_length=64)] | None = None
    reason: ShortText | None = None


class BatchResponse(StrictModel):
    accepted: int = Field(ge=0)
    duplicate: int = Field(ge=0)
    rejected: int = Field(ge=0)
    retryable: int = Field(ge=0)
    results: list[ItemResult]
    queue_depth: int | None = Field(default=None, ge=0)
    backpressure: bool = False
    server_time: datetime


class ErrorDetail(StrictModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    correlation_id: str | None = None


class ErrorResponse(StrictModel):
    error: ErrorDetail


class OkResponse(StrictModel):
    ok: bool = True
    message: str | None = None
