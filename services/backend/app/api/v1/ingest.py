"""Ingestion endpoints: conversations, messages, capture events, feedback, sync."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import PrincipalDep, SessionDep
from app.core.config import get_settings
from app.core.crypto import utcnow
from app.core.errors import BackpressureError, ValidationError
from app.core.logging import get_logger
from app.schemas.common import BatchResponse, ItemResult
from app.schemas.ingest import (
    CaptureEventBatchIn,
    ConversationUpsertIn,
    ConversationUpsertOut,
    FeedbackIn,
    FeedbackOut,
    MessageBatchIn,
    SyncStatusOut,
)
from app.services import ingest as ingest_service
from app.services import jobs as jobs_service
from app.services.runtime_config import COVERAGE_STATEMENT

logger = get_logger(__name__)
router = APIRouter(tags=["ingest"])


async def _guard_backpressure(session: SessionDep) -> int:
    """Refuse new work when the durable queue is saturated."""
    settings = get_settings()
    depth = await jobs_service.pending_job_count(session)
    if depth >= settings.job_queue_backpressure_threshold:
        raise BackpressureError(
            "Archive queue is saturated; retry after backing off",
            retry_after=60,
            details={"queue_depth": depth},
        )
    return depth


@router.post(
    "/conversations/upsert",
    response_model=ConversationUpsertOut,
    summary="Create or update a conversation record",
)
async def upsert_conversation(
    payload: ConversationUpsertIn, session: SessionDep, principal: PrincipalDep
) -> ConversationUpsertOut:
    await _guard_backpressure(session)
    ctx = await ingest_service.build_context(
        session,
        organization=principal.organization,
        user=principal.user,
        device=principal.device,
        workspace_ref=payload.workspace,
    )
    conversation, created, _duplicate = await ingest_service.upsert_conversation(
        session, ctx, payload
    )
    return ConversationUpsertOut(
        conversation_id=conversation.id,
        workspace_id=ctx.workspace.id,
        created=created,
        capture_completeness=str(conversation.capture_completeness),  # type: ignore[arg-type]
        already_archived_message_count=conversation.message_count,
        server_time=utcnow(),
    )


@router.post(
    "/messages/batch",
    response_model=BatchResponse,
    summary="Ingest a batch of committed message versions",
)
async def ingest_messages(
    payload: MessageBatchIn, session: SessionDep, principal: PrincipalDep
) -> BatchResponse:
    settings = get_settings()
    if len(payload.messages) > settings.max_batch_items:
        raise ValidationError(
            "Batch exceeds the configured maximum item count",
            details={"max_batch_items": settings.max_batch_items},
        )
    depth = await _guard_backpressure(session)
    ctx = await ingest_service.build_context(
        session,
        organization=principal.organization,
        user=principal.user,
        device=principal.device,
        workspace_ref=payload.workspace,
    )
    results = await ingest_service.ingest_message_batch(
        session, ctx, payload.messages, payload.client
    )
    if principal.device is not None:
        principal.device.last_sync_at = utcnow()
    return _batch_response(results, depth)


@router.post(
    "/capture-events/batch",
    response_model=BatchResponse,
    summary="Ingest a batch of raw capture events",
)
async def ingest_capture_events(
    payload: CaptureEventBatchIn, session: SessionDep, principal: PrincipalDep
) -> BatchResponse:
    settings = get_settings()
    if len(payload.events) > settings.max_batch_items:
        raise ValidationError(
            "Batch exceeds the configured maximum item count",
            details={"max_batch_items": settings.max_batch_items},
        )
    depth = await _guard_backpressure(session)
    ctx = await ingest_service.build_context(
        session,
        organization=principal.organization,
        user=principal.user,
        device=principal.device,
        workspace_ref=payload.workspace,
    )
    results = await ingest_service.ingest_capture_events(
        session, ctx, payload.events, payload.client
    )
    return _batch_response(results, depth)


def _batch_response(results: list[ItemResult], queue_depth: int) -> BatchResponse:
    settings = get_settings()
    counts = {"accepted": 0, "duplicate": 0, "rejected": 0, "retryable": 0}
    for result in results:
        counts[result.status] += 1
    return BatchResponse(
        accepted=counts["accepted"],
        duplicate=counts["duplicate"],
        rejected=counts["rejected"],
        retryable=counts["retryable"],
        results=results,
        queue_depth=queue_depth,
        backpressure=queue_depth >= settings.job_queue_backpressure_threshold,
        server_time=utcnow(),
    )


@router.post("/feedback", response_model=FeedbackOut, summary="Record employee feedback")
async def submit_feedback(
    payload: FeedbackIn, session: SessionDep, principal: PrincipalDep
) -> FeedbackOut:
    from app.schemas.common import WorkspaceRef

    # Feedback is metadata about an already-archived conversation, so it uses
    # the stored workspace rather than re-verifying page markers.
    settings = get_settings()
    workspace_ids = settings.managed_workspace_id_list
    ctx = await ingest_service.build_context(
        session,
        organization=principal.organization,
        user=principal.user,
        device=principal.device,
        workspace_ref=WorkspaceRef(
            kind="managed_company",
            verified=True,
            label=settings.managed_workspace_label,
            source_workspace_id=workspace_ids[0] if workspace_ids else None,
            verification_signals=["workspace_label_match"],
        ),
    )
    feedback, duplicate = await ingest_service.ingest_feedback(session, ctx, payload)
    return FeedbackOut(feedback_id=feedback.id, status="duplicate" if duplicate else "accepted")


@router.get(
    "/sync/status",
    response_model=SyncStatusOut,
    summary="What this employee's archive currently contains",
)
async def sync_status(session: SessionDep, principal: PrincipalDep) -> SyncStatusOut:
    settings = get_settings()
    summary = await ingest_service.sync_summary(
        session, organization_id=principal.organization.id, user_id=principal.user.id
    )
    depth = await jobs_service.pending_job_count(session)
    return SyncStatusOut(
        server_time=utcnow(),
        device_id=principal.device.id if principal.device else None,
        last_sync_at=principal.device.last_sync_at if principal.device else None,
        archived_conversation_count=summary["archived_conversation_count"],
        archived_message_count=summary["archived_message_count"],
        known_conversation_ids=summary["known_conversation_ids"],
        queue_depth=depth,
        backpressure=depth >= settings.job_queue_backpressure_threshold,
        capture_enabled=settings.browser_capture_active,
        kill_switch=settings.kill_switch_enabled,
        coverage_statement=COVERAGE_STATEMENT,
    )
