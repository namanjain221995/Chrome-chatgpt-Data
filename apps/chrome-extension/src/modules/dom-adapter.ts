/**
 * DOM adapter: the ONLY place that knows ChatGPT's markup.
 *
 * Every selector and parsing heuristic lives behind this boundary so that a
 * product UI change is a single-file fix with fixture tests, not a rewrite.
 * The adapter is version-stamped (`ADAPTER_VERSION`) and each captured message
 * records the version that produced it.
 *
 * Safety rules enforced here:
 *   * page HTML is read, never executed — no innerHTML assignment, no eval;
 *   * the composer/textarea is never read, so unsent drafts cannot leak;
 *   * nothing is clicked, submitted, deleted or regenerated on the employee's
 *     behalf; the adapter is strictly read-only.
 */

import type {
  AttachmentRef,
  CitationPayload,
  ExtractedConversation,
  ExtractedMessage,
  MessagePartPayload,
  MessageRole,
  PartKind,
  WorkspaceObservation,
} from '../shared/types';
import { ADAPTER_VERSION, normalizeWhitespace } from '../shared/util';

export const ADAPTER_ID = 'chatgpt-dom-adapter';
export { ADAPTER_VERSION };

/** Selectors are ordered most-specific first; the first hit wins. */
export const SELECTORS = {
  messageTurn: [
    '[data-testid^="conversation-turn"]',
    'article[data-testid^="conversation-turn"]',
    '[data-message-id]',
  ],
  messageRoot: '[data-message-id]',
  roleAttribute: 'data-message-author-role',
  messageIdAttribute: 'data-message-id',
  modelSlugAttribute: 'data-message-model-slug',
  markdownBody: ['.markdown', '[data-message-content]', '.prose'],
  userTextBody: ['.whitespace-pre-wrap', '[data-message-content]'],
  streamingIndicator: [
    '[data-is-streaming="true"]',
    '.result-streaming',
    '[data-testid="stop-button"]',
  ],
  conversationTitle: [
    'nav [aria-current="page"]',
    '[data-testid="conversation-title"]',
    'header h1',
  ],
  workspaceLabel: [
    '[data-testid="workspace-name"]',
    '[data-testid="accounts-profile-button"] [data-workspace-name]',
    '[data-workspace-name]',
    'nav [data-testid="workspace-switcher"] span',
  ],
  workspaceId: ['[data-workspace-id]', '[data-testid="workspace-switcher"][data-workspace-id]'],
  personalWorkspaceMarker: [
    '[data-workspace-kind="personal"]',
    '[data-testid="workspace-personal"]',
  ],
  branchIndicator: [
    '[data-testid="conversation-turn-counter"]',
    '.tabular-nums',
    '[aria-label*="of"][role="status"]',
  ],
  attachmentTile: [
    '[data-testid="file-attachment"]',
    '[data-testid^="attachment"]',
    'a[download]',
  ],
  generatedImage: ['[data-testid="generated-image"]', 'img[alt*="Generated"]'],
  citationAnchor: ['a[data-citation]', 'a[target="_blank"][rel~="noopener"]'],
  scrollContainer: [
    '[data-testid="conversation-scroll-container"]',
    'main [class*="overflow-y-auto"]',
    'main',
  ],
  timestamp: ['time[datetime]', '[data-message-timestamp]'],
} as const;

const CONVERSATION_ID_PATTERNS: RegExp[] = [
  /\/c\/([0-9a-fA-F-]{16,64})/,
  /\/chat\/([0-9a-fA-F-]{16,64})/,
  /\/g\/[^/]+\/c\/([0-9a-fA-F-]{16,64})/,
];

/** Elements never read, so drafts and credentials cannot be captured. */
const FORBIDDEN_CONTAINERS = [
  'textarea',
  'input',
  '[contenteditable="true"]',
  '[data-testid="prompt-textarea"]',
  'form',
];

