/**
 * Managed policy + signed server configuration.
 *
 * `chrome.storage.managed` tells the extension *where* the company backend is.
 * The backend then returns a signed, versioned configuration that decides
 * *whether* anything may be captured. There is deliberately no local setting
 * that can widen capture: a `false` from the server always wins, and a missing
 * or expired configuration means "capture nothing".
 */

import type { ManagedPolicy, RuntimeConfig, SignedRuntimeConfig } from '../shared/types';
import { log, safeErrorMessage } from '../shared/logging';

const CONFIG_CACHE_KEY = 'runtimeConfig';
const CONFIG_FETCHED_AT_KEY = 'runtimeConfigFetchedAt';
const DEFAULT_TTL_MS = 15 * 60 * 1000;

export interface ConfigState {
  config: RuntimeConfig | null;
  fetchedAt: number | null;
  stale: boolean;
  source: 'server' | 'cache' | 'none';
  error?: string;
}

export async function readManagedPolicy(): Promise<ManagedPolicy> {
  try {
    const managed = await chrome.storage.managed.get(null);
    return (managed ?? {}) as ManagedPolicy;
  } catch (error) {
    // No policy configured (common in development) is not an error.
    log.debug('managed_policy_unavailable', { reason: safeErrorMessage(error) });
    return {};
  }
}

export function resolveApiBaseUrl(policy: ManagedPolicy, fallback?: string): string | null {
  const candidate = policy.apiBaseUrl ?? fallback ?? null;
  if (!candidate) return null;
  try {
    const url = new URL(candidate);
    // The backend must be HTTPS; a downgrade would expose employee content.
    if (url.protocol !== 'https:' && url.hostname !== 'localhost' && url.hostname !== '127.0.0.1') {
      log.warn('managed_policy_insecure_url');
      return null;
    }
    return url.toString().replace(/\/+$/, '');
  } catch {
    log.warn('managed_policy_invalid_url');
    return null;
  }
}

function isExpired(config: RuntimeConfig, now: number): boolean {
  const expires = Date.parse(config.expires_at);
  return Number.isFinite(expires) ? expires <= now : true;
}

export async function readCachedConfig(now = Date.now()): Promise<ConfigState> {
  try {
    const stored = await chrome.storage.local.get([CONFIG_CACHE_KEY, CONFIG_FETCHED_AT_KEY]);
    const cached = stored[CONFIG_CACHE_KEY] as SignedRuntimeConfig | undefined;
    const fetchedAt = (stored[CONFIG_FETCHED_AT_KEY] as number | undefined) ?? null;
    if (!cached?.config) return { config: null, fetchedAt: null, stale: true, source: 'none' };
    return {
      config: cached.config,
      fetchedAt,
      stale: isExpired(cached.config, now) || (fetchedAt ?? 0) + DEFAULT_TTL_MS < now,
      source: 'cache',
    };
  } catch (error) {
    return {
      config: null,
      fetchedAt: null,
      stale: true,
      source: 'none',
      error: safeErrorMessage(error),
    };
  }
}

export async function writeCachedConfig(signed: SignedRuntimeConfig): Promise<void> {
  await chrome.storage.local.set({
    [CONFIG_CACHE_KEY]: signed,
    [CONFIG_FETCHED_AT_KEY]: Date.now(),
  });
}

export async function clearCachedConfig(): Promise<void> {
  await chrome.storage.local.remove([CONFIG_CACHE_KEY, CONFIG_FETCHED_AT_KEY]);
}

/**
 * Shape validation for a configuration document.
 *
 * The HMAC signature is verified server-side on every authenticated call; the
 * client cannot hold the signing key. What the client *can* do is refuse a
 * document that is malformed, expired, or that tries to enable capture without
 * both gates being true — which is exactly what this check enforces.
 */
export function isUsableConfig(signed: SignedRuntimeConfig | null, now = Date.now()): boolean {
  if (!signed?.config || !signed.signature) return false;
  const config = signed.config;
  if (config.schema_version !== '1.0') return false;
  if (!Number.isFinite(config.config_version) || config.config_version < 1) return false;
  if (isExpired(config, now)) return false;
  if (!config.api_base_url.startsWith('https://') && !config.api_base_url.includes('localhost')) {
    return false;
  }
  const policy = config.policy;
  if (policy.personal_workspace_capture_enabled !== false) return false;
  if (policy.capture_unsent_drafts !== false) return false;
  // capture_active must imply both gates: a doctored cache cannot open capture.
  if (
    policy.capture_active &&
    !(policy.browser_content_capture_enabled && policy.openai_written_authorization_confirmed)
  ) {
    return false;
  }
  if (policy.capture_active && policy.kill_switch) return false;
  return true;
}

/** The effective decision: capture only when the server says so, right now. */
export function captureAllowed(config: RuntimeConfig | null, now = Date.now()): boolean {
  if (!config) return false;
  if (isExpired(config, now)) return false;
  const policy = config.policy;
  return (
    policy.capture_active &&
    policy.browser_content_capture_enabled &&
    policy.openai_written_authorization_confirmed &&
    !policy.kill_switch
  );
}

export function policyBlockReason(config: RuntimeConfig | null, now = Date.now()): string | null {
  if (!config) return 'Waiting for company configuration.';
  if (isExpired(config, now)) return 'Company configuration expired; reconnecting.';
  const policy = config.policy;
  if (policy.kill_switch) return 'Archiving is paused by your administrator.';
  if (!policy.browser_content_capture_enabled) {
    return 'Browser archiving is disabled by your administrator.';
  }
  if (!policy.openai_written_authorization_confirmed) {
    return 'Awaiting written authorization confirmation from your administrator.';
  }
  if (!policy.capture_active) return 'Archiving is not active.';
  return null;
}
