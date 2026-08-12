/**
 * Offline queue bounds and retry behaviour, attachment capture, PKCE auth and
 * the safety of support diagnostics.
 */

import { beforeEach, describe, expect, it } from 'vitest';
import { AttachmentObserver } from '../src/modules/attachment-observer';
import { OfflineQueue } from '../src/modules/offline-queue';
import { ApiClient, ApiError } from '../src/modules/api-client';
import * as auth from '../src/modules/auth-client';
import { log, recentLogs, clearLogs, safeErrorMessage, sanitizeDetail } from '../src/shared/logging';
import { backoffMs, idempotencyKey, sha256Hex, versionAtLeast } from '../src/shared/util';
import { installFakeChrome } from './setup';

function makeQueue(overrides = {}): OfflineQueue {
  return new OfflineQueue({ maxItems: 100, maxBytes: 100_000, maxAgeDays: 7, ...overrides });
}

beforeEach(async () => {
  const queue = makeQueue();
  await queue.clear();
  queue.close();
  clearLogs();
});

describe('offline queue', () => {
  it('stores and returns due items in order', async () => {
    const queue = makeQueue();
    await queue.enqueue('message_batch', { a: 1 }, 'key-1');
    await queue.enqueue('message_batch', { a: 2 }, 'key-2');
    const due = await queue.takeDue(10);
    expect(due).toHaveLength(2);
    expect(await queue.size()).toBe(2);
  });

  it('ignores a duplicate idempotency key', async () => {
    const queue = makeQueue();
    const first = await queue.enqueue('message_batch', { a: 1 }, 'same-key');
    const second = await queue.enqueue('message_batch', { a: 1 }, 'same-key');
    expect(first).not.toBeNull();
    expect(second).toBeNull();
    expect(await queue.size()).toBe(1);
  });

  it('reschedules a failure with growing backoff', async () => {
    const queue = makeQueue();
    const item = await queue.enqueue('message_batch', { a: 1 }, 'retry-key');
    expect(item).not.toBeNull();

    const outcome = await queue.reschedule(item!, 'network down');
    expect(outcome).toBe('retry');

    const all = await queue.peekAll();
    expect(all[0]?.attempts).toBe(1);
    expect(all[0]?.nextAttemptAt).toBeGreaterThan(Date.now());
    expect(all[0]?.lastError).toContain('network down');
    // Not yet due, so a flush will not pick it up.
    expect(await queue.takeDue(10)).toHaveLength(0);
  });

  it('drops an item once attempts are exhausted', async () => {
    const queue = makeQueue({ maxAttempts: 2 });
    const item = await queue.enqueue('message_batch', { a: 1 }, 'doomed');
    let current = item!;
    expect(await queue.reschedule(current, 'fail 1')).toBe('retry');
    current = (await queue.peekAll())[0]!;
    expect(await queue.reschedule(current, 'fail 2')).toBe('dropped');
    expect(await queue.size()).toBe(0);
  });

  it('enforces the item-count bound by evicting the oldest', async () => {
    const queue = makeQueue({ maxItems: 3 });
    for (let index = 0; index < 6; index += 1) {
      await queue.enqueue('message_batch', { index }, `key-${index}`);
    }
    expect(await queue.size()).toBe(3);
    const remaining = (await queue.peekAll()).map((i) => (i.payload as { index: number }).index);
    expect(remaining).toEqual([3, 4, 5]);
  });

  it('enforces the byte bound', async () => {
    const queue = makeQueue({ maxBytes: 400 });
    for (let index = 0; index < 10; index += 1) {
      await queue.enqueue('message_batch', { blob: 'x'.repeat(100), index }, `key-${index}`);
    }
    expect(await queue.totalBytes()).toBeLessThanOrEqual(400);
  });

  it('refuses a single item larger than the whole budget', async () => {
    const queue = makeQueue({ maxBytes: 100 });
    const item = await queue.enqueue('message_batch', { blob: 'x'.repeat(5000) }, 'huge');
    expect(item).toBeNull();
  });

  it('evicts items older than the age bound', async () => {
    const queue = makeQueue({ maxAgeDays: 1 });
    await queue.enqueue('message_batch', { a: 1 }, 'old-key');
    const future = Date.now() + 3 * 24 * 60 * 60 * 1000;
    const evicted = await queue.enforceLimits(future);
    expect(evicted).toBe(1);
    expect(await queue.size()).toBe(0);
  });

  it('survives being reopened (durable across worker restarts)', async () => {
    const first = makeQueue();
    await first.enqueue('message_batch', { a: 1 }, 'persist-key');
    first.close();

    const second = makeQueue();
    expect(await second.size()).toBe(1);
  });
});

