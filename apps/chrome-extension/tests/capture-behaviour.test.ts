/**
 * Capture behaviour: workspace fail-closed, kill switch, live streaming,
 * backfill, route changes and message normalisation.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { RuntimeConfig, WorkspaceObservation } from '../src/shared/types';
import {
  backfillCurrentConversation,
  describeBackfill,
} from '../src/modules/conversation-backfill';
import { LiveObserver } from '../src/modules/live-observer';
import { RouteObserver } from '../src/modules/route-observer';
import { normalizeMessage, normalizeMessages } from '../src/modules/message-normalizer';
import { extractConversation } from '../src/modules/dom-adapter';
import { verifyWorkspace } from '../src/modules/workspace-verifier';
import { captureAllowed, isUsableConfig, policyBlockReason } from '../src/modules/managed-config';
import { sha256Hex } from '../src/shared/util';
import {
  CONVERSATION_ID,
  CONVERSATION_URL,
  OTHER_CONVERSATION_URL,
  basicTranscript,
  completedTranscript,
  longTranscript,
  streamingTranscript,
} from './fixtures/transcripts';

function makeConfig(overrides: Partial<RuntimeConfig> = {}): RuntimeConfig {
  const base: RuntimeConfig = {
    schema_version: '1.0',
    config_version: 42,
    issued_at: new Date(Date.now() - 1000).toISOString(),
    expires_at: new Date(Date.now() + 900_000).toISOString(),
    organization_slug: 'techsara',
    api_base_url: 'https://archive.example.com/api/v1',
    policy: {
      browser_content_capture_enabled: true,
      openai_written_authorization_confirmed: true,
      capture_active: true,
      auto_archive_current_open_chat: true,
      attachment_capture_enabled: true,
      personal_workspace_capture_enabled: false,
      capture_unsent_drafts: false,
      kill_switch: false,
    },
    workspace_rules: {
      managed_workspace_label: "TechSara's Workspace",
      managed_workspace_ids: [],
      allowed_url_patterns: ['https://chatgpt.com/*', 'https://chat.openai.com/*'],
      require_all_signals: false,
      min_signals: 1,
    },
    limits: {
      max_batch_items: 100,
      max_request_bytes: 2_621_440,
      max_attachment_bytes: 20 * 1024 * 1024,
      allowed_mime_types: ['image/png', 'application/pdf'],
      allowed_extensions: ['.png', '.pdf'],
      offline_queue_max_items: 10_000,
      offline_queue_max_bytes: 52_428_800,
      offline_queue_max_age_days: 7,
      stable_response_quiet_ms: 2000,
      backfill_max_messages: 2000,
      backfill_max_seconds: 120,
      backfill_max_scrolls: 400,
      rate_limit_requests_per_minute: 300,
    },
    privacy_notice_url: 'https://archive.example.com/privacy-notice',
    support_contact: 'it-support@example.com',
    minimum_extension_version: '1.0.0',
    coverage_statement:
      'This extension archives the conversation you currently have open and every new message ' +
      'you send or receive in the company workspace. It does not archive conversations you ' +
      'never open in this browser, and it never captures unsent drafts.',
  };
  return { ...base, ...overrides, policy: { ...base.policy, ...(overrides.policy ?? {}) } };
}

function observation(overrides: Partial<WorkspaceObservation> = {}): WorkspaceObservation {
  return {
    label: "TechSara's Workspace",
    sourceWorkspaceId: null,
    signals: ['workspace_label_match'],
    looksPersonal: false,
    ...overrides,
  };
}

beforeEach(() => {
  document.body.innerHTML = '';
  window.history.replaceState({}, '', new URL(CONVERSATION_URL).pathname);
});

describe('workspace verifier (fails closed)', () => {
  it('verifies the managed workspace by label', () => {
    const result = verifyWorkspace(observation(), makeConfig(), CONVERSATION_URL);
    expect(result.verified).toBe(true);
    expect(result.ref.kind).toBe('managed_company');
  });

  it('refuses when there is no configuration at all', () => {
    const result = verifyWorkspace(observation(), null, CONVERSATION_URL);
    expect(result.verified).toBe(false);
    expect(result.reason).toBe('no_config');
  });

  it('refuses a personal workspace even with a matching label', () => {
    const result = verifyWorkspace(
      observation({ looksPersonal: true }),
      makeConfig(),
      CONVERSATION_URL,
    );
    expect(result.verified).toBe(false);
    expect(result.reason).toBe('personal_workspace');
    expect(result.ref.kind).toBe('personal');
  });

  it('refuses a different workspace label', () => {
    const result = verifyWorkspace(
      observation({ label: 'Someone Else Workspace' }),
      makeConfig(),
      CONVERSATION_URL,
    );
    expect(result.verified).toBe(false);
    expect(result.reason).toBe('label_mismatch');
  });

  it('refuses when no strong signal is present', () => {
    const result = verifyWorkspace(
      observation({ signals: ['managed_account_url_path'] }),
      makeConfig(),
      CONVERSATION_URL,
    );
    expect(result.verified).toBe(false);
    expect(result.reason).toBe('no_signals');
  });

  it('refuses on a non-approved URL', () => {
    const result = verifyWorkspace(observation(), makeConfig(), 'https://evil.example/c/1');
    expect(result.verified).toBe(false);
    expect(result.reason).toBe('url_not_approved');
  });

  it('refuses when the kill switch is engaged', () => {
    const config = makeConfig({ policy: { kill_switch: true } as never });
    const result = verifyWorkspace(observation(), config, CONVERSATION_URL);
    expect(result.verified).toBe(false);
    expect(result.reason).toBe('kill_switch');
  });

  it('refuses when the server gates are closed', () => {
    const config = makeConfig({ policy: { capture_active: false } as never });
    expect(verifyWorkspace(observation(), config, CONVERSATION_URL).reason).toBe(
      'capture_gates_closed',
    );
  });

  it('refuses when the server has configured no workspace identifiers', () => {
    const config = makeConfig();
    config.workspace_rules.managed_workspace_label = null;
    config.workspace_rules.managed_workspace_ids = [];
    expect(verifyWorkspace(observation(), config, CONVERSATION_URL).reason).toBe(
      'rules_unconfigured',
    );
  });

  it('honours an id allowlist over a matching label', () => {
    const config = makeConfig();
    config.workspace_rules.managed_workspace_ids = ['ws-approved'];

    expect(
      verifyWorkspace(
        observation({ sourceWorkspaceId: 'ws-approved', signals: ['workspace_id_match'] }),
        config,
        CONVERSATION_URL,
      ).verified,
    ).toBe(true);

    expect(
      verifyWorkspace(
        observation({ sourceWorkspaceId: 'ws-other', signals: ['workspace_id_match'] }),
        config,
        CONVERSATION_URL,
      ).reason,
    ).toBe('id_not_allowlisted');
  });
});

describe('server-side gates cannot be bypassed locally', () => {
  it('rejects a config that claims capture_active without both gates', () => {
    const tampered = makeConfig();
    tampered.policy.browser_content_capture_enabled = false;
    tampered.policy.capture_active = true;
    expect(
      isUsableConfig({
        config: tampered,
        signature: 'x',
        signature_algorithm: 'HMAC-SHA256',
        key_id: 'k',
      }),
    ).toBe(false);
  });

  it('rejects a config that claims capture_active with the kill switch on', () => {
    const tampered = makeConfig();
    tampered.policy.kill_switch = true;
    tampered.policy.capture_active = true;
    expect(
      isUsableConfig({
        config: tampered,
        signature: 'x',
        signature_algorithm: 'HMAC-SHA256',
        key_id: 'k',
      }),
    ).toBe(false);
  });

  it('rejects a config that re-enables personal workspace capture', () => {
    const tampered = makeConfig();
    (tampered.policy as unknown as Record<string, unknown>).personal_workspace_capture_enabled =
      true;
    expect(
      isUsableConfig({
        config: tampered,
        signature: 'x',
        signature_algorithm: 'HMAC-SHA256',
        key_id: 'k',
      }),
    ).toBe(false);
  });

  it('rejects a config that re-enables draft capture', () => {
    const tampered = makeConfig();
    (tampered.policy as unknown as Record<string, unknown>).capture_unsent_drafts = true;
    expect(
      isUsableConfig({
        config: tampered,
        signature: 'x',
        signature_algorithm: 'HMAC-SHA256',
        key_id: 'k',
      }),
    ).toBe(false);
  });

  it('rejects an expired configuration', () => {
    const expired = makeConfig({ expires_at: new Date(Date.now() - 1000).toISOString() });
    expect(captureAllowed(expired)).toBe(false);
    expect(policyBlockReason(expired)).toContain('expired');
  });

  it('rejects an unsigned configuration', () => {
    expect(isUsableConfig(null)).toBe(false);
    expect(
      isUsableConfig({
        config: makeConfig(),
        signature: '',
        signature_algorithm: 'HMAC-SHA256',
        key_id: 'k',
      }),
    ).toBe(false);
  });

  it('allows capture only when everything lines up', () => {
    expect(captureAllowed(makeConfig())).toBe(true);
    expect(policyBlockReason(makeConfig())).toBeNull();
  });
});

describe('live observer', () => {
  function makeObserver(quietMs = 5): LiveObserver {
    let callback: MutationCallback | null = null;
    const observer = new LiveObserver({
      quietMs,
      observerFactory: (cb) => {
        callback = cb;
        return {
          observe: vi.fn(),
          disconnect: vi.fn(),
          takeRecords: vi.fn(() => []),
        } as unknown as MutationObserver;
      },
    });
    (observer as unknown as { _fire: () => void })._fire = () => {
      callback?.([], {} as MutationObserver);
    };
    return observer;
  }

  /** Simulate the DOM change notification the real observer would deliver. */
  function fire(observer: LiveObserver): void {
    (observer as unknown as { _fire: () => void })._fire();
  }

  it('does not emit while a message is still streaming', async () => {
    document.body.innerHTML = streamingTranscript();
    const observer = makeObserver();
    const seen: string[] = [];
    observer.onStable((batch) => seen.push(...batch.map((b) => b.message.text)));
    observer.start(CONVERSATION_ID);

    await new Promise((r) => setTimeout(r, 30));
    // The user turn is stable and may be emitted; the streaming answer is not.
    expect(seen).not.toContain('The first half of the ans');
    observer.stop();
  });

  it('emits one complete version once the answer stops changing', async () => {
    document.body.innerHTML = streamingTranscript();
    const observer = makeObserver();
    const seen: Array<{ text: string; status: string }> = [];
    observer.onStable((batch) =>
      seen.push(...batch.map((b) => ({ text: b.message.text, status: b.status }))),
    );
    observer.start(CONVERSATION_ID);
    await new Promise((r) => setTimeout(r, 20));

    // Swap in the finished answer and deliver a mutation, the way the real
    // MutationObserver would.
    document.body.innerHTML = completedTranscript('The complete answer.');
    fire(observer);
    await new Promise((r) => setTimeout(r, 20));
    fire(observer);
    await new Promise((r) => setTimeout(r, 30));

    const complete = seen.filter((s) => s.status === 'complete');
    expect(complete.some((s) => s.text === 'The complete answer.')).toBe(true);
    // Exactly one emission per message version, not one per token.
    expect(complete.filter((s) => s.text === 'The complete answer.')).toHaveLength(1);
    observer.stop();
  });

  it('emits a partial record when the page goes away mid-stream', async () => {
    document.body.innerHTML = streamingTranscript();
    const observer = makeObserver(10_000);
    const seen: Array<{ text: string; status: string }> = [];
    observer.onStable((batch) =>
      seen.push(...batch.map((b) => ({ text: b.message.text, status: b.status }))),
    );
    observer.start(CONVERSATION_ID);
    observer.flush();
    await new Promise((r) => setTimeout(r, 10));

    observer.flushPartial();
    const partial = seen.filter((s) => s.status === 'partial');
    expect(partial.length).toBeGreaterThan(0);
    expect(partial.some((p) => p.text.includes('The first half'))).toBe(true);
    observer.stop();
  });

  it('never emits the same stable message twice', async () => {
    document.body.innerHTML = completedTranscript('Stable answer.');
    const observer = makeObserver();
    const seen: string[] = [];
    observer.onStable((batch) => seen.push(...batch.map((b) => b.message.text)));
    observer.start(CONVERSATION_ID);

    for (let i = 0; i < 4; i += 1) {
      observer.flush();
      await new Promise((r) => setTimeout(r, 10));
    }
    expect(seen.filter((t) => t === 'Stable answer.')).toHaveLength(1);
    observer.stop();
  });
});

