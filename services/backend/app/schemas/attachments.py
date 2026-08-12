"""Attachment init/complete contracts and admin export contracts."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator

from app.schemas.common import ClientContext, Sha256Hex, ShortText, SourceId, StrictModel

AttachmentStateLiteral = Literal[
    "pending", "quarantine", "clean", "rejected", "expired", "metadata_only"
]


class AttachmentInitIn(StrictModel):
    """Metadata only. Bytes go straight to S3 via the returned presigned PUT."""

    client_attachment_id: Annotated[str, Field(min_length=8, max_length=128)]
    source_conversation_id: SourceId
    source_message_id: SourceId | None = None
    filename: Annotated[str, Field(min_length=1, max_length=512)]
    mime_type: Annotated[str, Field(max_length=255)]
    byte_size: int = Field(ge=1)
    sha256: Sha256Hex
    relation: Literal["uploaded_by_user", "generated_by_assistant", "referenced_historical"] = (
        "uploaded_by_user"
    )
    #: True when the page only exposes metadata (historical or generated files).
    metadata_only: bool = False
    source_file_id: SourceId | None = None
    client: ClientContext

    @field_validator("mime_type")
    @classmethod
    def _lower(cls, v: str) -> str:
        return v.split(";")[0].strip().lower()


class AttachmentInitOut(StrictModel):
    attachment_id: uuid.UUID
    state: AttachmentStateLiteral
    #: Absent when `metadata_only` or when the attachment already exists.
    upload_url: str | None = None
    upload_method: Literal["PUT"] = "PUT"
    upload_headers: dict[str, str] = Field(default_factory=dict)
    s3_key: str | None = None
    expires_at: datetime | None = None
    duplicate: bool = False


class AttachmentCompleteIn(StrictModel):
    attachment_id: uuid.UUID
    sha256: Sha256Hex
    byte_size: int = Field(ge=1)
    source_message_id: SourceId | None = None
    client_message_idempotency_key: Annotated[str, Field(max_length=128)] | None = None
    client: ClientContext


class AttachmentCompleteOut(StrictModel):
    attachment_id: uuid.UUID
    state: AttachmentStateLiteral
    verified: bool
    linked_message_id: uuid.UUID | None = None
    reason: ShortText | None = None


# ---------------------------------------------------------------------------
# Admin surfaces
# ---------------------------------------------------------------------------


class ExportCreateIn(StrictModel):
    kind: Literal["curated_training_jsonl", "compliance_extract", "legal_hold_bundle"]
    conversation_ids: list[uuid.UUID] = Field(default_factory=list, max_length=5000)
    from_time: datetime | None = None
    to_time: datetime | None = None
    workspace_id: uuid.UUID | None = None
    include_attachments: bool = False
    split_strategy: Literal["conversation_hash", "none"] = "conversation_hash"
    split_ratios: dict[str, float] = Field(
        default_factory=lambda: {"train": 0.8, "validation": 0.1, "test": 0.1}
    )
    reason: Annotated[str, Field(min_length=8, max_length=1024)]

    @field_validator("split_ratios")
    @classmethod
    def _ratios_sum_to_one(cls, v: dict[str, float]) -> dict[str, float]:
        if not v:
            raise ValueError("split_ratios cannot be empty")
        total = sum(v.values())
        if abs(total - 1.0) > 1e-6:
            raise ValueError("split_ratios must sum to 1.0")
        if any(ratio < 0 for ratio in v.values()):
            raise ValueError("split_ratios must be non-negative")
        return v


class ExportOut(StrictModel):
    export_id: uuid.UUID
    kind: str
    status: str
    conversation_count: int = Field(ge=0)
    record_count: int = Field(ge=0)
    byte_size: int = Field(ge=0)
    s3_prefix: str | None = None
    manifest_s3_key: str | None = None
    manifest_sha256: str | None = None
    parts: list[dict[str, Any]] = Field(default_factory=list)
    #: Short-lived presigned GETs, issued only to authorized roles.
    download_urls: list[str] = Field(default_factory=list)
    created_at: datetime
    completed_at: datetime | None = None
    error_summary: str | None = None


class ComplianceHealth(StrictModel):
    enabled: bool
    configured: bool
    last_success_at: datetime | None = None
    last_attempt_at: datetime | None = None
    last_event_time: datetime | None = None
    lag_seconds: float | None = None
    consecutive_errors: int = Field(ge=0)
    total_events: int = Field(ge=0)
    cursor_healthy: bool
    note: str | None = None


class QueueHealth(StrictModel):
    pending: int = Field(ge=0)
    running: int = Field(ge=0)
    failed: int = Field(ge=0)
    dead: int = Field(ge=0)
    oldest_pending_age_seconds: float | None = None
    stale_locks: int = Field(ge=0)
    backpressure: bool


class StorageHealth(StrictModel):
    bucket: str
    reachable: bool
    unarchived_events: int = Field(ge=0)
    stale_snapshots: int = Field(ge=0)
    pending_attachments: int = Field(ge=0)


class AdminHealthSummary(StrictModel):
    server_time: datetime
    environment: str
    version: str
    git_sha: str
    database_ok: bool
    storage: StorageHealth
    queue: QueueHealth
    compliance: ComplianceHealth
    policy: dict[str, bool]
    counts: dict[str, int]
    warnings: list[str] = Field(default_factory=list)


class HealthOut(StrictModel):
    status: Literal["ok", "degraded", "error"]
    checks: dict[str, bool] = Field(default_factory=dict)
    version: str
    server_time: datetime
