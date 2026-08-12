/**
 * Content script: observes an approved ChatGPT page and reports what it sees.
 *
 * It performs no network calls and holds no credentials. Everything is handed
 * to the service worker, which re-checks the server policy before storing
 * anything. If the workspace cannot be verified, the script parses nothing.
 */

import type {
  CaptureCompleteness,
  ExtractedMessage,
  RuntimeConfig,
  RuntimeResponse,
  WorkspaceObservation,
} from '../shared/types';
import { log, safeErrorMessage } from '../shared/logging';
import { AttachmentObserver } from '../modules/attachment-observer';
import { LiveObserver } from '../modules/live-observer';
import { RouteObserver } from '../modules/route-observer';
import {
  backfillCurrentConversation,
  describeBackfill,
} from '../modules/conversation-backfill';
import { extractConversation, observeWorkspace } from '../modules/dom-adapter';
import { verifyWorkspace, describeReason } from '../modules/workspace-verifier';

const STATUS_ELEMENT_ID = 'techsara-archive-status';
const STATUS_VISIBLE_MS = 6000;

let config: RuntimeConfig | null = null;
let liveObserver: LiveObserver | null = null;
let attachmentObserver: AttachmentObserver | null = null;
let routeObserver: RouteObserver | null = null;
let currentConversationId: string | null = null;
const backfilledConversations = new Set<string>();

async function send<T = unknown>(message: unknown): Promise<RuntimeResponse<T>> {
  try {
    return ((await chrome.runtime.sendMessage(message)) ?? {
      ok: false,
      error: 'No response',
    }) as RuntimeResponse<T>;
  } catch (error) {
    return { ok: false, error: safeErrorMessage(error) };
  }
}

/**
 * Non-blocking status pill.
 *
 * Built with `textContent` only — never `innerHTML` — so nothing from the page
 * or the archive can be injected as markup.
 */
function showStatus(text: string): void {
  let host = document.getElementById(STATUS_ELEMENT_ID);
  if (!host) {
    host = document.createElement('div');
    host.id = STATUS_ELEMENT_ID;
    host.setAttribute('role', 'status');
    host.setAttribute('aria-live', 'polite');
    Object.assign(host.style, {
      position: 'fixed',
      right: '16px',
      bottom: '16px',
      zIndex: '2147483647',
      maxWidth: '320px',
      padding: '10px 14px',
      borderRadius: '10px',
      background: 'rgba(17, 24, 39, 0.94)',
      color: '#f9fafb',
      font: '13px/1.4 system-ui, -apple-system, sans-serif',
      boxShadow: '0 8px 24px rgba(0,0,0,0.28)',
      pointerEvents: 'none',
    } satisfies Partial<CSSStyleDeclaration>);
    document.body.appendChild(host);
  }
  host.textContent = text;
  host.style.opacity = '1';
  window.setTimeout(() => {
    if (host) host.style.opacity = '0';
  }, STATUS_VISIBLE_MS);
}

function currentWorkspace(): WorkspaceObservation {
  return observeWorkspace(document);
}

function verify(): { ok: boolean; observation: WorkspaceObservation; message: string } {
  const observation = currentWorkspace();
  const result = verifyWorkspace(observation, config, window.location.href);
  return { ok: result.verified, observation, message: describeReason(result.reason) };
}

async function reportMessages(
  conversationId: string,
  messages: ExtractedMessage[],
  completeness: CaptureCompleteness,
): Promise<RuntimeResponse> {
  const { ok, observation, message } = verify();
  if (!ok) {
    log.info('capture_skipped', { reason: message });
    return { ok: false, error: message, code: 'not_verified' };
  }
  return send({
    type: 'MESSAGES_CAPTURED',
    conversationId,
    messages,
    workspace: observation,
    completeness,
  });
}

// ---------------------------------------------------------------------------
// Backfill of the currently open conversation
// ---------------------------------------------------------------------------

