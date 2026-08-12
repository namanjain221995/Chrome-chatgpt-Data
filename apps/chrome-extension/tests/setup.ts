/**
 * Test environment setup.
 *
 * Provides jsdom-compatible stand-ins for Web Crypto, IndexedDB and the parts
 * of the `chrome.*` API the extension uses, so the real modules run unmodified
 * against sanitized fixtures. No test ever contacts a live ChatGPT account.
 */

import { webcrypto } from 'node:crypto';
import 'fake-indexeddb/auto';
import { beforeEach, vi } from 'vitest';
import { setLogLevel } from '../src/shared/logging';

if (!globalThis.crypto?.subtle) {
  Object.defineProperty(globalThis, 'crypto', { value: webcrypto, configurable: true });
}

// jsdom does not implement Blob.arrayBuffer(), which every supported Chrome
// version has. Polyfilling it here keeps the production attachment path free of
// test-only branches.
if (typeof Blob !== 'undefined' && typeof Blob.prototype.arrayBuffer !== 'function') {
  Object.defineProperty(Blob.prototype, 'arrayBuffer', {
    configurable: true,
    writable: true,
    value(this: Blob): Promise<ArrayBuffer> {
      return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(reader.result as ArrayBuffer);
        reader.onerror = () => reject(reader.error ?? new Error('read failed'));
        reader.readAsArrayBuffer(this);
      });
    },
  });
}

export interface FakeStorageArea {
  get: (keys?: string | string[] | null) => Promise<Record<string, unknown>>;
  set: (items: Record<string, unknown>) => Promise<void>;
  remove: (keys: string | string[]) => Promise<void>;
  clear: () => Promise<void>;
  _data: Map<string, unknown>;
}

function makeStorageArea(initial: Record<string, unknown> = {}): FakeStorageArea {
  const data = new Map<string, unknown>(Object.entries(initial));
  return {
    _data: data,
    async get(keys) {
      if (keys === null || keys === undefined) return Object.fromEntries(data);
      const list = Array.isArray(keys) ? keys : [keys];
      const out: Record<string, unknown> = {};
      for (const key of list) if (data.has(key)) out[key] = data.get(key);
      return out;
    },
    async set(items) {
      for (const [key, value] of Object.entries(items)) data.set(key, value);
    },
    async remove(keys) {
      const list = Array.isArray(keys) ? keys : [keys];
      for (const key of list) data.delete(key);
    },
    async clear() {
      data.clear();
    },
  };
}

export interface FakeChrome {
  runtime: {
    id: string;
    getManifest: () => { version: string };
    sendMessage: ReturnType<typeof vi.fn>;
    onMessage: { addListener: ReturnType<typeof vi.fn> };
    onInstalled: { addListener: ReturnType<typeof vi.fn> };
    onStartup: { addListener: ReturnType<typeof vi.fn> };
    openOptionsPage: ReturnType<typeof vi.fn>;
    lastError?: { message: string };
  };
  storage: {
    local: FakeStorageArea;
    session: FakeStorageArea;
    managed: FakeStorageArea;
  };
  alarms: {
    create: ReturnType<typeof vi.fn>;
    onAlarm: { addListener: ReturnType<typeof vi.fn> };
  };
  identity: {
    getRedirectURL: (path?: string) => string;
    launchWebAuthFlow: ReturnType<typeof vi.fn>;
  };
  tabs: {
    query: ReturnType<typeof vi.fn>;
    sendMessage: ReturnType<typeof vi.fn>;
  };
}

export function installFakeChrome(managed: Record<string, unknown> = {}): FakeChrome {
  const fake: FakeChrome = {
    runtime: {
      id: 'abcdefghijklmnopabcdefghijklmnop',
      getManifest: () => ({ version: '1.0.0' }),
      sendMessage: vi.fn(async () => ({ ok: true })),
      onMessage: { addListener: vi.fn() },
      onInstalled: { addListener: vi.fn() },
      onStartup: { addListener: vi.fn() },
      openOptionsPage: vi.fn(),
    },
    storage: {
      local: makeStorageArea(),
      session: makeStorageArea(),
      managed: makeStorageArea(managed),
    },
    alarms: { create: vi.fn(async () => undefined), onAlarm: { addListener: vi.fn() } },
    identity: {
      getRedirectURL: (path = '') => `https://abcdefghijklmnopabcdefghijklmnop.chromiumapp.org/${path}`,
      launchWebAuthFlow: vi.fn(),
    },
    tabs: { query: vi.fn(async () => []), sendMessage: vi.fn(async () => ({ ok: true })) },
  };
  (globalThis as unknown as { chrome: FakeChrome }).chrome = fake;
  return fake;
}

// Keep the suite output readable: the ring buffer is still exercised, but the
// console mirror is silenced.
setLogLevel('error');

beforeEach(() => {
  installFakeChrome();
});
