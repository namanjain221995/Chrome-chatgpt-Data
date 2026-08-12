"""Ingestion: conversations, message versions, capture events, feedback.

Identity and idempotency
------------------------
1. Every client item carries an ``idempotency_key``. The first acceptance
   writes a row into ``idempotency_keys``; a replay short-circuits to
   ``duplicate`` and returns the original identifiers.
2. A message is identified by ``source_message_id`` when ChatGPT exposes one.
3. Otherwise a deterministic fingerprint (conversation, role, normalised
   content hash, sequence neighbourhood, timestamp bucket) is used.
4. As a final safety net, a message whose normalised content already exists in
   the conversation for the same role is treated as the *same* message rather
   than a duplicate row. This is what makes re-capture safe after a backfill
   shifts every sequence index.

Nothing is overwritten: edits and regenerations append a new
``message_versions`` row and move ``messages.current_version_id``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.crypto import (
    canonical_json_sha256,
    content_hash,
    message_fingerprint,
    pseudonymize,
    sha256_hex,
    utcnow,
)
from app.core.errors import PolicyError, ValidationError
from app.core.logging import get_logger
from app.core.sanitize import clean_plain_text, sanitize_html
from app.models.conversation import (
    Conversation,
    ConversationBranch,
    Message,
    MessagePart,
    MessageVersion,
)
from app.models.enums import (
    CaptureCompleteness,
    CaptureEventKind,
    CompletionStatus,
    IngestStatus,
    JobKind,
    MessageRole,
)
from app.models.events import CaptureEvent, IdempotencyKey
from app.models.governance import Feedback
from app.models.identity import Device, Organization, User, Workspace
from app.schemas.common import ClientContext, ItemResult, WorkspaceRef
from app.schemas.ingest import (
    CaptureEventIn,
    ConversationUpsertIn,
    FeedbackIn,
    MessageIn,
)
from app.services import jobs as jobs_service
from app.services.policy import assert_browser_capture_allowed, resolve_workspace
from app.services.storage import raw_event_key

logger = get_logger(__name__)

#: Rough token estimate used only for export sizing, never for billing.
CHARS_PER_TOKEN = 4


@dataclass
class IngestContext:
    """Everything the ingest functions need about the authenticated caller."""

    organization: Organization
    user: User
    device: Device | None
    workspace: Workspace
    workspace_hash: str
    settings: Settings = field(default_factory=get_settings)

    @property
    def device_id(self) -> uuid.UUID | None:
        return self.device.id if self.device else None


async def build_context(
    session: AsyncSession,
    *,
    organization: Organization,
    user: User,
    device: Device | None,
    workspace_ref: WorkspaceRef,
    settings: Settings | None = None,
) -> IngestContext:
    """Verify policy gates and the workspace before any write happens."""
    settings = settings or get_settings()
    assert_browser_capture_allowed(settings)
    decision = await resolve_workspace(
        session, organization=organization, ref=workspace_ref, settings=settings
    )
    return IngestContext(
        organization=organization,
        user=user,
        device=device,
        workspace=decision.workspace,
        workspace_hash=decision.workspace_hash,
        settings=settings,
    )


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


async def _claim_idempotency(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    key: str,
    capture_event_id: uuid.UUID,
    capture_event_created_at: datetime,
    result_ref: dict[str, Any],
) -> dict[str, Any] | None:
    """Claim an idempotency key.

    Returns ``None`` when the key was newly claimed, or the previously stored
    ``result_ref`` when this is a replay.
    """
    stmt = (
        pg_insert(IdempotencyKey)
        .values(
            organization_id=organization_id,
            idempotency_key=key,
            capture_event_id=capture_event_id,
            capture_event_created_at=capture_event_created_at,
            result_ref=result_ref,
        )
        .on_conflict_do_nothing(index_elements=["organization_id", "idempotency_key"])
        .returning(IdempotencyKey.idempotency_key)
    )
    inserted = (await session.execute(stmt)).first()
    if inserted is not None:
        return None
    existing = (
        await session.execute(
            select(IdempotencyKey.result_ref).where(
                IdempotencyKey.organization_id == organization_id,
                IdempotencyKey.idempotency_key == key,
            )
        )
    ).scalar_one_or_none()
    return dict(existing or {})


async def _update_idempotency_result(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    key: str,
    result_ref: dict[str, Any],
) -> None:
    await session.execute(
        update(IdempotencyKey)
        .where(
            IdempotencyKey.organization_id == organization_id,
            IdempotencyKey.idempotency_key == key,
        )
        .values(result_ref=result_ref)
    )


# ---------------------------------------------------------------------------
# Capture events (immutable raw record + durable archive job)
# ---------------------------------------------------------------------------


async def record_capture_event(
    session: AsyncSession,
    ctx: IngestContext,
    *,
    kind: CaptureEventKind,
    payload: dict[str, Any],
    idempotency_key: str,
    conversation_id: uuid.UUID | None = None,
    message_id: uuid.UUID | None = None,
    source_conversation_id: str | None = None,
    client_captured_at: datetime | None = None,
    adapter_version: str | None = None,
    extension_version: str | None = None,
    status: IngestStatus = IngestStatus.ACCEPTED,
    enqueue_archive: bool = True,
) -> CaptureEvent:
    """Persist the raw event and, in the same transaction, queue its S3 write."""
    now = utcnow()
    event_id = uuid.uuid4()
    payload_text = canonical_json_sha256(payload)
    event = CaptureEvent(
        id=event_id,
        created_at=now,
        organization_id=ctx.organization.id,
        workspace_id=ctx.workspace.id,
        user_id=ctx.user.id,
        device_id=ctx.device_id,
        conversation_id=conversation_id,
        message_id=message_id,
        kind=kind,
        idempotency_key=idempotency_key,
        status=status,
        schema_version="1.0",
        adapter_version=adapter_version,
        extension_version=extension_version,
        client_captured_at=client_captured_at,
        payload=payload,
        payload_sha256=payload_text,
        payload_bytes=len(str(payload)),
    )
    session.add(event)
    await session.flush()

    if enqueue_archive and status == IngestStatus.ACCEPTED:
        key = raw_event_key(
            workspace_hash=ctx.workspace_hash,
            conversation_id=source_conversation_id or str(conversation_id or "unknown"),
            event_id=str(event_id),
            when=now,
        )
        await jobs_service.enqueue_job(
            session,
            kind=JobKind.ARCHIVE_RAW_EVENT,
            organization_id=ctx.organization.id,
            priority=50,
            dedupe_key=f"archive_raw_event:{event_id}",
            payload={
                "capture_event_id": str(event_id),
                "capture_event_created_at": now.isoformat(),
                "s3_key": key,
            },
        )
    return event


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------


async def get_or_create_conversation(
    session: AsyncSession,
    ctx: IngestContext,
    *,
    source_conversation_id: str,
    title: str | None = None,
    source_url: str | None = None,
    model_slug: str | None = None,
    completeness: CaptureCompleteness | None = None,
    source_created_at: datetime | None = None,
    source_updated_at: datetime | None = None,
) -> tuple[Conversation, bool]:
    existing = (
        await session.execute(
            select(Conversation).where(
                Conversation.organization_id == ctx.organization.id,
                Conversation.workspace_id == ctx.workspace.id,
                Conversation.source_conversation_id == source_conversation_id,
            )
        )
    ).scalar_one_or_none()

    now = utcnow()
    if existing is not None:
        if title:
            existing.title = clean_plain_text(title, max_length=2048)
        if source_url:
            existing.source_url = source_url[:2048]
        if model_slug:
            existing.model_slug = model_slug
        if source_updated_at:
            existing.source_updated_at = source_updated_at
        if completeness is not None:
            existing.capture_completeness = _merge_completeness(
                existing.capture_completeness, completeness
            )
        if existing.user_id is None:
            existing.user_id = ctx.user.id
        existing.last_captured_at = now
        _add_capture_source(existing, "chrome_extension")
        return existing, False

    conversation = Conversation(
        id=uuid.uuid4(),
        organization_id=ctx.organization.id,
        workspace_id=ctx.workspace.id,
        user_id=ctx.user.id,
        source_conversation_id=source_conversation_id,
        source_url=source_url[:2048] if source_url else None,
        title=clean_plain_text(title, max_length=2048) if title else None,
        model_slug=model_slug,
        capture_sources=["chrome_extension"],
        # Merge against UNKNOWN so a browser claim of `compliance_verified`
        # is dropped on the create path as well as on update.
        capture_completeness=_merge_completeness(
            CaptureCompleteness.UNKNOWN, completeness or CaptureCompleteness.UNKNOWN
        ),
        employee_id_hash=pseudonymize(ctx.user.email),
        source_created_at=source_created_at,
        source_updated_at=source_updated_at,
        first_captured_at=now,
        last_captured_at=now,
        snapshot_stale=True,
    )
    session.add(conversation)
    try:
        await session.flush()
    except IntegrityError:
        # Concurrent insert from another device: fall back to the winner.
        await session.rollback()
        raise
    return conversation, True


def _add_capture_source(conversation: Conversation, source: str) -> None:
    sources = list(conversation.capture_sources or [])
    if source not in sources:
        sources.append(source)
        conversation.capture_sources = sources


#: Ranked from weakest to strongest claim about archive completeness.
_COMPLETENESS_RANK = {
    CaptureCompleteness.UNKNOWN: 0,
    CaptureCompleteness.LIVE_ONLY: 1,
    CaptureCompleteness.PARTIAL_SCROLL_LIMIT: 2,
    CaptureCompleteness.COMPLETE_CURRENT_PAGE: 3,
    CaptureCompleteness.RECONCILED: 4,
    CaptureCompleteness.COMPLIANCE_VERIFIED: 5,
}


def _merge_completeness(
    current: CaptureCompleteness, incoming: CaptureCompleteness
) -> CaptureCompleteness:
    """Never downgrade, and never let the browser claim compliance coverage."""
    if incoming == CaptureCompleteness.COMPLIANCE_VERIFIED:
        # Only the compliance importer sets this; browser input cannot.
        return current
    if _COMPLETENESS_RANK.get(incoming, 0) > _COMPLETENESS_RANK.get(current, 0):
        return incoming
    return current


async def upsert_conversation(
    session: AsyncSession, ctx: IngestContext, payload: ConversationUpsertIn
) -> tuple[Conversation, bool, bool]:
    """Returns ``(conversation, created, duplicate)``."""
    replay = await _claim_idempotency(
        session,
        organization_id=ctx.organization.id,
        key=payload.idempotency_key,
        capture_event_id=uuid.uuid4(),
        capture_event_created_at=utcnow(),
        result_ref={},
    )
    conversation, created = await get_or_create_conversation(
        session,
        ctx,
        source_conversation_id=payload.source_conversation_id,
        title=payload.title,
        source_url=payload.source_url,
        model_slug=payload.model_slug,
        completeness=CaptureCompleteness(payload.capture_completeness),
        source_created_at=payload.source_created_at,
        source_updated_at=payload.source_updated_at,
    )
    if replay is not None:
        return conversation, False, True

    await _update_idempotency_result(
        session,
        organization_id=ctx.organization.id,
        key=payload.idempotency_key,
        result_ref={"conversation_id": str(conversation.id)},
    )
    await record_capture_event(
        session,
        ctx,
        kind=CaptureEventKind.CONVERSATION_UPSERT,
        payload=payload.model_dump(mode="json"),
        idempotency_key=payload.idempotency_key,
        conversation_id=conversation.id,
        source_conversation_id=payload.source_conversation_id,
        client_captured_at=payload.client.captured_at,
        adapter_version=payload.client.adapter_version,
        extension_version=payload.client.extension_version,
    )
    await mark_snapshot_stale(session, conversation, ctx)
    return conversation, created, False


async def mark_snapshot_stale(
    session: AsyncSession, conversation: Conversation, ctx: IngestContext
) -> None:
    conversation.snapshot_stale = True
    await jobs_service.enqueue_job(
        session,
        kind=JobKind.BUILD_CONVERSATION_SNAPSHOT,
        organization_id=ctx.organization.id,
        priority=200,
        # One live snapshot job per conversation; further edits coalesce into it.
        dedupe_key=f"snapshot:{conversation.id}",
        payload={
            "conversation_id": str(conversation.id),
            "workspace_hash": ctx.workspace_hash,
        },
    )


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


async def _find_message(
    session: AsyncSession,
    *,
    conversation: Conversation,
    payload: MessageIn,
    fingerprint: str,
    normalized_hash: str,
) -> Message | None:
    if payload.source_message_id:
        found = (
            await session.execute(
                select(Message).where(
                    Message.conversation_id == conversation.id,
                    Message.source_message_id == payload.source_message_id,
                )
            )
        ).scalar_one_or_none()
        if found is not None:
            return found

    found = (
        await session.execute(
            select(Message).where(
                Message.conversation_id == conversation.id,
                Message.fingerprint == fingerprint,
            )
        )
    ).scalar_one_or_none()
    if found is not None:
        return found

    # Content-identity fallback (see module docstring): identical normalised
    # content for the same role in the same conversation is the same message.
    stmt = (
        select(Message)
        .join(MessageVersion, MessageVersion.message_id == Message.id)
        .where(
            Message.conversation_id == conversation.id,
            Message.role == payload.role,
            MessageVersion.normalized_sha256 == normalized_hash,
        )
        .order_by(Message.sequence_index.asc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalars().first()


async def _resolve_branch(
    session: AsyncSession, *, conversation: Conversation, payload: MessageIn
) -> ConversationBranch | None:
    if not payload.branch_key:
        return None
    existing = (
        await session.execute(
            select(ConversationBranch).where(
                ConversationBranch.conversation_id == conversation.id,
                ConversationBranch.branch_key == payload.branch_key,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.is_selected = payload.branch_selected
        return existing
    branch = ConversationBranch(
        id=uuid.uuid4(),
        conversation_id=conversation.id,
        branch_key=payload.branch_key,
        source_branch_id=payload.branch_key,
        is_selected=payload.branch_selected,
        detected_at=utcnow(),
    )
    session.add(branch)
    await session.flush()
    return branch


async def ingest_message(
    session: AsyncSession,
    ctx: IngestContext,
    payload: MessageIn,
    *,
    index: int,
    client: ClientContext | None = None,
) -> ItemResult:
    """Ingest one message version idempotently.

    ``client`` carries the batch-level provenance (extension and DOM adapter
    versions); it is recorded on the capture event so a parsing regression can
    be traced back to the adapter build that produced it.
    """
    text = clean_plain_text(payload.text)
    declared = payload.content_sha256
    actual = sha256_hex(payload.text)
    if declared != actual:
        return ItemResult(
            index=index,
            idempotency_key=payload.idempotency_key,
            status="rejected",
            reason_code="checksum_mismatch",
            reason="content_sha256 does not match the supplied text",
        )

    conversation, _ = await get_or_create_conversation(
        session,
        ctx,
        source_conversation_id=payload.source_conversation_id,
        completeness=CaptureCompleteness.LIVE_ONLY,
    )

    normalized_hash = content_hash(payload.text)
    fingerprint = message_fingerprint(
        conversation_id=conversation.id,
        role=payload.role,
        text=payload.text,
        sequence_index=payload.sequence_index,
        created_at=payload.source_created_at,
    )

    replay = await _claim_idempotency(
        session,
        organization_id=ctx.organization.id,
        key=payload.idempotency_key,
        capture_event_id=uuid.uuid4(),
        capture_event_created_at=utcnow(),
        result_ref={"conversation_id": str(conversation.id)},
    )
    if replay is not None:
        return ItemResult(
            index=index,
            idempotency_key=payload.idempotency_key,
            status="duplicate",
            conversation_id=conversation.id,
            message_id=_maybe_uuid(replay.get("message_id")),
            message_version_id=_maybe_uuid(replay.get("message_version_id")),
            reason_code="idempotent_replay",
        )

    message = await _find_message(
        session,
        conversation=conversation,
        payload=payload,
        fingerprint=fingerprint,
        normalized_hash=normalized_hash,
    )
    branch = await _resolve_branch(session, conversation=conversation, payload=payload)

    now = utcnow()
    if message is None:
        message = Message(
            id=uuid.uuid4(),
            conversation_id=conversation.id,
            organization_id=ctx.organization.id,
            branch_id=branch.id if branch else None,
            source_message_id=payload.source_message_id,
            fingerprint=fingerprint,
            role=MessageRole(payload.role),
            author_name=payload.author_name,
            sequence_index=payload.sequence_index,
            source_created_at=payload.source_created_at,
            completion_status=CompletionStatus(payload.completion_status),
            version_count=0,
            has_attachments=bool(payload.attachment_client_ids),
        )
        session.add(message)
        await session.flush()
    else:
        # Late-arriving authoritative data may improve what we know.
        if payload.source_message_id and not message.source_message_id:
            message.source_message_id = payload.source_message_id
        if branch is not None and message.branch_id is None:
            message.branch_id = branch.id
        if payload.source_created_at and not message.source_created_at:
            message.source_created_at = payload.source_created_at
        # A backfill establishes true ordering; live capture ordering is a hint.
        if payload.sequence_index != message.sequence_index:
            message.sequence_index = payload.sequence_index
        if payload.attachment_client_ids:
            message.has_attachments = True

    existing_version = (
        await session.execute(
            select(MessageVersion).where(
                MessageVersion.message_id == message.id,
                MessageVersion.normalized_sha256 == normalized_hash,
            )
        )
    ).scalar_one_or_none()

    if existing_version is not None:
        reconciled = False
        if (
            existing_version.completion_status == CompletionStatus.PARTIAL
            and payload.completion_status == "complete"
        ):
            # Partial -> complete for identical content: promote in place and
            # record that the promotion happened (nothing is lost).
            existing_version.completion_status = CompletionStatus.RECONCILED
            existing_version.metadata_json = {
                **(existing_version.metadata_json or {}),
                "reconciled_at": now.isoformat(),
                "reconciled_from": "partial",
            }
            message.completion_status = CompletionStatus.COMPLETE
            reconciled = True
        await _update_idempotency_result(
            session,
            organization_id=ctx.organization.id,
            key=payload.idempotency_key,
            result_ref={
                "conversation_id": str(conversation.id),
                "message_id": str(message.id),
                "message_version_id": str(existing_version.id),
            },
        )
        if reconciled:
            await mark_snapshot_stale(session, conversation, ctx)
        return ItemResult(
            index=index,
            idempotency_key=payload.idempotency_key,
            status="duplicate",
            conversation_id=conversation.id,
            message_id=message.id,
            message_version_id=existing_version.id,
            reason_code="reconciled_partial" if reconciled else "identical_content",
        )

    version_number = message.version_count + 1
    version = MessageVersion(
        id=uuid.uuid4(),
        message_id=message.id,
        conversation_id=conversation.id,
        organization_id=ctx.organization.id,
        version_number=version_number,
        is_edit=payload.is_edit or (version_number > 1 and payload.role == "user"),
        is_regeneration=payload.is_regeneration
        or (version_number > 1 and payload.role == "assistant"),
        completion_status=CompletionStatus(payload.completion_status),
        plain_text=text,
        sanitized_html=sanitize_html(payload.sanitized_html),
        content_sha256=actual,
        normalized_sha256=normalized_hash,
        char_count=len(payload.text),
        token_estimate=max(1, len(payload.text) // CHARS_PER_TOKEN),
        captured_at=now,
        source_created_at=payload.source_created_at,
        capture_source="chrome_extension",
        device_id=ctx.device_id,
        citations=[c.model_dump(mode="json") for c in payload.citations],
        metadata_json={"branch_selected": payload.branch_selected},
    )
    session.add(version)
    await session.flush()

    for part in payload.parts[:500]:
        session.add(
            MessagePart(
                id=uuid.uuid4(),
                message_version_id=version.id,
                conversation_id=conversation.id,
                part_index=part.index,
                kind=part.kind,
                language=part.language,
                text_content=clean_plain_text(part.text) if part.text else None,
                structured=part.structured,
                byte_size=len(part.text or ""),
            )
        )

    message.current_version_id = version.id
    message.version_count = version_number
    if payload.completion_status == "complete":
        message.completion_status = CompletionStatus.COMPLETE
    elif message.completion_status != CompletionStatus.COMPLETE:
        message.completion_status = CompletionStatus(payload.completion_status)

    if version_number == 1:
        conversation.message_count = (conversation.message_count or 0) + 1
    conversation.last_captured_at = now

    event = await record_capture_event(
        session,
        ctx,
        kind=CaptureEventKind.MESSAGE_VERSION,
        payload=payload.model_dump(mode="json"),
        idempotency_key=payload.idempotency_key,
        conversation_id=conversation.id,
        message_id=message.id,
        source_conversation_id=payload.source_conversation_id,
        client_captured_at=client.captured_at if client else None,
        adapter_version=client.adapter_version if client else None,
        extension_version=client.extension_version if client else None,
    )
    version.capture_event_id = event.id

    await _update_idempotency_result(
        session,
        organization_id=ctx.organization.id,
        key=payload.idempotency_key,
        result_ref={
            "conversation_id": str(conversation.id),
            "message_id": str(message.id),
            "message_version_id": str(version.id),
        },
    )
    await mark_snapshot_stale(session, conversation, ctx)

    if payload.completion_status == "partial":
        # Ask the worker to look for a completed version later.
        await jobs_service.enqueue_job(
            session,
            kind=JobKind.RECONCILE_PARTIAL,
            organization_id=ctx.organization.id,
            priority=400,
            dedupe_key=f"reconcile:{message.id}",
            payload={"message_id": str(message.id)},
        )

    return ItemResult(
        index=index,
        idempotency_key=payload.idempotency_key,
        status="accepted",
        conversation_id=conversation.id,
        message_id=message.id,
        message_version_id=version.id,
    )


def _maybe_uuid(value: Any) -> uuid.UUID | None:
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError):
        return None


async def ingest_message_batch(
    session: AsyncSession,
    ctx: IngestContext,
    messages: list[MessageIn],
    client: ClientContext | None = None,
) -> list[ItemResult]:
    """Ingest a batch, isolating per-item failures with SAVEPOINTs."""
    results: list[ItemResult] = []
    for index, payload in enumerate(messages):
        savepoint = await session.begin_nested()
        try:
            result = await ingest_message(session, ctx, payload, index=index, client=client)
            await savepoint.commit()
        except PolicyError:
            await savepoint.rollback()
            raise
        except IntegrityError as exc:
            await savepoint.rollback()
            logger.warning("message_integrity_conflict", index=index, error=str(exc.orig)[:200])
            result = ItemResult(
                index=index,
                idempotency_key=payload.idempotency_key,
                status="retryable",
                reason_code="write_conflict",
                reason="Concurrent write conflict; retry this item",
            )
        except ValidationError as exc:
            await savepoint.rollback()
            result = ItemResult(
                index=index,
                idempotency_key=payload.idempotency_key,
                status="rejected",
                reason_code=exc.code,
                reason=exc.message,
            )
        except Exception as exc:
            await savepoint.rollback()
            logger.exception("message_ingest_failed", index=index, error_type=type(exc).__name__)
            result = ItemResult(
                index=index,
                idempotency_key=payload.idempotency_key,
                status="retryable",
                reason_code="internal_error",
                reason="Temporary failure; retry this item",
            )
        results.append(result)
    return results


# ---------------------------------------------------------------------------
# Generic capture events and feedback
# ---------------------------------------------------------------------------


async def ingest_capture_events(
    session: AsyncSession, ctx: IngestContext, events: list[CaptureEventIn], client_meta: Any
) -> list[ItemResult]:
    results: list[ItemResult] = []
    for index, item in enumerate(events):
        savepoint = await session.begin_nested()
        try:
            conversation_id: uuid.UUID | None = None
            if item.source_conversation_id:
                conversation, _ = await get_or_create_conversation(
                    session,
                    ctx,
                    source_conversation_id=item.source_conversation_id,
                    completeness=CaptureCompleteness.LIVE_ONLY,
                )
                conversation_id = conversation.id

            replay = await _claim_idempotency(
                session,
                organization_id=ctx.organization.id,
                key=item.idempotency_key,
                capture_event_id=uuid.uuid4(),
                capture_event_created_at=utcnow(),
                result_ref={},
            )
            if replay is not None:
                await savepoint.commit()
                results.append(
                    ItemResult(
                        index=index,
                        idempotency_key=item.idempotency_key,
                        status="duplicate",
                        reason_code="idempotent_replay",
                    )
                )
                continue

            event = await record_capture_event(
                session,
                ctx,
                kind=CaptureEventKind(item.kind),
                payload=item.model_dump(mode="json"),
                idempotency_key=item.idempotency_key,
                conversation_id=conversation_id,
                source_conversation_id=item.source_conversation_id,
                client_captured_at=item.occurred_at,
                adapter_version=getattr(client_meta, "adapter_version", None),
                extension_version=getattr(client_meta, "extension_version", None),
            )
            await _update_idempotency_result(
                session,
                organization_id=ctx.organization.id,
                key=item.idempotency_key,
                result_ref={"capture_event_id": str(event.id)},
            )
            await savepoint.commit()
            results.append(
                ItemResult(
                    index=index,
                    idempotency_key=item.idempotency_key,
                    status="accepted",
                    id=event.id,
                    conversation_id=conversation_id,
                )
            )
        except Exception as exc:
            await savepoint.rollback()
            logger.exception("capture_event_failed", index=index, error_type=type(exc).__name__)
            results.append(
                ItemResult(
                    index=index,
                    idempotency_key=item.idempotency_key,
                    status="retryable",
                    reason_code="internal_error",
                )
            )
    return results


async def ingest_feedback(
    session: AsyncSession, ctx: IngestContext, payload: FeedbackIn
) -> tuple[Feedback, bool]:
    conversation, _ = await get_or_create_conversation(
        session, ctx, source_conversation_id=payload.source_conversation_id
    )
    existing = (
        await session.execute(
            select(Feedback).where(
                Feedback.organization_id == ctx.organization.id,
                Feedback.client_feedback_id == payload.client_feedback_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing, True

    message_id: uuid.UUID | None = None
    if payload.source_message_id:
        message_id = (
            await session.execute(
                select(Message.id).where(
                    Message.conversation_id == conversation.id,
                    Message.source_message_id == payload.source_message_id,
                )
            )
        ).scalar_one_or_none()

    feedback = Feedback(
        id=uuid.uuid4(),
        organization_id=ctx.organization.id,
        conversation_id=conversation.id,
        message_id=message_id,
        user_id=ctx.user.id,
        kind=payload.kind,
        rating=payload.rating,
        note=clean_plain_text(payload.note, max_length=4000) if payload.note else None,
        client_feedback_id=payload.client_feedback_id,
    )
    session.add(feedback)
    await session.flush()
    return feedback, False


# ---------------------------------------------------------------------------
# Read helpers used by /sync/status
# ---------------------------------------------------------------------------


async def sync_summary(
    session: AsyncSession, *, organization_id: uuid.UUID, user_id: uuid.UUID, limit: int = 500
) -> dict[str, Any]:
    conversation_rows = (
        (
            await session.execute(
                select(Conversation.source_conversation_id)
                .where(
                    Conversation.organization_id == organization_id,
                    Conversation.user_id == user_id,
                    Conversation.deleted_at.is_(None),
                )
                .order_by(Conversation.last_captured_at.desc().nullslast())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )

    conversation_count = (
        await session.execute(
            select(func.count())
            .select_from(Conversation)
            .where(
                Conversation.organization_id == organization_id,
                Conversation.user_id == user_id,
                Conversation.deleted_at.is_(None),
            )
        )
    ).scalar_one()

    message_count = (
        await session.execute(
            select(func.coalesce(func.sum(Conversation.message_count), 0)).where(
                Conversation.organization_id == organization_id,
                Conversation.user_id == user_id,
                Conversation.deleted_at.is_(None),
            )
        )
    ).scalar_one()

    return {
        "known_conversation_ids": list(conversation_rows),
        "archived_conversation_count": int(conversation_count or 0),
        "archived_message_count": int(message_count or 0),
    }
