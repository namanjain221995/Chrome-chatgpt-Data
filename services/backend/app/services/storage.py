"""S3 access layer.

Design rules enforced here:
  * file bytes never transit FastAPI — clients PUT directly to S3 with a
    short-lived presigned URL that pins bucket, key, content type and length;
  * every write is server-side encrypted (SSE-S3 by default, SSE-KMS optional);
  * objects are written under deterministic, workspace-pseudonymised prefixes;
  * boto3 is synchronous, so all calls are dispatched to a worker thread.
"""

from __future__ import annotations

import asyncio
import gzip
import io
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import boto3
from botocore.client import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import Settings, get_settings
from app.core.crypto import canonical_json, hex_to_b64, safe_filename, sha256_hex
from app.core.errors import UpstreamError
from app.core.logging import get_logger

logger = get_logger(__name__)

#: Object-key segments are built from identifiers that originate in the page.
#: S3 keys are opaque strings, so a "../" segment cannot escape the prefix, but
#: an unsanitised value could still embed a newline or a confusing path. Every
#: key segment therefore passes through `_key_segment` as well as being charset
#: constrained at the schema boundary.
_KEY_SEGMENT_RE = re.compile(r"[^A-Za-z0-9._:@-]")


def _key_segment(value: str, *, max_length: int = 200, fallback: str = "unknown") -> str:
    cleaned = _KEY_SEGMENT_RE.sub("_", (value or "").strip())[:max_length]
    return cleaned.strip("._-") or fallback


RAW_EVENT_PREFIX = "raw/events"
NORMALIZED_PREFIX = "normalized/conversations"
ATTACHMENT_QUARANTINE_PREFIX = "attachments/quarantine"
ATTACHMENT_CLEAN_PREFIX = "attachments/clean"
ATTACHMENT_CURATED_PREFIX = "attachments/curated"
EXPORT_PREFIX = "exports/jsonl"
BACKUP_PREFIX = "backups/postgres"
BACKUP_MANIFEST_PREFIX = "backups/manifests"
COMPLIANCE_RAW_PREFIX = "raw/compliance"


@dataclass(frozen=True)
class PutResult:
    key: str
    version_id: str | None
    sha256: str
    byte_size: int


@dataclass(frozen=True)
class ObjectHead:
    exists: bool
    byte_size: int
    content_type: str | None
    version_id: str | None
    checksum_sha256_b64: str | None
    metadata: dict[str, str]


# ---------------------------------------------------------------------------
# Key builders
# ---------------------------------------------------------------------------


def raw_event_key(
    *, workspace_hash: str, conversation_id: str, event_id: str, when: datetime
) -> str:
    when = when.astimezone(UTC)
    return (
        f"{RAW_EVENT_PREFIX}/year={when:%Y}/month={when:%m}/day={when:%d}"
        f"/workspace={_key_segment(workspace_hash)}"
        f"/conversation={_key_segment(conversation_id)}/{_key_segment(event_id)}.json"
    )


def compliance_raw_key(*, workspace_hash: str, source_event_id: str, when: datetime) -> str:
    when = when.astimezone(UTC)
    return (
        f"{COMPLIANCE_RAW_PREFIX}/year={when:%Y}/month={when:%m}/day={when:%d}"
        f"/workspace={_key_segment(workspace_hash)}"
        f"/{_key_segment(source_event_id, max_length=180, fallback='event')}.json"
    )


def snapshot_key(*, workspace_hash: str, conversation_id: str, version: int, when: datetime) -> str:
    when = when.astimezone(UTC)
    return (
        f"{NORMALIZED_PREFIX}/year={when:%Y}/month={when:%m}"
        f"/workspace={_key_segment(workspace_hash)}"
        f"/conversation={_key_segment(conversation_id)}"
        f"/snapshot-{version:06d}.json"
    )


def attachment_key(
    *,
    stage: Literal["quarantine", "clean", "curated"],
    workspace_hash: str,
    conversation_id: str,
    attachment_id: str,
    filename: str,
) -> str:
    prefix = {
        "quarantine": ATTACHMENT_QUARANTINE_PREFIX,
        "clean": ATTACHMENT_CLEAN_PREFIX,
        "curated": ATTACHMENT_CURATED_PREFIX,
    }[stage]
    return (
        f"{prefix}/workspace={_key_segment(workspace_hash)}"
        f"/conversation={_key_segment(conversation_id)}"
        f"/{_key_segment(attachment_id)}/{safe_filename(filename)}"
    )


def export_part_key(*, export_id: str, part_number: int, split: str | None = None) -> str:
    suffix = f"{_key_segment(split)}/" if split else ""
    return f"{EXPORT_PREFIX}/{_key_segment(export_id)}/{suffix}part-{part_number:05d}.jsonl.gz"


