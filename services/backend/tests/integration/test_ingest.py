"""Ingestion against a real PostgreSQL: identity, idempotency, versioning."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.core.crypto import sha256_hex
from app.core.errors import PolicyError
from app.models.conversation import Conversation, Message, MessagePart, MessageVersion
from app.models.enums import CaptureCompleteness, CompletionStatus, JobKind
from app.models.events import CaptureEvent, IdempotencyKey
from app.models.jobs import Job
from app.schemas.ingest import ConversationUpsertIn, MessageIn
from app.services import ingest as ingest_service
from tests.conftest import make_client_context, managed_workspace_ref, new_idempotency_key

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

BASE_TIME = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)


def msg(
    *,
    text: str,
    role: str = "user",
    sequence_index: int = 0,
    source_message_id: str | None = None,
    conversation: str = "conv-integration-1",
    key: str | None = None,
    **overrides: object,
) -> MessageIn:
    payload: dict = {
        "idempotency_key": key or new_idempotency_key("msg"),
        "source_conversation_id": conversation,
        "source_message_id": source_message_id,
        "role": role,
        "sequence_index": sequence_index,
        "text": text,
        "content_sha256": sha256_hex(text),
    }
    payload.update(overrides)
    return MessageIn(**payload)


class TestConversationUpsert:
    async def test_upsert_creates_and_is_idempotent(self, db_session, ingest_ctx) -> None:
        key = new_idempotency_key("conv")
        payload = ConversationUpsertIn(
            idempotency_key=key,
            source_conversation_id="conv-upsert-1",
            title="Quarterly plan",
            workspace=managed_workspace_ref(),
            capture_completeness="complete_current_page",
            client=make_client_context(),
        )
        conversation, created, duplicate = await ingest_service.upsert_conversation(
            db_session, ingest_ctx, payload
        )
        assert created is True
        assert duplicate is False
        assert conversation.title == "Quarterly plan"

        again, created2, duplicate2 = await ingest_service.upsert_conversation(
            db_session, ingest_ctx, payload
        )
        assert again.id == conversation.id
        assert created2 is False
        assert duplicate2 is True

    async def test_completeness_never_downgrades(self, db_session, ingest_ctx) -> None:
        complete = ConversationUpsertIn(
            idempotency_key=new_idempotency_key("conv"),
            source_conversation_id="conv-completeness",
            workspace=managed_workspace_ref(),
            capture_completeness="complete_current_page",
            client=make_client_context(),
        )
        conversation, _, _ = await ingest_service.upsert_conversation(
            db_session, ingest_ctx, complete
        )
        assert conversation.capture_completeness == CaptureCompleteness.COMPLETE_CURRENT_PAGE

        weaker = ConversationUpsertIn(
            idempotency_key=new_idempotency_key("conv"),
            source_conversation_id="conv-completeness",
            workspace=managed_workspace_ref(),
            capture_completeness="live_only",
            client=make_client_context(),
        )
        conversation2, _, _ = await ingest_service.upsert_conversation(
            db_session, ingest_ctx, weaker
        )
        assert conversation2.capture_completeness == CaptureCompleteness.COMPLETE_CURRENT_PAGE

    async def test_browser_can_never_claim_compliance_verified(
        self, db_session, ingest_ctx
    ) -> None:
        payload = ConversationUpsertIn(
            idempotency_key=new_idempotency_key("conv"),
            source_conversation_id="conv-overclaim",
            workspace=managed_workspace_ref(),
            capture_completeness="compliance_verified",
            client=make_client_context(),
        )
        conversation, _, _ = await ingest_service.upsert_conversation(
            db_session, ingest_ctx, payload
        )
        assert conversation.capture_completeness != CaptureCompleteness.COMPLIANCE_VERIFIED

    async def test_upsert_enqueues_a_snapshot_job(self, db_session, ingest_ctx) -> None:
        payload = ConversationUpsertIn(
            idempotency_key=new_idempotency_key("conv"),
            source_conversation_id="conv-jobs",
            workspace=managed_workspace_ref(),
            client=make_client_context(),
        )
        conversation, _, _ = await ingest_service.upsert_conversation(
            db_session, ingest_ctx, payload
        )
        jobs = (
            (
                await db_session.execute(
                    select(Job).where(Job.dedupe_key == f"snapshot:{conversation.id}")
                )
            )
            .scalars()
            .all()
        )
        assert len(jobs) == 1
        assert jobs[0].kind == JobKind.BUILD_CONVERSATION_SNAPSHOT


class TestMessageIngestion:
    async def test_message_creates_version_and_capture_event(self, db_session, ingest_ctx) -> None:
        result = await ingest_service.ingest_message(
            db_session, ingest_ctx, msg(text="What is our travel policy?"), index=0
        )
        assert result.status == "accepted"
        assert result.message_version_id is not None

        version = await db_session.get(MessageVersion, result.message_version_id)
        assert version is not None
        assert version.version_number == 1
        assert version.plain_text == "What is our travel policy?"
        assert version.content_sha256 == sha256_hex("What is our travel policy?")

        events = (
            (
                await db_session.execute(
                    select(CaptureEvent).where(CaptureEvent.message_id == result.message_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        assert events[0].payload["text"] == "What is our travel policy?"

    async def test_archive_job_is_enqueued_in_the_same_transaction(
        self, db_session, ingest_ctx
    ) -> None:
        result = await ingest_service.ingest_message(
            db_session, ingest_ctx, msg(text="archive me"), index=0
        )
        archive_jobs = (
            (
                await db_session.execute(
                    select(Job).where(Job.kind == JobKind.ARCHIVE_RAW_EVENT.value)
                )
            )
            .scalars()
            .all()
        )
        assert archive_jobs, "an archive job must exist alongside the capture event"
        assert result.status == "accepted"

    async def test_replayed_idempotency_key_is_a_duplicate(self, db_session, ingest_ctx) -> None:
        key = new_idempotency_key("msg")
        first = await ingest_service.ingest_message(
            db_session, ingest_ctx, msg(text="only once", key=key), index=0
        )
        second = await ingest_service.ingest_message(
            db_session, ingest_ctx, msg(text="only once", key=key), index=0
        )
        assert first.status == "accepted"
        assert second.status == "duplicate"
        assert second.message_id == first.message_id
        assert second.message_version_id == first.message_version_id

        rows = (
            await db_session.execute(
                select(func.count())
                .select_from(IdempotencyKey)
                .where(IdempotencyKey.idempotency_key == key)
            )
        ).scalar_one()
        assert rows == 1

    async def test_identical_content_with_new_key_does_not_duplicate_the_message(
        self, db_session, ingest_ctx
    ) -> None:
        await ingest_service.ingest_message(db_session, ingest_ctx, msg(text="same"), index=0)
        second = await ingest_service.ingest_message(
            db_session, ingest_ctx, msg(text="same"), index=0
        )
        assert second.status == "duplicate"
        assert second.reason_code == "identical_content"

        count = (
            await db_session.execute(
                select(func.count())
                .select_from(Message)
                .where(Message.conversation_id == second.conversation_id)
            )
        ).scalar_one()
        assert count == 1

    async def test_checksum_mismatch_is_rejected(self, db_session, ingest_ctx) -> None:
        payload = msg(text="honest text")
        tampered = payload.model_copy(update={"text": "tampered text"})
        result = await ingest_service.ingest_message(db_session, ingest_ctx, tampered, index=0)
        assert result.status == "rejected"
        assert result.reason_code == "checksum_mismatch"

    async def test_source_message_id_is_the_primary_identity(self, db_session, ingest_ctx) -> None:
        """Re-capture with a different sequence index maps to the same message."""
        first = await ingest_service.ingest_message(
            db_session,
            ingest_ctx,
            msg(text="original", source_message_id="src-1", sequence_index=2),
            index=0,
        )
        second = await ingest_service.ingest_message(
            db_session,
            ingest_ctx,
            msg(text="edited later", source_message_id="src-1", sequence_index=40),
            index=0,
        )
        assert second.message_id == first.message_id
        assert second.message_version_id != first.message_version_id

    async def test_backfill_shift_does_not_duplicate_messages(self, db_session, ingest_ctx) -> None:
        """A later backfill renumbers positions; content identity keeps them one row."""
        live = await ingest_service.ingest_message(
            db_session, ingest_ctx, msg(text="hello there", sequence_index=0), index=0
        )
        backfilled = await ingest_service.ingest_message(
            db_session, ingest_ctx, msg(text="hello there", sequence_index=84), index=0
        )
        assert backfilled.message_id == live.message_id

        count = (
            await db_session.execute(
                select(func.count())
                .select_from(Message)
                .where(Message.conversation_id == live.conversation_id)
            )
        ).scalar_one()
        assert count == 1

    async def test_out_of_order_messages_keep_their_sequence(self, db_session, ingest_ctx) -> None:
        later = await ingest_service.ingest_message(
            db_session, ingest_ctx, msg(text="second turn", sequence_index=1), index=0
        )
        earlier = await ingest_service.ingest_message(
            db_session, ingest_ctx, msg(text="first turn", sequence_index=0), index=1
        )
        rows = (
            (
                await db_session.execute(
                    select(Message)
                    .where(Message.conversation_id == later.conversation_id)
                    .order_by(Message.sequence_index)
                )
            )
            .scalars()
            .all()
        )
        assert [m.sequence_index for m in rows] == [0, 1]
        assert rows[0].id == earlier.message_id

    async def test_structured_parts_are_persisted(self, db_session, ingest_ctx) -> None:
        payload = msg(
            text="Here is code",
            role="assistant",
            parts=[
                {"index": 0, "kind": "text", "text": "Here is code"},
                {"index": 1, "kind": "code", "language": "python", "text": "print('hi')"},
                {
                    "index": 2,
                    "kind": "table",
                    "structured": {"headers": ["a"], "rows": [["1"]]},
                },
            ],
        )
        result = await ingest_service.ingest_message(db_session, ingest_ctx, payload, index=0)
        parts = (
            (
                await db_session.execute(
                    select(MessagePart)
                    .where(MessagePart.message_version_id == result.message_version_id)
                    .order_by(MessagePart.part_index)
                )
            )
            .scalars()
            .all()
        )
        assert [p.kind.value for p in parts] == ["text", "code", "table"]
        assert parts[1].language == "python"
        assert parts[2].structured["headers"] == ["a"]

    async def test_html_is_sanitised_server_side(self, db_session, ingest_ctx) -> None:
        payload = msg(
            text="hello",
            role="assistant",
            sanitized_html="<p>hello</p><script>fetch('https://evil.example')</script>",
        )
        result = await ingest_service.ingest_message(db_session, ingest_ctx, payload, index=0)
        version = await db_session.get(MessageVersion, result.message_version_id)
        assert version is not None
        assert "<script" not in (version.sanitized_html or "")
        assert "<p>hello</p>" in (version.sanitized_html or "")


class TestVersioning:
    async def test_edited_prompt_creates_a_new_version(self, db_session, ingest_ctx) -> None:
        first = await ingest_service.ingest_message(
            db_session,
            ingest_ctx,
            msg(text="draft question", source_message_id="src-edit", role="user"),
            index=0,
        )
        second = await ingest_service.ingest_message(
            db_session,
            ingest_ctx,
            msg(
                text="improved question",
                source_message_id="src-edit",
                role="user",
                is_edit=True,
            ),
            index=0,
        )
        assert second.message_id == first.message_id

        message = await db_session.get(Message, first.message_id)
        assert message is not None
        assert message.version_count == 2
        assert message.current_version_id == second.message_version_id

        versions = (
            (
                await db_session.execute(
                    select(MessageVersion)
                    .where(MessageVersion.message_id == message.id)
                    .order_by(MessageVersion.version_number)
                )
            )
            .scalars()
            .all()
        )
        # Nothing is overwritten: the original text is still recoverable.
        assert [v.plain_text for v in versions] == ["draft question", "improved question"]
        assert versions[1].is_edit is True

    async def test_regenerated_answer_creates_a_new_version(self, db_session, ingest_ctx) -> None:
        first = await ingest_service.ingest_message(
            db_session,
            ingest_ctx,
            msg(text="answer v1", source_message_id="src-regen", role="assistant"),
            index=0,
        )
        second = await ingest_service.ingest_message(
            db_session,
            ingest_ctx,
            msg(
                text="answer v2",
                source_message_id="src-regen",
                role="assistant",
                is_regeneration=True,
            ),
            index=0,
        )
        message = await db_session.get(Message, first.message_id)
        assert message is not None
        assert message.version_count == 2
        version = await db_session.get(MessageVersion, second.message_version_id)
        assert version is not None
        assert version.is_regeneration is True

    async def test_conversation_message_count_counts_messages_not_versions(
        self, db_session, ingest_ctx
    ) -> None:
        result = await ingest_service.ingest_message(
            db_session,
            ingest_ctx,
            msg(text="v1", source_message_id="src-count", role="assistant"),
            index=0,
        )
        await ingest_service.ingest_message(
            db_session,
            ingest_ctx,
            msg(text="v2", source_message_id="src-count", role="assistant"),
            index=0,
        )
        conversation = await db_session.get(Conversation, result.conversation_id)
        assert conversation is not None
        assert conversation.message_count == 1


class TestPartialReconciliation:
    async def test_partial_answer_is_recorded_and_queued_for_reconciliation(
        self, db_session, ingest_ctx
    ) -> None:
        result = await ingest_service.ingest_message(
            db_session,
            ingest_ctx,
            msg(
                text="The first half of the ans",
                role="assistant",
                source_message_id="src-partial",
                completion_status="partial",
            ),
            index=0,
        )
        message = await db_session.get(Message, result.message_id)
        assert message is not None
        assert message.completion_status == CompletionStatus.PARTIAL

        jobs = (
            (
                await db_session.execute(
                    select(Job).where(Job.dedupe_key == f"reconcile:{message.id}")
                )
            )
            .scalars()
            .all()
        )
        assert len(jobs) == 1

    async def test_completed_answer_supersedes_the_partial_version(
        self, db_session, ingest_ctx
    ) -> None:
        partial = await ingest_service.ingest_message(
            db_session,
            ingest_ctx,
            msg(
                text="The first half of the ans",
                role="assistant",
                source_message_id="src-partial-2",
                completion_status="partial",
            ),
            index=0,
        )
        complete = await ingest_service.ingest_message(
            db_session,
            ingest_ctx,
            msg(
                text="The first half of the answer, and the rest of it.",
                role="assistant",
                source_message_id="src-partial-2",
                completion_status="complete",
            ),
            index=0,
        )
        assert complete.message_id == partial.message_id
        message = await db_session.get(Message, partial.message_id)
        assert message is not None
        assert message.completion_status == CompletionStatus.COMPLETE
        assert message.current_version_id == complete.message_version_id
        # The truncated version is retained for audit.
        first_version = await db_session.get(MessageVersion, partial.message_version_id)
        assert first_version is not None
        assert first_version.plain_text == "The first half of the ans"

    async def test_identical_partial_then_complete_is_promoted_in_place(
        self, db_session, ingest_ctx
    ) -> None:
        text = "A short but complete answer."
        partial = await ingest_service.ingest_message(
            db_session,
            ingest_ctx,
            msg(
                text=text,
                role="assistant",
                source_message_id="src-partial-3",
                completion_status="partial",
            ),
            index=0,
        )
        promoted = await ingest_service.ingest_message(
            db_session,
            ingest_ctx,
            msg(
                text=text,
                role="assistant",
                source_message_id="src-partial-3",
                completion_status="complete",
            ),
            index=0,
        )
        assert promoted.status == "duplicate"
        assert promoted.reason_code == "reconciled_partial"
        message = await db_session.get(Message, partial.message_id)
        assert message is not None
        assert message.completion_status == CompletionStatus.COMPLETE


class TestBatchBehaviour:
    async def test_batch_isolates_a_bad_item(self, db_session, ingest_ctx) -> None:
        good = msg(text="good one")
        bad = msg(text="declared").model_copy(update={"text": "actual"})
        results = await ingest_service.ingest_message_batch(
            db_session, ingest_ctx, [good, bad, msg(text="good two")]
        )
        statuses = [r.status for r in results]
        assert statuses == ["accepted", "rejected", "accepted"]

    async def test_batch_of_a_full_conversation(self, db_session, ingest_ctx) -> None:
        messages = []
        for index in range(20):
            role = "user" if index % 2 == 0 else "assistant"
            messages.append(
                msg(
                    text=f"turn number {index}",
                    role=role,
                    sequence_index=index,
                    source_message_id=f"src-batch-{index}",
                    source_created_at=BASE_TIME + timedelta(minutes=index),
                )
            )
        results = await ingest_service.ingest_message_batch(db_session, ingest_ctx, messages)
        assert all(r.status == "accepted" for r in results)

        conversation_id = results[0].conversation_id
        count = (
            await db_session.execute(
                select(func.count())
                .select_from(Message)
                .where(Message.conversation_id == conversation_id)
            )
        ).scalar_one()
        assert count == 20

    async def test_replaying_a_whole_batch_is_a_no_op(self, db_session, ingest_ctx) -> None:
        messages = [
            msg(text=f"line {i}", sequence_index=i, source_message_id=f"replay-{i}")
            for i in range(5)
        ]
        first = await ingest_service.ingest_message_batch(db_session, ingest_ctx, messages)
        second = await ingest_service.ingest_message_batch(db_session, ingest_ctx, messages)
        assert all(r.status == "accepted" for r in first)
        assert all(r.status == "duplicate" for r in second)


class TestPolicyEnforcementAtIngest:
    async def test_personal_workspace_context_is_refused(
        self, db_session, seeded_org, fake_storage
    ) -> None:
        organization, user, device = seeded_org
        with pytest.raises(PolicyError) as exc:
            await ingest_service.build_context(
                db_session,
                organization=organization,
                user=user,
                device=device,
                workspace_ref=managed_workspace_ref(kind="personal"),
            )
        assert exc.value.code == "personal_workspace_blocked"

    async def test_capture_gate_closed_blocks_context(
        self, db_session, seeded_org, fake_storage, monkeypatch
    ) -> None:
        from app.core.config import Settings

        organization, user, device = seeded_org
        closed = Settings(
            environment="test",
            browser_content_capture_enabled=False,
            openai_written_authorization_confirmed=True,
        )
        with pytest.raises(PolicyError) as exc:
            await ingest_service.build_context(
                db_session,
                organization=organization,
                user=user,
                device=device,
                workspace_ref=managed_workspace_ref(),
                settings=closed,
            )
        assert exc.value.code == "capture_disabled"


class TestSyncSummary:
    async def test_summary_reports_what_is_actually_archived(
        self, db_session, ingest_ctx, seeded_org
    ) -> None:
        organization, user, _ = seeded_org
        await ingest_service.ingest_message_batch(
            db_session,
            ingest_ctx,
            [
                msg(text="a", conversation="conv-sync-1", sequence_index=0),
                msg(text="b", conversation="conv-sync-2", sequence_index=0),
            ],
        )
        summary = await ingest_service.sync_summary(
            db_session, organization_id=organization.id, user_id=user.id
        )
        assert summary["archived_conversation_count"] >= 2
        assert "conv-sync-1" in summary["known_conversation_ids"]
        assert summary["archived_message_count"] >= 2


class TestPartitionedWrites:
    async def test_capture_events_land_in_a_monthly_partition(self, db_session, ingest_ctx) -> None:
        await ingest_service.ingest_message(
            db_session, ingest_ctx, msg(text="partition me"), index=0
        )
        row = (
            await db_session.execute(select(func.count()).select_from(CaptureEvent))
        ).scalar_one()
        assert row >= 1

        from sqlalchemy import text as sql_text

        # tableoid resolves to the child partition that actually stores the row.
        partition = (
            await db_session.execute(
                sql_text(
                    "SELECT tableoid::regclass::text FROM capture_events "
                    "ORDER BY created_at DESC LIMIT 1"
                )
            )
        ).scalar_one()
        assert partition.startswith("capture_events_p"), partition
