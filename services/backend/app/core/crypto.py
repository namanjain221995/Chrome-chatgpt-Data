"""Hashing, fingerprinting and configuration signing helpers."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import unicodedata
import uuid
from datetime import UTC, datetime
from typing import Any

_WHITESPACE_RE = re.compile(r"\s+")
_UNSAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]")
_DOTS_RE = re.compile(r"\.{2,}")


def new_uuid() -> uuid.UUID:
    """UUID4 from the OS CSPRNG."""
    return uuid.uuid4()


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def sha256_b64(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return base64.b64encode(hashlib.sha256(data).digest()).decode("ascii")


def hex_to_b64(hex_digest: str) -> str:
    return base64.b64encode(bytes.fromhex(hex_digest)).decode("ascii")


def canonical_json(payload: Any) -> str:
    """Stable JSON used for hashing and signing (sorted keys, no whitespace)."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def canonical_json_sha256(payload: Any) -> str:
    return sha256_hex(canonical_json(payload))


def normalize_text(text: str) -> str:
    """Normalise message text for fingerprinting.

    NFKC-normalise, collapse whitespace and lowercase so that trivial
    re-rendering differences do not create duplicate message rows.
    """
    normalized = unicodedata.normalize("NFKC", text or "")
    normalized = _WHITESPACE_RE.sub(" ", normalized).strip()
    return normalized.casefold()


def content_hash(text: str) -> str:
    return sha256_hex(normalize_text(text))


def timestamp_bucket(ts: datetime | None, *, bucket_seconds: int = 300) -> int:
    """Bucket a timestamp so near-identical timestamps fingerprint identically."""
    if ts is None:
        return 0
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    epoch = int(ts.astimezone(UTC).timestamp())
    return epoch - (epoch % bucket_seconds)


def message_fingerprint(
    *,
    conversation_id: uuid.UUID | str,
    role: str,
    text: str,
    sequence_index: int | None,
    created_at: datetime | None,
    neighborhood: int = 5,
) -> str:
    """Stable fingerprint for messages with no reliable source message id.

    Uses conversation, role, normalised content hash, a coarse sequence
    neighbourhood and a 5-minute timestamp bucket. The neighbourhood keeps the
    fingerprint stable when a few earlier messages are backfilled later.
    """
    seq_bucket = -1 if sequence_index is None else sequence_index // neighborhood
    material = "|".join(
        [
            str(conversation_id),
            role.strip().lower(),
            content_hash(text),
            str(seq_bucket),
            str(timestamp_bucket(created_at)),
        ]
    )
    return sha256_hex(material)


def pseudonymize(value: str, *, salt: str = "") -> str:
    """One-way pseudonym for identifiers embedded in S3 keys and exports."""
    return sha256_hex(f"{salt}|{value.strip().lower()}")


def sign_payload(payload: Any, key: str) -> str:
    """Detached HMAC-SHA256 signature over canonical JSON."""
    mac = hmac.new(key.encode("utf-8"), canonical_json(payload).encode("utf-8"), hashlib.sha256)
    return base64.urlsafe_b64encode(mac.digest()).decode("ascii").rstrip("=")


def verify_signature(payload: Any, key: str, signature: str) -> bool:
    return hmac.compare_digest(sign_payload(payload, key), signature)


def safe_filename(name: str, *, max_length: int = 128, fallback: str = "attachment.bin") -> str:
    """Strip path traversal, control characters and shell/HTTP-hostile bytes.

    The extension is preserved when it is recognisable, because the server-side
    allowlist check compares it against the declared MIME type.
    """
    if not name:
        return fallback
    candidate = name.replace("\\", "/").split("/")[-1]
    candidate = unicodedata.normalize("NFKD", candidate)
    candidate = "".join(ch for ch in candidate if ch.isprintable())

    stem, dot, ext = candidate.rpartition(".")
    if not dot or not ext.isalnum() or len(ext) > 10:
        stem, ext = candidate, ""
    ext = ext.lower()

    stem = _UNSAFE_FILENAME_RE.sub("_", stem)
    stem = _DOTS_RE.sub(".", stem).strip("._-")
    if not stem:
        stem = "file"

    budget = max_length - (len(ext) + 1 if ext else 0)
    if budget < 1:
        return fallback
    stem = stem[:budget]
    return f"{stem}.{ext}" if ext else stem


def utcnow() -> datetime:
    return datetime.now(UTC)