describe('backoff', () => {
  it('grows with attempts and stays capped', () => {
    expect(backoffMs(0)).toBeLessThanOrEqual(15 * 60 * 1000);
    expect(backoffMs(30)).toBeLessThanOrEqual(15 * 60 * 1000);
  });

  it('includes jitter so clients do not retry in lockstep', () => {
    const values = new Set(Array.from({ length: 30 }, () => backoffMs(5)));
    expect(values.size).toBeGreaterThan(1);
  });
});

describe('attachment observer', () => {
  function makeFile(name: string, type: string, contents = 'hello'): File {
    return new File([contents], name, { type });
  }

  it('captures a file chosen through a file input', async () => {
    const observer = new AttachmentObserver({ conversationId: () => 'conv-1' });
    const captured: string[] = [];
    observer.onAttachment((attachment) => {
      captured.push(attachment.ref.filename);
    });
    observer.start();

    const input = document.createElement('input');
    input.type = 'file';
    document.body.appendChild(input);
    const file = makeFile('diagram.png', 'image/png');
    Object.defineProperty(input, 'files', { value: [file], configurable: true });
    input.dispatchEvent(new Event('change', { bubbles: true }));

    await new Promise((r) => setTimeout(r, 20));
    expect(captured).toEqual(['diagram.png']);
    observer.stop();
  });

  it('captures a pasted file but ignores pasted text', async () => {
    const observer = new AttachmentObserver({ conversationId: () => 'conv-1' });
    const captured: string[] = [];
    observer.onAttachment((attachment) => {
      captured.push(attachment.ref.filename);
    });
    observer.start();

    const file = makeFile('shot.png', 'image/png');
    const event = new Event('paste', { bubbles: true }) as ClipboardEvent;
    Object.defineProperty(event, 'clipboardData', {
      value: {
        items: [
          { kind: 'string', getAsFile: () => null },
          { kind: 'file', getAsFile: () => file },
        ],
      },
    });
    document.dispatchEvent(event);

    await new Promise((r) => setTimeout(r, 20));
    expect(captured).toEqual(['shot.png']);
    observer.stop();
  });

  it('captures a dropped file', async () => {
    const observer = new AttachmentObserver({ conversationId: () => 'conv-1' });
    const captured: string[] = [];
    observer.onAttachment((attachment) => {
      captured.push(attachment.ref.filename);
    });
    observer.start();

    const event = new Event('drop', { bubbles: true }) as DragEvent;
    Object.defineProperty(event, 'dataTransfer', {
      value: { files: [makeFile('notes.pdf', 'application/pdf')] },
    });
    document.dispatchEvent(event);

    await new Promise((r) => setTimeout(r, 20));
    expect(captured).toEqual(['notes.pdf']);
    observer.stop();
  });

  it('computes the SHA-256 of the exact bytes', async () => {
    const observer = new AttachmentObserver({ conversationId: () => 'conv-1' });
    let digest = '';
    observer.onAttachment((attachment) => {
      digest = attachment.sha256;
    });
    observer.start();

    const event = new Event('drop', { bubbles: true }) as DragEvent;
    Object.defineProperty(event, 'dataTransfer', {
      value: { files: [makeFile('a.txt', 'text/plain', 'abc')] },
    });
    document.dispatchEvent(event);
    await new Promise((r) => setTimeout(r, 20));

    expect(digest).toBe(await sha256Hex('abc'));
  });

  it('does not capture the same file twice', async () => {
    const observer = new AttachmentObserver({ conversationId: () => 'conv-1' });
    let count = 0;
    observer.onAttachment(() => {
      count += 1;
    });
    observer.start();

    for (let i = 0; i < 3; i += 1) {
      const event = new Event('drop', { bubbles: true }) as DragEvent;
      Object.defineProperty(event, 'dataTransfer', {
        value: { files: [makeFile('same.png', 'image/png', 'identical')] },
      });
      document.dispatchEvent(event);
      await new Promise((r) => setTimeout(r, 10));
    }
    expect(count).toBe(1);
    observer.stop();
  });

  it('refuses a file above the configured size limit', async () => {
    const observer = new AttachmentObserver({ maxBytes: 4, conversationId: () => 'conv-1' });
    let count = 0;
    observer.onAttachment(() => {
      count += 1;
    });
    observer.start();

    const event = new Event('drop', { bubbles: true }) as DragEvent;
    Object.defineProperty(event, 'dataTransfer', {
      value: { files: [makeFile('big.png', 'image/png', 'way too many bytes')] },
    });
    document.dispatchEvent(event);
    await new Promise((r) => setTimeout(r, 20));
    expect(count).toBe(0);
    observer.stop();
  });

  it('refuses a MIME type outside the allowlist', async () => {
    const observer = new AttachmentObserver({
      allowedMimeTypes: ['image/png'],
      conversationId: () => 'conv-1',
    });
    let count = 0;
    observer.onAttachment(() => {
      count += 1;
    });
    observer.start();

    const event = new Event('drop', { bubbles: true }) as DragEvent;
    Object.defineProperty(event, 'dataTransfer', {
      value: { files: [makeFile('run.exe', 'application/x-msdownload')] },
    });
    document.dispatchEvent(event);
    await new Promise((r) => setTimeout(r, 20));
    expect(count).toBe(0);
    observer.stop();
  });

  it('stops listening after stop()', async () => {
    const observer = new AttachmentObserver({ conversationId: () => 'conv-1' });
    let count = 0;
    observer.onAttachment(() => {
      count += 1;
    });
    observer.start();
    observer.stop();

    const event = new Event('drop', { bubbles: true }) as DragEvent;
    Object.defineProperty(event, 'dataTransfer', {
      value: { files: [makeFile('after.png', 'image/png')] },
    });
    document.dispatchEvent(event);
    await new Promise((r) => setTimeout(r, 20));
    expect(count).toBe(0);
  });

  it('supports cancellation before the listener runs', async () => {
    const observer = new AttachmentObserver({ conversationId: () => 'conv-1' });
    observer.cancel('att-known');
    expect(observer.isCancelled('att-known')).toBe(true);
    expect(observer.isCancelled('att-other')).toBe(false);
  });
});

