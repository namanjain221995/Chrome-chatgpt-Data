"""String enumerations shared by the ORM, API schemas and exports.

Stored as VARCHAR + CHECK constraint (``native_enum=False``) so that adding a
value is an ordinary migration instead of a PostgreSQL type mutation.
"""

from __future__ import annotations

import enum


class StrEnum(str, enum.Enum):
    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value

    @classmethod
    def values(cls) -> list[str]:
        return [m.value for m in cls]


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"
    SYSTEM = "system"


class CompletionStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    RECONCILED = "reconciled"
    UNKNOWN = "unknown"


class CaptureCompleteness(StrEnum):
    """How complete a conversation archive is believed to be.

    Never set ``COMPLIANCE_VERIFIED`` from browser capture alone.
    """

    COMPLETE_CURRENT_PAGE = "complete_current_page"
    PARTIAL_SCROLL_LIMIT = "partial_scroll_limit"
    LIVE_ONLY = "live_only"
    COMPLIANCE_VERIFIED = "compliance_verified"
    RECONCILED = "reconciled"
    UNKNOWN = "unknown"


class CaptureSource(StrEnum):
    CHROME_EXTENSION = "chrome_extension"
    COMPLIANCE_API = "compliance_api"
    MANUAL_IMPORT = "manual_import"


class WorkspaceKind(StrEnum):
    MANAGED_COMPANY = "managed_company"
    PERSONAL = "personal"
    UNVERIFIED = "unverified"


class CaptureEventKind(StrEnum):
    CONVERSATION_UPSERT = "conversation_upsert"
    MESSAGE_VERSION = "message_version"
    ATTACHMENT_METADATA = "attachment_metadata"
    FEEDBACK = "feedback"
    DIAGNOSTIC = "diagnostic"
    COMPLIANCE_EVENT = "compliance_event"


class IngestStatus(StrEnum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    REJECTED = "rejected"
    RETRYABLE = "retryable"


class AttachmentState(StrEnum):
    PENDING = "pending"
    QUARANTINE = "quarantine"
    CLEAN = "clean"
    REJECTED = "rejected"
    EXPIRED = "expired"
    METADATA_ONLY = "metadata_only"


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DEAD = "dead"
    CANCELLED = "cancelled"


class JobKind(StrEnum):
    ARCHIVE_RAW_EVENT = "archive_raw_event"
    BUILD_CONVERSATION_SNAPSHOT = "build_conversation_snapshot"
    FINALIZE_ATTACHMENT = "finalize_attachment"
    RUN_EXPORT = "run_export"
    RETENTION_SWEEP = "retention_sweep"
    COMPLIANCE_SYNC = "compliance_sync"
    RECONCILE_PARTIAL = "reconcile_partial"
    MAINTAIN_PARTITIONS = "maintain_partitions"
    CLEANUP_STALE = "cleanup_stale"


class ExportStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ExportKind(StrEnum):
    CURATED_TRAINING_JSONL = "curated_training_jsonl"
    COMPLIANCE_EXTRACT = "compliance_extract"
    LEGAL_HOLD_BUNDLE = "legal_hold_bundle"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"


class FeedbackKind(StrEnum):
    USEFUL = "useful"
    INCORRECT = "incorrect"
    APPROVED = "approved"
    REJECTED = "rejected"
    NOTE = "note"


class PartKind(StrEnum):
    TEXT = "text"
    CODE = "code"
    HEADING = "heading"
    LIST = "list"
    TABLE = "table"
    QUOTE = "quote"
    LINK = "link"
    CITATION = "citation"
    IMAGE_REF = "image_ref"
    ATTACHMENT_REF = "attachment_ref"
    TOOL_OUTPUT = "tool_output"
    UNKNOWN = "unknown"


class AuditAction(StrEnum):
    ADMIN_READ = "admin_read"
    ADMIN_SEARCH = "admin_search"
    EXPORT_CREATED = "export_created"
    EXPORT_DOWNLOADED = "export_downloaded"
    APPROVAL_CHANGED = "approval_changed"
    RETENTION_SOFT_DELETE = "retention_soft_delete"
    RETENTION_HARD_DELETE = "retention_hard_delete"
    LEGAL_HOLD_SET = "legal_hold_set"
    LEGAL_HOLD_CLEARED = "legal_hold_cleared"
    DEVICE_REVOKED = "device_revoked"
    AUTH_LOGIN = "auth_login"
    AUTH_REFRESH = "auth_refresh"
    AUTH_DENIED = "auth_denied"
    CONFIG_READ = "config_read"
    POLICY_BLOCKED = "policy_blocked"
    ATTACHMENT_PRESIGNED = "attachment_presigned"
    ATTACHMENT_FINALIZED = "attachment_finalized"


class RetentionAction(StrEnum):
    SOFT_DELETE = "soft_delete"
    HARD_DELETE = "hard_delete"
    ANONYMIZE = "anonymize"


class SourceEventKind(StrEnum):
    CONVERSATION = "conversation"
    MESSAGE = "message"
    FILE = "file"
    DELETION = "deletion"
    UNKNOWN = "unknown"
