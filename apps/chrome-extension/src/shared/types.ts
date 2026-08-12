/**
 * Shared types for the TechSara managed ChatGPT archive extension.
 *
 * These mirror the backend Pydantic contracts. `packages/schemas` holds the
 * generated JSON Schema that both sides validate against, and
 * `npm run validate:schemas` fails the build if this file drifts from it.
 */

export const SCHEMA_VERSION = '1.0' as const;

export type MessageRole = 'user' | 'assistant' | 'tool' | 'system';

export type CompletionStatus = 'complete' | 'partial' | 'reconciled' | 'unknown';

export type CaptureCompleteness =
  | 'complete_current_page'
  | 'partial_scroll_limit'
  | 'live_only'
  | 'compliance_verified'
  | 'reconciled'
  | 'unknown';

export type WorkspaceKind = 'managed_company' | 'personal' | 'unverified';

export type PartKind =
  | 'text'
  | 'code'
  | 'heading'
  | 'list'
  | 'table'
  | 'quote'
  | 'link'
  | 'citation'
  | 'image_ref'
  | 'attachment_ref'
  | 'tool_output'
  | 'unknown';

export type IngestStatus = 'accepted' | 'duplicate' | 'rejected' | 'retryable';

export interface ClientContext {
  extension_version: string;
  adapter_version: string;
  schema_version: typeof SCHEMA_VERSION;
  device_fingerprint?: string | null;
  page_locale?: string | null;
  captured_at: string;
}

export interface WorkspaceRef {
  source_workspace_id?: string | null;
  label?: string | null;
  kind: WorkspaceKind;
  verified: boolean;
  verification_signals: string[];
}

export interface MessagePartPayload {
  index: number;
  kind: PartKind;
  language?: string | null;
  text?: string | null;
  structured?: Record<string, unknown>;
}

export interface CitationPayload {
  index: number;
  title?: string | null;
  url?: string | null;
  source_id?: string | null;
}

export interface MessagePayload {
  idempotency_key: string;
  source_conversation_id: string;
  source_message_id?: string | null;
  role: MessageRole;
  sequence_index: number;
  text: string;
  sanitized_html?: string | null;
  parts: MessagePartPayload[];
  citations: CitationPayload[];
  completion_status: CompletionStatus;
  is_edit: boolean;
  is_regeneration: boolean;
  parent_source_message_id?: string | null;
  branch_key?: string | null;
  branch_selected: boolean;
  source_created_at?: string | null;
  content_sha256: string;
  attachment_client_ids: string[];
  author_name?: string | null;
}

export interface ConversationUpsertPayload {
  idempotency_key: string;
  source_conversation_id: string;
  source_url?: string | null;
  title?: string | null;
  model_slug?: string | null;
  workspace: WorkspaceRef;
  capture_completeness: CaptureCompleteness;
  capture_source: 'chrome_extension';
  source_created_at?: string | null;
  source_updated_at?: string | null;
  observed_message_count?: number | null;
  branch_hint?: string | null;
  client: ClientContext;
}

export interface AttachmentInitPayload {
  client_attachment_id: string;
  source_conversation_id: string;
  source_message_id?: string | null;
  filename: string;
  mime_type: string;
  byte_size: number;
  sha256: string;
  relation: 'uploaded_by_user' | 'generated_by_assistant' | 'referenced_historical';
  metadata_only: boolean;
  source_file_id?: string | null;
  client: ClientContext;
}

export interface ItemResult {
  index: number;
  idempotency_key?: string | null;
  status: IngestStatus;
  id?: string | null;
  conversation_id?: string | null;
  message_id?: string | null;
  message_version_id?: string | null;
  reason_code?: string | null;
  reason?: string | null;
}

export interface BatchResponse {
  accepted: number;
  duplicate: number;
  rejected: number;
  retryable: number;
  results: ItemResult[];
  queue_depth?: number | null;
  backpressure: boolean;
  server_time: string;
}

// ---------------------------------------------------------------------------
// Server-authoritative configuration
// ---------------------------------------------------------------------------

export interface CapturePolicy {
  browser_content_capture_enabled: boolean;
  openai_written_authorization_confirmed: boolean;
  capture_active: boolean;
  auto_archive_current_open_chat: boolean;
  attachment_capture_enabled: boolean;
  personal_workspace_capture_enabled: false;
  capture_unsent_drafts: false;
  kill_switch: boolean;
}

export interface WorkspaceRules {
  managed_workspace_label?: string | null;
  managed_workspace_ids: string[];
  allowed_url_patterns: string[];
  require_all_signals: boolean;
  min_signals: number;
}

export interface CaptureLimits {
  max_batch_items: number;
  max_request_bytes: number;
  max_attachment_bytes: number;
  allowed_mime_types: string[];
  allowed_extensions: string[];
  offline_queue_max_items: number;
  offline_queue_max_bytes: number;
  offline_queue_max_age_days: number;
  stable_response_quiet_ms: number;
  backfill_max_messages: number;
  backfill_max_seconds: number;
  backfill_max_scrolls: number;
  rate_limit_requests_per_minute: number;
}

