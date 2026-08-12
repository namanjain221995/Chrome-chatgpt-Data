"""Service logic that needs no database: exports, EXIF, storage keys, adapter."""

from __future__ import annotations

import gzip
import io
import json
import uuid
from datetime import UTC, datetime

import boto3
import pytest
from botocore import UNSIGNED
from botocore.client import Config as BotoConfig
from botocore.stub import Stubber

from app.adapters.openai_compliance import ComplianceAdapter, FieldMap, _parse_time
from app.core.config import Settings
from app.core.errors import PolicyError
from app.models.enums import ExportKind
from app.services.exif import strip_jpeg_metadata, strip_metadata, strip_png_metadata
from app.services.exports import (
    _prompt_answer_pairs,
    assert_export_allowed,
    split_for_conversation,
)
from app.services.partitions import (
    create_partition_sql,
    default_partition_sql,
    month_start,
    next_month,
    partition_name,
)
from app.services.storage import (
    attachment_key,
    backup_key,
    export_part_key,
    raw_event_key,
    snapshot_key,
)
from tests.fakes import JPEG_WITH_EXIF, PNG_BYTES, FakeStorageService

WHEN = datetime(2026, 3, 15, 10, 30, tzinfo=UTC)


class TestStorageKeys:
    def test_raw_event_key_is_partition_friendly(self) -> None:
        key = raw_event_key(
            workspace_hash="wshash", conversation_id="conv-1", event_id="evt-1", when=WHEN
        )
        assert key == (
            "raw/events/year=2026/month=03/day=15/workspace=wshash"
            "/conversation=conv-1/evt-1.json"
        )

    def test_snapshot_key_is_zero_padded(self) -> None:
        key = snapshot_key(workspace_hash="wshash", conversation_id="conv-1", version=7, when=WHEN)
        assert key.endswith("snapshot-000007.json")

    def test_attachment_keys_separate_stages(self) -> None:
        args = {
            "workspace_hash": "wshash",
            "conversation_id": "conv-1",
            "attachment_id": "att-1",
            "filename": "../evil name.png",
        }
        quarantine = attachment_key(stage="quarantine", **args)
        clean = attachment_key(stage="clean", **args)
        assert quarantine.startswith("attachments/quarantine/")
        assert clean.startswith("attachments/clean/")
        # The traversal attempt never reaches the key.
        assert ".." not in quarantine
        assert quarantine.endswith("evil_name.png")

    def test_export_part_key_is_ordered(self) -> None:
        key = export_part_key(export_id="e1", part_number=1, split="train")
        assert key == "exports/jsonl/e1/train/part-00001.jsonl.gz"

    def test_backup_key_is_date_partitioned(self) -> None:
        assert backup_key(timestamp=WHEN, name="techsara-x.dump.gz") == (
            "backups/postgres/2026/03/15/techsara-x.dump.gz"
        )


class TestExportRules:
    def test_curated_export_blocked_when_disabled(self) -> None:
        settings = Settings(environment="test", training_export_enabled=False)
        with pytest.raises(PolicyError) as exc:
            assert_export_allowed(ExportKind.CURATED_TRAINING_JSONL, settings)
        assert exc.value.code == "training_export_disabled"

    def test_curated_export_allowed_when_enabled(self) -> None:
        settings = Settings(environment="test", training_export_enabled=True)
        assert_export_allowed(ExportKind.CURATED_TRAINING_JSONL, settings)

    def test_compliance_extract_is_not_gated_by_training_flag(self) -> None:
        settings = Settings(environment="test", training_export_enabled=False)
        assert_export_allowed(ExportKind.COMPLIANCE_EXTRACT, settings)

    def test_split_is_deterministic_per_conversation(self) -> None:
        ratios = {"train": 0.8, "validation": 0.1, "test": 0.1}
        conversation = uuid.uuid4()
        assert split_for_conversation(conversation, ratios) == split_for_conversation(
            conversation, ratios
        )

    def test_split_covers_all_buckets_roughly_proportionally(self) -> None:
        ratios = {"train": 0.8, "validation": 0.1, "test": 0.1}
        counts: dict[str, int] = {}
        for _ in range(3000):
            split = split_for_conversation(uuid.uuid4(), ratios)
            counts[split] = counts.get(split, 0) + 1
        assert set(counts) == set(ratios)
        assert 0.7 < counts["train"] / 3000 < 0.9

    def test_pairs_are_adjacent_user_assistant_turns_only(self) -> None:
        turns = [
            {"role": "user", "text": "q1"},
            {"role": "assistant", "text": "a1"},
            {"role": "assistant", "text": "a1-regen"},
            {"role": "user", "text": "q2"},
        ]
        pairs = _prompt_answer_pairs(turns)
        assert pairs == [{"prompt": "q1", "answer": "a1"}]


