"""Durable job queue, worker execution and S3-before-complete ordering."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.core.crypto import sha256_hex, utcnow
from app.models.attachment import Attachment
from app.models.conversation import Conversation
from app.models.enums import AttachmentState, JobKind, JobStatus
from app.models.events import CaptureEvent
from app.models.jobs import Job, JobAttempt
from app.schemas.attachments import AttachmentCompleteIn, AttachmentInitIn
from app.services import attachments as attachment_service
from app.services import ingest as ingest_service
from app.services import jobs as jobs_service
from app.services import partitions as partition_service
from app.services.storage import raw_event_key
from app.workers.handlers import (
    NonRetryableError,
    handle_archive_raw_event,
    handle_build_snapshot,
    handle_finalize_attachment,
    handle_maintain_partitions,
)
from app.workers.worker import Worker
from tests.conftest import make_client_context
from tests.fakes import JPEG_WITH_EXIF, PNG_BYTES
from tests.integration.test_ingest import msg

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


class TestJobQueue:
    async def test_enqueue_and_claim(self, db_session) -> None:
        job = await jobs_service.enqueue_job(
            db_session, kind=JobKind.CLEANUP_STALE, payload={"n": 1}
        )
        assert job is not None
        claimed = await jobs_service.claim_jobs(db_session, worker_id="w1", limit=5)
        assert [j.id for j, _ in claimed] == [job.id]
        assert claimed[0][0].status == JobStatus.RUNNING
        assert claimed[0][0].locked_by == "w1"
        assert claimed[0][0].attempts == 1

    async def test_dedupe_key_prevents_duplicate_live_jobs(self, db_session) -> None:
        first = await jobs_service.enqueue_job(
            db_session, kind=JobKind.CLEANUP_STALE, payload={}, dedupe_key="only-one"
        )
        second = await jobs_service.enqueue_job(
            db_session, kind=JobKind.CLEANUP_STALE, payload={}, dedupe_key="only-one"
        )
        assert first is not None
        assert second is None

    async def test_dedupe_key_is_reusable_after_completion(self, db_session) -> None:
        first = await jobs_service.enqueue_job(
            db_session, kind=JobKind.CLEANUP_STALE, payload={}, dedupe_key="reusable"
        )
        assert first is not None
        claimed = await jobs_service.claim_jobs(db_session, worker_id="w1")
        await jobs_service.complete_job(
            db_session, job_id=claimed[0][0].id, lock_token=claimed[0][1]
        )
        again = await jobs_service.enqueue_job(
            db_session, kind=JobKind.CLEANUP_STALE, payload={}, dedupe_key="reusable"
        )
        assert again is not None

    async def test_claim_respects_priority_then_age(self, db_session) -> None:
        low = await jobs_service.enqueue_job(
            db_session, kind=JobKind.CLEANUP_STALE, payload={"p": "low"}, priority=900
        )
        high = await jobs_service.enqueue_job(
            db_session, kind=JobKind.CLEANUP_STALE, payload={"p": "high"}, priority=10
        )
        assert low is not None and high is not None
        claimed = await jobs_service.claim_jobs(db_session, worker_id="w1", limit=1)
        assert claimed[0][0].id == high.id

    async def test_future_run_after_is_not_claimed(self, db_session) -> None:
        await jobs_service.enqueue_job(
            db_session,
            kind=JobKind.CLEANUP_STALE,
            payload={},
            run_after=utcnow() + timedelta(hours=1),
        )
        assert await jobs_service.claim_jobs(db_session, worker_id="w1") == []

    async def test_kind_filter_is_honoured(self, db_session) -> None:
        await jobs_service.enqueue_job(db_session, kind=JobKind.CLEANUP_STALE, payload={})
        claimed = await jobs_service.claim_jobs(
            db_session, worker_id="w1", kinds=[JobKind.RUN_EXPORT]
        )
        assert claimed == []

    async def test_failure_schedules_a_retry_with_backoff(self, db_session) -> None:
        job = await jobs_service.enqueue_job(db_session, kind=JobKind.CLEANUP_STALE, payload={})
        assert job is not None
        claimed = await jobs_service.claim_jobs(db_session, worker_id="w1")
        job_obj, token = claimed[0]
        await jobs_service.fail_job(
            db_session, job_id=job_obj.id, lock_token=token, error_summary="boom"
        )
        refreshed = await db_session.get(Job, job_obj.id)
        assert refreshed is not None
        assert refreshed.status == JobStatus.PENDING
        assert refreshed.run_after > utcnow()
        assert refreshed.lock_token is None
        assert "boom" in (refreshed.error_summary or "")

    async def test_non_retryable_failure_buries_the_job(self, db_session) -> None:
        job = await jobs_service.enqueue_job(db_session, kind=JobKind.CLEANUP_STALE, payload={})
        assert job is not None
        job_obj, token = (await jobs_service.claim_jobs(db_session, worker_id="w1"))[0]
        await jobs_service.fail_job(
            db_session,
            job_id=job_obj.id,
            lock_token=token,
            error_summary="bad payload",
            retryable=False,
        )
        refreshed = await db_session.get(Job, job_obj.id)
        assert refreshed is not None
        assert refreshed.status == JobStatus.DEAD

    async def test_attempts_are_capped(self, db_session) -> None:
        job = await jobs_service.enqueue_job(
            db_session, kind=JobKind.CLEANUP_STALE, payload={}, max_attempts=2
        )
        assert job is not None
        for _ in range(2):
            claimed = await jobs_service.claim_jobs(db_session, worker_id="w1")
            if not claimed:
                break
            job_obj, token = claimed[0]
            await jobs_service.fail_job(
                db_session, job_id=job_obj.id, lock_token=token, error_summary="again"
            )
            job_obj.run_after = utcnow() - timedelta(seconds=1)
            await db_session.flush()
        refreshed = await db_session.get(Job, job.id)
        assert refreshed is not None
        assert refreshed.status == JobStatus.FAILED

    async def test_stale_locks_are_recovered(self, db_session) -> None:
        job = await jobs_service.enqueue_job(db_session, kind=JobKind.CLEANUP_STALE, payload={})
        assert job is not None
        job_obj, token = (await jobs_service.claim_jobs(db_session, worker_id="dead-worker"))[0]
        job_obj.locked_at = utcnow() - timedelta(hours=2)
        await db_session.flush()

        recovered = await jobs_service.recover_stale_jobs(db_session)
        assert recovered == 1
        refreshed = await db_session.get(Job, job_obj.id)
        assert refreshed is not None
        assert refreshed.status == JobStatus.PENDING
        assert refreshed.locked_by is None

        # The original worker can no longer complete the job it lost.
        assert not await jobs_service.complete_job(db_session, job_id=job_obj.id, lock_token=token)

    async def test_completion_requires_the_matching_lock_token(self, db_session) -> None:
        job = await jobs_service.enqueue_job(db_session, kind=JobKind.CLEANUP_STALE, payload={})
        assert job is not None
        job_obj, _token = (await jobs_service.claim_jobs(db_session, worker_id="w1"))[0]
        assert not await jobs_service.complete_job(
            db_session, job_id=job_obj.id, lock_token=uuid.uuid4()
        )

    async def test_queue_stats_report_backpressure_inputs(self, db_session) -> None:
        for index in range(3):
            await jobs_service.enqueue_job(
                db_session, kind=JobKind.CLEANUP_STALE, payload={"i": index}
            )
        stats = await jobs_service.queue_stats(db_session)
        assert stats["pending"] >= 3
        assert stats["backpressure"] is False
        assert stats["oldest_pending_age_seconds"] is not None

    async def test_backoff_grows_and_is_capped(self) -> None:
        assert jobs_service.backoff_delay(0, jitter=False) <= 10
        assert jobs_service.backoff_delay(20, jitter=False) == jobs_service.MAX_BACKOFF_SECONDS
        jittered = {jobs_service.backoff_delay(5) for _ in range(20)}
        assert len(jittered) > 1, "backoff must include jitter"


class TestConcurrentClaiming:
    async def test_skip_locked_prevents_double_delivery(self, db_engine) -> None:
        """Two independent sessions must never claim the same job."""
        from sqlalchemy.ext.asyncio import async_sessionmaker

        factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
        marker = f"race-{uuid.uuid4().hex}"
        async with factory() as setup:
            for index in range(4):
                await jobs_service.enqueue_job(
                    setup,
                    kind=JobKind.CLEANUP_STALE,
                    payload={"marker": marker, "i": index},
                    dedupe_key=f"{marker}-{index}",
                )
            await setup.commit()

        async with factory() as a, factory() as b:
            claimed_a = await jobs_service.claim_jobs(a, worker_id="worker-a", limit=2)
            claimed_b = await jobs_service.claim_jobs(b, worker_id="worker-b", limit=2)
            await a.commit()
            await b.commit()

            ids_a = {j.id for j, _ in claimed_a}
            ids_b = {j.id for j, _ in claimed_b}
            assert ids_a and ids_b
            assert not (ids_a & ids_b), "FOR UPDATE SKIP LOCKED must not double-deliver"

        async with factory() as cleanup:
            from sqlalchemy import delete

            await cleanup.execute(delete(Job).where(Job.dedupe_key.like(f"{marker}%")))
            await cleanup.commit()


class TestArchiveHandler:
    async def test_raw_json_reaches_storage_before_completion(
        self, db_session, ingest_ctx, fake_storage
    ) -> None:
        result = await ingest_service.ingest_message(
            db_session, ingest_ctx, msg(text="archive this"), index=0
        )
        job = (
            (
                await db_session.execute(
                    select(Job).where(Job.kind == JobKind.ARCHIVE_RAW_EVENT.value)
                )
            )
            .scalars()
            .first()
        )
        assert job is not None

        outcome = await handle_archive_raw_event(db_session, job)
        assert outcome["s3_key"] in fake_storage.objects

        event = (
            (
                await db_session.execute(
                    select(CaptureEvent).where(CaptureEvent.message_id == result.message_id)
                )
            )
            .scalars()
            .first()
        )
        assert event is not None
        assert event.archived_at is not None
        assert event.raw_s3_key == outcome["s3_key"]

        stored = fake_storage.json_at(outcome["s3_key"])
        assert stored["payload"]["text"] == "archive this"
        assert stored["integrity"]["payload_sha256"] == event.payload_sha256

    async def test_archive_is_idempotent(self, db_session, ingest_ctx, fake_storage) -> None:
        await ingest_service.ingest_message(db_session, ingest_ctx, msg(text="twice"), index=0)
        job = (
            (
                await db_session.execute(
                    select(Job).where(Job.kind == JobKind.ARCHIVE_RAW_EVENT.value)
                )
            )
            .scalars()
            .first()
        )
        assert job is not None
        await handle_archive_raw_event(db_session, job)
        second = await handle_archive_raw_event(db_session, job)
        assert second.get("skipped") is True

    async def test_missing_event_is_non_retryable(self, db_session) -> None:
        job = await jobs_service.enqueue_job(
            db_session,
            kind=JobKind.ARCHIVE_RAW_EVENT,
            payload={
                "capture_event_id": str(uuid.uuid4()),
                "s3_key": raw_event_key(
                    workspace_hash="w", conversation_id="c", event_id="e", when=utcnow()
                ),
            },
        )
        assert job is not None
        with pytest.raises(NonRetryableError):
            await handle_archive_raw_event(db_session, job)

    async def test_storage_failure_leaves_the_event_unarchived(
        self, db_session, ingest_ctx, fake_storage
    ) -> None:
        await ingest_service.ingest_message(db_session, ingest_ctx, msg(text="fails"), index=0)
        job = (
            (
                await db_session.execute(
                    select(Job).where(Job.kind == JobKind.ARCHIVE_RAW_EVENT.value)
                )
            )
            .scalars()
            .first()
        )
        assert job is not None
        fake_storage.fail_next_put = True
        with pytest.raises(Exception):  # noqa: B017
            await handle_archive_raw_event(db_session, job)

        event = (await db_session.execute(select(CaptureEvent))).scalars().first()
        assert event is not None
        assert event.archived_at is None


class TestSnapshotHandler:
    async def test_snapshot_document_is_complete_and_hashed(
        self, db_session, ingest_ctx, fake_storage
    ) -> None:
        messages = [
            msg(
                text="What is the policy?",
                role="user",
                sequence_index=0,
                source_message_id="s0",
                conversation="conv-snapshot",
            ),
            msg(
                text="The policy is documented here.",
                role="assistant",
                sequence_index=1,
                source_message_id="s1",
                conversation="conv-snapshot",
            ),
        ]
        results = await ingest_service.ingest_message_batch(db_session, ingest_ctx, messages)
        conversation_id = results[0].conversation_id

        job = (
            (
                await db_session.execute(
                    select(Job).where(Job.dedupe_key == f"snapshot:{conversation_id}")
                )
            )
            .scalars()
            .first()
        )
        assert job is not None
        outcome = await handle_build_snapshot(db_session, job)

        document = fake_storage.json_at(outcome["s3_key"])
        assert document["schema_version"] == "1.0"
        assert document["message_count"] == 2
        assert [m["role"] for m in document["messages"]] == ["user", "assistant"]
        assert document["integrity"]["sha256"] == outcome["sha256"]
        assert document["capture_completeness"] in {
            "live_only",
            "complete_current_page",
            "unknown",
        }
        # No file bytes ever appear inside conversation JSON.
        assert "body" not in document
        assert "base64" not in str(document)

        conversation = await db_session.get(Conversation, conversation_id)
        assert conversation is not None
        assert conversation.snapshot_stale is False
        assert conversation.snapshot_version == 1

    async def test_snapshot_of_missing_conversation_is_non_retryable(self, db_session) -> None:
        job = await jobs_service.enqueue_job(
            db_session,
            kind=JobKind.BUILD_CONVERSATION_SNAPSHOT,
            payload={"conversation_id": str(uuid.uuid4())},
        )
        assert job is not None
        with pytest.raises(NonRetryableError):
            await handle_build_snapshot(db_session, job)


class TestAttachmentPipeline:
    async def _init(self, db_session, ctx, *, data: bytes = PNG_BYTES, filename="diagram.png"):
        payload = AttachmentInitIn(
            client_attachment_id=f"att-{uuid.uuid4().hex}",
            source_conversation_id="conv-attachments",
            filename=filename,
            mime_type="image/png" if filename.endswith(".png") else "image/jpeg",
            byte_size=len(data),
            sha256=sha256_hex(data),
            client=make_client_context(),
        )
        return payload, await attachment_service.init_attachment(db_session, ctx, payload)

    async def test_full_upload_lifecycle(self, db_session, ingest_ctx, fake_storage) -> None:
        payload, presigned = await self._init(db_session, ingest_ctx)
        attachment = presigned.attachment
        assert attachment.state == AttachmentState.PENDING
        assert presigned.upload_url is not None
        assert presigned.headers["Content-Length"] == str(len(PNG_BYTES))
        # The presign is pinned to the exact declared size.
        assert fake_storage.presigned[-1]["content_length"] == len(PNG_BYTES)

        fake_storage.simulate_upload(attachment.quarantine_s3_key, PNG_BYTES)

        completed, verified, reason = await attachment_service.complete_attachment(
            db_session,
            ingest_ctx,
            AttachmentCompleteIn(
                attachment_id=attachment.id,
                sha256=payload.sha256,
                byte_size=payload.byte_size,
                client=make_client_context(),
            ),
        )
        assert verified is True
        assert completed.state == AttachmentState.QUARANTINE

        job = (
            (
                await db_session.execute(
                    select(Job).where(Job.dedupe_key == f"finalize_attachment:{attachment.id}")
                )
            )
            .scalars()
            .first()
        )
        assert job is not None
        outcome = await handle_finalize_attachment(db_session, job)
        assert outcome["state"] == "clean"

        refreshed = await db_session.get(Attachment, attachment.id)
        assert refreshed is not None
        assert refreshed.state == AttachmentState.CLEAN
        assert refreshed.verified_sha256 == sha256_hex(PNG_BYTES)
        assert refreshed.clean_s3_key in fake_storage.objects

    async def test_checksum_mismatch_is_rejected_by_the_worker(
        self, db_session, ingest_ctx, fake_storage
    ) -> None:
        payload, presigned = await self._init(db_session, ingest_ctx)
        attachment = presigned.attachment
        # A different payload of the same length is uploaded.
        tampered = bytearray(PNG_BYTES)
        tampered[-1] ^= 0xFF
        fake_storage.simulate_upload(attachment.quarantine_s3_key, bytes(tampered))

        await attachment_service.complete_attachment(
            db_session,
            ingest_ctx,
            AttachmentCompleteIn(
                attachment_id=attachment.id,
                sha256=payload.sha256,
                byte_size=payload.byte_size,
                client=make_client_context(),
            ),
        )
        job = (
            (
                await db_session.execute(
                    select(Job).where(Job.dedupe_key == f"finalize_attachment:{attachment.id}")
                )
            )
            .scalars()
            .first()
        )
        assert job is not None
        outcome = await handle_finalize_attachment(db_session, job)
        assert outcome["state"] == "rejected"
        assert "sha256" in outcome["reason"]

        refreshed = await db_session.get(Attachment, attachment.id)
        assert refreshed is not None
        assert refreshed.state == AttachmentState.REJECTED
        assert refreshed.clean_s3_key is None

    async def test_content_type_lying_is_detected(
        self, db_session, ingest_ctx, fake_storage
    ) -> None:
        """A .png that is really a script must never be promoted to clean."""
        evil = b"#!/bin/sh\nrm -rf /\n"
        payload = AttachmentInitIn(
            client_attachment_id=f"att-{uuid.uuid4().hex}",
            source_conversation_id="conv-attachments",
            filename="innocent.png",
            mime_type="image/png",
            byte_size=len(evil),
            sha256=sha256_hex(evil),
            client=make_client_context(),
        )
        presigned = await attachment_service.init_attachment(db_session, ingest_ctx, payload)
        fake_storage.simulate_upload(presigned.attachment.quarantine_s3_key, evil)
        await attachment_service.complete_attachment(
            db_session,
            ingest_ctx,
            AttachmentCompleteIn(
                attachment_id=presigned.attachment.id,
                sha256=payload.sha256,
                byte_size=payload.byte_size,
                client=make_client_context(),
            ),
        )
        job = (
            (
                await db_session.execute(
                    select(Job).where(
                        Job.dedupe_key == f"finalize_attachment:{presigned.attachment.id}"
                    )
                )
            )
            .scalars()
            .first()
        )
        assert job is not None
        outcome = await handle_finalize_attachment(db_session, job)
        assert outcome["state"] == "rejected"

    async def test_size_mismatch_is_rejected_at_complete(
        self, db_session, ingest_ctx, fake_storage
    ) -> None:
        payload, presigned = await self._init(db_session, ingest_ctx)
        fake_storage.simulate_upload(
            presigned.attachment.quarantine_s3_key, PNG_BYTES + b"extra bytes"
        )
        _attachment, verified, reason = await attachment_service.complete_attachment(
            db_session,
            ingest_ctx,
            AttachmentCompleteIn(
                attachment_id=presigned.attachment.id,
                sha256=payload.sha256,
                byte_size=payload.byte_size,
                client=make_client_context(),
            ),
        )
        assert verified is False
        assert reason == "size_mismatch"

    async def test_complete_without_upload_is_not_acknowledged(
        self, db_session, ingest_ctx, fake_storage
    ) -> None:
        payload, presigned = await self._init(db_session, ingest_ctx)
        _attachment, verified, reason = await attachment_service.complete_attachment(
            db_session,
            ingest_ctx,
            AttachmentCompleteIn(
                attachment_id=presigned.attachment.id,
                sha256=payload.sha256,
                byte_size=payload.byte_size,
                client=make_client_context(),
            ),
        )
        assert verified is False
        assert reason == "object_missing"

    async def test_another_employee_cannot_complete_someone_elses_attachment(
        self, db_session, ingest_ctx, fake_storage, seeded_org
    ) -> None:
        from app.core.errors import PolicyError
        from app.core.security import VerifiedIdentity
        from app.services import accounts

        payload, presigned = await self._init(db_session, ingest_ctx)
        fake_storage.simulate_upload(presigned.attachment.quarantine_s3_key, PNG_BYTES)

        organization, _user, _device = seeded_org
        intruder = await accounts.get_or_create_user(
            db_session,
            organization=organization,
            identity=VerifiedIdentity(
                subject=f"sub-{uuid.uuid4().hex}",
                issuer="https://accounts.google.com",
                email=f"intruder-{uuid.uuid4().hex[:6]}@example.com",
                email_verified=True,
                hosted_domain="example.com",
            ),
        )
        hostile_ctx = ingest_ctx
        hostile_ctx.user = intruder
        with pytest.raises(PolicyError) as exc:
            await attachment_service.complete_attachment(
                db_session,
                hostile_ctx,
                AttachmentCompleteIn(
                    attachment_id=presigned.attachment.id,
                    sha256=payload.sha256,
                    byte_size=payload.byte_size,
                    client=make_client_context(),
                ),
            )
        assert exc.value.code == "attachment_owner_mismatch"

    async def test_metadata_only_attachments_get_no_upload_url(
        self, db_session, ingest_ctx, fake_storage
    ) -> None:
        """Historical or generated files whose bytes the page never exposes."""
        payload = AttachmentInitIn(
            client_attachment_id=f"att-{uuid.uuid4().hex}",
            source_conversation_id="conv-attachments",
            filename="generated-image.png",
            mime_type="image/png",
            byte_size=123456,
            sha256=sha256_hex(b"unknown"),
            relation="generated_by_assistant",
            metadata_only=True,
            client=make_client_context(),
        )
        presigned = await attachment_service.init_attachment(db_session, ingest_ctx, payload)
        assert presigned.upload_url is None
        assert presigned.attachment.state == AttachmentState.METADATA_ONLY
        assert presigned.attachment.metadata_json["reason"] == "bytes_not_available_in_page"

    async def test_exif_is_stripped_into_a_separate_curated_copy(
        self, db_session, ingest_ctx, fake_storage
    ) -> None:
        payload = AttachmentInitIn(
            client_attachment_id=f"att-{uuid.uuid4().hex}",
            source_conversation_id="conv-attachments",
            filename="photo.jpg",
            mime_type="image/jpeg",
            byte_size=len(JPEG_WITH_EXIF),
            sha256=sha256_hex(JPEG_WITH_EXIF),
            client=make_client_context(),
        )
        presigned = await attachment_service.init_attachment(db_session, ingest_ctx, payload)
        fake_storage.simulate_upload(
            presigned.attachment.quarantine_s3_key, JPEG_WITH_EXIF, "image/jpeg"
        )
        await attachment_service.complete_attachment(
            db_session,
            ingest_ctx,
            AttachmentCompleteIn(
                attachment_id=presigned.attachment.id,
                sha256=payload.sha256,
                byte_size=payload.byte_size,
                client=make_client_context(),
            ),
        )
        job = (
            (
                await db_session.execute(
                    select(Job).where(
                        Job.dedupe_key == f"finalize_attachment:{presigned.attachment.id}"
                    )
                )
            )
            .scalars()
            .first()
        )
        assert job is not None
        outcome = await handle_finalize_attachment(db_session, job)
        assert outcome["exif_stripped"] is True

        refreshed = await db_session.get(Attachment, presigned.attachment.id)
        assert refreshed is not None
        # The exact original is retained for audit...
        original = fake_storage.objects[refreshed.clean_s3_key].body
        assert b"SECRETGPS" in original
        # ...and the curated derivative has the metadata removed.
        curated = fake_storage.objects[refreshed.curated_s3_key].body
        assert b"SECRETGPS" not in curated


class TestWorkerLoop:
    async def test_worker_executes_claimed_jobs(self, db_session, ingest_ctx, fake_storage) -> None:
        await ingest_service.ingest_message(db_session, ingest_ctx, msg(text="worker run"), index=0)
        worker = Worker(worker_id="test-worker", concurrency=5)
        executed = await worker.run_once(db_session)
        assert executed >= 1
        assert worker.processed >= 1

        succeeded = (
            await db_session.execute(
                select(func.count()).select_from(Job).where(Job.status == JobStatus.SUCCEEDED.value)
            )
        ).scalar_one()
        assert succeeded >= 1

    async def test_worker_records_attempts(self, db_session, fake_storage) -> None:
        await jobs_service.enqueue_job(db_session, kind=JobKind.CLEANUP_STALE, payload={})
        worker = Worker(worker_id="test-worker", concurrency=1)
        await worker.run_once(db_session)
        attempts = (await db_session.execute(select(JobAttempt))).scalars().all()
        assert attempts
        assert attempts[0].worker_id == "test-worker"
        assert attempts[0].duration_ms is not None

    async def test_unknown_job_kind_is_buried_not_retried(self, db_session, fake_storage) -> None:
        job = await jobs_service.enqueue_job(db_session, kind=JobKind.COMPLIANCE_SYNC, payload={})
        assert job is not None
        # Remove the handler to simulate an unregistered kind.
        from app.workers import handlers as handlers_module

        original = handlers_module.HANDLERS.pop(JobKind.COMPLIANCE_SYNC)
        try:
            worker = Worker(worker_id="test-worker", concurrency=1)
            await worker.run_once(db_session)
        finally:
            handlers_module.HANDLERS[JobKind.COMPLIANCE_SYNC] = original

        refreshed = await db_session.get(Job, job.id)
        assert refreshed is not None
        assert refreshed.status == JobStatus.DEAD


class TestPartitionMaintenance:
    async def test_maintenance_creates_future_partitions(self, db_session) -> None:
        job = await jobs_service.enqueue_job(
            db_session, kind=JobKind.MAINTAIN_PARTITIONS, payload={}
        )
        assert job is not None
        outcome = await handle_maintain_partitions(db_session, job)
        assert outcome["partitions_ensured"] > 0

        names = await partition_service.list_partitions(db_session, "capture_events")
        assert any(name.endswith("_default") for name in names)
        assert len([n for n in names if not n.endswith("_default")]) >= 4

    async def test_maintenance_is_idempotent(self, db_session) -> None:
        first = await partition_service.ensure_partitions(db_session)
        second = await partition_service.ensure_partitions(db_session)
        assert first == second

    async def test_old_partition_drop_is_dry_run_by_default(self, db_session) -> None:
        from datetime import date

        dropped = await partition_service.drop_partitions_older_than(
            db_session, table="capture_events", cutoff=date(2100, 1, 1)
        )
        remaining = await partition_service.list_partitions(db_session, "capture_events")
        # Nothing was actually dropped because dry_run defaults to True.
        assert set(dropped).issubset(set(remaining))
