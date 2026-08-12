"""Retention, legal hold, curated export gates and compliance import."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.adapters.openai_compliance import ComplianceEvent
from app.core.crypto import utcnow
from app.models.conversation import Conversation, Message
from app.models.enums import (
    ApprovalStatus,
    CaptureCompleteness,
    ExportKind,
    ExportStatus,
    RetentionAction,
)
from app.models.events import SourceEvent
from app.models.governance import Export, TrainingApproval
from app.services import compliance_import, retention
from app.services import exports as export_service
from app.services.exports import run_export
from tests.integration.test_ingest import msg

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _make_conversation(db_session, ingest_ctx, *, source_id: str, turns: int = 4):
    from app.services import ingest as ingest_service

    messages = []
    for index in range(turns):
        role = "user" if index % 2 == 0 else "assistant"
        messages.append(
            msg(
                text=f"{role} turn {index} in {source_id}",
                role=role,
                sequence_index=index,
                source_message_id=f"{source_id}-m{index}",
                conversation=source_id,
            )
        )
    results = await ingest_service.ingest_message_batch(db_session, ingest_ctx, messages)
    conversation = await db_session.get(Conversation, results[0].conversation_id)
    assert conversation is not None
    return conversation


class TestRetention:
    async def test_expired_conversations_are_soft_deleted(
        self, db_session, ingest_ctx, seeded_org
    ) -> None:
        organization, _, _ = seeded_org
        conversation = await _make_conversation(db_session, ingest_ctx, source_id="conv-retain")
        conversation.created_at = utcnow() - timedelta(days=400)
        await db_session.flush()

        policy = await retention.ensure_default_policy(db_session, organization_id=organization.id)
        policy.retain_days = 365
        count = await retention.soft_delete_expired(db_session, policy=policy)
        assert count >= 1

        refreshed = await db_session.get(Conversation, conversation.id)
        assert refreshed is not None
        assert refreshed.deleted_at is not None
        assert refreshed.deletion_reason == f"retention_policy:{policy.name}"

        messages = (
            (
                await db_session.execute(
                    select(Message).where(Message.conversation_id == conversation.id)
                )
            )
            .scalars()
            .all()
        )
        assert all(m.deleted_at is not None for m in messages)

    async def test_recent_conversations_are_untouched(
        self, db_session, ingest_ctx, seeded_org
    ) -> None:
        organization, _, _ = seeded_org
        conversation = await _make_conversation(db_session, ingest_ctx, source_id="conv-fresh")
        policy = await retention.ensure_default_policy(db_session, organization_id=organization.id)
        await retention.soft_delete_expired(db_session, policy=policy)
        refreshed = await db_session.get(Conversation, conversation.id)
        assert refreshed is not None
        assert refreshed.deleted_at is None

    async def test_legal_hold_survives_retention(self, db_session, ingest_ctx, seeded_org) -> None:
        organization, user, _ = seeded_org
        conversation = await _make_conversation(db_session, ingest_ctx, source_id="conv-hold")
        conversation.created_at = utcnow() - timedelta(days=1000)
        await db_session.flush()

        await retention.apply_legal_hold(
            db_session,
            organization_id=organization.id,
            conversation_ids=[conversation.id],
            hold_name="matter-42",
            actor_user_id=user.id,
            matter_reference="LEGAL-42",
        )
        policy = await retention.ensure_default_policy(db_session, organization_id=organization.id)
        policy.retain_days = 1
        policy.action = RetentionAction.HARD_DELETE
        policy.grace_days = 0

        await retention.soft_delete_expired(db_session, policy=policy)
        await retention.hard_delete_grace_expired(db_session, policy=policy)

        survived = await db_session.get(Conversation, conversation.id)
        assert survived is not None, "a conversation under legal hold must never be deleted"
        assert survived.deleted_at is None
        assert survived.legal_hold is True

    async def test_hard_delete_removes_only_grace_expired_records(
        self, db_session, ingest_ctx, seeded_org
    ) -> None:
        organization, _, _ = seeded_org
        conversation = await _make_conversation(db_session, ingest_ctx, source_id="conv-purge")
        conversation.deleted_at = utcnow() - timedelta(days=60)
        await db_session.flush()

        policy = await retention.ensure_default_policy(db_session, organization_id=organization.id)
        policy.action = RetentionAction.HARD_DELETE
        policy.grace_days = 30
        removed = await retention.hard_delete_grace_expired(db_session, policy=policy)
        assert removed >= 1
        assert await db_session.get(Conversation, conversation.id) is None

    async def test_soft_delete_policy_never_hard_deletes(
        self, db_session, ingest_ctx, seeded_org
    ) -> None:
        organization, _, _ = seeded_org
        conversation = await _make_conversation(db_session, ingest_ctx, source_id="conv-soft")
        conversation.deleted_at = utcnow() - timedelta(days=999)
        await db_session.flush()
        policy = await retention.ensure_default_policy(db_session, organization_id=organization.id)
        assert policy.action == RetentionAction.SOFT_DELETE
        assert await retention.hard_delete_grace_expired(db_session, policy=policy) == 0
        assert await db_session.get(Conversation, conversation.id) is not None

    async def test_idempotency_keys_are_pruned_after_the_retry_horizon(
        self, db_session, ingest_ctx
    ) -> None:
        from app.models.events import IdempotencyKey
        from app.services import ingest as ingest_service

        await ingest_service.ingest_message(db_session, ingest_ctx, msg(text="prune me"), index=0)
        await db_session.execute(
            IdempotencyKey.__table__.update().values(created_at=utcnow() - timedelta(days=400))
        )
        pruned = await retention.prune_idempotency_keys(db_session)
        assert pruned >= 1
        remaining = (
            await db_session.execute(select(func.count()).select_from(IdempotencyKey))
        ).scalar_one()
        assert remaining == 0


class TestCuratedExport:
    async def _approved_export(self, db_session, ingest_ctx, seeded_org, *, approve: bool):
        organization, user, _ = seeded_org
        conversation = await _make_conversation(db_session, ingest_ctx, source_id="conv-export")
        conversation.capture_completeness = CaptureCompleteness.COMPLETE_CURRENT_PAGE
        if approve:
            db_session.add(
                TrainingApproval(
                    id=uuid.uuid4(),
                    organization_id=organization.id,
                    conversation_id=conversation.id,
                    status=ApprovalStatus.APPROVED,
                    reviewed_by_user_id=user.id,
                    reviewed_at=utcnow(),
                )
            )
        export = Export(
            id=uuid.uuid4(),
            organization_id=organization.id,
            requested_by_user_id=user.id,
            kind=ExportKind.CURATED_TRAINING_JSONL,
            status=ExportStatus.PENDING,
            filters={},
            split_strategy="conversation_hash",
            split_ratios={"train": 0.8, "validation": 0.1, "test": 0.1},
        )
        db_session.add(export)
        await db_session.flush()
        return conversation, export

    async def test_unapproved_conversations_are_never_exported(
        self, db_session, ingest_ctx, seeded_org, fake_storage, monkeypatch
    ) -> None:
        from app.core.config import Settings

        _conversation, export = await self._approved_export(
            db_session, ingest_ctx, seeded_org, approve=False
        )
        settings = Settings(environment="test", training_export_enabled=True)
        await run_export(db_session, export, storage=fake_storage, settings=settings)
        assert export.conversation_count == 0
        assert export.record_count == 0

    async def test_approved_conversations_are_exported_with_checksums(
        self, db_session, ingest_ctx, seeded_org, fake_storage
    ) -> None:
        from app.core.config import Settings

        conversation, export = await self._approved_export(
            db_session, ingest_ctx, seeded_org, approve=True
        )
        settings = Settings(environment="test", training_export_enabled=True)
        await run_export(db_session, export, storage=fake_storage, settings=settings)

        assert export.status == ExportStatus.COMPLETED
        assert export.conversation_count == 1
        assert export.record_count == 1
        assert export.manifest_s3_key in fake_storage.objects

        manifest = fake_storage.json_at(export.manifest_s3_key)
        assert manifest["conversation_count"] == 1
        assert manifest["parts"][0]["sha256"]
        assert "does not start any model training" in " ".join(manifest["notes"])

        record = await export_service.build_conversation_record(
            db_session, conversation.id, include_attachments=False
        )
        assert record is not None
        assert record["record_sha256"]
        assert record["pairs"], "prompt/answer pairs are produced for training use"

    async def test_export_is_blocked_when_the_flag_is_off(
        self, db_session, ingest_ctx, seeded_org, fake_storage
    ) -> None:
        from app.core.config import Settings
        from app.core.errors import PolicyError

        _conversation, export = await self._approved_export(
            db_session, ingest_ctx, seeded_org, approve=True
        )
        settings = Settings(environment="test", training_export_enabled=False)
        with pytest.raises(PolicyError):
            await run_export(db_session, export, storage=fake_storage, settings=settings)

    async def test_legal_hold_records_are_excluded_from_curated_exports(
        self, db_session, ingest_ctx, seeded_org, fake_storage
    ) -> None:
        from app.core.config import Settings

        conversation, export = await self._approved_export(
            db_session, ingest_ctx, seeded_org, approve=True
        )
        conversation.legal_hold = True
        await db_session.flush()

        settings = Settings(environment="test", training_export_enabled=True)
        await run_export(db_session, export, storage=fake_storage, settings=settings)
        assert export.conversation_count == 0

    async def test_incomplete_conversations_are_excluded(
        self, db_session, ingest_ctx, seeded_org, fake_storage
    ) -> None:
        from app.core.config import Settings

        conversation, export = await self._approved_export(
            db_session, ingest_ctx, seeded_org, approve=True
        )
        conversation.capture_completeness = CaptureCompleteness.PARTIAL_SCROLL_LIMIT
        await db_session.flush()

        settings = Settings(environment="test", training_export_enabled=True)
        await run_export(db_session, export, storage=fake_storage, settings=settings)
        assert export.conversation_count == 0

    async def test_split_keeps_whole_conversations_together(
        self, db_session, ingest_ctx, seeded_org, fake_storage
    ) -> None:
        import gzip
        import io
        import json

        from app.core.config import Settings

        organization, user, _ = seeded_org
        for index in range(6):
            conversation = await _make_conversation(
                db_session, ingest_ctx, source_id=f"conv-split-{index}"
            )
            conversation.capture_completeness = CaptureCompleteness.COMPLETE_CURRENT_PAGE
            db_session.add(
                TrainingApproval(
                    id=uuid.uuid4(),
                    organization_id=organization.id,
                    conversation_id=conversation.id,
                    status=ApprovalStatus.APPROVED,
                    reviewed_by_user_id=user.id,
                    reviewed_at=utcnow(),
                )
            )
        export = Export(
            id=uuid.uuid4(),
            organization_id=organization.id,
            requested_by_user_id=user.id,
            kind=ExportKind.CURATED_TRAINING_JSONL,
            status=ExportStatus.PENDING,
            filters={},
            split_strategy="conversation_hash",
            split_ratios={"train": 0.5, "test": 0.5},
        )
        db_session.add(export)
        await db_session.flush()

        settings = Settings(environment="test", training_export_enabled=True)
        await run_export(db_session, export, storage=fake_storage, settings=settings)

        seen: dict[str, str] = {}
        for part in export.parts:
            raw = fake_storage.objects[part["s3_key"]].body
            for line in gzip.GzipFile(fileobj=io.BytesIO(raw)).read().decode().splitlines():
                record = json.loads(line)
                previous = seen.get(record["conversation_id"])
                assert previous in (None, record["split"]), "conversation spans two splits"
                seen[record["conversation_id"]] = record["split"]
        assert len(seen) == 6


class TestComplianceImport:
    def _event(self, **overrides) -> ComplianceEvent:
        payload = {
            "source_event_id": f"evt-{uuid.uuid4().hex[:8]}",
            "event_time": utcnow(),
            "kind": "conversation",
            "conversation_id": "compliance-conv-1",
            "message_id": None,
            "workspace_id": "ws-company",
            "actor_email": "alice@example.com",
            "is_deletion": False,
            "raw": {"id": "evt", "type": "conversation.created"},
        }
        payload.update(overrides)
        return ComplianceEvent(**payload)

    async def test_event_is_written_to_storage_before_the_row(
        self, db_session, seeded_org, fake_storage
    ) -> None:
        organization, _, _ = seeded_org
        event = self._event()
        created = await compliance_import.persist_event(
            db_session, organization=organization, event=event, storage=fake_storage
        )
        assert created is True
        assert fake_storage.keys_with_prefix("raw/compliance/")

        row = (
            (
                await db_session.execute(
                    select(SourceEvent).where(SourceEvent.source_event_id == event.source_event_id)
                )
            )
            .scalars()
            .first()
        )
        assert row is not None
        assert row.raw_s3_key in fake_storage.objects
        assert row.payload_sha256 == event.payload_sha256

    async def test_duplicate_events_are_ignored(self, db_session, seeded_org, fake_storage) -> None:
        organization, _, _ = seeded_org
        event = self._event()
        assert await compliance_import.persist_event(
            db_session, organization=organization, event=event, storage=fake_storage
        )
        assert not await compliance_import.persist_event(
            db_session, organization=organization, event=event, storage=fake_storage
        )

    async def test_compliance_import_marks_conversations_verified(
        self, db_session, seeded_org, fake_storage
    ) -> None:
        organization, _, _ = seeded_org
        await compliance_import.persist_event(
            db_session,
            organization=organization,
            event=self._event(conversation_id="compliance-conv-verify"),
            storage=fake_storage,
        )
        imported = await compliance_import.import_pending_source_events(
            db_session, organization_id=organization.id
        )
        assert imported == 1

        conversation = (
            (
                await db_session.execute(
                    select(Conversation).where(
                        Conversation.source_conversation_id == "compliance-conv-verify"
                    )
                )
            )
            .scalars()
            .first()
        )
        assert conversation is not None
        assert conversation.capture_completeness == CaptureCompleteness.COMPLIANCE_VERIFIED
        assert "compliance_api" in conversation.capture_sources

    async def test_deletion_events_leave_a_tombstone(
        self, db_session, seeded_org, fake_storage
    ) -> None:
        organization, _, _ = seeded_org
        await compliance_import.persist_event(
            db_session,
            organization=organization,
            event=self._event(
                conversation_id="compliance-conv-deleted", is_deletion=True, kind="deletion"
            ),
            storage=fake_storage,
        )
        await compliance_import.import_pending_source_events(
            db_session, organization_id=organization.id
        )
        conversation = (
            (
                await db_session.execute(
                    select(Conversation).where(
                        Conversation.source_conversation_id == "compliance-conv-deleted"
                    )
                )
            )
            .scalars()
            .first()
        )
        assert conversation is not None
        # The archive keeps the record and marks that upstream removed it.
        assert conversation.metadata_json["upstream_deleted"] is True
        assert conversation.deleted_at is None

    async def test_checkpoint_only_advances_after_success(self, db_session, seeded_org) -> None:
        organization, _, _ = seeded_org
        checkpoint = await compliance_import.get_or_create_checkpoint(
            db_session, organization_id=organization.id
        )
        original_window = checkpoint.window_start

        compliance_import.record_checkpoint_failure(checkpoint, RuntimeError("upstream 500"))
        assert checkpoint.window_start == original_window
        assert checkpoint.consecutive_errors == 1
        assert checkpoint.last_error == "RuntimeError"

        window_end = utcnow()
        await compliance_import.advance_checkpoint(
            checkpoint,
            window_end=window_end,
            cursor="cursor-1",
            last_event_time=window_end,
            events_seen=5,
        )
        assert checkpoint.window_start == window_end
        assert checkpoint.consecutive_errors == 0
        assert checkpoint.total_events == 5

    async def test_checkpoint_error_never_stores_upstream_message_text(
        self, db_session, seeded_org
    ) -> None:
        organization, _, _ = seeded_org
        checkpoint = await compliance_import.get_or_create_checkpoint(
            db_session, organization_id=organization.id
        )
        compliance_import.record_checkpoint_failure(
            checkpoint, RuntimeError("Bearer sk-secret-token leaked in message")
        )
        assert "sk-secret" not in (checkpoint.last_error or "")

    async def test_poller_is_a_no_op_when_disabled(self, db_session, fake_storage) -> None:
        from app.workers.compliance_poller import CompliancePoller

        poller = CompliancePoller()
        result = await poller.run_cycle()
        assert result["skipped"] is True
        assert result["reason"] == "disabled_or_unconfigured"

    async def test_checkpoint_health_reports_lag(self, db_session, seeded_org) -> None:
        organization, _, _ = seeded_org
        checkpoint = await compliance_import.get_or_create_checkpoint(
            db_session, organization_id=organization.id
        )
        checkpoint.last_event_time = utcnow() - timedelta(minutes=10)
        health = compliance_import.checkpoint_health(checkpoint)
        assert health["cursor_healthy"] is True
        assert health["lag_seconds"] > 500

    async def test_missing_checkpoint_is_reported_unhealthy(self) -> None:
        assert compliance_import.checkpoint_health(None)["cursor_healthy"] is False