class TestExifStripping:
    def test_jpeg_exif_segment_is_removed(self) -> None:
        stripped = strip_jpeg_metadata(JPEG_WITH_EXIF)
        assert b"SECRETGPS" not in stripped
        assert stripped.startswith(b"\xff\xd8")
        assert stripped.endswith(b"\xff\xd9")

    def test_png_text_chunks_are_removed(self) -> None:
        import struct
        import zlib

        chunk_data = b"Comment\x00secret location"
        text_chunk = (
            struct.pack(">I", len(chunk_data))
            + b"tEXt"
            + chunk_data
            + struct.pack(">I", zlib.crc32(b"tEXt" + chunk_data))
        )
        with_text = PNG_BYTES[:8] + text_chunk + PNG_BYTES[8:]
        stripped = strip_png_metadata(with_text)
        assert b"secret location" not in stripped
        assert stripped.startswith(b"\x89PNG")

    def test_pixel_data_is_untouched_for_clean_png(self) -> None:
        stripped, changed = strip_metadata(PNG_BYTES, "image/png")
        assert stripped == PNG_BYTES
        assert changed is False

    def test_unknown_types_are_passed_through(self) -> None:
        data = b"%PDF-1.4 something"
        stripped, changed = strip_metadata(data, "application/pdf")
        assert stripped == data
        assert changed is False

    def test_malformed_jpeg_is_returned_intact(self) -> None:
        junk = b"\xff\xd8\xff\xe1\x00\x02"
        assert strip_jpeg_metadata(junk).startswith(b"\xff\xd8")


class TestPartitionHelpers:
    def test_month_arithmetic_wraps_the_year(self) -> None:
        assert next_month(datetime(2026, 12, 1).date()).isoformat() == "2027-01-01"
        assert month_start(datetime(2026, 5, 17).date()).isoformat() == "2026-05-01"

    def test_partition_name_matches_convention(self) -> None:
        assert partition_name("capture_events", datetime(2026, 3, 1).date()) == (
            "capture_events_p202603"
        )

    def test_partition_sql_has_bounds(self) -> None:
        sql = create_partition_sql("capture_events", datetime(2026, 3, 1).date())
        assert "FOR VALUES FROM ('2026-03-01') TO ('2026-04-01')" in sql
        assert "DEFAULT" in default_partition_sql("capture_events")


class TestComplianceAdapter:
    def _settings(self, **overrides: object) -> Settings:
        base: dict = {"environment": "test"}
        base.update(overrides)
        return Settings(**base)

    def test_adapter_is_disabled_without_configuration(self) -> None:
        adapter = ComplianceAdapter(self._settings(compliance_poll_enabled=True))
        assert adapter.is_configured is False
        assert adapter.is_enabled is False

    def test_adapter_is_disabled_when_flag_is_off(self) -> None:
        adapter = ComplianceAdapter(
            self._settings(
                compliance_poll_enabled=False,
                openai_compliance_base_url="https://api.example.com",
                openai_compliance_log_path="/v1/logs",
                openai_compliance_api_key="secret",
            )
        )
        assert adapter.is_configured is True
        assert adapter.is_enabled is False

    def test_adapter_enabled_when_configured_and_flagged(self) -> None:
        adapter = ComplianceAdapter(
            self._settings(
                compliance_poll_enabled=True,
                openai_compliance_base_url="https://api.example.com",
                openai_compliance_log_path="/v1/logs",
                openai_compliance_api_key="secret",
            )
        )
        assert adapter.is_enabled is True

    def test_describe_never_leaks_the_api_key(self) -> None:
        adapter = ComplianceAdapter(
            self._settings(
                openai_compliance_base_url="https://api.example.com",
                openai_compliance_log_path="/v1/logs",
                openai_compliance_api_key="super-secret-key",
            )
        )
        assert "super-secret-key" not in json.dumps(adapter.describe())

    def test_field_map_defaults_and_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert FieldMap().items == "data"
        monkeypatch.setenv(
            "OPENAI_COMPLIANCE_FIELD_MAP", json.dumps({"items": "results", "event_id": "uuid"})
        )
        mapped = FieldMap.from_env()
        assert mapped.items == "results"
        assert mapped.event_id == "uuid"
        # Unknown keys are ignored rather than crashing the poller.
        monkeypatch.setenv("OPENAI_COMPLIANCE_FIELD_MAP", json.dumps({"nope": 1}))
        assert FieldMap.from_env().items == "data"

    def test_event_parsing_extracts_the_documented_fields(self) -> None:
        adapter = ComplianceAdapter(self._settings())
        event = adapter.parse_event(
            {
                "id": "evt-1",
                "type": "conversation.message.created",
                "created_at": "2026-03-15T10:30:00Z",
                "conversation_id": "conv-9",
                "message_id": "msg-9",
                "workspace_id": "ws-1",
                "user": {"email": "alice@example.com"},
            }
        )
        assert event is not None
        assert event.source_event_id == "evt-1"
        assert event.kind == "message"
        assert event.conversation_id == "conv-9"
        assert event.actor_email == "alice@example.com"
        assert event.is_deletion is False

    def test_deletion_events_are_classified_as_tombstones(self) -> None:
        adapter = ComplianceAdapter(self._settings())
        event = adapter.parse_event({"id": "evt-2", "type": "conversation.deleted"})
        assert event is not None
        assert event.is_deletion is True
        assert event.kind == "deletion"

    def test_event_without_id_is_dropped(self) -> None:
        adapter = ComplianceAdapter(self._settings())
        assert adapter.parse_event({"type": "message"}) is None

    @pytest.mark.parametrize(
        ("value", "year"),
        [("2026-03-15T10:30:00Z", 2026), ("2026-03-15T10:30:00+00:00", 2026), (1773570600, 2026)],
    )
    def test_timestamp_parsing_accepts_documented_formats(self, value: object, year: int) -> None:
        parsed = _parse_time(value)
        assert parsed is not None
        assert parsed.tzinfo is not None
        assert parsed.year == year

    def test_unparseable_timestamp_returns_none(self) -> None:
        assert _parse_time("not a time") is None
        assert _parse_time(None) is None


