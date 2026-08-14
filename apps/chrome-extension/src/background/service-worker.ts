/**
 * MV3 service worker: authentication, configuration, queueing and sync.
 *
 * The content script never talks to the network. It reports what it saw; this
 * worker decides — against the signed server configuration — whether anything
 * may be stored, then queues and uploads it.
 */

import type {
  ArchiveStatus,
  CaptureCompleteness,
  ExtractedMessage,
  RuntimeConfig,
  RuntimeMessage,
  RuntimeResponse,
  SignedRuntimeConfig,
  WorkspaceRef,
} from '../shared/types';
import { log, recentLogs, safeErrorMessage } from '../shared/logging';
import { ApiClient } from '../modules/api-client';
import { OfflineQueue } from '../modules/offline-queue';
import { SyncEngine } from './sync-engine';
import {
  archivedConversationIds,
  markConversationArchived,
  patchStatus,
  readStatus,
  statusFromConfig,
} from './state';
import {
  captureAllowed,
  isUsableConfig,
  policyBlockReason,
  readCachedConfig,
  readManagedPolicy,
  resolveApiBaseUrl,
  writeCachedConfig,
} from '../modules/managed-config';
import * as auth from '../modules/auth-client';
import { normalizeMessages } from '../modules/message-normalizer';
import { randomId, sha256Hex } from '../shared/util';

const FLUSH_ALARM = 'techsara-flush';
const CONFIG_ALARM = 'techsara-config';
const FLUSH_PERIOD_MINUTES = 1;
const CONFIG_PERIOD_MINUTES = 10;
const DEVICE_FINGERPRINT_KEY = 'deviceFingerprint';

const queue = new OfflineQueue();
let cachedConfig: RuntimeConfig | null = null;

// ---------------------------------------------------------------------------
// Wiring
// ---------------------------------------------------------------------------

async function deviceFingerprint(): Promise<string> {
  const stored = await chrome.storage.local.get(DEVICE_FINGERPRINT_KEY);
  const existing = stored?.[DEVICE_FINGERPRINT_KEY] as string | undefined;
  if (existing) return existing;
  // Random per browser profile: it identifies the install, not the human, and
  // it is never derived from hardware or network identifiers.
  const fingerprint = await sha256Hex(`${randomId()}:${chrome.runtime.id}`);
  await chrome.storage.local.set({ [DEVICE_FINGERPRINT_KEY]: fingerprint });
  return fingerprint;
}

async function apiBaseUrl(): Promise<string | null> {
  const policy = await readManagedPolicy();
  const cached = await readCachedConfig();
  return resolveApiBaseUrl(policy, cached.config?.api_base_url);
}

async function accessToken(): Promise<string | null> {
  const session = await auth.readSession();
  if (auth.sessionValid(session)) return session?.accessToken ?? null;
  if (auth.refreshUsable(session)) {
    const refreshed = await refreshSession();
    return refreshed?.accessToken ?? null;
  }
  return null;
}

async function buildApi(): Promise<ApiClient | null> {
  const baseUrl = await apiBaseUrl();
  if (!baseUrl) return null;
  return new ApiClient({
    baseUrl,
    getAccessToken: accessToken,
    onUnauthorized: async () => {
      await patchStatus({ signedIn: false, lastSyncError: 'Session expired; sign in again.' });
    },
  });
}

async function buildSync(api: ApiClient): Promise<SyncEngine> {
  return new SyncEngine({
    api,
    queue,
    config: () => cachedConfig,
    extensionVersion: chrome.runtime.getManifest().version,
    deviceFingerprint,
  });
}

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

async function loadConfig(force = false): Promise<RuntimeConfig | null> {
  const cached = await readCachedConfig();
  if (!force && cached.config && !cached.stale) {
    cachedConfig = cached.config;
    return cachedConfig;
  }

  const api = await buildApi();
  if (!api) {
    cachedConfig = cached.config;
    await patchStatus({
      policyBlockReason: 'No company backend configured by policy.',
      captureActive: false,
    });
    return cachedConfig;
  }

  try {
    const signed: SignedRuntimeConfig = await api.getConfig();
    if (!isUsableConfig(signed)) {
      log.warn('config_rejected');
      cachedConfig = cached.config;
    } else {
      await writeCachedConfig(signed);
      cachedConfig = signed.config;
      queue.configure({
        maxItems: signed.config.limits.offline_queue_max_items,
        maxBytes: signed.config.limits.offline_queue_max_bytes,
        maxAgeDays: signed.config.limits.offline_queue_max_age_days,
      });
    }
  } catch (error) {
    log.warn('config_fetch_failed', { reason: safeErrorMessage(error) });
    cachedConfig = cached.config;
  }

  const status = await readStatus();
  await patchStatus({
    ...statusFromConfig(status, cachedConfig),
    policyBlockReason: policyBlockReason(cachedConfig),
    backendHealthy: cachedConfig !== null,
  });
  return cachedConfig;
}

