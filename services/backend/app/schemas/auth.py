"""Authentication, device registration and signed runtime configuration."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field

from app.schemas.common import SemVerish, ShortText, StrictModel


class AuthExchangeIn(StrictModel):
    """Authorization Code + PKCE exchange, or a directly supplied ID token.

    The extension never sends an e-mail address as proof of identity: the
    backend derives identity from the verified ID token only.
    """

    grant_type: Literal["authorization_code", "id_token", "refresh_token"] = "authorization_code"
    code: Annotated[str, Field(max_length=2048)] | None = None
    code_verifier: Annotated[str, Field(min_length=43, max_length=128)] | None = None
    redirect_uri: Annotated[str, Field(max_length=2048)] | None = None
    id_token: Annotated[str, Field(max_length=8192)] | None = None
    refresh_token: Annotated[str, Field(max_length=512)] | None = None
    nonce: Annotated[str, Field(max_length=128)] | None = None
    state: Annotated[str, Field(max_length=128)] | None = None
    device_fingerprint: Annotated[str, Field(min_length=16, max_length=128)] | None = None
    extension_version: SemVerish | None = None


class AuthTokensOut(StrictModel):
    access_token: str
    token_type: Literal["Bearer"] = "Bearer"  # noqa: S105 - scheme name
    expires_in: int = Field(ge=1)
    refresh_token: str
    refresh_expires_in: int = Field(ge=1)
    user_id: uuid.UUID
    organization_id: uuid.UUID
    device_id: uuid.UUID | None = None
    email: str
    roles: list[str]
    notice_acknowledged: bool


class DeviceRegisterIn(StrictModel):
    device_fingerprint: Annotated[str, Field(min_length=16, max_length=128)]
    extension_id: Annotated[str, Field(max_length=64)] | None = None
    extension_version: SemVerish
    adapter_version: SemVerish
    browser_version: Annotated[str, Field(max_length=64)] | None = None
    platform: Annotated[str, Field(max_length=64)] | None = None
    managed_by_policy: bool = False
    notice_acknowledged: bool = False


class DeviceRegisterOut(StrictModel):
    device_id: uuid.UUID
    registered_at: datetime
    revoked: bool
    server_time: datetime


class CapturePolicy(StrictModel):
    """Server-authoritative capture policy.

    Every flag here is decided on the server. The extension treats these as
    read-only and has no local override path; a false value always wins.
    """

    browser_content_capture_enabled: bool
    openai_written_authorization_confirmed: bool
    #: True only when BOTH gates above are true and the kill switch is off.
    capture_active: bool
    auto_archive_current_open_chat: bool
    attachment_capture_enabled: bool
    personal_workspace_capture_enabled: Literal[False] = False
    capture_unsent_drafts: Literal[False] = False
    kill_switch: bool


class WorkspaceRules(StrictModel):
    """Conservative workspace verification rules; the client fails closed."""

    managed_workspace_label: str | None = None
    managed_workspace_ids: list[str] = Field(default_factory=list)
    allowed_url_patterns: list[str]
    require_all_signals: bool = True
    min_signals: int = Field(default=1, ge=1)


class CaptureLimits(StrictModel):
    max_batch_items: int = Field(ge=1)
    max_request_bytes: int = Field(ge=1024)
    max_attachment_bytes: int = Field(ge=1024)
    allowed_mime_types: list[str]
    allowed_extensions: list[str]
    offline_queue_max_items: int = Field(ge=1)
    offline_queue_max_bytes: int = Field(ge=1024)
    offline_queue_max_age_days: int = Field(ge=1)
    stable_response_quiet_ms: int = Field(ge=250, le=30_000)
    backfill_max_messages: int = Field(ge=1)
    backfill_max_seconds: int = Field(ge=1)
    backfill_max_scrolls: int = Field(ge=1)
    rate_limit_requests_per_minute: int = Field(ge=1)


class RuntimeConfig(StrictModel):
    """The payload the extension caches and enforces."""

    schema_version: Literal["1.0"] = "1.0"
    config_version: int = Field(ge=1)
    issued_at: datetime
    expires_at: datetime
    organization_slug: str
    api_base_url: str
    policy: CapturePolicy
    workspace_rules: WorkspaceRules
    limits: CaptureLimits
    privacy_notice_url: str
    support_contact: str
    minimum_extension_version: SemVerish
    coverage_statement: ShortText


class SignedRuntimeConfig(StrictModel):
    """Versioned + HMAC-signed config so a tampered cache is detectable."""

    config: RuntimeConfig
    signature: str
    signature_algorithm: Literal["HMAC-SHA256"] = "HMAC-SHA256"
    key_id: str


class DeviceRevokeIn(StrictModel):
    device_id: uuid.UUID
    reason: ShortText | None = None
