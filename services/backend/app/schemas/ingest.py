"""Ingestion contracts: conversations, messages, capture events, feedback."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import Field, model_validator

from app.schemas.common import (
    MAX_HTML_BYTES,
    MAX_TEXT_BYTES,
    ClientContext,
    IdempotencyKey,
    Sha256Hex,
    ShortText,
    SourceId,
    StrictModel,
    WorkspaceRef,
)

MessageRoleLiteral = Literal["user", "assistant", "tool", "system"]
CompletionLiteral = Literal["complete", "partial", "reconciled", "unknown"]
CompletenessLiteral = Literal[
    "complete_current_page",
    "partial_scroll_limit",
    "live_only",
    "compliance_verified",
    "reconciled",
    "unknown",
]
PartKindLiteral = Literal[
    "text",
    "code",
    "heading",
    "list",
    "table",
    "quote",
    "link",
    "citation",
    "image_ref",
    "attachment_ref",
    "tool_output",
    "unknown",
]


class Citation(StrictModel):
    index: int = Field(ge=0)
    title: ShortText | None = None
    url: Annotated[str, Field(max_length=2048)] | None = None
    source_id: SourceId | None = None


class MessagePartIn(StrictModel):
    """One structured fragment of a message, in document order."""

    index: int = Field(ge=0)
    kind: PartKindLiteral
    language: Annotated[str, Field(max_length=64)] | None = None
    text: Annotated[str, Field(max_length=MAX_TEXT_BYTES)] | None = None
    structured: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _code_requires_text(self) -> MessagePartIn:
        if self.kind == "code" and not self.text:
            raise ValueError("code parts must carry text")
        return self


class ConversationUpsertIn(StrictModel):
    idempotency_key: IdempotencyKey
    source_conversation_id: SourceId
    source_url: Annotated[str, Field(max_length=2048)] | None = None
    title: Annotated[str, Field(max_length=2048)] | None = None
    model_slug: Annotated[str, Field(max_length=128)] | None = None
    workspace: WorkspaceRef
    capture_completeness: CompletenessLiteral = "unknown"
    capture_source: Literal["chrome_extension", "compliance_api", "manual_import"] = (
        "chrome_extension"
    )
    source_created_at: datetime | None = None
    source_updated_at: datetime | None = None
    observed_message_count: int | None = Field(default=None, ge=0)
    branch_hint: Annotated[str, Field(max_length=255)] | None = None
    client: ClientContext

    @model_validator(mode="after")
    def _reject_unverified_personal(self) -> ConversationUpsertIn:
        # Fail closed at the edge of the contract as well as in the service.
        if self.workspace.kind == "personal":
            raise ValueError("personal workspace conversations are never accepted")
        return self


class ConversationUpsertOut(StrictModel):
    conversation_id: uuid.UUID
    workspace_id: uuid.UUID
    created: bool
    capture_completeness: CompletenessLiteral
    already_archived_message_count: int = Field(ge=0)
    server_time: datetime


class MessageIn(StrictModel):
    """A single committed message version.

    ``text`` is the normalised plain text; ``sanitized_html`` is optional rich
    text that the extension has already sanitised. Neither may contain unsent
    draft content — the extension only emits messages present in the rendered
    transcript.
    """

    idempotency_key: IdempotencyKey
    source_conversation_id: SourceId
    source_message_id: SourceId | None = None
    role: MessageRoleLiteral
    sequence_index: int = Field(ge=0)
    text: Annotated[str, Field(max_length=MAX_TEXT_BYTES)]
    sanitized_html: Annotated[str, Field(max_length=MAX_HTML_BYTES)] | None = None
    parts: list[MessagePartIn] = Field(default_factory=list, max_length=500)
    citations: list[Citation] = Field(default_factory=list, max_length=100)
    completion_status: CompletionLiteral = "complete"
    is_edit: bool = False
    is_regeneration: bool = False
    parent_source_message_id: SourceId | None = None
    branch_key: Annotated[str, Field(max_length=64)] | None = None
    branch_selected: bool = True
    source_created_at: datetime | None = None
    content_sha256: Sha256Hex
    attachment_client_ids: list[Annotated[str, Field(max_length=128)]] = Field(
        default_factory=list, max_length=20
    )
    author_name: Annotated[str, Field(max_length=128)] | None = None

    @model_validator(mode="after")
    def _partial_must_be_assistant(self) -> MessageIn:
        # A user message is only emitted after it is committed to the
        # transcript, so it can never legitimately be 'partial'.
        if self.completion_status == "partial" and self.role == "user":
            raise ValueError("user messages cannot be partial")
        return self


class MessageBatchIn(StrictModel):
    workspace: WorkspaceRef
    messages: list[MessageIn] = Field(min_length=1, max_length=100)
    client: ClientContext


class CaptureEventIn(StrictModel):
    """Envelope for events that are not (yet) modelled as first-class rows."""

    idempotency_key: IdempotencyKey
    kind: Literal[
        "conversation_upsert",
        "message_version",
        "attachment_metadata",
        "feedback",
        "diagnostic",
    ]
    source_conversation_id: SourceId | None = None
    source_message_id: SourceId | None = None
    occurred_at: datetime
    payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _bounded_payload(self) -> CaptureEventIn:
        # Cheap guard; the request body limit is the real defence.
        if len(str(self.payload)) > 512_000:
            raise ValueError("capture event payload too large")
        return self


class CaptureEventBatchIn(StrictModel):
    workspace: WorkspaceRef
    events: list[CaptureEventIn] = Field(min_length=1, max_length=100)
    client: ClientContext


class FeedbackIn(StrictModel):
    client_feedback_id: Annotated[str, Field(min_length=8, max_length=128)]
    source_conversation_id: SourceId
    source_message_id: SourceId | None = None
    kind: Literal["useful", "incorrect", "approved", "rejected", "note"]
    rating: int | None = Field(default=None, ge=1, le=5)
    note: Annotated[str, Field(max_length=4000)] | None = None
    client: ClientContext


class FeedbackOut(StrictModel):
    feedback_id: uuid.UUID
    status: Literal["accepted", "duplicate"]


class SyncStatusOut(StrictModel):
    server_time: datetime
    device_id: uuid.UUID | None
    last_sync_at: datetime | None
    archived_conversation_count: int = Field(ge=0)
    archived_message_count: int = Field(ge=0)
    known_conversation_ids: list[str] = Field(default_factory=list)
    queue_depth: int = Field(ge=0)
    backpressure: bool = False
    capture_enabled: bool
    kill_switch: bool
    #: Honest statement of coverage; never claims full workspace history.
    coverage_statement: str
