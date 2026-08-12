/**
 * Turns extracted DOM messages into wire payloads.
 *
 * Responsibilities:
 *   * sanitise HTML with an allowlist (page HTML is never executed or re-parsed
 *     into a live document);
 *   * compute the content SHA-256 the backend re-verifies;
 *   * derive a deterministic idempotency key so retries are free;
 *   * preserve code language, table cells, list hierarchy, links, citations and
 *     attachment references.
 */

import type {
  ExtractedMessage,
  MessagePayload,
  MessagePartPayload,
} from '../shared/types';
import { idempotencyKey, sha256Hex } from '../shared/util';

const ALLOWED_TAGS = new Set([
  'p', 'br', 'hr', 'span', 'div',
  'strong', 'b', 'em', 'i', 'u', 's', 'del', 'ins', 'mark', 'sub', 'sup',
  'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
  'ul', 'ol', 'li', 'dl', 'dt', 'dd',
  'blockquote', 'pre', 'code', 'kbd', 'samp',
  'table', 'thead', 'tbody', 'tfoot', 'tr', 'th', 'td', 'caption', 'colgroup', 'col',
  'a', 'img', 'figure', 'figcaption', 'cite', 'abbr', 'time',
]);

const ALLOWED_ATTRS: Record<string, Set<string>> = {
  '*': new Set(['class', 'title', 'dir', 'lang']),
  a: new Set(['href', 'rel', 'target']),
  img: new Set(['src', 'alt', 'width', 'height']),
  code: new Set(['class', 'data-language']),
  pre: new Set(['class', 'data-language']),
  th: new Set(['colspan', 'rowspan', 'scope']),
  td: new Set(['colspan', 'rowspan']),
  col: new Set(['span']),
  time: new Set(['datetime']),
  ol: new Set(['start', 'type']),
};

const SAFE_URL = /^(https?:|mailto:)/i;
const MAX_HTML_BYTES = 2_000_000;
const MAX_TEXT_BYTES = 1_000_000;

/**
 * Allowlist sanitiser.
 *
 * Uses the inert document produced by DOMParser, so nothing is loaded,
 * executed or connected to the live page while cleaning.
 */
export function sanitizeHtml(html: string | null | undefined, doc?: Document): string | null {
  if (!html) return null;
  const source = html.length > MAX_HTML_BYTES ? html.slice(0, MAX_HTML_BYTES) : html;

  const parser = new DOMParser();
  const parsed = parser.parseFromString(`<body>${source}</body>`, 'text/html');
  const body = parsed.body;
  if (!body) return null;

  const walk = (node: Element): void => {
    for (const child of Array.from(node.children)) {
      const tag = child.tagName.toLowerCase();
      if (!ALLOWED_TAGS.has(tag)) {
        // Keep the text, drop the element.
        const text = parsed.createTextNode(child.textContent ?? '');
        child.replaceWith(text);
        continue;
      }
      const allowed = ALLOWED_ATTRS[tag] ?? new Set<string>();
      const global = ALLOWED_ATTRS['*'] as Set<string>;
      for (const attr of Array.from(child.attributes)) {
        const name = attr.name.toLowerCase();
        const keep = allowed.has(name) || global.has(name);
        if (!keep || name.startsWith('on')) {
          child.removeAttribute(attr.name);
          continue;
        }
        if ((name === 'href' || name === 'src') && !SAFE_URL.test(attr.value.trim())) {
          child.removeAttribute(attr.name);
        }
      }
      if (tag === 'a') {
        child.setAttribute('rel', 'noopener noreferrer');
      }
      walk(child);
    }
  };

  walk(body);
  const cleaned = body.innerHTML.trim();
  void doc; // the live document is deliberately never touched
  return cleaned || null;
}

function truncateText(text: string): string {
  return text.length > MAX_TEXT_BYTES ? text.slice(0, MAX_TEXT_BYTES) : text;
}

function boundParts(parts: MessagePartPayload[]): MessagePartPayload[] {
  return parts.slice(0, 500).map((part, index) => ({
    ...part,
    index,
    text: part.text ? truncateText(part.text) : part.text,
  }));
}

export interface NormalizeOptions {
  sourceConversationId: string;
  sequenceIndex: number;
  completionStatus?: MessagePayload['completion_status'];
  isEdit?: boolean;
  isRegeneration?: boolean;
  parentSourceMessageId?: string | null;
  attachmentClientIds?: string[];
}

/** Build the exact payload the backend accepts for one message version. */
export async function normalizeMessage(
  message: ExtractedMessage,
  options: NormalizeOptions,
): Promise<MessagePayload> {
  const text = truncateText(message.text);
  const contentSha256 = await sha256Hex(text);

  // Identity material excludes the volatile sequence index so that a retry
  // after a backfill (which renumbers positions) reuses the same key.
  const key = await idempotencyKey([
    options.sourceConversationId,
    message.sourceMessageId ?? 'nosrc',
    message.role,
    contentSha256,
    options.completionStatus ?? 'complete',
    options.isEdit ? 'edit' : '',
    options.isRegeneration ? 'regen' : '',
  ]);

  return {
    idempotency_key: key,
    source_conversation_id: options.sourceConversationId,
    source_message_id: message.sourceMessageId,
    role: message.role,
    sequence_index: Math.max(0, options.sequenceIndex),
    text,
    sanitized_html: sanitizeHtml(message.html),
    parts: boundParts(message.parts),
    citations: message.citations.slice(0, 100),
    completion_status: options.completionStatus ?? (message.isStreaming ? 'partial' : 'complete'),
    is_edit: options.isEdit ?? false,
    is_regeneration: options.isRegeneration ?? false,
    parent_source_message_id: options.parentSourceMessageId ?? null,
    branch_key: message.branchKey,
    branch_selected: message.branchSelected,
    source_created_at: message.timestamp,
    content_sha256: contentSha256,
    attachment_client_ids: (options.attachmentClientIds ?? []).slice(0, 20),
    author_name: message.authorName,
  };
}

export async function normalizeMessages(
  messages: ExtractedMessage[],
  sourceConversationId: string,
  startIndex = 0,
): Promise<MessagePayload[]> {
  const payloads: MessagePayload[] = [];
  for (let index = 0; index < messages.length; index += 1) {
    const message = messages[index];
    if (!message) continue;
    payloads.push(
      await normalizeMessage(message, {
        sourceConversationId,
        sequenceIndex: startIndex + index,
        completionStatus: message.isStreaming ? 'partial' : 'complete',
        attachmentClientIds: message.attachmentRefs.map((ref) => ref.clientAttachmentId),
      }),
    );
  }
  return payloads;
}

/** Stable per-message signature used to detect real content changes. */
export async function messageSignature(message: ExtractedMessage): Promise<string> {
  return sha256Hex(`${message.role}|${message.sourceMessageId ?? ''}|${message.text}`);
}