export interface RuntimeConfig {
  schema_version: typeof SCHEMA_VERSION;
  config_version: number;
  issued_at: string;
  expires_at: string;
  organization_slug: string;
  api_base_url: string;
  policy: CapturePolicy;
  workspace_rules: WorkspaceRules;
  limits: CaptureLimits;
  privacy_notice_url: string;
  support_contact: string;
  minimum_extension_version: string;
  coverage_statement: string;
}

export interface SignedRuntimeConfig {
  config: RuntimeConfig;
  signature: string;
  signature_algorithm: 'HMAC-SHA256';
  key_id: string;
}

export interface ManagedPolicy {
  apiBaseUrl?: string;
  organizationSlug?: string;
  oidcClientId?: string;
  allowedEmailDomains?: string[];
  managedWorkspaceLabel?: string;
  managedWorkspaceIds?: string[];
  privacyNoticeUrl?: string;
  supportContact?: string;
  enabled?: boolean;
}

// ---------------------------------------------------------------------------
// Extraction results (produced by the DOM adapter, consumed by the normalizer)
// ---------------------------------------------------------------------------

export interface ExtractedMessage {
  sourceMessageId: string | null;
  role: MessageRole;
  /** Document order within the currently rendered transcript. */
  domIndex: number;
  text: string;
  html: string | null;
  parts: MessagePartPayload[];
  citations: CitationPayload[];
  attachmentRefs: AttachmentRef[];
  isStreaming: boolean;
  branchKey: string | null;
  branchSelected: boolean;
  branchIndex: number | null;
  branchTotal: number | null;
  authorName: string | null;
  timestamp: string | null;
  modelSlug: string | null;
}

export interface AttachmentRef {
  /** Stable per-conversation id the backend uses to deduplicate. */
  clientAttachmentId: string;
  filename: string;
  mimeType: string | null;
  byteSize: number | null;
  /** True when the page shows the file but never exposes its bytes. */
  metadataOnly: boolean;
  sourceFileId: string | null;
  relation: 'uploaded_by_user' | 'generated_by_assistant' | 'referenced_historical';
}

export interface ExtractedConversation {
  sourceConversationId: string | null;
  title: string | null;
  modelSlug: string | null;
  url: string;
  messages: ExtractedMessage[];
  reachedTop: boolean;
  adapterVersion: string;
}

export interface WorkspaceObservation {
  label: string | null;
  sourceWorkspaceId: string | null;
  signals: string[];
  looksPersonal: boolean;
}

// ---------------------------------------------------------------------------
// Queue and status
// ---------------------------------------------------------------------------

export type QueueItemKind =
  | 'conversation_upsert'
  | 'message_batch'
  | 'capture_event_batch'
  | 'attachment_init'
  | 'attachment_complete'
  | 'feedback';

export interface QueueItem {
  id: string;
  kind: QueueItemKind;
  payload: unknown;
  idempotencyKey: string;
  createdAt: number;
  /** Monotonic tie-breaker so eviction order is deterministic. */
  seq?: number;
  attempts: number;
  nextAttemptAt: number;
  byteSize: number;
  lastError?: string;
}

export interface ArchiveStatus {
  signedIn: boolean;
  email: string | null;
  workspaceVerified: boolean;
  workspaceLabel: string | null;
  currentConversationId: string | null;
  captureActive: boolean;
  killSwitch: boolean;
  lastSyncAt: number | null;
  lastSyncError: string | null;
  queueSize: number;
  queueBytes: number;
  backendHealthy: boolean;
  archivedConversationCount: number;
  archivedMessageCount: number;
  archivedConversationIds: string[];
  coverageStatement: string;
  privacyNoticeUrl: string;
  configVersion: number | null;
  policyBlockReason: string | null;
}

// ---------------------------------------------------------------------------
// Runtime messaging between content script, popup and service worker
// ---------------------------------------------------------------------------

export type RuntimeMessage =
  | { type: 'GET_STATUS' }
  | { type: 'SIGN_IN' }
  | { type: 'SIGN_OUT' }
  | { type: 'ARCHIVE_CURRENT_CONVERSATION' }
  | { type: 'FLUSH_QUEUE' }
  | { type: 'REFRESH_CONFIG' }
  | { type: 'GET_CONFIG' }
  | { type: 'CONTENT_READY'; url: string }
  | { type: 'CONVERSATION_DETECTED'; conversation: ExtractedConversation; workspace: WorkspaceObservation; completeness: CaptureCompleteness }
  | { type: 'MESSAGES_CAPTURED'; conversationId: string; messages: ExtractedMessage[]; workspace: WorkspaceObservation; completeness: CaptureCompleteness }
  | { type: 'ATTACHMENT_CAPTURED'; conversationId: string; attachment: AttachmentRef; bytes: ArrayBuffer | null; sha256: string; sourceMessageId: string | null }
  | { type: 'DIAGNOSTIC'; event: string; detail?: Record<string, unknown> };

export interface RuntimeResponse<T = unknown> {
  ok: boolean;
  data?: T;
  error?: string;
  code?: string;
}
