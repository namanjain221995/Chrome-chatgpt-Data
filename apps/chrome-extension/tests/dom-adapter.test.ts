/**
 * DOM adapter tests against sanitized fixtures.
 *
 * Covers user, assistant, tool, code, tables, lists, citations, images, files,
 * branches and streaming states — plus the invariants that matter most:
 * unsent drafts are never read, and hostile markup never survives.
 */

import { beforeEach, describe, expect, it } from 'vitest';
import {
  adapterDiagnostics,
  extractConversation,
  extractConversationId,
  extractParts,
  isApprovedUrl,
  observeWorkspace,
  partsToText,
} from '../src/modules/dom-adapter';
import { sanitizeHtml } from '../src/modules/message-normalizer';
import {
  CONVERSATION_ID,
  CONVERSATION_URL,
  attachmentTranscript,
  basicTranscript,
  branchedTranscript,
  hostileTranscript,
  longTranscript,
  streamingTranscript,
  toolTranscript,
} from './fixtures/transcripts';

function loadFixture(html: string, url = CONVERSATION_URL): Document {
  document.body.innerHTML = html;
  const target = new URL(url);
  window.history.replaceState({}, '', target.pathname + target.search);
  return document;
}

beforeEach(() => {
  document.body.innerHTML = '';
});

describe('conversation id extraction', () => {
  it.each([
    ['https://chatgpt.com/c/11111111-2222-3333-4444-555555555555', CONVERSATION_ID],
    ['https://chat.openai.com/c/11111111-2222-3333-4444-555555555555', CONVERSATION_ID],
    ['https://chatgpt.com/g/g-abc/c/11111111-2222-3333-4444-555555555555', CONVERSATION_ID],
  ])('parses %s', (url, expected) => {
    expect(extractConversationId(url)).toBe(expected);
  });

  it('returns null on a route with no conversation', () => {
    expect(extractConversationId('https://chatgpt.com/')).toBeNull();
    expect(extractConversationId('https://chatgpt.com/gpts')).toBeNull();
  });
});

describe('url allowlist', () => {
  const patterns = ['https://chatgpt.com/*', 'https://chat.openai.com/*'];

  it('accepts approved hosts', () => {
    expect(isApprovedUrl('https://chatgpt.com/c/abc', patterns)).toBe(true);
    expect(isApprovedUrl('https://chat.openai.com/c/abc', patterns)).toBe(true);
  });

  it('rejects lookalikes, http and unrelated sites', () => {
    expect(isApprovedUrl('https://chatgpt.com.evil.example/c/abc', patterns)).toBe(false);
    expect(isApprovedUrl('http://chatgpt.com/c/abc', patterns)).toBe(false);
    expect(isApprovedUrl('https://mail.google.com/', patterns)).toBe(false);
    expect(isApprovedUrl('not a url', patterns)).toBe(false);
  });
});

