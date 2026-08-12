/**
 * Live capture.
 *
 * The observer watches the transcript continuously but uploads *nothing* while
 * text is still changing. A message is emitted once, when it is stable:
 *
 *   1. a committed user message appears in the transcript (never the composer);
 *   2. an assistant message streams — kept in memory only;
 *   3. streaming visibly ends, or no relevant change occurs for `quietMs`
 *      (default 2000 ms);
 *   4. one complete version is emitted;
 *   5. if the page is hidden/unloaded first, the in-flight text is emitted once
 *      with `completion_status = "partial"` so nothing is silently lost.
 */

import type { ExtractedMessage } from '../shared/types';
import { debounce } from '../shared/util';
import { extractConversation } from './dom-adapter';

export interface StableMessage {
  message: ExtractedMessage;
  status: 'complete' | 'partial';
}

export type StableListener = (batch: StableMessage[]) => void;

export interface LiveObserverOptions {
  quietMs?: number;
  doc?: Document;
  /** Injected for tests; defaults to the real MutationObserver. */
  observerFactory?: (callback: MutationCallback) => MutationObserver;
}

interface TrackedMessage {
  signature: string;
  emitted: boolean;
  message: ExtractedMessage;
}

function signatureOf(message: ExtractedMessage): string {
  return `${message.role}::${message.sourceMessageId ?? `idx${message.domIndex}`}::${message.text.length}::${message.text.slice(-64)}`;
}

function identityOf(message: ExtractedMessage): string {
  return message.sourceMessageId ?? `${message.role}#${message.domIndex}`;
}

export class LiveObserver {
  private readonly doc: Document;
  private readonly quietMs: number;
  private readonly listeners = new Set<StableListener>();
  private readonly tracked = new Map<string, TrackedMessage>();
  private observer: MutationObserver | null = null;
  private conversationId: string | null = null;
  private readonly evaluate: ReturnType<typeof debounce>;
  private running = false;

  constructor(private readonly options: LiveObserverOptions = {}) {
    this.doc = options.doc ?? document;
    this.quietMs = options.quietMs ?? 2000;
    // Debouncing is what keeps every streaming token out of the network: the
    // callback only runs after the DOM has been quiet for `quietMs`.
    this.evaluate = debounce(() => this.emitStable(), this.quietMs);
  }

  onStable(listener: StableListener): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  start(conversationId: string | null): void {
    this.stop();
    this.conversationId = conversationId;
    this.tracked.clear();
    this.running = true;

    const factory =
      this.options.observerFactory ?? ((cb: MutationCallback) => new MutationObserver(cb));
    this.observer = factory(() => {
      if (!this.running) return;
      this.evaluate();
    });

    const target = this.doc.body ?? this.doc.documentElement;
    if (target) {
      this.observer.observe(target, {
        childList: true,
        subtree: true,
        characterData: true,
        attributes: true,
        attributeFilter: ['data-is-streaming', 'data-message-id', 'class'],
      });
    }
    // Seed the tracker so an already-rendered transcript is considered.
    this.evaluate();
  }

  stop(): void {
    this.running = false;
    this.evaluate.cancel();
    this.observer?.disconnect();
    this.observer = null;
  }

  /** Emit whatever is currently in flight as `partial` (tab closing, route change). */
  flushPartial(): void {
    if (!this.running) return;
    this.evaluate.cancel();
    const pending: StableMessage[] = [];
    for (const tracked of this.tracked.values()) {
      if (tracked.emitted) continue;
      tracked.emitted = true;
      pending.push({ message: tracked.message, status: 'partial' });
    }
    if (pending.length > 0) this.publish(pending);
  }

  /** Force an immediate stability evaluation (used by tests and on unload). */
  flush(): void {
    this.evaluate.flush();
  }

  private emitStable(): void {
    if (!this.running) return;
    const conversation = extractConversation(this.doc);
    if (this.conversationId && conversation.sourceConversationId !== this.conversationId) {
      // The route changed underneath us; the content script restarts us.
      return;
    }

    const ready: StableMessage[] = [];
    for (const message of conversation.messages) {
      const identity = identityOf(message);
      const signature = signatureOf(message);
      const existing = this.tracked.get(identity);

      if (!existing) {
        this.tracked.set(identity, { signature, emitted: false, message });
        continue;
      }
      if (existing.signature !== signature) {
        // Still changing: remember the newest text, stay silent.
        existing.signature = signature;
        existing.message = message;
        existing.emitted = false;
        continue;
      }
      existing.message = message;
      if (existing.emitted || message.isStreaming) continue;

      existing.emitted = true;
      ready.push({ message, status: 'complete' });
    }

    if (ready.length > 0) this.publish(ready);

    // Anything still unstable keeps the timer alive so it is re-checked.
    const unstable = Array.from(this.tracked.values()).some(
      (t) => !t.emitted && !t.message.isStreaming,
    );
    if (unstable) this.evaluate();
  }

  private publish(batch: StableMessage[]): void {
    for (const listener of this.listeners) {
      try {
        listener(batch);
      } catch {
        // One bad listener must not stop the others.
      }
    }
  }

  /** Test/diagnostic helper: how many turns are being tracked. */
  get trackedCount(): number {
    return this.tracked.size;
  }
}