// ---------------------------------------------------------------------------
// Authentication
// ---------------------------------------------------------------------------

async function signIn(): Promise<RuntimeResponse> {
  const api = await buildApi();
  if (!api) return { ok: false, error: 'No company backend is configured by policy.' };

  const policy = await readManagedPolicy();
  const clientId = policy.oidcClientId;
  if (!clientId) {
    return { ok: false, error: 'Company sign-in is not configured. Contact IT support.' };
  }

  try {
    const { url } = await auth.buildAuthorizationUrl({
      clientId,
      redirectUri: auth.redirectUri(),
      hostedDomain: policy.allowedEmailDomains?.[0] ?? null,
    });
    const redirect = await auth.launchWebAuthFlow(url);
    const { code } = await auth.parseRedirect(redirect);
    const pkce = await auth.readPkce();
    if (!pkce) return { ok: false, error: 'Sign-in state was lost. Please try again.' };

    const tokens = (await api.exchange({
      grant_type: 'authorization_code',
      code,
      code_verifier: pkce.verifier,
      redirect_uri: auth.redirectUri(),
      nonce: pkce.nonce,
      device_fingerprint: await deviceFingerprint(),
      extension_version: chrome.runtime.getManifest().version,
    })) as Record<string, unknown>;

    await auth.clearPkce();
    await persistTokens(tokens);
    await registerDevice(api);
    // Signing in is what unlocks the workspace rules in the runtime
    // configuration, so the cached anonymous copy is stale the moment we hold
    // a token. Without this the workspace stays unverified until the ten
    // minute config alarm happens to fire.
    await loadConfig(true);
    await broadcastConfigToTabs();
    await patchStatus({ signedIn: true, email: String(tokens.email ?? ''), lastSyncError: null });
    return { ok: true, data: { email: tokens.email } };
  } catch (error) {
    return { ok: false, error: auth.describeAuthError(error) };
  }
}

async function persistTokens(tokens: Record<string, unknown>): Promise<auth.AuthSession> {
  const session: auth.AuthSession = {
    accessToken: String(tokens.access_token ?? ''),
    refreshToken: String(tokens.refresh_token ?? ''),
    expiresAt: Date.now() + Number(tokens.expires_in ?? 0) * 1000,
    refreshExpiresAt: Date.now() + Number(tokens.refresh_expires_in ?? 0) * 1000,
    email: String(tokens.email ?? ''),
    userId: String(tokens.user_id ?? ''),
    organizationId: String(tokens.organization_id ?? ''),
    deviceId: tokens.device_id ? String(tokens.device_id) : null,
    roles: Array.isArray(tokens.roles) ? (tokens.roles as string[]) : [],
    noticeAcknowledged: Boolean(tokens.notice_acknowledged),
  };
  await auth.writeSession(session);
  return session;
}

async function refreshSession(): Promise<auth.AuthSession | null> {
  const session = await auth.readSession();
  if (!auth.refreshUsable(session) || !session) return null;
  const baseUrl = await apiBaseUrl();
  if (!baseUrl) return null;
  const api = new ApiClient({ baseUrl, getAccessToken: async () => null });
  try {
    const tokens = await api.exchange({
      grant_type: 'refresh_token',
      refresh_token: session.refreshToken,
    });
    return await persistTokens(tokens);
  } catch (error) {
    log.warn('refresh_failed', { reason: safeErrorMessage(error) });
    await auth.clearSession();
    await patchStatus({ signedIn: false });
    return null;
  }
}

async function registerDevice(api: ApiClient): Promise<void> {
  try {
    const response = await api.registerDevice({
      device_fingerprint: await deviceFingerprint(),
      extension_id: chrome.runtime.id,
      extension_version: chrome.runtime.getManifest().version,
      adapter_version: '2024.1',
      platform: navigator.userAgent.slice(0, 64),
      managed_by_policy: Boolean((await readManagedPolicy()).apiBaseUrl),
      notice_acknowledged: true,
    });
    log.info('device_registered', { revoked: response.revoked });
  } catch (error) {
    log.warn('device_registration_failed', { reason: safeErrorMessage(error) });
  }
}

// ---------------------------------------------------------------------------
// Capture handling
// ---------------------------------------------------------------------------

