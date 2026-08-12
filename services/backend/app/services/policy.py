"""Server-authoritative capture policy and workspace verification.

Everything in this module fails closed. The extension's own opinion about a
workspace is treated as an untrusted hint; the decision is made here.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.crypto import pseudonymize, utcnow
from app.core.errors import PolicyError
from app.core.logging import get_logger
from app.models.enums import WorkspaceKind
from app.models.identity import Organization, Workspace
from app.schemas.common import WorkspaceRef

logger = get_logger(__name__)

#: Signals the extension may report. Only these are considered.
KNOWN_SIGNALS = frozenset(
    {
        "workspace_label_match",
        "workspace_id_match",
        "account_switcher_managed_badge",
        "managed_account_url_path",
        "enterprise_workspace_marker",
    }
)

#: Signals strong enough to prove *identity* of the workspace on their own.
STRONG_SIGNALS = frozenset({"workspace_id_match", "workspace_label_match"})


@dataclass(frozen=True)
class WorkspaceDecision:
    workspace: Workspace
    workspace_hash: str
    reason: str


def workspace_hash_for(org_slug: str, ref: WorkspaceRef) -> str:
    """Stable pseudonym used in S3 prefixes; never the raw workspace name."""
    identity = ref.source_workspace_id or (ref.label or "").strip().lower()
    return pseudonymize(f"{org_slug}|{identity}")[:32]


def assert_browser_capture_allowed(settings: Settings | None = None) -> None:
    """Both server gates must be true and the kill switch must be off."""
    settings = settings or get_settings()
    if settings.kill_switch_enabled:
        raise PolicyError(
            "Capture is disabled by the administrator kill switch",
            code="kill_switch_active",
        )
    if not settings.browser_content_capture_enabled:
        raise PolicyError(
            "Browser content capture is disabled on the server "
            "(BROWSER_CONTENT_CAPTURE_ENABLED=false)",
            code="capture_disabled",
        )
    if not settings.openai_written_authorization_confirmed:
        raise PolicyError(
            "Written authorization has not been confirmed "
            "(OPENAI_WRITTEN_AUTHORIZATION_CONFIRMED=false)",
            code="authorization_not_confirmed",
        )


def assert_attachment_capture_allowed(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    assert_browser_capture_allowed(settings)
    if not settings.attachment_capture_enabled:
        raise PolicyError("Attachment capture is disabled", code="attachments_disabled")


def evaluate_workspace_ref(ref: WorkspaceRef, settings: Settings | None = None) -> str:
    """Return the verification reason, or raise :class:`PolicyError`.

    Order matters: an explicitly personal workspace is rejected before any
    other consideration, and an unconfigured server rejects everything.
    """
    settings = settings or get_settings()

    # PERSONAL_WORKSPACE_CAPTURE_ENABLED exists only to document the invariant;
    # it is never honoured, in any environment, even if set to true.
    if ref.kind == "personal":
        raise PolicyError(
            "Personal-workspace conversations are never archived",
            code="personal_workspace_blocked",
        )
    if ref.kind != "managed_company":
        raise PolicyError(
            "Workspace is not verified as the managed company workspace",
            code="workspace_unverified",
        )
    if not ref.verified:
        raise PolicyError(
            "Extension did not assert workspace verification", code="workspace_unverified"
        )

    signals = {s for s in ref.verification_signals if s in KNOWN_SIGNALS}
    allowed_ids = {i.strip() for i in settings.managed_workspace_id_list if i.strip()}
    configured_label = (settings.managed_workspace_label or "").strip().lower()

    if not allowed_ids and not configured_label:
        # Nothing to verify against: refuse rather than guess.
        raise PolicyError(
            "Server has no managed workspace identifiers configured; capture is refused",
            code="workspace_policy_unconfigured",
        )

    if allowed_ids:
        if not ref.source_workspace_id or ref.source_workspace_id.strip() not in allowed_ids:
            raise PolicyError(
                "Workspace id is not in the configured managed workspace allowlist",
                code="workspace_not_allowlisted",
            )
        # An allowlisted id is strong evidence, but the client must also report
        # having actually observed it rather than only asserting the value.
        if not signals & STRONG_SIGNALS:
            raise PolicyError(
                "No strong workspace verification signal was reported",
                code="workspace_signal_missing",
            )
        return "workspace_id_allowlisted"

    label = (ref.label or "").strip().lower()
    if not label or label != configured_label:
        raise PolicyError(
            "Workspace label does not match the configured managed workspace",
            code="workspace_label_mismatch",
        )
    if not signals & STRONG_SIGNALS:
        raise PolicyError(
            "No strong workspace verification signal was reported",
            code="workspace_signal_missing",
        )
    return "workspace_label_matched"


async def resolve_workspace(
    session: AsyncSession,
    *,
    organization: Organization,
    ref: WorkspaceRef,
    settings: Settings | None = None,
) -> WorkspaceDecision:
    """Verify the workspace and return (creating if needed) its row."""
    settings = settings or get_settings()
    reason = evaluate_workspace_ref(ref, settings)
    ws_hash = workspace_hash_for(organization.slug, ref)

    existing = (
        await session.execute(
            select(Workspace).where(
                Workspace.organization_id == organization.id,
                Workspace.workspace_hash == ws_hash,
            )
        )
    ).scalar_one_or_none()

    if existing is not None:
        if existing.kind != WorkspaceKind.MANAGED_COMPANY or not existing.capture_enabled:
            raise PolicyError(
                "Stored workspace record does not permit capture",
                code="workspace_capture_disabled",
            )
        if ref.source_workspace_id and not existing.source_workspace_id:
            existing.source_workspace_id = ref.source_workspace_id
        if ref.label and not existing.label:
            existing.label = ref.label
        return WorkspaceDecision(existing, ws_hash, reason)

    workspace = Workspace(
        id=uuid.uuid4(),
        organization_id=organization.id,
        source_workspace_id=ref.source_workspace_id,
        label=ref.label,
        workspace_hash=ws_hash,
        kind=WorkspaceKind.MANAGED_COMPANY,
        verified_at=utcnow(),
        capture_enabled=True,
    )
    session.add(workspace)
    await session.flush()
    logger.info("workspace_verified", workspace_hash=ws_hash, reason=reason)
    return WorkspaceDecision(workspace, ws_hash, reason)


def capture_policy_dict(settings: Settings | None = None) -> dict[str, bool]:
    settings = settings or get_settings()
    return {
        "browser_content_capture_enabled": settings.browser_content_capture_enabled,
        "openai_written_authorization_confirmed": settings.openai_written_authorization_confirmed,
        "capture_active": settings.browser_capture_active,
        "auto_archive_current_open_chat": settings.auto_archive_current_open_chat,
        "attachment_capture_enabled": settings.attachment_capture_enabled,
        "personal_workspace_capture_enabled": False,
        "capture_unsent_drafts": False,
        "kill_switch": settings.kill_switch_enabled,
        "compliance_poll_enabled": settings.compliance_poll_enabled,
        "training_export_enabled": settings.training_export_enabled,
    }