@pytest.mark.asyncio
class TestFakeStorageContract:
    async def test_put_and_head_round_trip(self) -> None:
        storage = FakeStorageService()
        result = await storage.put_json("k.json", {"a": 1})
        head = await storage.head_object("k.json")
        assert head.exists
        assert head.byte_size == result.byte_size
        assert storage.json_at("k.json") == {"a": 1}

    async def test_missing_object_head_is_negative(self) -> None:
        storage = FakeStorageService()
        assert (await storage.head_object("nope")).exists is False

    async def test_jsonl_gz_is_readable(self) -> None:
        storage = FakeStorageService()
        await storage.put_jsonl_gz("x.jsonl.gz", [{"a": 1}, {"b": 2}])
        raw = storage.objects["x.jsonl.gz"].body
        lines = gzip.GzipFile(fileobj=io.BytesIO(raw)).read().decode().strip().split("\n")
        assert [json.loads(line) for line in lines] == [{"a": 1}, {"b": 2}]

    async def test_presign_records_the_pinned_constraints(self) -> None:
        storage = FakeStorageService()
        url, headers, _ = storage.presign_put(
            key="k", content_type="image/png", content_length=1234, sha256_hex_digest="a" * 64
        )
        assert url.startswith("https://")
        assert headers["Content-Length"] == "1234"
        assert storage.presigned[0]["content_length"] == 1234


class TestEncryptionArguments:
    """Encryption and checksum headers are mandatory on AWS S3."""

    def _service(self, **overrides):
        from app.services.storage import StorageService

        base: dict = {"environment": "test", "s3_bucket": "b"}
        base.update(overrides)
        return StorageService(Settings(**base))

    def test_real_s3_gets_sse_s3_by_default(self) -> None:
        args = self._service()._encryption_args()
        assert args["ServerSideEncryption"] == "AES256"

    def test_real_s3_gets_sse_kms_when_configured(self) -> None:
        args = self._service(
            s3_encryption_mode="SSE-KMS",
            s3_kms_key_id="arn:aws:kms:us-east-1:1:key/abc",
        )._encryption_args()
        assert args["ServerSideEncryption"] == "aws:kms"
        assert args["SSEKMSKeyId"].endswith("key/abc")

    def test_presign_pins_encryption_and_checksum(self) -> None:
        class PresignClient:
            def generate_presigned_url(self, *_args: object, **kwargs: object) -> str:
                params = kwargs["Params"]
                assert isinstance(params, dict)
                assert params["ServerSideEncryption"] == "AES256"
                assert params["ChecksumSHA256"]
                return "https://techsara-chatgpt.s3.us-east-1.amazonaws.com/key?signed=true"

        service = self._service()
        service._client = PresignClient()
        _url, aws_headers, _expiry = service.presign_put(
            key="k", content_type="image/png", content_length=10, sha256_hex_digest="a" * 64
        )
        assert aws_headers["x-amz-server-side-encryption"] == "AES256"
        assert aws_headers["x-amz-checksum-sha256"]

    @pytest.mark.asyncio
    async def test_put_bytes_matches_botocore_contract(self) -> None:
        """The unit suite validates the AWS request without a network service."""
        from app.core.crypto import hex_to_b64, sha256_hex
        from app.services.storage import StorageService

        body = b"stubbed-s3-body"
        digest = sha256_hex(body)
        client = boto3.client(
            "s3",
            region_name="us-east-1",
            config=BotoConfig(signature_version=UNSIGNED),
        )
        stubber = Stubber(client)
        stubber.add_response(
            "put_object",
            {"VersionId": "version-1", "ETag": '"etag"'},
            {
                "Bucket": "test-bucket",
                "Key": "raw/test.json",
                "Body": body,
                "ContentType": "application/json",
                "Metadata": {"sha256": digest},
                "ServerSideEncryption": "AES256",
                "ChecksumSHA256": hex_to_b64(digest),
            },
        )
        service = StorageService(Settings(environment="test", s3_bucket="test-bucket"))
        service._client = client
        with stubber:
            result = await service.put_bytes("raw/test.json", body, content_type="application/json")
        stubber.assert_no_pending_responses()
        assert result.version_id == "version-1"


