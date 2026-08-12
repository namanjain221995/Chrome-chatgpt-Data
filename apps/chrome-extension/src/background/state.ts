/**
 * Service-worker state.
 *
 * An MV3 worker is evicted aggressively, so nothing important lives in memory
 * only: durable facts go to `chrome.storage.local`, credentials to
 * `chrome.storage.session`, and pending uploads to IndexedDB.
 */

import type { ArchiveStatus, RuntimeConfig } from '../shared/types';

const STATUS_KEY = 'archiveStatus';
const ARCHIVED_IDS_KEY = 'archivedConversationIds';
const MAX_TRACKED_IDS = 2000;

export const DEFAULT_STATUS: ArchiveStatus = {
  signedIn: false,
  email: null,
  workspaceVerified: false,
  workspaceLabel: null,
  currentConversationId: null,
  captureActive: false,
  killSwitch: false,
  lastSyncAt: null,
  lastSyncError: null,
  queueSize: 0,
  queueBytes: 0,
  backendHealthy: false,
  archivedConversationCount: 0,
  archivedMessageCount: 0,
  archivedConversationIds: [],
  coverageStatement:
    'This extension archives the conversation you currently have open and every new message ' +
    'you send or receive in the company workspace. It does not archive conversations you ' +
    'never open in this browser, and it never captures unsent drafts.',
  privacyNoticeUrl: '',
  configVersion: null,
  policyBlockReason: 'Waiting for company configuration.',
};

export async function readStatus(): Promise<ArchiveStatus> {
  const stored = await chrome.storage.local.get(STATUS_KEY);
  const status = stored?.[STATUS_KEY] as Partial<ArchiveStatus> | undefined;
  return { ...DEFAULT_STATUS, ...(status ?? {}) };
}

export async function patchStatus(patch: Partial<ArchiveStatus>): Promise<ArchiveStatus> {
  const current = await readStatus();
  const next = { ...current, ...patch };
  await chrome.storage.local.set({ [STATUS_KEY]: next });
  return next;
}

/**
 * Conversations this browser profile has archived at least once.
 *
 * Used by the Historical Archive Progress page. It is explicitly *not* a claim
 * that all workspace history is archived — only what this device has seen.
 */
export async function markConversationArchived(sourceConversationId: string): Promise<number> {
  const stored = await chrome.storage.local.get(ARCHIVED_IDS_KEY);
  const ids = new Set<string>((stored?.[ARCHIVED_IDS_KEY] as string[] | undefined) ?? []);
  ids.add(sourceConversationId);
  const trimmed = Array.from(ids).slice(-MAX_TRACKED_IDS);
  await chrome.storage.local.set({ [ARCHIVED_IDS_KEY]: trimmed });
  return trimmed.length;
}

export async function archivedConversationIds(): Promise<string[]> {
  const stored = await chrome.storage.local.get(ARCHIVED_IDS_KEY);
  return (stored?.[ARCHIVED_IDS_KEY] as string[] | undefined) ?? [];
}

export async function isConversationArchived(sourceConversationId: string): Promise<boolean> {
  const ids = await archivedConversationIds();
  return ids.includes(sourceConversationId);
}

export function statusFromConfig(
  status: ArchiveStatus,
  config: RuntimeConfig | null,
): ArchiveStatus {
  if (!config) return status;
  return {
    ...status,
    captureActive: config.policy.capture_active,
    killSwitch: config.policy.kill_switch,
    coverageStatement: config.coverage_statement,
    privacyNoticeUrl: config.privacy_notice_url,
    configVersion: config.config_version,
  };
}
