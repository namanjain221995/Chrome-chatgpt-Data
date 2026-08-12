"""Wire-contract validation: strict schemas, size limits, rate limiting."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError as PydanticValidationError

from app.core.errors import ValidationError
from app.core.ratelimit import CompositeRateLimiter, SlidingWindowLimiter
from app.schemas.attachments import AttachmentInitIn, ExportCreateIn
from app.schemas.common import ClientContext, WorkspaceRef
from app.schemas.ingest import (
    CaptureEventIn,
    ConversationUpsertIn,
    MessageBatchIn,
    MessageIn,
)
from app.services.attachments import ALLOWED_MIME_TYPES, validate_attachment_metadata
from tests.conftest import make_client_context, managed_workspace_ref


def message_payload(**overrides: object) -> dict:
    payload: dict = {
        "idempotency_key": "msg-0000000000000001",
        "source_conversation_id": "conv-1",
        "role": "user",
        "sequence_index": 0,
        "text": "hello",
        "content_sha256": "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824",
    }
    payload.update(overrides)
    return payload


class TestStrictSchemas:
    def test_unknown_fields_are_rejected(self) -> None:
        with pytest.raises(PydanticValidationError):
            MessageIn(**message_payload(unexpected_field="surprise"))

    def test_valid_message_parses(self) -> None:
        msg = MessageIn(**message_payload())
        assert msg.role == "user"
        assert msg.completion_status == "complete"

    def test_message_carries_no_client_block(self) -> None:
        """Client provenance is sent once per batch, not per message."""
        assert "client" not in MessageIn.model_fields

    def test_naive_timestamp_is_rejected(self) -> None:
        with pytest.raises(PydanticValidationError, match="timezone-aware"):
            ClientContext(
                extension_version="1.0.0",
                adapter_version="2024.1",
                captured_at=datetime(2026, 1, 1, 0, 0),
            )

    def test_user_messages_cannot_be_partial(self) -> None:
        """A user message only exists once it is committed to the transcript."""
        with pytest.raises(PydanticValidationError, match="cannot be partial"):
            MessageIn(**message_payload(role="user", completion_status="partial"))

    def test_assistant_messages_may_be_partial(self) -> None:
        msg = MessageIn(**message_payload(role="assistant", completion_status="partial"))
        assert msg.completion_status == "partial"

    def test_personal_workspace_conversation_is_rejected_at_the_schema(self) -> None:
        with pytest.raises(PydanticValidationError, match="personal workspace"):
            ConversationUpsertIn(
                idempotency_key="conv-0000000000000001",
                source_conversation_id="c1",
                workspace=WorkspaceRef(kind="personal", verified=True),
                client=make_client_context(),
            )

    def test_batch_size_is_capped(self) -> None:
        with pytest.raises(PydanticValidationError):
            MessageBatchIn(
                workspace=managed_workspace_ref(),
                messages=[MessageIn(**message_payload()) for _ in range(101)],
                client=make_client_context(),
            )

    def test_empty_batch_is_rejected(self) -> None:
        with pytest.raises(PydanticValidationError):
            MessageBatchIn(
                workspace=managed_workspace_ref(), messages=[], client=make_client_context()
            )

    def test_bad_checksum_format_is_rejected(self) -> None:
        with pytest.raises(PydanticValidationError):
            MessageIn(**message_payload(content_sha256="not-a-hash"))

    def test_idempotency_key_charset_is_constrained(self) -> None:
        with pytest.raises(PydanticValidationError):
            MessageIn(**message_payload(idempotency_key="has spaces and $ymbols"))

    def test_code_part_requires_text(self) -> None:
        with pytest.raises(PydanticValidationError, match="code parts"):
            MessageIn(**message_payload(parts=[{"index": 0, "kind": "code", "language": "py"}]))

    def test_oversized_capture_event_payload_is_rejected(self) -> None:
        with pytest.raises(PydanticValidationError, match="too large"):
            CaptureEventIn(
                idempotency_key="evt-0000000000000001",
                kind="diagnostic",
                occurred_at=datetime.now(UTC),
                payload={"blob": "x" * 600_000},
            )

    def test_export_ratios_must_sum_to_one(self) -> None:
        with pytest.raises(PydanticValidationError, match="sum to 1.0"):
            ExportCreateIn(
                kind="curated_training_jsonl",
                split_ratios={"train": 0.5, "test": 0.2},
                reason="quarterly curation review",
            )

    def test_export_requires_a_reason(self) -> None:
        with pytest.raises(PydanticValidationError):
            ExportCreateIn(kind="curated_training_jsonl", reason="short")


class TestAttachmentValidation:
    def _payload(self, **overrides: object) -> AttachmentInitIn:
        base: dict = {
            "client_attachment_id": "att-000000000000001",
            "source_conversation_id": "conv-1",
            "filename": "diagram.png",
            "mime_type": "image/png",
            "byte_size": 2048,
            "sha256": "a" * 64,
            "client": make_client_context(),
        }
        base.update(overrides)
        return AttachmentInitIn(**base)

    def test_allowed_type_passes(self, settings) -> None:  # type: ignore[no-untyped-def]
        assert validate_attachment_metadata(self._payload(), settings) == "diagram.png"

    def test_disallowed_mime_type_is_rejected(self, settings) -> None:  # type: ignore[no-untyped-def]
        with pytest.raises(ValidationError) as exc:
            validate_attachment_metadata(
                self._payload(filename="run.exe", mime_type="application/x-msdownload"), settings
            )
        assert exc.value.code == "mime_type_not_allowed"

    def test_extension_must_match_mime_type(self, settings) -> None:  # type: ignore[no-untyped-def]
        with pytest.raises(ValidationError) as exc:
            validate_attachment_metadata(
                self._payload(filename="payload.exe", mime_type="image/png"), settings
            )
        assert exc.value.code == "extension_mismatch"

    def test_oversized_attachment_is_rejected(self, settings) -> None:  # type: ignore[no-untyped-def]
        with pytest.raises(ValidationError) as exc:
            validate_attachment_metadata(
                self._payload(byte_size=settings.max_attachment_bytes + 1), settings
            )
        assert exc.value.code == "attachment_too_large"

    def test_traversal_filename_is_neutralised(self, settings) -> None:  # type: ignore[no-untyped-def]
        safe = validate_attachment_metadata(
            self._payload(filename="../../../etc/shadow.png"), settings
        )
        assert safe == "shadow.png"

    def test_mime_parameters_are_stripped(self) -> None:
        assert self._payload(mime_type="image/png; charset=binary").mime_type == "image/png"

    def test_every_allowed_type_has_extensions(self) -> None:
        for mime, extensions in ALLOWED_MIME_TYPES.items():
            assert extensions, f"{mime} has no allowed extension"
            assert all(e.startswith(".") for e in extensions)


class TestRateLimiter:
    def test_requests_within_limit_are_allowed(self) -> None:
        limiter = SlidingWindowLimiter(limit=5)
        for _ in range(5):
            assert limiter.check("user-1").allowed

    def test_limit_is_enforced(self) -> None:
        limiter = SlidingWindowLimiter(limit=3)
        for _ in range(3):
            limiter.check("user-1")
        decision = limiter.check("user-1")
        assert not decision.allowed
        assert decision.retry_after >= 1

    def test_keys_are_isolated(self) -> None:
        limiter = SlidingWindowLimiter(limit=1)
        assert limiter.check("user-1").allowed
        assert limiter.check("user-2").allowed

    def test_window_slides(self) -> None:
        limiter = SlidingWindowLimiter(limit=2, window_seconds=60)
        limiter.check("k", now=0.0)
        limiter.check("k", now=1.0)
        assert not limiter.check("k", now=2.0).allowed
        assert limiter.check("k", now=61.5).allowed

    def test_composite_divides_budget_across_workers(self) -> None:
        limiter = CompositeRateLimiter(per_minute=300, workers=3, burst=60)
        assert limiter.user.limit == 100

    def test_composite_denies_when_any_dimension_is_exhausted(self) -> None:
        limiter = CompositeRateLimiter(per_minute=2, workers=1, burst=100)
        limiter.check(user_key="u", device_key="d", ip_key="1.2.3.4")
        limiter.check(user_key="u", device_key="d", ip_key="1.2.3.4")
        assert not limiter.check(user_key="u", device_key="d", ip_key="1.2.3.4").allowed

    def test_memory_is_bounded(self) -> None:
        limiter = SlidingWindowLimiter(limit=10)
        for index in range(5000):
            limiter.check(f"key-{index}")
        assert len(limiter._hits) <= 100_000
