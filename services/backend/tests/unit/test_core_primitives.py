"""Hashing, fingerprinting, filename safety, sanitisation and log redaction."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import DATABASE_CONNECTION_RESERVE, Settings
from app.core.crypto import (
    canonical_json,
    content_hash,
    message_fingerprint,
    normalize_text,
    safe_filename,
    sha256_hex,
    sign_payload,
    timestamp_bucket,
    verify_signature,
)
from app.core.logging import _scrub
from app.core.sanitize import clean_plain_text, sanitize_html


class TestNormalisation:
    def test_normalize_collapses_whitespace_and_case(self) -> None:
        assert normalize_text("  Hello   WORLD \n") == "hello world"

    def test_normalize_is_nfkc(self) -> None:
        # Full-width characters normalise to their ASCII equivalents. Written as
        # escapes so the source stays unambiguous ASCII.
        fullwidth = "\uff28\uff25\uff2c\uff2c\uff2f"  # HELLO
        assert normalize_text(fullwidth) == "hello"

    def test_content_hash_is_stable_across_rendering_noise(self) -> None:
        assert content_hash("Hello  world") == content_hash("hello world")

    def test_content_hash_differs_for_different_content(self) -> None:
        assert content_hash("hello") != content_hash("hello!")


class TestFingerprint:
    def _fp(self, **overrides: object) -> str:
        payload = {
            "conversation_id": uuid.UUID("11111111-1111-1111-1111-111111111111"),
            "role": "assistant",
            "text": "The answer is 42.",
            "sequence_index": 7,
            "created_at": datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        }
        payload.update(overrides)
        return message_fingerprint(**payload)  # type: ignore[arg-type]

    def test_fingerprint_is_deterministic(self) -> None:
        assert self._fp() == self._fp()

    def test_fingerprint_tolerates_small_sequence_drift(self) -> None:
        # Same neighbourhood (7 and 9 both floor-divide to 1 with width 5).
        assert self._fp(sequence_index=7) == self._fp(sequence_index=9)

    def test_fingerprint_changes_for_distant_positions(self) -> None:
        assert self._fp(sequence_index=7) != self._fp(sequence_index=99)

    def test_fingerprint_changes_with_role(self) -> None:
        assert self._fp(role="user") != self._fp(role="assistant")

    def test_fingerprint_changes_with_conversation(self) -> None:
        other = uuid.UUID("22222222-2222-2222-2222-222222222222")
        assert self._fp(conversation_id=other) != self._fp()

    def test_timestamp_bucket_groups_nearby_times(self) -> None:
        base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        assert timestamp_bucket(base) == timestamp_bucket(base + timedelta(seconds=120))
        assert timestamp_bucket(base) != timestamp_bucket(base + timedelta(minutes=30))

    def test_timestamp_bucket_handles_none(self) -> None:
        assert timestamp_bucket(None) == 0


class TestSafeFilename:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("../../etc/passwd", "passwd"),
            ("..\\..\\windows\\system32\\cmd.exe", "cmd.exe"),
            ("report final.pdf", "report_final.pdf"),
            ("nul\x00byte.txt", "nulbyte.txt"),
            ("", "attachment.bin"),
            ("photo (1).png", "photo__1.png"),
            ("...", "file"),
            ("$(rm -rf /).png", "file.png"),
        ],
    )
    def test_dangerous_names_are_neutralised(self, raw: str, expected: str) -> None:
        assert safe_filename(raw) == expected

    def test_long_names_keep_their_extension(self) -> None:
        name = safe_filename("a" * 400 + ".png")
        assert len(name) <= 128
        assert name.endswith(".png")

    def test_no_path_separators_survive(self) -> None:
        assert "/" not in safe_filename("a/b/c.png")
        assert "\\" not in safe_filename("a\\b\\c.png")


class TestSanitizeHtml:
    @pytest.mark.parametrize(
        "payload",
        [
            "<script>alert(1)</script>",
            "<img src=x onerror=alert(1)>",
            "<iframe src='https://evil.example'></iframe>",
            "<a href='javascript:alert(1)'>click</a>",
            "<object data='x'></object>",
            "<svg/onload=alert(1)>",
        ],
    )
    def test_active_content_is_removed(self, payload: str) -> None:
        cleaned = sanitize_html(payload) or ""
        lowered = cleaned.lower()
        assert "<script" not in lowered
        assert "onerror" not in lowered
        assert "onload" not in lowered
        assert "javascript:" not in lowered
        assert "<iframe" not in lowered

    def test_rich_text_is_preserved(self) -> None:
        html = (
            "<h2>Heading</h2><p><strong>bold</strong> and <em>italic</em></p>"
            "<pre><code class='language-python'>print(1)</code></pre>"
            "<table><tr><th>a</th><td>b</td></tr></table>"
            "<ul><li>one</li></ul>"
            "<a href='https://example.com' rel='noopener'>link</a>"
        )
        cleaned = sanitize_html(html) or ""
        for fragment in ("<h2>", "<strong>", "<code", "<table>", "<li>", "href="):
            assert fragment in cleaned

    def test_empty_input_returns_none(self) -> None:
        assert sanitize_html("") is None
        assert sanitize_html(None) is None

    def test_plain_text_control_characters_removed(self) -> None:
        assert "\x07" not in clean_plain_text("bell\x07here")

    def test_plain_text_is_truncated(self) -> None:
        assert len(clean_plain_text("x" * 50, max_length=10)) == 10


class TestConfigSigning:
    def test_signature_round_trip(self) -> None:
        payload = {"policy": {"capture_active": False}, "config_version": 7}
        signature = sign_payload(payload, "test-key")
        assert verify_signature(payload, "test-key", signature)

    def test_tampered_payload_fails(self) -> None:
        payload = {"policy": {"capture_active": False}}
        signature = sign_payload(payload, "test-key")
        payload["policy"]["capture_active"] = True  # type: ignore[index]
        assert not verify_signature(payload, "test-key", signature)

    def test_wrong_key_fails(self) -> None:
        payload = {"a": 1}
        assert not verify_signature(payload, "other-key", sign_payload(payload, "test-key"))

    def test_canonical_json_is_key_order_independent(self) -> None:
        assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})


class TestLogRedaction:
    def test_credentials_are_redacted(self) -> None:
        scrubbed = _scrub(
            {
                "authorization": "Bearer secret-token",
                "cookie": "session=abc",
                "aws_secret_access_key": "shhh",
                "nested": {"password": "hunter2"},
            },
            allow_content=False,
        )
        assert scrubbed["authorization"] == "[REDACTED]"
        assert scrubbed["cookie"] == "[REDACTED]"
        assert scrubbed["aws_secret_access_key"] == "[REDACTED]"
        assert scrubbed["nested"]["password"] == "[REDACTED]"

    def test_message_content_is_suppressed_by_default(self) -> None:
        scrubbed = _scrub({"text": "confidential prompt"}, allow_content=False)
        assert scrubbed["text"] == "[CONTENT_SUPPRESSED]"

    def test_content_can_be_enabled_for_debugging(self) -> None:
        scrubbed = _scrub({"text": "hello"}, allow_content=True)
        assert scrubbed["text"] == "hello"

    def test_inline_bearer_tokens_are_scrubbed(self) -> None:
        scrubbed = _scrub({"note": "sent Bearer abc.def-ghi to upstream"}, allow_content=True)
        assert "abc.def-ghi" not in scrubbed["note"]

    def test_jwt_shaped_strings_are_scrubbed(self) -> None:
        token = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJlSGVyZQ"
        scrubbed = _scrub({"detail": token}, allow_content=True)
        assert scrubbed["detail"] == "[REDACTED_JWT]"

    def test_presigned_signature_is_scrubbed(self) -> None:
        url = "https://s3/bucket/key?X-Amz-Signature=deadbeef1234&X-Amz-Credential=AKIA/x"
        scrubbed = _scrub({"detail": url}, allow_content=True)
        assert "deadbeef1234" not in scrubbed["detail"]

    def test_deep_nesting_is_truncated(self) -> None:
        deep: dict = {"a": {}}
        cursor = deep["a"]
        for _ in range(20):
            cursor["a"] = {}
            cursor = cursor["a"]
        scrubbed = _scrub(deep, allow_content=True)
        assert "TRUNCATED" in str(scrubbed)


def test_sha256_hex_matches_known_vector() -> None:
    assert sha256_hex("abc") == ("ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")


class TestConnectionBudget:
    """Pools multiply by process, so the budget must be enforced, not documented."""

    @staticmethod
    def _production(**overrides: object) -> Settings:
        base: dict[str, object] = {
            "environment": "production",
            "dev_auth_enabled": False,
            "jwt_secret": "x" * 48,
            "config_signing_key": "y" * 48,
            "database_url": "postgresql+asyncpg://u:strongpassword@postgres:5432/db",
            "public_base_url": "https://archive.example.com",
            "archive_hostname": "archive.example.com",
            "allowed_email_domains": "example.com",
            "oidc_client_id": "real-client-id",
        }
        base.update(overrides)
        return Settings(**base)  # type: ignore[arg-type]

    def test_worst_case_counts_every_process(self) -> None:
        settings = self._production(
            database_pool_size=10,
            database_max_overflow=2,
            api_workers=3,
        )
        # (10 + 2) * (3 API workers + 1 job worker), poller disabled.
        assert settings.max_expected_database_connections == 48

    def test_enabling_the_poller_adds_a_pool(self) -> None:
        settings = self._production(
            database_pool_size=10,
            database_max_overflow=2,
            api_workers=3,
            compliance_poll_enabled=True,
        )
        assert settings.max_expected_database_connections == 60

    def test_defaults_fit_inside_the_configured_maximum(self) -> None:
        settings = self._production()
        budget = settings.postgres_max_connections - DATABASE_CONNECTION_RESERVE
        assert settings.max_expected_database_connections <= budget

    def test_production_refuses_pools_that_could_exhaust_postgres(self) -> None:
        with pytest.raises(ValueError, match="max_connections"):
            self._production(
                database_pool_size=40,
                database_max_overflow=20,
                api_workers=4,
                postgres_max_connections=120,
            )

    def test_raising_max_connections_makes_the_same_pools_acceptable(self) -> None:
        settings = self._production(
            database_pool_size=40,
            database_max_overflow=20,
            api_workers=4,
            postgres_max_connections=400,
        )
        assert settings.max_expected_database_connections == 300

    def test_development_is_not_constrained(self) -> None:
        """The guardrail is production-only; a laptop may oversubscribe freely."""
        settings = Settings(
            environment="development",
            database_pool_size=50,
            database_max_overflow=50,
            api_workers=8,
        )
        assert settings.max_expected_database_connections == 900