describe('message extraction', () => {
  it('extracts user and assistant turns in order', () => {
    const doc = loadFixture(basicTranscript());
    const conversation = extractConversation(doc, CONVERSATION_URL);

    expect(conversation.sourceConversationId).toBe(CONVERSATION_ID);
    expect(conversation.messages).toHaveLength(2);
    expect(conversation.messages[0]?.role).toBe('user');
    expect(conversation.messages[1]?.role).toBe('assistant');
    expect(conversation.messages[0]?.sourceMessageId).toBe('msg-1');
    expect(conversation.messages[0]?.text).toBe('What is our expense policy?');
  });

  it('never reads the composer, so unsent drafts cannot leak', () => {
    const doc = loadFixture(basicTranscript());
    const conversation = extractConversation(doc, CONVERSATION_URL);
    const everything = JSON.stringify(conversation);
    expect(everything).not.toContain('DRAFT');
    expect(everything).not.toContain('must never be captured');
  });

  it('still extracts a transcript that the app nests inside a page-level form', () => {
    // Regression: the live product can wrap rendered turns in a <form>. A bare
    // closest('form') exclusion dropped every message on the page while the
    // composer keeps its own protection through the widget selectors.
    const doc = loadFixture(
      `<form data-page-wrapper="true">${basicTranscript()}</form>`,
    );
    const conversation = extractConversation(doc, CONVERSATION_URL);
    expect(conversation.messages.length).toBeGreaterThan(0);
    const everything = JSON.stringify(conversation);
    expect(everything).not.toContain('DRAFT');
    expect(everything).not.toContain('must never be captured');
  });

  it('preserves code blocks with their language', () => {
    const doc = loadFixture(basicTranscript());
    const assistant = extractConversation(doc, CONVERSATION_URL).messages[1];
    const code = assistant?.parts.find((part) => part.kind === 'code');
    expect(code).toBeDefined();
    expect(code?.language).toBe('python');
    expect(code?.text).toContain('print("expense report")');
    expect(assistant?.text).toContain('```python');
  });

  it('preserves table cells', () => {
    const doc = loadFixture(basicTranscript());
    const assistant = extractConversation(doc, CONVERSATION_URL).messages[1];
    const table = assistant?.parts.find((part) => part.kind === 'table');
    const structured = table?.structured as { headers: string[]; rows: string[][] };
    expect(structured.headers).toEqual(['Category', 'Limit']);
    expect(structured.rows).toEqual([
      ['Meals', '45'],
      ['Taxi', '80'],
    ]);
  });

  it('preserves list hierarchy and ordering', () => {
    const doc = loadFixture(basicTranscript());
    const assistant = extractConversation(doc, CONVERSATION_URL).messages[1];
    const list = assistant?.parts.find((part) => part.kind === 'list');
    const structured = list?.structured as { ordered: boolean; items: Array<{ text: string }> };
    expect(structured.ordered).toBe(true);
    expect(structured.items.map((i) => i.text)).toEqual([
      'Submit within 30 days',
      'Attach a receipt',
      'Use the company card',
    ]);
  });

  it('preserves headings and quotes', () => {
    const doc = loadFixture(basicTranscript());
    const assistant = extractConversation(doc, CONVERSATION_URL).messages[1];
    const kinds = assistant?.parts.map((p) => p.kind) ?? [];
    expect(kinds).toContain('heading');
    expect(kinds).toContain('quote');
  });

  it('captures citations with their urls', () => {
    const doc = loadFixture(basicTranscript());
    const assistant = extractConversation(doc, CONVERSATION_URL).messages[1];
    expect(assistant?.citations[0]?.url).toBe('https://intranet.example.com/policy');
    expect(assistant?.citations[0]?.source_id).toBe('c1');
  });

  it('records the model slug when the page exposes it', () => {
    const doc = loadFixture(basicTranscript());
    expect(extractConversation(doc, CONVERSATION_URL).modelSlug).toBe('gpt-4o');
  });

  it('extracts visible tool messages', () => {
    const doc = loadFixture(toolTranscript());
    const roles = extractConversation(doc, CONVERSATION_URL).messages.map((m) => m.role);
    expect(roles).toEqual(['user', 'tool', 'assistant']);
  });

  it('flags a streaming assistant message', () => {
    const doc = loadFixture(streamingTranscript());
    const messages = extractConversation(doc, CONVERSATION_URL).messages;
    expect(messages[1]?.isStreaming).toBe(true);
    expect(messages[0]?.isStreaming).toBe(false);
  });

  it('detects branch counters from regenerated answers', () => {
    const doc = loadFixture(branchedTranscript());
    const assistant = extractConversation(doc, CONVERSATION_URL).messages[1];
    expect(assistant?.branchIndex).toBe(2);
    expect(assistant?.branchTotal).toBe(3);
    expect(assistant?.branchKey).toBe('branch-2-of-3');
  });

  it('records attachment metadata without claiming to have the bytes', () => {
    const doc = loadFixture(attachmentTranscript());
    const messages = extractConversation(doc, CONVERSATION_URL).messages;
    const fileRef = messages[0]?.attachmentRefs[0];
    expect(fileRef?.filename).toBe('q3-report.pdf');
    expect(fileRef?.mimeType).toBe('application/pdf');
    expect(fileRef?.byteSize).toBe(24576);
    // Historical/rendered attachments expose no bytes.
    expect(fileRef?.metadataOnly).toBe(true);
    expect(fileRef?.relation).toBe('referenced_historical');
  });

  it('records generated images as metadata only', () => {
    const doc = loadFixture(attachmentTranscript());
    const assistant = extractConversation(doc, CONVERSATION_URL).messages[1];
    const image = assistant?.attachmentRefs.find((r) => r.relation === 'generated_by_assistant');
    expect(image).toBeDefined();
    expect(image?.metadataOnly).toBe(true);
  });

  it('produces an empty result on an unrecognised page instead of guessing', () => {
    const doc = loadFixture('<main><div>Some unrelated page</div></main>');
    expect(extractConversation(doc, CONVERSATION_URL).messages).toHaveLength(0);
  });

  it('scales to a long transcript', () => {
    const doc = loadFixture(longTranscript(200));
    expect(extractConversation(doc, CONVERSATION_URL).messages).toHaveLength(200);
  });
});

