/**
 * Route observer for ChatGPT's single-page navigation.
 *
 * Detects conversation changes without a page reload by patching the History
 * API (restored on stop), listening for popstate, and polling as a cheap
 * backstop for framework-internal navigations that emit no event.
 */

import { extractConversationId } from './dom-adapter';

export interface RouteChange {
  url: string;
  conversationId: string | null;
  previousConversationId: string | null;
}

export type RouteListener = (change: RouteChange) => void;

const POLL_INTERVAL_MS = 750;

export class RouteObserver {
  private listeners = new Set<RouteListener>();
  private currentUrl: string;
  private currentConversationId: string | null;
  private pollTimer: ReturnType<typeof setInterval> | null = null;
  private originalPushState: History['pushState'] | null = null;
  private originalReplaceState: History['replaceState'] | null = null;
  private readonly boundPopState = (): void => this.check();
  private started = false;

  constructor(private readonly win: Window = window) {
    this.currentUrl = win.location.href;
    this.currentConversationId = extractConversationId(this.currentUrl);
  }

  get conversationId(): string | null {
    return this.currentConversationId;
  }

  get url(): string {
    return this.currentUrl;
  }

  onChange(listener: RouteListener): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  start(): void {
    if (this.started) return;
    this.started = true;

    const history = this.win.history;
    // Keep the *original* references so stop() restores them exactly; binding
    // here would leave a wrapper behind and break repeated start/stop cycles.
    this.originalPushState = history.pushState;
    this.originalReplaceState = history.replaceState;

    const wrap = (original: History['pushState']): History['pushState'] =>
      ((...args: Parameters<History['pushState']>) => {
        const result = original.apply(history, args);
        // Let the framework finish its own state update first.
        queueMicrotask(() => this.check());
        return result;
      }) as History['pushState'];

    history.pushState = wrap(this.originalPushState);
    history.replaceState = wrap(this.originalReplaceState);

    this.win.addEventListener('popstate', this.boundPopState);
    this.pollTimer = setInterval(() => this.check(), POLL_INTERVAL_MS);
  }

  stop(): void {
    if (!this.started) return;
    this.started = false;
    if (this.pollTimer) clearInterval(this.pollTimer);
    this.pollTimer = null;
    this.win.removeEventListener('popstate', this.boundPopState);
    if (this.originalPushState) this.win.history.pushState = this.originalPushState;
    if (this.originalReplaceState) this.win.history.replaceState = this.originalReplaceState;
    this.originalPushState = null;
    this.originalReplaceState = null;
    this.listeners.clear();
  }

  /** Public so tests (and the content script) can force a re-evaluation. */
  check(): void {
    const url = this.win.location.href;
    if (url === this.currentUrl) return;
    const previous = this.currentConversationId;
    this.currentUrl = url;
    this.currentConversationId = extractConversationId(url);
    if (this.currentConversationId === previous) return;

    const change: RouteChange = {
      url,
      conversationId: this.currentConversationId,
      previousConversationId: previous,
    };
    for (const listener of this.listeners) {
      try {
        listener(change);
      } catch {
        // A misbehaving listener must not break navigation tracking.
      }
    }
  }
}
