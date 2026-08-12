/**
 * Backfill of the conversation the employee currently has open.
 *
 * What it does: saves the scroll position, scrolls the transcript upward in
 * steps so ChatGPT loads older turns, parses everything that becomes visible,
 * then restores the exact original scroll position.
 *
 * What it never does: click Send, Regenerate, Edit, Delete, Share or any other
 * control; open other conversations; crawl the sidebar; or exceed the
 * server-configured time / message / scroll limits.
 */

import type { CaptureCompleteness, ExtractedConversation } from '../shared/types';
import { sleep } from '../shared/util';
import { extractConversation, findScrollContainer } from './dom-adapter';

export interface BackfillLimits {
  maxMessages: number;
  maxSeconds: number;
  maxScrolls: number;
  /** Pause between scroll steps, giving the app time to render older turns. */
  stepDelayMs?: number;
  /** Consecutive no-growth steps that mean "we have reached the top". */
  stableStepsToStop?: number;
}

export interface BackfillResult {
  conversation: ExtractedConversation;
  completeness: CaptureCompleteness;
  messageCount: number;
  scrolls: number;
  durationMs: number;
  reachedTop: boolean;
  scrollRestored: boolean;
  stoppedBecause: 'reached_top' | 'message_limit' | 'time_limit' | 'scroll_limit' | 'no_container';
}

export const DEFAULT_LIMITS: BackfillLimits = {
  maxMessages: 2000,
  maxSeconds: 120,
  maxScrolls: 400,
  stepDelayMs: 120,
  stableStepsToStop: 3,
};

export interface BackfillDeps {
  doc?: Document;
  now?: () => number;
  wait?: (ms: number) => Promise<void>;
}

/**
 * Scroll upward until the beginning is reached or a limit stops us.
 *
 * The returned completeness is honest: `complete_current_page` only when the
 * top was actually reached, otherwise `partial_scroll_limit`.
 */
export async function backfillCurrentConversation(
  limits: Partial<BackfillLimits> = {},
  deps: BackfillDeps = {},
): Promise<BackfillResult> {
  const settings = { ...DEFAULT_LIMITS, ...limits };
  const doc = deps.doc ?? document;
  const now = deps.now ?? (() => Date.now());
  const wait = deps.wait ?? sleep;

  const startedAt = now();
  const container = findScrollContainer(doc);

  if (!container) {
    const conversation = extractConversation(doc);
    return {
      conversation,
      completeness: 'live_only',
      messageCount: conversation.messages.length,
      scrolls: 0,
      durationMs: now() - startedAt,
      reachedTop: false,
      scrollRestored: true,
      stoppedBecause: 'no_container',
    };
  }

  const originalScrollTop = container.scrollTop;
  let scrolls = 0;
  let stableSteps = 0;
  let previousCount = extractConversation(doc).messages.length;
  let stoppedBecause: BackfillResult['stoppedBecause'] = 'reached_top';
  let reachedTop = false;

  try {
    for (;;) {
      if (container.scrollTop <= 0) {
        reachedTop = true;
        stoppedBecause = 'reached_top';
        break;
      }
      if (scrolls >= settings.maxScrolls) {
        stoppedBecause = 'scroll_limit';
        break;
      }
      if ((now() - startedAt) / 1000 >= settings.maxSeconds) {
        stoppedBecause = 'time_limit';
        break;
      }

      const step = Math.max(200, Math.floor(container.clientHeight * 0.8) || 600);
      container.scrollTop = Math.max(0, container.scrollTop - step);
      scrolls += 1;
      await wait(settings.stepDelayMs ?? 120);

      const count = extractConversation(doc).messages.length;
      if (count >= settings.maxMessages) {
        stoppedBecause = 'message_limit';
        break;
      }
      if (count === previousCount) {
        stableSteps += 1;
        // No new turns after several steps: either the top, or loading stalled.
        if (stableSteps >= (settings.stableStepsToStop ?? 3)) {
          reachedTop = container.scrollTop <= 0;
          stoppedBecause = reachedTop ? 'reached_top' : 'scroll_limit';
          break;
        }
      } else {
        stableSteps = 0;
        previousCount = count;
      }
    }
  } finally {
    // Always give the employee their view back, even if parsing threw.
    container.scrollTop = originalScrollTop;
  }

  const conversation = extractConversation(doc);
  conversation.reachedTop = reachedTop;

  const completeness: CaptureCompleteness = reachedTop
    ? 'complete_current_page'
    : 'partial_scroll_limit';

  return {
    conversation,
    completeness,
    messageCount: conversation.messages.length,
    scrolls,
    durationMs: now() - startedAt,
    reachedTop,
    scrollRestored: container.scrollTop === originalScrollTop,
    stoppedBecause,
  };
}

/** Non-blocking status line shown to the employee after a backfill. */
export function describeBackfill(result: BackfillResult): string {
  if (result.messageCount === 0) return 'No messages found to archive on this page.';
  const base = `Archived ${result.messageCount} message${result.messageCount === 1 ? '' : 's'} from this conversation`;
  if (result.reachedTop) return `${base}.`;
  switch (result.stoppedBecause) {
    case 'message_limit':
      return `${base} (stopped at the ${result.messageCount}-message safety limit).`;
    case 'time_limit':
      return `${base} (stopped at the time limit; scroll up and retry for older messages).`;
    default:
      return `${base} (older messages were not loaded).`;
  }
}
