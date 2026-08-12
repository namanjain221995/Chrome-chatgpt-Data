"""ORM model registry.

Importing this package registers every table on :data:`app.db.base.Base.metadata`,
which is what Alembic's ``target_metadata`` and the drift check rely on.
"""

from __future__ import annotations

from app.db.base import Base
from app.models.attachment import Attachment, MessageAttachment
from app.models.conversation import (
    Conversation,
    ConversationBranch,
    Message,
    MessagePart,
    MessageVersion,
)
from app.models.enums import (
    ApprovalStatus,
    AttachmentState,
    AuditAction,
    CaptureCompleteness,
    CaptureEventKind,
    CaptureSource,
    CompletionStatus,
    ExportKind,
    ExportStatus,
    FeedbackKind,
    IngestStatus,
    JobKind,
    JobStatus,
    MessageRole,
    PartKind,
    RetentionAction,
    SourceEventKind,
    WorkspaceKind,
)
from app.models.events import (
    MONTHLY_PARTITIONED_TABLES,
    AuditEvent,
    CaptureEvent,
    IdempotencyKey,
    SourceEvent,
    SourceEventKeyIndex,
    SyncCheckpoint,
)
from app.models.governance import (
    Export,
    Feedback,
    LegalHold,
    RetentionPolicy,
    TrainingApproval,
)
from app.models.identity import Device, Organization, User, UserIdentity, Workspace
from app.models.jobs import Job, JobAttempt

__all__ = [
    "MONTHLY_PARTITIONED_TABLES",
    "ApprovalStatus",
    "Attachment",
    "AttachmentState",
    "AuditAction",
    "AuditEvent",
    "Base",
    "CaptureCompleteness",
    "CaptureEvent",
    "CaptureEventKind",
    "CaptureSource",
    "CompletionStatus",
    "Conversation",
    "ConversationBranch",
    "Device",
    "Export",
    "ExportKind",
    "ExportStatus",
    "Feedback",
    "FeedbackKind",
    "IdempotencyKey",
    "IngestStatus",
    "Job",
    "JobAttempt",
    "JobKind",
    "JobStatus",
    "LegalHold",
    "Message",
    "MessageAttachment",
    "MessagePart",
    "MessageRole",
    "MessageVersion",
    "Organization",
    "PartKind",
    "RetentionAction",
    "RetentionPolicy",
    "SourceEvent",
    "SourceEventKeyIndex",
    "SourceEventKind",
    "SyncCheckpoint",
    "TrainingApproval",
    "User",
    "UserIdentity",
    "Workspace",
    "WorkspaceKind",
]