async function handleMessagesCaptured(
  conversationId: string,
  messages: ExtractedMessage[],
  workspace: WorkspaceRef,
  completeness: CaptureCompleteness,
): Promise<RuntimeResponse> {
  const config = cachedConfig ?? (await loadConfig());
  if (!captureAllowed(config)) {
    return { ok: false, error: policyBlockReason(config) ?? 'Capture is disabled.', code: 'policy' };
  }
  if (!workspace.verified || workspace.kind !== 'managed_company') {
    return { ok: false, error: 'Workspace not verified.', code: 'workspace' };
  }

  const api = await buildApi();
  if (!api) return { ok: false, error: 'No backend configured.' };
  const sync = await buildSync(api);

  const payloads = await normalizeMessages(messages, conversationId, 0);
  await sync.enqueueConversation(
    workspace,
    {
      sourceConversationId: conversationId,
      title: null,
      url: `https://chatgpt.com/c/${conversationId}`,
      modelSlug: messages.find((m) => m.modelSlug)?.modelSlug ?? null,
      messageCount: messages.length,
    },
    completeness,
  );
  const queued = await sync.enqueueMessages(workspace, payloads);
  await markConversationArchived(conversationId);

  const flush = await sync.flush();
  await afterFlush(flush.backpressure, null);
  return { ok: true, data: { queued, flushed: flush.succeeded } };
}

async function afterFlush(backpressure: boolean, error: string | null): Promise<ArchiveStatus> {
  return patchStatus({
    queueSize: await queue.size(),
    queueBytes: await queue.totalBytes(),
    lastSyncAt: error ? undefined : Date.now(),
    lastSyncError: error ?? (backpressure ? 'Server is busy; retrying shortly.' : null),
  });
}

async function flushNow(): Promise<RuntimeResponse> {
  const api = await buildApi();
  if (!api) return { ok: false, error: 'No backend configured.' };
  const sync = await buildSync(api);
  try {
    const result = await sync.flush(50);
    await afterFlush(result.backpressure, null);
    return { ok: true, data: result };
  } catch (error) {
    const message = safeErrorMessage(error);
    await afterFlush(false, message);
    return { ok: false, error: message };
  }
}

async function refreshRemoteStatus(): Promise<void> {
  const api = await buildApi();
  if (!api) return;
  const token = await accessToken();
  if (!token) return;
  try {
    const remote = await api.syncStatus();
    await patchStatus({
      archivedConversationCount: remote.archived_conversation_count,
      archivedMessageCount: remote.archived_message_count,
      coverageStatement: remote.coverage_statement,
      captureActive: remote.capture_enabled,
      killSwitch: remote.kill_switch,
      backendHealthy: true,
    });
  } catch (error) {
    await patchStatus({ backendHealthy: false, lastSyncError: safeErrorMessage(error) });
  }
}

// ---------------------------------------------------------------------------
// Runtime messaging
// ---------------------------------------------------------------------------

async function handleMessage(message: RuntimeMessage): Promise<RuntimeResponse> {
  switch (message.type) {
    case 'GET_STATUS': {
      const status = await readStatus();
      return {
        ok: true,
        data: {
          ...status,
          queueSize: await queue.size(),
          queueBytes: await queue.totalBytes(),
          archivedConversationIds: await archivedConversationIds(),
          policyBlockReason: policyBlockReason(cachedConfig),
        },
      };
    }
    case 'GET_CONFIG':
      return { ok: true, data: cachedConfig ?? (await loadConfig()) };
    case 'REFRESH_CONFIG': {
      const config = await loadConfig(true);
      await refreshRemoteStatus();
      await broadcastConfigToTabs();
      return { ok: true, data: config };
    }
    case 'SIGN_IN':
      return signIn();
    case 'SIGN_OUT':
      await auth.clearSession();
      await patchStatus({ signedIn: false, email: null });
      return { ok: true };
    case 'FLUSH_QUEUE':
      return flushNow();
    case 'CONTENT_READY':
      await loadConfig();
      return { ok: true, data: cachedConfig };
    case 'CONVERSATION_DETECTED': {
      const conversationId = message.conversation.sourceConversationId;
      if (!conversationId) return { ok: false, error: 'No conversation id.' };
      await patchStatus({
        currentConversationId: conversationId,
        workspaceVerified: true,
        workspaceLabel: message.workspace.label,
      });
      return handleMessagesCaptured(
        conversationId,
        message.conversation.messages,
        {
          source_workspace_id: message.workspace.sourceWorkspaceId,
          label: message.workspace.label,
          kind: 'managed_company',
          verified: true,
          verification_signals: message.workspace.signals,
        },
        message.completeness,
      );
    }
    case 'MESSAGES_CAPTURED':
      return handleMessagesCaptured(
        message.conversationId,
        message.messages,
        {
          source_workspace_id: message.workspace.sourceWorkspaceId,
          label: message.workspace.label,
          kind: 'managed_company',
          verified: true,
          verification_signals: message.workspace.signals,
        },
        message.completeness,
      );
    case 'ATTACHMENT_CAPTURED': {
      const config = cachedConfig ?? (await loadConfig());
      if (!captureAllowed(config) || !config?.policy.attachment_capture_enabled) {
        return { ok: false, error: 'Attachment capture is disabled.', code: 'policy' };
      }
      const api = await buildApi();
      if (!api) return { ok: false, error: 'No backend configured.' };
      const sync = await buildSync(api);
      if (!message.bytes) {
        await sync.recordMetadataOnlyAttachment({
          sourceConversationId: message.conversationId,
          sourceMessageId: message.sourceMessageId,
          clientAttachmentId: message.attachment.clientAttachmentId,
          filename: message.attachment.filename,
          mimeType: message.attachment.mimeType,
          byteSize: message.attachment.byteSize,
          relation:
            message.attachment.relation === 'generated_by_assistant'
              ? 'generated_by_assistant'
              : 'referenced_historical',
          sourceFileId: message.attachment.sourceFileId,
        });
        return { ok: true, data: { state: 'metadata_only' } };
      }
      const outcome = await sync.uploadAttachment({
        workspaceReady: true,
        sourceConversationId: message.conversationId,
        sourceMessageId: message.sourceMessageId,
        clientAttachmentId: message.attachment.clientAttachmentId,
        filename: message.attachment.filename,
        mimeType: message.attachment.mimeType ?? 'application/octet-stream',
        byteSize: message.attachment.byteSize ?? message.bytes.byteLength,
        sha256: message.sha256,
        bytes: message.bytes,
      });
      return { ok: outcome !== 'failed', data: { state: outcome } };
    }
    case 'ARCHIVE_CURRENT_CONVERSATION': {
      const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
      if (!tab?.id) return { ok: false, error: 'No active tab.' };
      try {
        const response = (await chrome.tabs.sendMessage(tab.id, {
          type: 'ARCHIVE_CURRENT_CONVERSATION',
        })) as RuntimeResponse | undefined;
        return response ?? { ok: false, error: 'The ChatGPT tab did not respond.' };
      } catch {
        return { ok: false, error: 'Open a company ChatGPT conversation first.' };
      }
    }
    case 'DIAGNOSTIC':
      log.info(`content:${message.event}`, message.detail);
      return { ok: true };
    default:
      return { ok: false, error: 'Unknown message type.' };
  }
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  handleMessage(message as RuntimeMessage)
    .then(sendResponse)
    .catch((error: unknown) => sendResponse({ ok: false, error: safeErrorMessage(error) }));
  return true; // keep the channel open for the async response
});

