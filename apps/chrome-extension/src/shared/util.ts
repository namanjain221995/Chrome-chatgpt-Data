/** Small dependency-free helpers shared by every module. */

import type { ClientContext } from './types';
import { SCHEMA_VERSION } from './types';

export const ADAPTER_VERSION = '2026.1';

/**
 * SHA-256 as lowercase hex, using Web Crypto (available in MV3 workers).
 *
 * The input is always normalised into a freshly allocated `Uint8Array` before
 * hashing. Besides being cheap, this sidesteps cross-realm buffers — a
 * `File.arrayBuffer()` result can originate from a different JS realm, and
 * `crypto.subtle.digest` rejects such a value with an opaque type error.
 */
export async function sha256Hex(input: string | ArrayBuffer | Uint8Array): Promise<string> {
  let bytes: Uint8Array;
  if (typeof input === 'string') {
    bytes = new TextEncoder().encode(input);
  } else if (ArrayBuffer.isView(input)) {
    const view = input as Uint8Array;
    bytes = new Uint8Array(view.byteLength);
    bytes.set(view);
  } else {
    const source = new Uint8Array(input);
    bytes = new Uint8Array(source.byteLength);
    for (let index = 0; index < source.byteLength; index += 1) {
      bytes[index] = source[index] as number;
    }
  }
  const digest = await crypto.subtle.digest('SHA-256', bytes);
  return Array.from(new Uint8Array(digest))
    .map((byte) => byte.toString(16).padStart(2, '0'))
    .join('');
}

export function randomId(prefix = ''): string {
  const id = crypto.randomUUID().replace(/-/g, '');
  return prefix ? `${prefix}-${id}` : id;
}

/**
 * Deterministic idempotency key.
 *
 * Derived from stable identity material rather than a random value, so an
 * offline retry after a service-worker restart produces the same key and the
 * backend recognises the replay instead of storing a duplicate.
 */
export async function idempotencyKey(parts: Array<string | number | null | undefined>): Promise<string> {
  const material = parts.map((part) => String(part ?? '')).join('|');
  const digest = await sha256Hex(material);
  return `k-${digest.slice(0, 40)}`;
}

export function nowIso(): string {
  return new Date().toISOString();
}

export function clientContext(
  extensionVersion: string,
  deviceFingerprint?: string | null,
): ClientContext {
  return {
    extension_version: extensionVersion,
    adapter_version: ADAPTER_VERSION,
    schema_version: SCHEMA_VERSION,
    device_fingerprint: deviceFingerprint ?? null,
    page_locale: typeof navigator !== 'undefined' ? navigator.language?.slice(0, 16) : null,
    captured_at: nowIso(),
  };
}

/** Exponential backoff with full jitter, capped. */
export function backoffMs(attempt: number, baseMs = 2000, capMs = 15 * 60 * 1000): number {
  const ceiling = Math.min(baseMs * 2 ** Math.min(attempt, 12), capMs);
  return Math.floor(baseMs + Math.random() * Math.max(1, ceiling - baseMs));
}

export function debounce<A extends unknown[]>(
  fn: (...args: A) => void,
  waitMs: number,
): ((...args: A) => void) & { cancel: () => void; flush: () => void } {
  let timer: ReturnType<typeof setTimeout> | null = null;
  let lastArgs: A | null = null;

  const wrapped = (...args: A): void => {
    lastArgs = args;
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => {
      timer = null;
      const call = lastArgs;
      lastArgs = null;
      if (call) fn(...call);
    }, waitMs);
  };

  wrapped.cancel = (): void => {
    if (timer) clearTimeout(timer);
    timer = null;
    lastArgs = null;
  };

  wrapped.flush = (): void => {
    if (timer) clearTimeout(timer);
    timer = null;
    const call = lastArgs;
    lastArgs = null;
    if (call) fn(...call);
  };

  return wrapped;
}

export function byteLengthOf(value: unknown): number {
  try {
    return new TextEncoder().encode(JSON.stringify(value)).length;
  } catch {
    return 0;
  }
}

export function chunk<T>(items: T[], size: number): T[][] {
  if (size <= 0) return [items];
  const output: T[][] = [];
  for (let index = 0; index < items.length; index += size) {
    output.push(items.slice(index, index + size));
  }
  return output;
}

/** Compare dotted versions; returns true when `version` >= `minimum`. */
export function versionAtLeast(version: string, minimum: string): boolean {
  const toParts = (value: string): number[] =>
    value.split(/[.+-]/).map((part) => Number.parseInt(part, 10) || 0);
  const left = toParts(version);
  const right = toParts(minimum);
  const length = Math.max(left.length, right.length);
  for (let index = 0; index < length; index += 1) {
    const a = left[index] ?? 0;
    const b = right[index] ?? 0;
    if (a > b) return true;
    if (a < b) return false;
  }
  return true;
}

export function normalizeWhitespace(value: string): string {
  // U+00A0 is a non-breaking space. ChatGPT renders them freely, but they defeat
  // whitespace-insensitive comparison, so they are folded to a plain space
  // before any hashing or fingerprinting happens.
  return value
    .replace(new RegExp('[\\u00a0\\u2007\\u202f]', 'g'), ' ')
    .replace(/[ \t]+\n/g, '\n')
    .replace(/\n{3,}/g, '\n\n')
    .trim();
}

export async function sleep(ms: number): Promise<void> {
  await new Promise<void>((resolve) => setTimeout(resolve, ms));
}