describe('route observer', () => {
  it('detects a conversation change without a reload', () => {
    const observer = new RouteObserver(window);
    const changes: Array<string | null> = [];
    observer.onChange((change) => changes.push(change.conversationId));
    observer.start();

    window.history.pushState({}, '', new URL(OTHER_CONVERSATION_URL).pathname);
    observer.check();

    expect(changes).toEqual(['99999999-8888-7777-6666-555555555555']);
    observer.stop();
  });

  it('ignores a same-conversation url change', () => {
    const observer = new RouteObserver(window);
    const changes: Array<string | null> = [];
    observer.onChange((change) => changes.push(change.conversationId));
    observer.start();

    window.history.pushState({}, '', `${new URL(CONVERSATION_URL).pathname}?x=1`);
    observer.check();
    expect(changes).toHaveLength(0);
    observer.stop();
  });

  it('restores the History API on stop', () => {
    const original = window.history.pushState;
    const observer = new RouteObserver(window);
    observer.start();
    expect(window.history.pushState).not.toBe(original);
    observer.stop();
    expect(window.history.pushState).toBe(original);
  });
});

describe('conversation backfill', () => {
  function scrollableFixture(messageCount: number, scrollHeight = 5000): HTMLElement {
    document.body.innerHTML = longTranscript(messageCount);
    const container = document.querySelector(
      '[data-testid="conversation-scroll-container"]',
    ) as HTMLElement;
    Object.defineProperty(container, 'clientHeight', { value: 800, configurable: true });
    Object.defineProperty(container, 'scrollHeight', { value: scrollHeight, configurable: true });
    container.scrollTop = 4000;
    return container;
  }

  it('restores the original scroll position', async () => {
    const container = scrollableFixture(10);
    container.scrollTop = 3210;

    const result = await backfillCurrentConversation(
      { maxScrolls: 10, stepDelayMs: 0 },
      { wait: async () => undefined },
    );

    expect(container.scrollTop).toBe(3210);
    expect(result.scrollRestored).toBe(true);
  });

  it('reports complete_current_page only when the top is reached', async () => {
    const container = scrollableFixture(6);
    container.scrollTop = 100;
    const result = await backfillCurrentConversation(
      { maxScrolls: 50, stepDelayMs: 0 },
      { wait: async () => undefined },
    );
    expect(result.reachedTop).toBe(true);
    expect(result.completeness).toBe('complete_current_page');
    expect(result.messageCount).toBe(6);
  });

  it('reports partial_scroll_limit when a limit stops it', async () => {
    const container = scrollableFixture(6);
    container.scrollTop = 100_000;
    const result = await backfillCurrentConversation(
      { maxScrolls: 2, stepDelayMs: 0 },
      { wait: async () => undefined },
    );
    expect(result.reachedTop).toBe(false);
    expect(result.completeness).toBe('partial_scroll_limit');
    expect(result.stoppedBecause).toBe('scroll_limit');
  });

  it('respects the time limit', async () => {
    scrollableFixture(6).scrollTop = 100_000;
    let clock = 0;
    const result = await backfillCurrentConversation(
      { maxScrolls: 1000, maxSeconds: 1, stepDelayMs: 0 },
      {
        wait: async () => {
          clock += 5000;
        },
        now: () => clock,
      },
    );
    expect(result.stoppedBecause).toBe('time_limit');
    expect(result.completeness).toBe('partial_scroll_limit');
  });

  it('never claims completeness without a scroll container', async () => {
    document.body.innerHTML = '<div data-message-id="x"></div>';
    const result = await backfillCurrentConversation({}, { wait: async () => undefined });
    expect(result.completeness).toBe('live_only');
    expect(result.stoppedBecause).toBe('no_container');
  });

  it('describes the outcome honestly to the employee', () => {
    expect(
      describeBackfill({
        conversation: extractConversation(document, CONVERSATION_URL),
        completeness: 'complete_current_page',
        messageCount: 84,
        scrolls: 12,
        durationMs: 900,
        reachedTop: true,
        scrollRestored: true,
        stoppedBecause: 'reached_top',
      }),
    ).toBe('Archived 84 messages from this conversation.');

    expect(
      describeBackfill({
        conversation: extractConversation(document, CONVERSATION_URL),
        completeness: 'partial_scroll_limit',
        messageCount: 20,
        scrolls: 400,
        durationMs: 900,
        reachedTop: false,
        scrollRestored: true,
        stoppedBecause: 'scroll_limit',
      }),
    ).toContain('older messages were not loaded');
  });
});