describe('auth client (PKCE)', () => {
  beforeEach(() => {
    installFakeChrome({});
  });

  it('builds an authorization url with a challenge, state and nonce', async () => {
    const { url, pkce } = await auth.buildAuthorizationUrl({
      clientId: 'client-123',
      redirectUri: 'https://ext.chromiumapp.org/oidc',
      hostedDomain: 'example.com',
    });
    const parsed = new URL(url);
    expect(parsed.searchParams.get('client_id')).toBe('client-123');
    expect(parsed.searchParams.get('code_challenge_method')).toBe('S256');
    expect(parsed.searchParams.get('code_challenge')).toBe(
      await auth.pkceChallenge(pkce.verifier),
    );
    expect(parsed.searchParams.get('state')).toBe(pkce.state);
    expect(parsed.searchParams.get('nonce')).toBe(pkce.nonce);
    expect(parsed.searchParams.get('hd')).toBe('example.com');
    // The verifier itself must never be in the URL.
    expect(url).not.toContain(pkce.verifier);
  });

  it('accepts a redirect whose state matches', async () => {
    const { pkce } = await auth.buildAuthorizationUrl({
      clientId: 'c',
      redirectUri: 'https://ext.chromiumapp.org/oidc',
    });
    const result = await auth.parseRedirect(
      `https://ext.chromiumapp.org/oidc?code=abc&state=${encodeURIComponent(pkce.state)}`,
    );
    expect(result.code).toBe('abc');
  });

  it('rejects a redirect whose state does not match', async () => {
    await auth.buildAuthorizationUrl({ clientId: 'c', redirectUri: 'https://x/oidc' });
    await expect(
      auth.parseRedirect('https://x/oidc?code=abc&state=forged'),
    ).rejects.toThrow(/state mismatch/i);
  });

  it('rejects an error redirect', async () => {
    await auth.buildAuthorizationUrl({ clientId: 'c', redirectUri: 'https://x/oidc' });
    await expect(auth.parseRedirect('https://x/oidc?error=access_denied')).rejects.toThrow(
      /refused/i,
    );
  });

  it('treats an expired session as signed out', () => {
    const session: auth.AuthSession = {
      accessToken: 'a',
      refreshToken: 'r',
      expiresAt: Date.now() - 1000,
      refreshExpiresAt: Date.now() + 100000,
      email: 'a@example.com',
      userId: 'u',
      organizationId: 'o',
      deviceId: null,
      roles: [],
      noticeAcknowledged: true,
    };
    expect(auth.sessionValid(session)).toBe(false);
    expect(auth.refreshUsable(session)).toBe(true);
  });

  it('stores the session in session storage, never in page storage', async () => {
    const fake = installFakeChrome({});
    await auth.writeSession({
      accessToken: 'secret-token',
      refreshToken: 'secret-refresh',
      expiresAt: Date.now() + 1000,
      refreshExpiresAt: Date.now() + 1000,
      email: 'a@example.com',
      userId: 'u',
      organizationId: 'o',
      deviceId: null,
      roles: [],
      noticeAcknowledged: true,
    });
    expect(fake.storage.session._data.has('authSession')).toBe(true);
    expect(fake.storage.local._data.has('authSession')).toBe(false);
  });
});