describe('parts to text', () => {
  it('renders a table as a readable grid', () => {
    const parts = extractParts(
      (() => {
        const div = document.createElement('div');
        div.innerHTML =
          '<table><thead><tr><th>A</th><th>B</th></tr></thead><tbody><tr><td>1</td><td>2</td></tr></tbody></table>';
        return div;
      })(),
    );
    expect(partsToText(parts)).toBe('A | B\n--- | ---\n1 | 2');
  });

  it('renders nested lists with indentation', () => {
    const div = document.createElement('div');
    div.innerHTML = '<ul><li>one<ul><li>nested</li></ul></li><li>two</li></ul>';
    expect(partsToText(extractParts(div))).toBe('- one\n  - nested\n- two');
  });
});

describe('workspace observation', () => {
  it('reports the label and strong signal', () => {
    const doc = loadFixture(basicTranscript({ workspaceLabel: "TechSara's Workspace" }));
    const observed = observeWorkspace(doc);
    expect(observed.label).toBe("TechSara's Workspace");
    expect(observed.signals).toContain('workspace_label_match');
    expect(observed.looksPersonal).toBe(false);
  });

  it('reports the workspace id when present', () => {
    const doc = loadFixture(basicTranscript({ workspaceId: 'ws-company-1' }));
    const observed = observeWorkspace(doc);
    expect(observed.sourceWorkspaceId).toBe('ws-company-1');
    expect(observed.signals).toContain('workspace_id_match');
  });

  it('flags a personal workspace', () => {
    const doc = loadFixture(basicTranscript({ personal: true }));
    expect(observeWorkspace(doc).looksPersonal).toBe(true);
  });

  it('reports no signals on an unrecognised page', () => {
    const doc = loadFixture('<main></main>');
    expect(observeWorkspace(doc).signals).toHaveLength(0);
  });
});

describe('html sanitisation', () => {
  it('strips scripts, handlers, iframes and javascript urls', () => {
    const doc = loadFixture(hostileTranscript());
    const assistant = extractConversation(doc, CONVERSATION_URL).messages[0];
    const cleaned = sanitizeHtml(assistant?.html ?? '') ?? '';

    expect(cleaned).not.toContain('<script');
    expect(cleaned).not.toContain('onerror');
    expect(cleaned).not.toContain('onclick');
    expect(cleaned).not.toContain('javascript:');
    expect(cleaned).not.toContain('<iframe');
    expect(cleaned).toContain('Innocent looking text');
  });

  it('keeps safe rich text and adds rel to links', () => {
    const cleaned =
      sanitizeHtml(
        '<p><strong>bold</strong></p><a href="https://example.com">link</a><pre><code class="language-ts">x</code></pre>',
      ) ?? '';
    expect(cleaned).toContain('<strong>bold</strong>');
    expect(cleaned).toContain('rel="noopener noreferrer"');
    expect(cleaned).toContain('language-ts');
  });

  it('returns null for empty input', () => {
    expect(sanitizeHtml('')).toBeNull();
    expect(sanitizeHtml(null)).toBeNull();
  });

  it('does not execute anything while cleaning', () => {
    const spy = { called: false };
    (globalThis as unknown as Record<string, unknown>).__pwned = () => {
      spy.called = true;
    };
    sanitizeHtml('<img src=x onerror="globalThis.__pwned()"><script>globalThis.__pwned()</script>');
    expect(spy.called).toBe(false);
  });
});

describe('diagnostics', () => {
  it('reports page shape without any content', () => {
    const doc = loadFixture(basicTranscript());
    const diagnostics = adapterDiagnostics(doc);
    expect(diagnostics.turnCount).toBe(2);
    expect(diagnostics.userTurns).toBe(1);
    expect(diagnostics.assistantTurns).toBe(1);
    expect(diagnostics.hasScrollContainer).toBe(true);
    expect(JSON.stringify(diagnostics)).not.toContain('expense policy');
  });
});
