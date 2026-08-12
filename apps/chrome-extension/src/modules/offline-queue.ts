/**
 * Bounded offline queue backed by IndexedDB.
 *
 * Why IndexedDB and not `chrome.storage.sync`: message bodies must never be
 * synchronised to a personal Google account, and `sync` has hard per-item size
 * limits. IndexedDB stays on the device, inside the browser profile, and is
 * covered by the browser's at-rest protection.
 *
 * Bounds are enforced on three axes — item count, total bytes and age — with
 * the oldest items evicted first, so a long offline period cannot fill the disk.
 */

import type { QueueItem, QueueItemKind } from '../shared/types';
import { log } from '../shared/logging';
import { backoffMs, byteLengthOf, randomId } from '../shared/util';

const DB_NAME = 'techsara-archive-queue';
const DB_VERSION = 1;
const STORE = 'items';

export interface QueueLimits {
  maxItems: number;
  maxBytes: number;
  maxAgeDays: number;
  maxAttempts: number;
}

export const DEFAULT_QUEUE_LIMITS: QueueLimits = {
  maxItems: 10_000,
  maxBytes: 50 * 1024 * 1024,
  maxAgeDays: 7,
  maxAttempts: 12,
};

/**
 * Monotonic tie-breaker.
 *
 * Several items can be enqueued inside the same millisecond, so `createdAt`
 * alone does not define an eviction order. `seq` makes "oldest first"
 * deterministic.
 */
let sequenceCounter = 0;

function nextSequence(): number {
  sequenceCounter += 1;
  return sequenceCounter;
}

/** Oldest-first comparator. A composite number would lose precision: an epoch
 * millisecond scaled by 1e6 exceeds Number.MAX_SAFE_INTEGER. */
function byAge(a: { createdAt: number; seq?: number }, b: { createdAt: number; seq?: number }): number {
  if (a.createdAt !== b.createdAt) return a.createdAt - b.createdAt;
  return (a.seq ?? 0) - (b.seq ?? 0);
}

function promisify<T>(request: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error ?? new Error('IndexedDB request failed'));
  });
}

export class OfflineQueue {
  private db: IDBDatabase | null = null;
  private limits: QueueLimits;

  constructor(limits: Partial<QueueLimits> = {}, private readonly factory: IDBFactory = indexedDB) {
    this.limits = { ...DEFAULT_QUEUE_LIMITS, ...limits };
  }

  configure(limits: Partial<QueueLimits>): void {
    this.limits = { ...this.limits, ...limits };
  }

  async open(): Promise<IDBDatabase> {
    if (this.db) return this.db;
    this.db = await new Promise<IDBDatabase>((resolve, reject) => {
      const request = this.factory.open(DB_NAME, DB_VERSION);
      request.onupgradeneeded = () => {
        const db = request.result;
        if (!db.objectStoreNames.contains(STORE)) {
          const store = db.createObjectStore(STORE, { keyPath: 'id' });
          store.createIndex('nextAttemptAt', 'nextAttemptAt');
          store.createIndex('createdAt', 'createdAt');
          store.createIndex('idempotencyKey', 'idempotencyKey', { unique: false });
        }
      };
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error ?? new Error('Unable to open queue database'));
    });
    return this.db;
  }

  close(): void {
    this.db?.close();
    this.db = null;
  }

  private async tx(mode: IDBTransactionMode): Promise<IDBObjectStore> {
    const db = await this.open();
    return db.transaction(STORE, mode).objectStore(STORE);
  }

  async enqueue(
    kind: QueueItemKind,
    payload: unknown,
    idempotencyKey: string,
  ): Promise<QueueItem | null> {
    const byteSize = byteLengthOf(payload);
    if (byteSize > this.limits.maxBytes) {
      log.warn('queue_item_too_large', { kind, byteSize });
      return null;
    }

    // Same idempotency key already queued: nothing to add.
    const store = await this.tx('readonly');
    const existing = await promisify(
      store.index('idempotencyKey').getAll(IDBKeyRange.only(idempotencyKey)),
    );
    if (existing.length > 0) return null;

    const item: QueueItem = {
      id: randomId('q'),
      kind,
      payload,
      idempotencyKey,
      createdAt: Date.now(),
      seq: nextSequence(),
      attempts: 0,
      nextAttemptAt: Date.now(),
      byteSize,
    };

    const write = await this.tx('readwrite');
    await promisify(write.add(item));
    await this.enforceLimits();
    return item;
  }

  async takeDue(limit: number, now = Date.now()): Promise<QueueItem[]> {
    const store = await this.tx('readonly');
    const all = (await promisify(store.getAll())) as QueueItem[];
    return all
      .filter((item) => item.nextAttemptAt <= now)
      .sort(byAge)
      .slice(0, limit);
  }

  /** Every item, oldest first. IndexedDB returns key order, which is the
   * random item id, so the sort is what makes inspection deterministic. */
  async peekAll(): Promise<QueueItem[]> {
    const store = await this.tx('readonly');
    return ((await promisify(store.getAll())) as QueueItem[]).sort(byAge);
  }

  async remove(id: string): Promise<void> {
    const store = await this.tx('readwrite');
    await promisify(store.delete(id));
  }

  /** Record a failure: reschedule with jittered backoff, or drop when exhausted. */
  async reschedule(item: QueueItem, error: string): Promise<'retry' | 'dropped'> {
    const attempts = item.attempts + 1;
    if (attempts >= this.limits.maxAttempts) {
      await this.remove(item.id);
      log.warn('queue_item_dropped', { kind: item.kind, attempts });
      return 'dropped';
    }
    const store = await this.tx('readwrite');
    await promisify(
      store.put({
        ...item,
        attempts,
        nextAttemptAt: Date.now() + backoffMs(attempts),
        lastError: error.slice(0, 200),
      }),
    );
    return 'retry';
  }

  async size(): Promise<number> {
    const store = await this.tx('readonly');
    return promisify(store.count());
  }

  async totalBytes(): Promise<number> {
    const items = await this.peekAll();
    return items.reduce((sum, item) => sum + (item.byteSize || 0), 0);
  }

  async clear(): Promise<void> {
    const store = await this.tx('readwrite');
    await promisify(store.clear());
  }

  /** Evict by age first, then oldest-first until the count/byte caps hold. */
  async enforceLimits(now = Date.now()): Promise<number> {
    const items = await this.peekAll();
    const maxAgeMs = this.limits.maxAgeDays * 24 * 60 * 60 * 1000;
    const doomed: string[] = [];

    let totalBytes = 0;
    const survivors: QueueItem[] = [];
    for (const item of items) {
      if (now - item.createdAt > maxAgeMs) {
        doomed.push(item.id);
        continue;
      }
      survivors.push(item);
      totalBytes += item.byteSize || 0;
    }

    let index = 0;
    while (survivors.length - index > this.limits.maxItems) {
      const victim = survivors[index];
      if (!victim) break;
      doomed.push(victim.id);
      totalBytes -= victim.byteSize || 0;
      index += 1;
    }
    while (totalBytes > this.limits.maxBytes && index < survivors.length) {
      const victim = survivors[index];
      if (!victim) break;
      doomed.push(victim.id);
      totalBytes -= victim.byteSize || 0;
      index += 1;
    }

    if (doomed.length === 0) return 0;
    const store = await this.tx('readwrite');
    for (const id of doomed) await promisify(store.delete(id));
    log.warn('queue_evicted', { count: doomed.length });
    return doomed.length;
  }
}