// ---------------------------------------------------------------------------
// Lifecycle
// ---------------------------------------------------------------------------

chrome.runtime.onInstalled.addListener(() => {
  void bootstrap();
});

chrome.runtime.onStartup.addListener(() => {
  void bootstrap();
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === FLUSH_ALARM) void flushNow();
  if (alarm.name === CONFIG_ALARM) {
    void loadConfig(true)
      .then(() => refreshRemoteStatus())
      .then(() => broadcastConfigToTabs());
  }
});

if (typeof self !== 'undefined' && 'addEventListener' in self) {
  self.addEventListener('online', () => {
    void flushNow();
  });
}

/**
 * Tell every open ChatGPT tab that the configuration changed.
 *
 * A content script that failed workspace verification parks itself and never
 * polls; this push is what revives it. Best-effort by design: a tab whose
 * content script is gone (or was never injected) rejects, and that is fine.
 */
async function broadcastConfigToTabs(): Promise<void> {
  try {
    const tabs = await chrome.tabs.query({
      url: ['https://chatgpt.com/*', 'https://chat.openai.com/*'],
    });
    await Promise.allSettled(
      tabs.map((tab) =>
        tab.id === undefined
          ? Promise.resolve()
          : chrome.tabs.sendMessage(tab.id, { type: 'REFRESH_CONFIG' }),
      ),
    );
  } catch (error) {
    log.debug('config_broadcast_failed', { reason: safeErrorMessage(error) });
  }
}

async function bootstrap(): Promise<void> {
  await chrome.alarms.create(FLUSH_ALARM, { periodInMinutes: FLUSH_PERIOD_MINUTES });
  await chrome.alarms.create(CONFIG_ALARM, { periodInMinutes: CONFIG_PERIOD_MINUTES });
  await loadConfig(true);
  await queue.enforceLimits();
  const session = await auth.readSession();
  await patchStatus({
    signedIn: auth.sessionValid(session),
    email: session?.email ?? null,
    queueSize: await queue.size(),
    queueBytes: await queue.totalBytes(),
  });
  log.info('service_worker_ready', { captureActive: captureAllowed(cachedConfig) });
}

void bootstrap();

/** Exposed for the options page diagnostics view. */
export function diagnosticsSnapshot(): Record<string, unknown> {
  return {
    extensionVersion: chrome.runtime.getManifest().version,
    configVersion: cachedConfig?.config_version ?? null,
    captureActive: captureAllowed(cachedConfig),
    logs: recentLogs(50),
  };
}