function firstMatch(root: ParentNode, selectors: readonly string[]): Element | null {
  for (const selector of selectors) {
    const found = root.querySelector(selector);
    if (found) return found;
  }
  return null;
}

function allMatches(root: ParentNode, selectors: readonly string[]): Element[] {
  for (const selector of selectors) {
    const found = Array.from(root.querySelectorAll(selector));
    if (found.length > 0) return found;
  }
  return [];
}

function isInsideComposer(element: Element): boolean {
  return FORBIDDEN_CONTAINERS.some((selector) => element.closest(selector) !== null);
}

export function extractConversationId(url: string): string | null {
  for (const pattern of CONVERSATION_ID_PATTERNS) {
    const match = pattern.exec(url);
    if (match?.[1]) return match[1];
  }
  return null;
}

export function isApprovedUrl(url: string, patterns: string[]): boolean {
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return false;
  }
  if (parsed.protocol !== 'https:') return false;
  return patterns.some((pattern) => {
    const [, host = ''] = /^https:\/\/([^/*]+)/.exec(pattern) ?? [];
    if (!host) return false;
    return parsed.hostname === host;
  });
}

// ---------------------------------------------------------------------------
// Workspace observation
// ---------------------------------------------------------------------------

/**
 * Report what the page says about the workspace. This is only ever a *hint*:
 * the backend re-decides, and an unrecognised page yields no signals so the
 * verifier fails closed.
 */
export function observeWorkspace(doc: Document): WorkspaceObservation {
  const signals: string[] = [];

  const labelNode = firstMatch(doc, SELECTORS.workspaceLabel);
  const label = labelNode ? normalizeWhitespace(labelNode.textContent ?? '') || null : null;
  if (label) signals.push('workspace_label_match');

  const idNode = firstMatch(doc, SELECTORS.workspaceId);
  const sourceWorkspaceId =
    idNode?.getAttribute('data-workspace-id')?.trim() || null;
  if (sourceWorkspaceId) signals.push('workspace_id_match');

  const personalNode = firstMatch(doc, SELECTORS.personalWorkspaceMarker);
  const looksPersonal = personalNode !== null;

  if (doc.querySelector('[data-testid="enterprise-badge"], [data-managed-account="true"]')) {
    signals.push('enterprise_workspace_marker');
  }
  if (/\/g\/g-/.test(doc.location?.pathname ?? '')) {
    signals.push('managed_account_url_path');
  }

  return { label, sourceWorkspaceId, signals, looksPersonal };
}

// ---------------------------------------------------------------------------
// Message extraction
// ---------------------------------------------------------------------------

function readRole(element: Element): MessageRole | null {
  const raw =
    element.getAttribute(SELECTORS.roleAttribute) ??
    element.querySelector(`[${SELECTORS.roleAttribute}]`)?.getAttribute(SELECTORS.roleAttribute) ??
    null;
  switch (raw) {
    case 'user':
    case 'assistant':
    case 'tool':
    case 'system':
      return raw;
    default:
      return null;
  }
}

function readMessageId(element: Element): string | null {
  const own = element.getAttribute(SELECTORS.messageIdAttribute);
  if (own) return own;
  return element.querySelector(SELECTORS.messageRoot)?.getAttribute(SELECTORS.messageIdAttribute) ?? null;
}

function isStreaming(element: Element): boolean {
  if (element.getAttribute('data-is-streaming') === 'true') return true;
  return SELECTORS.streamingIndicator.some((selector) => element.querySelector(selector) !== null);
}

function readTimestamp(element: Element): string | null {
  const node = firstMatch(element, SELECTORS.timestamp);
  const value =
    node?.getAttribute('datetime') ?? node?.getAttribute('data-message-timestamp') ?? null;
  if (!value) return null;
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? null : parsed.toISOString();
}

function readBranch(element: Element): {
  key: string | null;
  index: number | null;
  total: number | null;
  selected: boolean;
} {
  const node = firstMatch(element, SELECTORS.branchIndicator);
  const text = normalizeWhitespace(node?.textContent ?? '');
  const match = /(\d+)\s*\/\s*(\d+)/.exec(text);
  if (!match) return { key: null, index: null, total: null, selected: true };
  const index = Number.parseInt(match[1] ?? '', 10);
  const total = Number.parseInt(match[2] ?? '', 10);
  if (!Number.isFinite(index) || !Number.isFinite(total)) {
    return { key: null, index: null, total: null, selected: true };
  }
  return { key: `branch-${index}-of-${total}`, index, total, selected: true };
}

function readCitations(element: Element): CitationPayload[] {
  const anchors = allMatches(element, SELECTORS.citationAnchor);
  const citations: CitationPayload[] = [];
  anchors.forEach((anchor, index) => {
    const href = anchor.getAttribute('href');
    if (!href || !/^https?:/i.test(href)) return;
    citations.push({
      index,
      title: normalizeWhitespace(anchor.textContent ?? '').slice(0, 512) || null,
      url: href.slice(0, 2048),
      source_id: anchor.getAttribute('data-citation') || null,
    });
  });
  return citations.slice(0, 100);
}

function readAttachments(element: Element, conversationId: string | null): AttachmentRef[] {
  const refs: AttachmentRef[] = [];
  for (const tile of allMatches(element, SELECTORS.attachmentTile)) {
    const filename =
      tile.getAttribute('data-filename') ??
      tile.getAttribute('download') ??
      normalizeWhitespace(tile.textContent ?? '').slice(0, 200);
    if (!filename) continue;
    const sizeAttr = tile.getAttribute('data-file-size');
    const fileId = tile.getAttribute('data-file-id');
    refs.push({
      clientAttachmentId: `dom-${conversationId ?? 'unknown'}-${fileId ?? filename}`.slice(0, 128),
      filename,
      mimeType: tile.getAttribute('data-mime-type'),
      byteSize: sizeAttr ? Number.parseInt(sizeAttr, 10) || null : null,
      // Historical tiles expose no bytes; only the upload observer can do that.
      metadataOnly: true,
      sourceFileId: fileId,
      relation: 'referenced_historical',
    });
  }
  for (const image of allMatches(element, SELECTORS.generatedImage)) {
    const alt = image.getAttribute('alt') ?? 'generated-image';
    refs.push({
      clientAttachmentId: `gen-${conversationId ?? 'unknown'}-${alt}`.slice(0, 128),
      filename: `${alt.replace(/[^A-Za-z0-9._-]+/g, '_').slice(0, 64) || 'generated'}.png`,
      mimeType: 'image/png',
      byteSize: null,
      metadataOnly: true,
      sourceFileId: image.getAttribute('data-file-id'),
      relation: 'generated_by_assistant',
    });
  }
  return refs.slice(0, 20);
}

const BLOCK_TAGS = new Set([
  'P',
  'PRE',
  'H1',
  'H2',
  'H3',
  'H4',
  'H5',
  'H6',
  'UL',
  'OL',
  'TABLE',
  'BLOCKQUOTE',
]);

function partKindForTag(tag: string): PartKind {
  switch (tag) {
    case 'PRE':
      return 'code';
    case 'H1':
    case 'H2':
    case 'H3':
    case 'H4':
    case 'H5':
    case 'H6':
      return 'heading';
    case 'UL':
    case 'OL':
      return 'list';
    case 'TABLE':
      return 'table';
    case 'BLOCKQUOTE':
      return 'quote';
    default:
      return 'text';
  }
}

function codeLanguage(element: Element): string | null {
  const code = element.querySelector('code');
  const source = code ?? element;
  const explicit = source.getAttribute('data-language');
  if (explicit) return explicit.toLowerCase().slice(0, 64);
  const className = source.getAttribute('class') ?? '';
  const match = /language-([A-Za-z0-9+#-]+)/.exec(className);
  return match?.[1]?.toLowerCase() ?? null;
}

function listStructure(element: Element): Record<string, unknown> {
  const items: Array<{ depth: number; text: string }> = [];
  const walk = (node: Element, depth: number): void => {
    for (const child of Array.from(node.children)) {
      if (child.tagName === 'LI') {
        const own = Array.from(child.childNodes)
          .filter((n) => n.nodeType === 3 || !['UL', 'OL'].includes((n as Element).tagName ?? ''))
          .map((n) => n.textContent ?? '')
          .join('');
        items.push({ depth, text: normalizeWhitespace(own) });
        const nested = child.querySelector(':scope > ul, :scope > ol');
        if (nested) walk(nested, depth + 1);
      } else if (['UL', 'OL'].includes(child.tagName)) {
        walk(child, depth + 1);
      }
    }
  };
  walk(element, 0);
  return { ordered: element.tagName === 'OL', items };
}

function tableStructure(element: Element): Record<string, unknown> {
  const headers = Array.from(element.querySelectorAll('thead th')).map((cell) =>
    normalizeWhitespace(cell.textContent ?? ''),
  );
  const rows = Array.from(element.querySelectorAll('tbody tr')).map((row) =>
    Array.from(row.querySelectorAll('td, th')).map((cell) =>
      normalizeWhitespace(cell.textContent ?? ''),
    ),
  );
  if (headers.length === 0 && rows.length === 0) {
    const allRows = Array.from(element.querySelectorAll('tr'));
    const [first, ...rest] = allRows;
    if (first) {
      return {
        headers: Array.from(first.children).map((c) => normalizeWhitespace(c.textContent ?? '')),
        rows: rest.map((row) =>
          Array.from(row.children).map((c) => normalizeWhitespace(c.textContent ?? '')),
        ),
      };
    }
  }
  return { headers, rows };
}

/** Decompose a rendered message body into ordered structured parts. */
export function extractParts(body: Element): MessagePartPayload[] {
  const parts: MessagePartPayload[] = [];
  let index = 0;

  const push = (kind: PartKind, text: string, extra?: Partial<MessagePartPayload>): void => {
    const trimmed = kind === 'code' ? text.replace(/\n+$/, '') : normalizeWhitespace(text);
    if (!trimmed) return;
    parts.push({ index: index++, kind, text: trimmed, structured: {}, ...extra });
  };

  const children = Array.from(body.children);
  if (children.length === 0) {
    push('text', body.textContent ?? '');
    return parts;
  }

  for (const child of children) {
    const tag = child.tagName;
    if (!BLOCK_TAGS.has(tag)) {
      push('text', child.textContent ?? '');
      continue;
    }
    const kind = partKindForTag(tag);
    if (kind === 'code') {
      push('code', child.textContent ?? '', { language: codeLanguage(child) });
    } else if (kind === 'list') {
      push('list', child.textContent ?? '', { structured: listStructure(child) });
    } else if (kind === 'table') {
      push('table', child.textContent ?? '', { structured: tableStructure(child) });
    } else if (kind === 'heading') {
      push('heading', child.textContent ?? '', {
        structured: { level: Number.parseInt(tag.slice(1), 10) || 1 },
      });
    } else {
      push(kind, child.textContent ?? '');
    }
  }
  return parts;
}

/** Plain-text rendering that keeps code blocks and tables readable. */
export function partsToText(parts: MessagePartPayload[]): string {
  const chunks = parts.map((part) => {
    if (part.kind === 'code') {
      const fence = part.language ? `\`\`\`${part.language}` : '```';
      return `${fence}\n${part.text ?? ''}\n\`\`\``;
    }
    if (part.kind === 'table') {
      const structured = part.structured as { headers?: string[]; rows?: string[][] } | undefined;
      const headers = structured?.headers ?? [];
      const rows = structured?.rows ?? [];
      if (headers.length === 0 && rows.length === 0) return part.text ?? '';
      const lines = [headers.join(' | '), headers.map(() => '---').join(' | ')];
      for (const row of rows) lines.push(row.join(' | '));
      return lines.join('\n');
    }
    if (part.kind === 'list') {
      const structured = part.structured as
        | { ordered?: boolean; items?: Array<{ depth: number; text: string }> }
        | undefined;
      const items = structured?.items ?? [];
      if (items.length === 0) return part.text ?? '';
      return items
        .map((item, position) => {
          const indent = '  '.repeat(item.depth);
          const bullet = structured?.ordered ? `${position + 1}.` : '-';
          return `${indent}${bullet} ${item.text}`;
        })
        .join('\n');
    }
    return part.text ?? '';
  });
  return chunks.filter(Boolean).join('\n\n').trim();
}

function findBody(element: Element, role: MessageRole): Element | null {
  const selectors = role === 'user' ? SELECTORS.userTextBody : SELECTORS.markdownBody;
  return firstMatch(element, selectors) ?? firstMatch(element, SELECTORS.markdownBody) ?? element;
}

/** Extract one message turn, or null when the node is not a usable message. */
export function extractMessage(
  element: Element,
  domIndex: number,
  conversationId: string | null,
): ExtractedMessage | null {
  if (isInsideComposer(element)) return null;

  const role = readRole(element);
  if (!role) return null;

  const body = findBody(element, role);
  if (!body) return null;

  const parts = extractParts(body);
  const text = partsToText(parts);
  if (!text) return null;

  const branch = readBranch(element);
  return {
    sourceMessageId: readMessageId(element),
    role,
    domIndex,
    text,
    // The raw HTML is passed through the sanitizer before it ever leaves the
    // page; it is never assigned back into the DOM.
    html: body.innerHTML ?? null,
    parts,
    citations: readCitations(body),
    attachmentRefs: readAttachments(element, conversationId),
    isStreaming: isStreaming(element),
    branchKey: branch.key,
    branchSelected: branch.selected,
    branchIndex: branch.index,
    branchTotal: branch.total,
    authorName: element.getAttribute('data-author-name'),
    timestamp: readTimestamp(element),
    modelSlug: element.getAttribute(SELECTORS.modelSlugAttribute),
  };
}

/** Extract every message currently rendered in the transcript, in order. */
export function extractConversation(doc: Document, url?: string): ExtractedConversation {
  const href = url ?? doc.location?.href ?? '';
  const conversationId = extractConversationId(href);
  const turns = allMatches(doc, SELECTORS.messageTurn);

  const messages: ExtractedMessage[] = [];
  turns.forEach((turn, index) => {
    const message = extractMessage(turn, index, conversationId);
    if (message) messages.push(message);
  });

  const titleNode = firstMatch(doc, SELECTORS.conversationTitle);
  const title = titleNode ? normalizeWhitespace(titleNode.textContent ?? '') || null : null;

  return {
    sourceConversationId: conversationId,
    title,
    modelSlug: messages.find((m) => m.modelSlug)?.modelSlug ?? null,
    url: href,
    messages,
    reachedTop: false,
    adapterVersion: ADAPTER_VERSION,
  };
}

export function findScrollContainer(doc: Document): Element | null {
  return firstMatch(doc, SELECTORS.scrollContainer);
}

/** Support diagnostics: shape of the page, never its content. */
export function adapterDiagnostics(doc: Document): Record<string, unknown> {
  const turns = allMatches(doc, SELECTORS.messageTurn);
  const roles = turns.map((t) => readRole(t)).filter(Boolean) as MessageRole[];
  return {
    adapterVersion: ADAPTER_VERSION,
    turnCount: turns.length,
    userTurns: roles.filter((r) => r === 'user').length,
    assistantTurns: roles.filter((r) => r === 'assistant').length,
    withSourceIds: turns.filter((t) => readMessageId(t) !== null).length,
    streamingTurns: turns.filter((t) => isStreaming(t)).length,
    hasScrollContainer: findScrollContainer(doc) !== null,
    workspaceSignals: observeWorkspace(doc).signals,
  };
}
