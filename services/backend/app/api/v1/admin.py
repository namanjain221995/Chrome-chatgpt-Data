"""Administrative endpoints: health summary, exports, device revocation.

Every read here is audited. Message content is never returned by these
endpoints; they deal in counts, identifiers and object keys.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request
from sqlalchemy import func, select

from app.api.deps import Principal, SessionDep, client_ip, require_roles
from app.core.config import get_settings
from app.core.crypto import utcnow
from app.core.errors import NotFoundError, PolicyError
from app.core.logging import get_logger
from app.core.security import ADMIN_ROLES, EXPORT_ROLES, Role
from app.models.attachment import Attachment
from app.models.conversation import Conversation, Message
from app.models.enums import (
    AttachmentState,
    AuditAction,
    ExportKind,
    ExportStatus,
    JobKind,
)
from app.models.events import CaptureEvent, SyncCheckpoint
from app.models.governance import Export
from app.models.identity import Device, User
from app.schemas.attachments import (
    AdminHealthSummary,
    ComplianceHealth,
    ExportCreateIn,
    ExportOut,
    QueueHealth,
    StorageHealth,
)
from app.schemas.auth import DeviceRevokeIn
from app.schemas.common import OkResponse
from app.services import accounts
from app.services import jobs as jobs_service
from app.services.audit import record_audit
from app.services.exports import assert_export_allowed
from app.services.policy import capture_policy_dict
from app.services.storage import get_storage

logger = get_logger(__name__)
router = APIRouter(tags=["admin"], prefix="/admin")

AdminDep = Annotated[
    Principal,
    Depends(
        require_roles(
            Role.COMPLIANCE_ADMIN, Role.SECURITY_REVIEWER, Role.SUPPORT, Role.DATA_CURATOR
        )
    ),
]
ExporterDep = Annotated[Principal, Depends(require_roles(Role.DATA_CURATOR, Role.COMPLIANCE_ADMIN))]
ComplianceAdminDep = Annotated[Principal, Depends(require_roles(Role.COMPLIANCE_ADMIN))]


@router.get(
    "/health-summary",
    response_model=AdminHealthSummary,
    summary="Operational health for administrators",
)
async def health_summary(
    request: Request, session: SessionDep, principal: AdminDep
) -> AdminHealthSummary:
    settings = get_settings()
    storage = get_storage()

    queue = await jobs_service.queue_stats(session)
    storage_ok = await storage.check()

    unarchived = (
        await session.execute(
            select(func.count()).select_from(CaptureEvent).where(CaptureEvent.archived_at.is_(None))
        )
    ).scalar_one()
    stale_snapshots = (
        await session.execute(
            select(func.count())
            .select_from(Conversation)
            .where(Conversation.snapshot_stale.is_(True), Conversation.deleted_at.is_(None))
        )
    ).scalar_one()
    pending_attachments = (
        await session.execute(
            select(func.count())
            .select_from(Attachment)
            .where(Attachment.state.in_([AttachmentState.PENDING, AttachmentState.QUARANTINE]))
        )
    ).scalar_one()

    checkpoint = (
        (
            await session.execute(
                select(SyncCheckpoint).where(
                    SyncCheckpoint.organization_id == principal.organization.id,
                    SyncCheckpoint.source == "openai_compliance",
                )
            )
        )
        .scalars()
        .first()
    )

    now = utcnow()
    lag = None
    if checkpoint and checkpoint.last_event_time:
        lag = (now - checkpoint.last_event_time).total_seconds()

    compliance = ComplianceHealth(
        enabled=settings.compliance_poll_enabled,
        configured=bool(
            settings.openai_compliance_base_url
            and settings.openai_compliance_log_path
            and settings.openai_compliance_api_key
        ),
        last_success_at=checkpoint.last_success_at if checkpoint else None,
        last_attempt_at=checkpoint.last_attempt_at if checkpoint else None,
        last_event_time=checkpoint.last_event_time if checkpoint else None,
        lag_seconds=lag,
        consecutive_errors=checkpoint.consecutive_errors if checkpoint else 0,
        total_events=checkpoint.total_events if checkpoint else 0,
        cursor_healthy=bool(checkpoint and checkpoint.consecutive_errors < 5),
        note=(
            None
            if settings.compliance_poll_enabled
            else "Compliance polling is disabled until credentials and endpoints are configured."
        ),
    )

    counts = {
        "conversations": int(
            (await session.execute(select(func.count()).select_from(Conversation))).scalar_one()
        ),
        "messages": int(
            (await session.execute(select(func.count()).select_from(Message))).scalar_one()
        ),
        "users": int((await session.execute(select(func.count()).select_from(User))).scalar_one()),
        "devices": int(
            (await session.execute(select(func.count()).select_from(Device))).scalar_one()
        ),
    }

    warnings: list[str] = []
    if not settings.browser_capture_active:
        warnings.append(
            "Browser content capture is gated off; the extension will not archive content."
        )
    if queue["stale_locks"]:
        warnings.append(f"{queue['stale_locks']} job(s) hold a stale lock.")
    if not storage_ok:
        warnings.append("Object storage is not reachable from the API container.")
    if int(unarchived) > 1000:
        warnings.append(f"{unarchived} capture events are not yet archived to S3.")

    await record_audit(
        session,
        action=AuditAction.ADMIN_READ,
        organization_id=principal.organization.id,
        actor_user_id=principal.user.id,
        actor_email=principal.user.email,
        actor_roles=[r.value for r in principal.roles],
        resource_type="health_summary",
        client_ip=client_ip(request),
    )

    return AdminHealthSummary(
        server_time=now,
        environment=settings.environment,
        version=settings.app_version,
        git_sha=settings.git_sha,
        database_ok=True,
        storage=StorageHealth(
            bucket=settings.s3_bucket,
            reachable=storage_ok,
            unarchived_events=int(unarchived),
            stale_snapshots=int(stale_snapshots),
            pending_attachments=int(pending_attachments),
        ),
        queue=QueueHealth(
            pending=queue["pending"],
            running=queue["running"],
            failed=queue["failed"],
            dead=queue["dead"],
            oldest_pending_age_seconds=queue["oldest_pending_age_seconds"],
            stale_locks=queue["stale_locks"],
            backpressure=queue["backpressure"],
        ),
        compliance=compliance,
        policy=capture_policy_dict(settings),
        counts=counts,
        warnings=warnings,
    )


@router.post("/exports", response_model=ExportOut, summary="Queue a curated export")
async def create_export(
    request: Request, payload: ExportCreateIn, session: SessionDep, principal: ExporterDep
) -> ExportOut:
    settings = get_settings()
    kind = ExportKind(payload.kind)
    assert_export_allowed(kind, settings)

    if kind == ExportKind.LEGAL_HOLD_BUNDLE and not principal.claims.has_any(
        frozenset({Role.COMPLIANCE_ADMIN})
    ):
        raise PolicyError("Legal-hold bundles require the compliance_admin role")

    export = Export(
        id=uuid.uuid4(),
        organization_id=principal.organization.id,
        requested_by_user_id=principal.user.id,
        kind=kind,
        status=ExportStatus.PENDING,
        filters={
            "conversation_ids": [str(c) for c in payload.conversation_ids],
            "from_time": payload.from_time.isoformat() if payload.from_time else None,
            "to_time": payload.to_time.isoformat() if payload.to_time else None,
            "workspace_id": str(payload.workspace_id) if payload.workspace_id else None,
            "reason": payload.reason,
        },
        split_strategy=payload.split_strategy,
        split_ratios=payload.split_ratios,
        include_attachments=payload.include_attachments,
    )
    session.add(export)
    await session.flush()

    await jobs_service.enqueue_job(
        session,
        kind=JobKind.RUN_EXPORT,
        organization_id=principal.organization.id,
        priority=300,
        dedupe_key=f"export:{export.id}",
        payload={"export_id": str(export.id)},
    )
    await record_audit(
        session,
        action=AuditAction.EXPORT_CREATED,
        organization_id=principal.organization.id,
        actor_user_id=principal.user.id,
        actor_email=principal.user.email,
        actor_roles=[r.value for r in principal.roles],
        resource_type="export",
        resource_id=str(export.id),
        client_ip=client_ip(request),
        details={"kind": payload.kind, "reason_supplied": bool(payload.reason)},
    )
    return _export_out(export, download_urls=[])


@router.get(
    "/exports/{export_id}", response_model=ExportOut, summary="Export status and download links"
)
async def get_export(
    request: Request,
    session: SessionDep,
    principal: ExporterDep,
    export_id: Annotated[uuid.UUID, Path()],
) -> ExportOut:
    export = await session.get(Export, export_id)
    if export is None or export.organization_id != principal.organization.id:
        raise NotFoundError("Unknown export")

    download_urls: list[str] = []
    if export.status == ExportStatus.COMPLETED:
        storage = get_storage()
        # Short-lived presigned GETs, only for the roles allowed to export.
        for part in (export.parts or [])[:100]:
            download_urls.append(storage.presign_get(key=part["s3_key"]))
        if export.manifest_s3_key:
            download_urls.append(storage.presign_get(key=export.manifest_s3_key))
        await record_audit(
            session,
            action=AuditAction.EXPORT_DOWNLOADED,
            organization_id=principal.organization.id,
            actor_user_id=principal.user.id,
            actor_email=principal.user.email,
            actor_roles=[r.value for r in principal.roles],
            resource_type="export",
            resource_id=str(export.id),
            client_ip=client_ip(request),
            details={"url_count": len(download_urls)},
        )
    return _export_out(export, download_urls=download_urls)


def _export_out(export: Export, *, download_urls: list[str]) -> ExportOut:
    return ExportOut(
        export_id=export.id,
        kind=str(export.kind),
        status=str(export.status),
        conversation_count=export.conversation_count,
        record_count=export.record_count,
        byte_size=export.byte_size,
        s3_prefix=export.s3_prefix,
        manifest_s3_key=export.manifest_s3_key,
        manifest_sha256=export.manifest_sha256,
        parts=list(export.parts or []),
        download_urls=download_urls,
        created_at=export.created_at,
        completed_at=export.completed_at,
        error_summary=export.error_summary,
    )


@router.post("/devices/revoke", response_model=OkResponse, summary="Revoke a device session")
async def revoke_device(
    request: Request, payload: DeviceRevokeIn, session: SessionDep, principal: ComplianceAdminDep
) -> OkResponse:
    device = await session.get(Device, payload.device_id)
    if device is None or device.organization_id != principal.organization.id:
        raise NotFoundError("Unknown device")
    await accounts.revoke_device(session, device_id=payload.device_id, reason=payload.reason)
    await record_audit(
        session,
        action=AuditAction.DEVICE_REVOKED,
        organization_id=principal.organization.id,
        actor_user_id=principal.user.id,
        actor_email=principal.user.email,
        actor_roles=[r.value for r in principal.roles],
        resource_type="device",
        resource_id=str(payload.device_id),
        client_ip=client_ip(request),
    )
    return OkResponse(message="Device session revoked")


__all__ = ["ADMIN_ROLES", "EXPORT_ROLES", "router"]