describe('message normalisation', () => {
  it('produces a payload the backend will accept', async () => {
    document.body.innerHTML = basicTranscript();
    const conversation = extractConversation(document, CONVERSATION_URL);
    const payloads = await normalizeMessages(conversation.messages, CONVERSATION_ID);

    expect(payloads).toHaveLength(2);
    const [first] = payloads;
    expect(first?.source_conversation_id).toBe(CONVERSATION_ID);
    expect(first?.content_sha256).toBe(await sha256Hex(first?.text ?? ''));
    expect(first?.idempotency_key).toMatch(/^k-[0-9a-f]{40}$/);
    expect(first?.completion_status).toBe('complete');
  });

  it('derives a stable idempotency key across retries', async () => {
    document.body.innerHTML = basicTranscript();
    const [message] = extractConversation(document, CONVERSATION_URL).messages;
    const options = { sourceConversationId: CONVERSATION_ID, sequenceIndex: 0 };
    const a = await normalizeMessage(message!, options);
    const b = await normalizeMessage(message!, options);
    expect(a.idempotency_key).toBe(b.idempotency_key);
  });

  it('keeps the key stable when the sequence index shifts after a backfill', async () => {
    document.body.innerHTML = basicTranscript();
    const [message] = extractConversation(document, CONVERSATION_URL).messages;
    const live = await normalizeMessage(message!, {
      sourceConversationId: CONVERSATION_ID,
      sequenceIndex: 0,
    });
    const backfilled = await normalizeMessage(message!, {
      sourceConversationId: CONVERSATION_ID,
      sequenceIndex: 84,
    });
    expect(live.idempotency_key).toBe(backfilled.idempotency_key);
  });

  it('changes the key when the content changes', async () => {
    document.body.innerHTML = basicTranscript();
    const messages = extractConversation(document, CONVERSATION_URL).messages;
    const a = await normalizeMessage(messages[0]!, {
      sourceConversationId: CONVERSATION_ID,
      sequenceIndex: 0,
    });
    const b = await normalizeMessage(
      { ...messages[0]!, text: 'a different question' },
      { sourceConversationId: CONVERSATION_ID, sequenceIndex: 0 },
    );
    expect(a.idempotency_key).not.toBe(b.idempotency_key);
  });

  it('marks a streaming message as partial', async () => {
    document.body.innerHTML = streamingTranscript();
    const messages = extractConversation(document, CONVERSATION_URL).messages;
    const payloads = await normalizeMessages(messages, CONVERSATION_ID);
    expect(payloads[1]?.completion_status).toBe('partial');
  });

  it('carries attachment references through to the payload', async () => {
    document.body.innerHTML = basicTranscript();
    const [message] = extractConversation(document, CONVERSATION_URL).messages;
    const payload = await normalizeMessage(message!, {
      sourceConversationId: CONVERSATION_ID,
      sequenceIndex: 0,
      attachmentClientIds: ['att-1', 'att-2'],
    });
    expect(payload.attachment_client_ids).toEqual(['att-1', 'att-2']);
  });
});