async function archiveCurrentConversation(auto: boolean): Promise<RuntimeResponse> {
  const { ok, observation, message } = verify();
  if (!ok) {
    if (!auto) showStatus(message);
    return { ok: false, error: message, code: 'not_verified' };
  }

  const conversation = extractConversation(document);
  const conversationId = conversation.sourceConversationId;
  if (!conversationId) {
    if (!auto) showStatus('Open a saved conversation to archive it.');
    return { ok: false, error: 'No conversation is open.' };
  }

  const limits = config?.limits;
  const result = await backfillCurrentConversation({
    maxMessages: limits?.backfill_max_messages ?? 2000,
    maxSeconds: limits?.backfill_max_seconds ?? 120,
    maxScrolls: limits?.backfill_max_scrolls ?? 400,
  });

  const response = await send({
    type: 'CONVERSATION_DETECTED',
    conversation: result.conversation,
    workspace: observation,
    completeness: result.completeness,
  });

  if (response.ok) {
    backfilledConversations.add(conversationId);
    showStatus(describeBackfill(result));
  } else if (!auto) {
    showStatus(response.error ?? 'Could not archive this conversation.');
  }
  log.info('backfill_finished', {
    messageCount: result.messageCount,
    scrolls: result.scrolls,
    reachedTop: result.reachedTop,
    scrollRestored: result.scrollRestored,
    stoppedBecause: result.stoppedBecause,
  });
  return response;
}

// ---------------------------------------------------------------------------
// Observers
// ---------------------------------------------------------------------------

function startLiveObserver(conversationId: string | null): void {
  liveObserver?.stop();
  liveObserver = new LiveObserver({
    quietMs: config?.limits.stable_response_quiet_ms ?? 2000,
  });
  liveObserver.onStable((batch) => {
    if (!conversationId) return;
    const complete = batch.filter((item) => item.status === 'complete').map((i) => i.message);
    const partial = batch.filter((item) => item.status === 'partial').map((i) => i.message);
    if (complete.length > 0) void reportMessages(conversationId, complete, 'live_only');
    if (partial.length > 0) void reportMessages(conversationId, partial, 'live_only');
  });
  liveObserver.start(conversationId);
}

function startAttachmentObserver(): void {
  attachmentObserver?.stop();
  attachmentObserver = new AttachmentObserver({
    maxBytes: config?.limits.max_attachment_bytes,
    allowedMimeTypes: config?.limits.allowed_mime_types,
    conversationId: () => currentConversationId,
  });
  attachmentObserver.onAttachment(async (captured) => {
    if (!currentConversationId) return;
    const { ok } = verify();
    if (!ok) return;
    const response = await send({
      type: 'ATTACHMENT_CAPTURED',
      conversationId: currentConversationId,
      attachment: captured.ref,
      bytes: captured.bytes,
      sha256: captured.sha256,
      sourceMessageId: null,
    });
    if (response.ok) {
      showStatus(`Attached file archived: ${captured.ref.filename}`);
    }
  });
  attachmentObserver.start();
}

async function onConversationChanged(conversationId: string | null): Promise<void> {
  currentConversationId = conversationId;
  liveObserver?.flushPartial();
  startLiveObserver(conversationId);

  if (!conversationId) return;
  const autoArchive = config?.policy.auto_archive_current_open_chat ?? false;
  if (autoArchive && !backfilledConversations.has(conversationId)) {
    // Archive the conversation the employee just opened, once per session.
    await archiveCurrentConversation(true);
  }
}

// ---------------------------------------------------------------------------
// Bootstrap
// ---------------------------------------------------------------------------

async function bootstrap(): Promise<void> {
  const response = await send<RuntimeConfig>({
    type: 'CONTENT_READY',
    url: window.location.href,
  });
  config = (response.data as RuntimeConfig | null) ?? null;

  const { ok, message } = verify();
  if (!ok) {
    log.info('content_inactive', { reason: message });
    return;
  }

  routeObserver = new RouteObserver(window);
  routeObserver.onChange((change) => {
    void onConversationChanged(change.conversationId);
  });
  routeObserver.start();

  startAttachmentObserver();
  await onConversationChanged(routeObserver.conversationId);

  // A closing tab must not silently drop a half-streamed answer.
  window.addEventListener('pagehide', () => liveObserver?.flushPartial(), { once: false });
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') liveObserver?.flushPartial();
  });

  log.info('content_active', { conversationId: currentConversationId });
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  const request = message as { type?: string };
  if (request.type === 'ARCHIVE_CURRENT_CONVERSATION') {
    archiveCurrentConversation(false)
      .then(sendResponse)
      .catch((error: unknown) => sendResponse({ ok: false, error: safeErrorMessage(error) }));
    return true;
  }
  if (request.type === 'REFRESH_CONFIG') {
    void send<RuntimeConfig>({ type: 'GET_CONFIG' }).then((response) => {
      config = (response.data as RuntimeConfig | null) ?? null;
      sendResponse({ ok: true });
    });
    return true;
  }
  return false;
});

void bootstrap();