def export_manifest_key(*, export_id: str) -> str:
    return f"{EXPORT_PREFIX}/{_key_segment(export_id)}/manifest.json"


def backup_key(*, timestamp: datetime, name: str) -> str:
    ts = timestamp.astimezone(UTC)
    return f"{BACKUP_PREFIX}/{ts:%Y}/{ts:%m}/{ts:%d}/{name}"


def backup_manifest_key(*, timestamp: datetime, name: str) -> str:
    ts = timestamp.astimezone(UTC)
    return f"{BACKUP_MANIFEST_PREFIX}/{ts:%Y}/{ts:%m}/{ts:%d}/{name}"


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class StorageService:
    """Thin, well-typed wrapper around the S3 operations this system needs."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._client: Any | None = None

    # -- plumbing ---------------------------------------------------------
    @property
    def bucket(self) -> str:
        return self._settings.s3_bucket

    def _build_client(self) -> Any:
        s = self._settings
        config = BotoConfig(
            region_name=s.aws_region,
            signature_version="s3v4",
            s3={"addressing_style": "virtual"},
            retries={"max_attempts": 5, "mode": "standard"},
            connect_timeout=10,
            read_timeout=60,
        )
        # Credentials come from the EC2 instance profile in production; boto3
        # resolves and refreshes them automatically. No static key is accepted
        # by application configuration.
        return boto3.client("s3", config=config)

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = self._build_client()
        return self._client

    def _encryption_args(self) -> dict[str, str]:
        """Return the mandatory encryption parameters for every S3 write."""
        s = self._settings
        if s.s3_encryption_mode == "SSE-KMS" and s.s3_kms_key_id:
            return {"ServerSideEncryption": "aws:kms", "SSEKMSKeyId": s.s3_kms_key_id}
        return {"ServerSideEncryption": "AES256"}

    # -- writes -----------------------------------------------------------
    async def put_json(
        self, key: str, payload: Any, *, metadata: dict[str, str] | None = None
    ) -> PutResult:
        body = canonical_json(payload).encode("utf-8")
        return await self.put_bytes(key, body, content_type="application/json", metadata=metadata)

    async def put_jsonl_gz(
        self, key: str, records: list[Any], *, metadata: dict[str, str] | None = None
    ) -> PutResult:
        buffer = io.BytesIO()
        # mtime=0 keeps the gzip container byte-identical for identical input.
        with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as gz:
            for record in records:
                gz.write((json.dumps(record, sort_keys=True, default=str) + "\n").encode("utf-8"))
        return await self.put_bytes(
            key, buffer.getvalue(), content_type="application/gzip", metadata=metadata
        )

    async def put_bytes(
        self,
        key: str,
        body: bytes,
        *,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> PutResult:
        digest = sha256_hex(body)
        args: dict[str, Any] = {
            "Bucket": self.bucket,
            "Key": key,
            "Body": body,
            "ContentType": content_type,
            "Metadata": {"sha256": digest, **(metadata or {})},
            **self._encryption_args(),
        }
        args["ChecksumSHA256"] = hex_to_b64(digest)
        try:
            response = await asyncio.to_thread(lambda: self.client.put_object(**args))
        except (ClientError, BotoCoreError) as exc:
            logger.error("s3_put_failed", key=key, error_type=type(exc).__name__)
            raise UpstreamError("Object storage write failed") from exc
        return PutResult(
            key=key,
            version_id=response.get("VersionId"),
            sha256=digest,
            byte_size=len(body),
        )

    async def copy_object(self, *, source_key: str, dest_key: str) -> str | None:
        args: dict[str, Any] = {
            "Bucket": self.bucket,
            "Key": dest_key,
            "CopySource": {"Bucket": self.bucket, "Key": source_key},
            "MetadataDirective": "COPY",
            **self._encryption_args(),
        }
        try:
            response = await asyncio.to_thread(lambda: self.client.copy_object(**args))
        except (ClientError, BotoCoreError) as exc:
            logger.error("s3_copy_failed", error_type=type(exc).__name__)
            raise UpstreamError("Object storage copy failed") from exc
        return response.get("VersionId")

    async def delete_object(self, key: str) -> None:
        try:
            await asyncio.to_thread(lambda: self.client.delete_object(Bucket=self.bucket, Key=key))
        except (ClientError, BotoCoreError) as exc:
            logger.error("s3_delete_failed", error_type=type(exc).__name__)
            raise UpstreamError("Object storage delete failed") from exc

    # -- reads ------------------------------------------------------------
    async def head_object(self, key: str) -> ObjectHead:
        def _head() -> dict[str, Any]:
            response: dict[str, Any] = self.client.head_object(
                Bucket=self.bucket, Key=key, ChecksumMode="ENABLED"
            )
            return response

        try:
            response = await asyncio.to_thread(_head)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey", "NotFound"):
                return ObjectHead(False, 0, None, None, None, {})
            logger.error("s3_head_failed", error_type=type(exc).__name__, code=code)
            raise UpstreamError("Object storage head failed") from exc
        except BotoCoreError as exc:
            raise UpstreamError("Object storage head failed") from exc
        return ObjectHead(
            exists=True,
            byte_size=int(response.get("ContentLength", 0)),
            content_type=response.get("ContentType"),
            version_id=response.get("VersionId"),
            checksum_sha256_b64=response.get("ChecksumSHA256"),
            metadata=dict(response.get("Metadata", {})),
        )

    async def get_bytes(self, key: str, *, max_bytes: int | None = None) -> bytes:
        limit = max_bytes if max_bytes is not None else self._settings.max_attachment_bytes

        def _get() -> bytes:
            args: dict[str, Any] = {"Bucket": self.bucket, "Key": key}
            if limit:
                # Read one extra byte so an oversized object is detectable.
                args["Range"] = f"bytes=0-{limit}"
            response = self.client.get_object(**args)
            body: bytes = response["Body"].read()
            return body

        try:
            data = await asyncio.to_thread(_get)
        except (ClientError, BotoCoreError) as exc:
            logger.error("s3_get_failed", error_type=type(exc).__name__)
            raise UpstreamError("Object storage read failed") from exc
        if limit and len(data) > limit:
            raise UpstreamError("Stored object exceeds the configured maximum size")
        return data

    # -- presigning -------------------------------------------------------
    def presign_put(
        self,
        *,
        key: str,
        content_type: str,
        content_length: int,
        sha256_hex_digest: str | None = None,
        ttl_seconds: int | None = None,
    ) -> tuple[str, dict[str, str], datetime]:
        """Presigned PUT pinned to bucket, key, content type and exact length.

        Signing ``ContentLength`` means S3 rejects an upload whose body length
        differs from the declared size, so a client cannot inflate an upload
        past the policy limit after the presign call.
        """
        ttl = ttl_seconds or self._settings.presigned_upload_ttl_seconds
        params: dict[str, Any] = {
            "Bucket": self.bucket,
            "Key": key,
            "ContentType": content_type,
            "ContentLength": content_length,
            **self._encryption_args(),
        }
        headers: dict[str, str] = {
            "Content-Type": content_type,
            "Content-Length": str(content_length),
        }
        enc = self._encryption_args()
        if enc.get("ServerSideEncryption") == "aws:kms":
            headers["x-amz-server-side-encryption"] = "aws:kms"
            headers["x-amz-server-side-encryption-aws-kms-key-id"] = enc["SSEKMSKeyId"]
        elif enc.get("ServerSideEncryption"):
            headers["x-amz-server-side-encryption"] = enc["ServerSideEncryption"]

        # S3 verifies the checksum during upload and the worker verifies it
        # again before an attachment leaves quarantine.
        if sha256_hex_digest:
            params["ChecksumSHA256"] = hex_to_b64(sha256_hex_digest)
            headers["x-amz-checksum-sha256"] = hex_to_b64(sha256_hex_digest)

        try:
            url = self.client.generate_presigned_url(
                "put_object", Params=params, ExpiresIn=ttl, HttpMethod="PUT"
            )
        except (ClientError, BotoCoreError) as exc:
            raise UpstreamError("Unable to create upload URL") from exc
        expires_at = datetime.now(UTC) + timedelta(seconds=ttl)
        return str(url), headers, expires_at

    def presign_get(self, *, key: str, ttl_seconds: int | None = None) -> str:
        ttl = ttl_seconds or self._settings.presigned_download_ttl_seconds
        try:
            url = self.client.generate_presigned_url(
                "get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=ttl
            )
        except (ClientError, BotoCoreError) as exc:
            raise UpstreamError("Unable to create download URL") from exc
        return str(url)

    # -- health -----------------------------------------------------------
    async def check(self) -> bool:
        try:
            await asyncio.to_thread(lambda: self.client.head_bucket(Bucket=self.bucket))
            return True
        except Exception as exc:
            logger.warning("s3_check_failed", error_type=type(exc).__name__)
            return False


_storage: StorageService | None = None


def get_storage() -> StorageService:
    global _storage
    if _storage is None:
        _storage = StorageService()
    return _storage


def reset_storage(service: StorageService | None = None) -> None:
    global _storage
    _storage = service
