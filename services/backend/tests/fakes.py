"""In-memory doubles used by tests.

``FakeStorageService`` implements exactly the surface :class:`StorageService`
exposes, so tests exercise the real call sites without needing S3 or MinIO.
The MinIO-backed path is covered separately by tests/integration and by the
docker-compose integration test.
"""

from __future__ import annotations

import gzip
import io
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.crypto import canonical_json, sha256_hex
from app.services.storage import ObjectHead, PutResult


@dataclass
class StoredObject:
    body: bytes
    content_type: str
    metadata: dict[str, str]
    version_id: str


@dataclass
class FakeStorageService:
    """Deterministic in-memory stand-in for S3."""

    bucket: str = "test-bucket"
    objects: dict[str, StoredObject] = field(default_factory=dict)
    presigned: list[dict[str, Any]] = field(default_factory=list)
    reachable: bool = True
    fail_next_put: bool = False
    _version_counter: int = 0

    # -- properties mirroring StorageService --------------------------------
    @property
    def uses_custom_endpoint(self) -> bool:
        return True

    def _next_version(self) -> str:
        self._version_counter += 1
        return f"v{self._version_counter:06d}"

    # -- writes -------------------------------------------------------------
    async def put_json(
        self, key: str, payload: Any, *, metadata: dict[str, str] | None = None
    ) -> PutResult:
        body = canonical_json(payload).encode("utf-8")
        return await self.put_bytes(key, body, content_type="application/json", metadata=metadata)

    async def put_jsonl_gz(
        self, key: str, records: list[Any], *, metadata: dict[str, str] | None = None
    ) -> PutResult:
        buffer = io.BytesIO()
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
        if self.fail_next_put:
            self.fail_next_put = False
            from app.core.errors import UpstreamError

            raise UpstreamError("simulated object storage failure")
        digest = sha256_hex(body)
        version = self._next_version()
        self.objects[key] = StoredObject(
            body=body,
            content_type=content_type,
            metadata={"sha256": digest, **(metadata or {})},
            version_id=version,
        )
        return PutResult(key=key, version_id=version, sha256=digest, byte_size=len(body))

    async def copy_object(self, *, source_key: str, dest_key: str) -> str | None:
        source = self.objects.get(source_key)
        if source is None:
            from app.core.errors import UpstreamError

            raise UpstreamError("source object missing")
        version = self._next_version()
        self.objects[dest_key] = StoredObject(
            body=source.body,
            content_type=source.content_type,
            metadata=dict(source.metadata),
            version_id=version,
        )
        return version

    async def delete_object(self, key: str) -> None:
        self.objects.pop(key, None)

    # -- reads --------------------------------------------------------------
    async def head_object(self, key: str) -> ObjectHead:
        obj = self.objects.get(key)
        if obj is None:
            return ObjectHead(False, 0, None, None, None, {})
        return ObjectHead(
            exists=True,
            byte_size=len(obj.body),
            content_type=obj.content_type,
            version_id=obj.version_id,
            checksum_sha256_b64=None,
            metadata=dict(obj.metadata),
        )

    async def get_bytes(self, key: str, *, max_bytes: int | None = None) -> bytes:
        obj = self.objects.get(key)
        if obj is None:
            from app.core.errors import UpstreamError

            raise UpstreamError("object missing")
        return obj.body

    # -- presigning ---------------------------------------------------------
    def presign_put(
        self,
        *,
        key: str,
        content_type: str,
        content_length: int,
        sha256_hex_digest: str | None = None,
        ttl_seconds: int | None = None,
    ) -> tuple[str, dict[str, str], datetime]:
        ttl = ttl_seconds or 300
        expires_at = datetime.now(UTC) + timedelta(seconds=ttl)
        record = {
            "key": key,
            "content_type": content_type,
            "content_length": content_length,
            "sha256": sha256_hex_digest,
            "ttl": ttl,
        }
        self.presigned.append(record)
        headers = {
            "Content-Type": content_type,
            "Content-Length": str(content_length),
            "x-amz-server-side-encryption": "AES256",
        }
        return (f"https://fake-s3.invalid/{self.bucket}/{key}?sig=test", headers, expires_at)

    def presign_get(self, *, key: str, ttl_seconds: int | None = None) -> str:
        return f"https://fake-s3.invalid/{self.bucket}/{key}?sig=get"

    # -- health -------------------------------------------------------------
    async def check(self) -> bool:
        return self.reachable

    async def ensure_bucket(self) -> None:
        return None

    # -- test helpers -------------------------------------------------------
    def simulate_upload(self, key: str, body: bytes, content_type: str = "image/png") -> None:
        """Pretend a client PUT the bytes straight to S3."""
        self.objects[key] = StoredObject(
            body=body,
            content_type=content_type,
            metadata={"sha256": sha256_hex(body)},
            version_id=self._next_version(),
        )

    def keys_with_prefix(self, prefix: str) -> list[str]:
        return sorted(k for k in self.objects if k.startswith(prefix))

    def json_at(self, key: str) -> Any:
        return json.loads(self.objects[key].body.decode("utf-8"))


PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)

#: Minimal JPEG with an APP1/Exif segment, used by the EXIF-stripping tests.
JPEG_WITH_EXIF = (
    b"\xff\xd8"
    b"\xff\xe1\x00\x10Exif\x00\x00SECRETGPS"
    b"\xff\xdb\x00\x05\x00\x01\x02"
    b"\xff\xda\x00\x08\x01\x01\x00\x00?\x00"
    b"\xff\xd9"
)