describe('api client', () => {
  it('sends the bearer token and a correlation id', async () => {
    const calls: Array<{ url: string; init: RequestInit }> = [];
    const client = new ApiClient({
      baseUrl: 'https://archive.example.com',
      getAccessToken: async () => 'token-abc',
      fetchImpl: (async (url: string, init: RequestInit) => {
        calls.push({ url, init });
        return new Response(JSON.stringify({ ok: true }), { status: 200 });
      }) as unknown as typeof fetch,
    });
    await client.syncStatus();

    const headers = new Headers(calls[0]?.init.headers);
    expect(headers.get('Authorization')).toBe('Bearer token-abc');
    expect(headers.get('X-Correlation-Id')).toBeTruthy();
    expect(calls[0]?.init.credentials).toBe('omit');
  });

  it('classifies 429 and 5xx as retryable', async () => {
    const client = new ApiClient({
      baseUrl: 'https://archive.example.com',
      getAccessToken: async () => 'token',
      fetchImpl: (async () =>
        new Response(JSON.stringify({ error: { code: 'rate_limited' } }), {
          status: 429,
          headers: { 'Retry-After': '30' },
        })) as unknown as typeof fetch,
    });
    await expect(client.syncStatus()).rejects.toMatchObject({
      retryable: true,
      status: 429,
      retryAfterSeconds: 30,
    });
  });

  it('classifies a policy rejection as terminal', async () => {
    const client = new ApiClient({
      baseUrl: 'https://archive.example.com',
      getAccessToken: async () => 'token',
      fetchImpl: (async () =>
        new Response(JSON.stringify({ error: { code: 'personal_workspace_blocked' } }), {
          status: 403,
        })) as unknown as typeof fetch,
    });
    await expect(client.syncStatus()).rejects.toMatchObject({
      retryable: false,
      code: 'personal_workspace_blocked',
    });
  });

  it('never sends the backend token to storage hosts', async () => {
    const calls: Array<Record<string, unknown>> = [];
    const client = new ApiClient({
      baseUrl: 'https://archive.example.com',
      getAccessToken: async () => 'secret-token',
      fetchImpl: (async (url: string, init: RequestInit) => {
        calls.push({ url, headers: init.headers });
        return new Response('', { status: 200 });
      }) as unknown as typeof fetch,
    });
    await client.uploadToStorage(
      'https://bucket.s3.amazonaws.com/key?sig=x',
      { 'Content-Type': 'image/png' },
      new ArrayBuffer(8),
    );
    expect(JSON.stringify(calls)).not.toContain('secret-token');
  });

  it('reports a network failure as retryable', async () => {
    const client = new ApiClient({
      baseUrl: 'https://archive.example.com',
      getAccessToken: async () => 'token',
      fetchImpl: (async () => {
        throw new Error('connection refused');
      }) as unknown as typeof fetch,
    });
    await expect(client.syncStatus()).rejects.toBeInstanceOf(ApiError);
  });
});

describe('diagnostics safety', () => {
  it('redacts credentials and message content from logs', () => {
    log.info('captured', {
      text: 'confidential prompt text',
      access_token: 'secret',
      count: 3,
    });
    const entry = recentLogs(1)[0];
    expect(entry?.detail.text).toBe('[redacted]');
    expect(entry?.detail.access_token).toBe('[redacted]');
    expect(entry?.detail.count).toBe(3);
  });

  it('bounds arrays and deep objects', () => {
    const detail = sanitizeDetail({ items: [1, 2, 3], nested: { a: { b: { c: { d: 1 } } } } });
    expect(detail.items).toEqual({ arrayLength: 3 });
    expect(JSON.stringify(detail)).not.toContain('"d"');
  });

  it('scrubs token-shaped strings from error messages', () => {
    const message = safeErrorMessage(
      new Error('failed with Bearer eyJhbGciOi.eyJzdWIiOi.signature'),
    );
    expect(message).not.toContain('eyJhbGciOi');
  });

  it('keeps the ring buffer bounded', () => {
    for (let index = 0; index < 500; index += 1) log.info(`event-${index}`);
    expect(recentLogs(1000).length).toBeLessThanOrEqual(200);
  });
});

describe('shared utilities', () => {
  it('derives a deterministic idempotency key', async () => {
    expect(await idempotencyKey(['a', 1, null])).toBe(await idempotencyKey(['a', 1, null]));
    expect(await idempotencyKey(['a'])).not.toBe(await idempotencyKey(['b']));
  });

  it('compares versions correctly', () => {
    expect(versionAtLeast('1.2.0', '1.0.0')).toBe(true);
    expect(versionAtLeast('1.0.0', '1.0.0')).toBe(true);
    expect(versionAtLeast('0.9.9', '1.0.0')).toBe(false);
  });
});
