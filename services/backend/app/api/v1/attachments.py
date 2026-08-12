"""Attachment endpoints. Bytes never pass through this process."""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.api.deps import PrincipalDep, SessionDep, client_ip
from app.core.config import get_settings
from app.core.logging import get_logger
from app.models.enums import AuditAction
from app.schemas.attachments import (
    AttachmentCompleteIn,
    AttachmentCompleteOut,
    AttachmentInitIn,
    AttachmentInitOut,
)
from app.schemas.common import WorkspaceRef
from app.services import attachments as attachment_service
from app.services import ingest as ingest_service
from app.services.audit import record_audit

logger = get_logger(__name__)
router = APIRouter(tags=["attachments"])


def _stored_workspace_ref() -> WorkspaceRef:
    settings = get_settings()
    ids = settings.managed_workspace_id_list
    return WorkspaceRef(
        kind="managed_company",
        verified=True,
        label=settings.managed_workspace_label,
        source_workspace_id=ids[0] if ids else None,
        verification_signals=["workspace_id_match" if ids else "workspace_label_match"],
    )


@router.post(
    "/attachments/init",
    response_model=AttachmentInitOut,
    summary="Reserve an attachment and get a short-lived presigned upload URL",
)
async def init_attachment(
    request: Request,
    payload: AttachmentInitIn,
    session: SessionDep,
    principal: PrincipalDep,
) -> AttachmentInitOut:
    ctx = await ingest_service.build_context(
        session,
        organization=principal.organization,
        user=principal.user,
        device=principal.device,
        workspace_ref=_stored_workspace_ref(),
    )
    presigned = await attachment_service.init_attachment(session, ctx, payload)
    await record_audit(
        session,
        action=AuditAction.ATTACHMENT_PRESIGNED,
        organization_id=principal.organization.id,
        actor_user_id=principal.user.id,
        actor_email=principal.user.email,
        device_id=principal.device.id if principal.device else None,
        resource_type="attachment",
        resource_id=str(presigned.attachment.id),
        client_ip=client_ip(request),
        details={
            "byte_size": payload.byte_size,
            "mime_type": payload.mime_type,
            "metadata_only": payload.metadata_only,
        },
    )
    return AttachmentInitOut(
        attachment_id=presigned.attachment.id,
        state=str(presigned.attachment.state),  # type: ignore[arg-type]
        upload_url=presigned.upload_url,
        upload_headers=presigned.headers,
        s3_key=presigned.attachment.quarantine_s3_key,
        expires_at=presigned.expires_at,
        duplicate=presigned.duplicate,
    )


@router.post(
    "/attachments/complete",
    response_model=AttachmentCompleteOut,
    summary="Confirm an upload; the backend verifies it before acknowledging",
)
async def complete_attachment(
    request: Request,
    payload: AttachmentCompleteIn,
    session: SessionDep,
    principal: PrincipalDep,
) -> AttachmentCompleteOut:
    ctx = await ingest_service.build_context(
        session,
        organization=principal.organization,
        user=principal.user,
        device=principal.device,
        workspace_ref=_stored_workspace_ref(),
    )
    attachment, verified, reason = await attachment_service.complete_attachment(
        session, ctx, payload
    )
    linked = next((link.message_id for link in attachment.links), None) if verified else None
    await record_audit(
        session,
        action=AuditAction.ATTACHMENT_FINALIZED,
        organization_id=principal.organization.id,
        actor_user_id=principal.user.id,
        actor_email=principal.user.email,
        device_id=principal.device.id if principal.device else None,
        resource_type="attachment",
        resource_id=str(attachment.id),
        outcome="success" if verified else "failure",
        client_ip=client_ip(request),
        details={"state": str(attachment.state), "reason_code": reason},
    )
    return AttachmentCompleteOut(
        attachment_id=attachment.id,
        state=str(attachment.state),  # type: ignore[arg-type]
        verified=verified,
        linked_message_id=linked,
        reason=reason,
    )
