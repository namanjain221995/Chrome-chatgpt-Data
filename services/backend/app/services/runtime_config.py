"""Signed runtime configuration served to the extension.

The extension caches this document and enforces it locally, but the signature
and the ``config_version`` mean a tampered or stale cache is detectable. Every
gate is computed here from server settings; there is no field the extension can
set to widen its own permissions.
"""

from __future__ import annotations

from datetime import timedelta

from app.core.config import Settings, get_settings
from app.core.crypto import sha256_hex, sign_payload, utcnow
from app.schemas.auth import (
    CaptureLimits,
    CapturePolicy,
    RuntimeConfig,
    SignedRuntimeConfig,
    WorkspaceRules,
)
from app.services.attachments import ALLOWED_MIME_TYPES, allowed_extensions

CONFIG_TTL_SECONDS = 900
MINIMUM_EXTENSION_VERSION = "1.0.0"

ALLOWED_URL_PATTERNS = [
    "https://chatgpt.com/*",
    "https://chat.openai.com/*",
]

COVERAGE_STATEMENT = (
    "This extension archives the conversation you currently have open and every new "
    "message you send or receive in the company workspace. It does not archive "
    "conversations you never open in this browser, and it never captures unsent drafts."
)


def build_runtime_config(settings: Settings | None = None) -> RuntimeConfig:
    settings = settings or get_settings()
    now = utcnow()

    policy = CapturePolicy(
        browser_content_capture_enabled=settings.browser_content_capture_enabled,
        openai_written_authorization_confirmed=settings.openai_written_authorization_confirmed,
        capture_active=settings.browser_capture_active,
        auto_archive_current_open_chat=(
            settings.auto_archive_current_open_chat and settings.browser_capture_active
        ),
        attachment_capture_enabled=(
            settings.attachment_capture_enabled and settings.browser_capture_active
        ),
        kill_switch=settings.kill_switch_enabled,
    )

    workspace_rules = WorkspaceRules(
        managed_workspace_label=settings.managed_workspace_label or None,
        managed_workspace_ids=settings.managed_workspace_id_list,
        allowed_url_patterns=ALLOWED_URL_PATTERNS,
        require_all_signals=False,
        min_signals=1,
    )

    limits = CaptureLimits(
        max_batch_items=settings.max_batch_items,
        max_request_bytes=settings.max_request_bytes,
        max_attachment_bytes=settings.max_attachment_bytes,
        allowed_mime_types=sorted(ALLOWED_MIME_TYPES),
        allowed_extensions=allowed_extensions(),
        offline_queue_max_items=settings.offline_queue_max_items,
        offline_queue_max_bytes=settings.offline_queue_max_bytes,
        offline_queue_max_age_days=settings.offline_queue_max_age_days,
        stable_response_quiet_ms=2000,
        backfill_max_messages=2000,
        backfill_max_seconds=120,
        backfill_max_scrolls=400,
        rate_limit_requests_per_minute=settings.rate_limit_requests_per_minute,
    )

    # Version the document by content so an unchanged config keeps its number.
    config_version = _config_version(settings)

    return RuntimeConfig(
        config_version=config_version,
        issued_at=now,
        expires_at=now + timedelta(seconds=CONFIG_TTL_SECONDS),
        organization_slug="techsara",
        api_base_url=settings.public_base_url.rstrip("/") + settings.api_base_path,
        policy=policy,
        workspace_rules=workspace_rules,
        limits=limits,
        privacy_notice_url=settings.public_base_url.rstrip("/") + "/privacy-notice",
        support_contact="it-support@" + (settings.allowed_domains or ["example.com"])[0],
        minimum_extension_version=MINIMUM_EXTENSION_VERSION,
        coverage_statement=COVERAGE_STATEMENT,
    )


def _config_version(settings: Settings) -> int:
    """Deterministic monotonic-ish version derived from policy-relevant values."""
    material = "|".join(
        str(v)
        for v in (
            settings.browser_content_capture_enabled,
            settings.openai_written_authorization_confirmed,
            settings.auto_archive_current_open_chat,
            settings.attachment_capture_enabled,
            settings.kill_switch_enabled,
            settings.managed_workspace_label,
            ",".join(settings.managed_workspace_id_list),
            settings.max_batch_items,
            settings.max_attachment_bytes,
            settings.rate_limit_requests_per_minute,
            MINIMUM_EXTENSION_VERSION,
        )
    )
    # 1 + 31 bits keeps it a positive, stable integer.
    return 1 + (int(sha256_hex(material)[:8], 16) % 2_000_000_000)


def redact_for_public(config: RuntimeConfig) -> RuntimeConfig:
    """Strip workspace identifiers from a configuration served unauthenticated.

    The public document exists so the extension can discover the kill switch and
    the privacy notice before anyone signs in. The managed workspace label and
    id allowlist are internal configuration, and an attacker who knows them can
    craft a more convincing spoof, so they are withheld until the caller proves
    it holds a valid company session. Capture requires sign-in anyway, so a
    client that only ever sees this document simply fails closed.
    """
    return config.model_copy(
        update={
            "workspace_rules": config.workspace_rules.model_copy(
                update={"managed_workspace_label": None, "managed_workspace_ids": []}
            )
        }
    )


def sign_runtime_config(
    config: RuntimeConfig, settings: Settings | None = None
) -> SignedRuntimeConfig:
    settings = settings or get_settings()
    payload = config.model_dump(mode="json")
    signature = sign_payload(payload, settings.config_signing_key)
    return SignedRuntimeConfig(
        config=config,
        signature=signature,
        key_id=sha256_hex(settings.config_signing_key)[:16],
    )


def get_signed_config(
    settings: Settings | None = None, *, authenticated: bool = False
) -> SignedRuntimeConfig:
    """Signed configuration; workspace identifiers only for authenticated callers."""
    settings = settings or get_settings()
    config = build_runtime_config(settings)
    if not authenticated:
        config = redact_for_public(config)
    return sign_runtime_config(config, settings)