class TestCachedStorageHealth:
    """`/health/ready` is polled by Docker, the deploy script and Cloudflare.

    A HeadBucket per probe would be wasteful, and a slow one would block
    readiness, so the result is memoised and the probe is bounded.
    """

    class _CountingStorage:
        def __init__(self, result: bool = True) -> None:
            self.calls = 0
            self.result = result

        async def check(self) -> bool:
            self.calls += 1
            return self.result

    @pytest.fixture(autouse=True)
    def _isolate(self):  # type: ignore[no-untyped-def]
        from app.services import storage as storage_module

        yield
        storage_module.reset_storage(None)

    async def test_repeat_probes_reuse_the_cached_result(self) -> None:
        from app.services import storage as storage_module

        fake = self._CountingStorage()
        storage_module.reset_storage(fake)  # type: ignore[arg-type]
        settings = Settings(environment="test", s3_health_cache_seconds=60)

        assert await storage_module.check_storage(settings) is True
        assert await storage_module.check_storage(settings) is True
        assert await storage_module.check_storage(settings) is True
        assert fake.calls == 1

    async def test_a_zero_ttl_disables_caching(self) -> None:
        from app.services import storage as storage_module

        fake = self._CountingStorage()
        storage_module.reset_storage(fake)  # type: ignore[arg-type]
        settings = Settings(environment="test", s3_health_cache_seconds=0)

        await storage_module.check_storage(settings)
        await storage_module.check_storage(settings)
        assert fake.calls == 2

    async def test_concurrent_probes_collapse_into_one_call(self) -> None:
        """A burst of readiness checks must not become a burst of HeadBuckets."""
        import asyncio

        from app.services import storage as storage_module

        fake = self._CountingStorage()
        storage_module.reset_storage(fake)  # type: ignore[arg-type]
        settings = Settings(environment="test", s3_health_cache_seconds=60)

        results = await asyncio.gather(*(storage_module.check_storage(settings) for _ in range(10)))
        assert all(results)
        assert fake.calls == 1

    async def test_swapping_the_service_invalidates_the_cache(self) -> None:
        from app.services import storage as storage_module

        reachable = self._CountingStorage(result=True)
        storage_module.reset_storage(reachable)  # type: ignore[arg-type]
        settings = Settings(environment="test", s3_health_cache_seconds=60)
        assert await storage_module.check_storage(settings) is True

        unreachable = self._CountingStorage(result=False)
        storage_module.reset_storage(unreachable)  # type: ignore[arg-type]
        assert await storage_module.check_storage(settings) is False

    async def test_an_unreachable_bucket_reports_false_rather_than_raising(self) -> None:
        from app.services.storage import StorageService

        settings = Settings(
            environment="test",
            s3_bucket="bucket-that-does-not-exist",
            s3_health_timeout_seconds=1.0,
        )
        service = StorageService(settings)

        class _Boom:
            def head_bucket(self, **_: object) -> None:
                raise RuntimeError("network is unreachable")

        # Inject the probe client directly: no network, no credential chain.
        service._health_client = _Boom()
        assert await service.check() is False

    def test_the_probe_client_fails_fast_instead_of_retrying(self) -> None:
        """The data-path client retries five times over 60s; a probe must not.

        Only the configuration is built here. Instantiating a real boto3 client
        would resolve the credential chain, and a unit test must not reach for
        the instance metadata service.
        """
        from app.services.storage import StorageService

        service = StorageService(Settings(environment="test"))
        probe = service.probe_config()
        data_path = service.data_path_config()

        assert probe.retries["max_attempts"] == 1
        assert probe.connect_timeout <= 5
        assert probe.read_timeout <= 5
        assert data_path.retries["max_attempts"] > probe.retries["max_attempts"]
        assert data_path.read_timeout > probe.read_timeout
