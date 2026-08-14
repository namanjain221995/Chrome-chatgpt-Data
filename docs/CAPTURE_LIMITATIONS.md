# What this system does and does not capture

This is the honest-scope document. Everything here is enforced in code and
covered by tests; nothing in it is aspirational. If you only read one document
before approving this system, read this one.

---

## The short version

**The extension can archive the complete currently opened conversation, after
loading its messages.** It scrolls the transcript upward to pull older turns
into the page, parses them in order, and restores your scroll position.

**It continuously archives new committed messages.** Every message you send and
every answer you receive in the managed company workspace is archived once it is
final.

**It does not archive unsent text.** Anything typed in the composer and never
sent is never read. The parser refuses to look inside `textarea`,
`contenteditable` or `input` elements, or inside any `form` that wraps such a
widget without also wrapping the transcript.

That last clause is deliberate and was learned the hard way. The rule used to
be "never read anything inside any `form`". ChatGPT nests the rendered
transcript inside a form, so that rule silently discarded **every** message
while still appearing to work -- the archive stayed empty and nothing reported
an error. Drafts remain unreachable, because the composer is excluded by the
widget selectors themselves, not by the shape of its ancestors.

**It cannot guarantee that every old conversation is archived.** A browser
extension only sees what the browser renders. Conversations you never open in
this browser profile are not archived by the extension. Company-wide coverage
requires the authorized OpenAI Enterprise Compliance feed
(see [COMPLIANCE_ADAPTER.md](COMPLIANCE_ADAPTER.md)).

**It cannot capture hidden model reasoning.** Internal chain-of-thought is not
rendered in the page, so it does not exist as far as this system is concerned.

**Historical attachment originals may not be recoverable.** A file shown in an
older conversation is often only a rendered tile. When the page does not expose
the bytes, the archive stores metadata and says so
(`state = metadata_only`) rather than pretending to hold the file.

---

## Captured, when available and authorized

| Data | Source | Notes |
| --- | --- | --- |
| Conversation id, URL, title | DOM adapter | From the route and the transcript header |
| Workspace identity | DOM adapter + server policy | Verified server-side; fails closed |
| Employee identity | Google Workspace OIDC | From a verified ID token, never a self-reported email |
| User messages | Committed transcript entries | Only after the message appears in the transcript |
| Assistant messages | Committed transcript entries | One upload per stable version, not per token |
| Visible tool messages and citations | DOM adapter | Only what is rendered |
| Message order | `sequence_index` | Authoritative after a backfill |
| Message timestamps | `time[datetime]` when present | Null when the page exposes none |
| Code, headings, lists, tables, links | `message_parts` | Language, cells and hierarchy preserved |
| Sanitized rich text | `sanitized_html` | Allowlist-sanitised in the content script at extraction, then again server-side. Raw page HTML never leaves the page |
| Edited prompts | New `message_versions` row | The original version is never overwritten |
| Regenerated answers | New `message_versions` row | Both variants retained |
| Branch relationships | `conversation_branches` | Only when the page shows a counter such as "2 / 3" |
| Partial answers | `completion_status = partial` | When a tab closes mid-stream |
| Uploaded images and files | `File`/`Blob` handed to ChatGPT | Only bytes the browser gives us directly |
| Attachment metadata | Rendered file tiles | Metadata only; see below |
| Generated-image metadata | Rendered image elements | Metadata only unless an authorized export provides bytes |
| Employee feedback | Popup and options UI | useful / incorrect / approved / rejected / note |

## Never captured

| Never captured | How that is enforced |
| --- | --- |
| Unsent drafts | The adapter refuses to read composer elements; a test asserts draft text never appears in any extracted payload |
| Raw keystrokes | No keyboard listener exists anywhere in the extension |
| Password fields | No `input` element is ever read |
| Browser cookies | The manifest requests no `cookies` permission; the manifest validator fails the build if one appears |
| ChatGPT session tokens | Never read; the extension holds only its own backend token |
| Hidden model reasoning | Not rendered, therefore not present |
| Personal-workspace conversations | Rejected in the extension verifier, again in the API schema, and again in the ingestion service |
| Unverifiable workspaces | Verification fails closed: no signals means no capture |
| Other websites | Content script matches only `https://chatgpt.com/*` and `https://chat.openai.com/*` |

---

## Coverage: what "archived" actually means

Every conversation carries a `capture_completeness` value. It is deliberately
conservative, and it is never upgraded by a guess.

| Value | Meaning |
| --- | --- |
| `complete_current_page` | The backfill reached the top of the conversation. Everything the page could render was archived. |
| `partial_scroll_limit` | A time, message or scroll limit stopped the backfill first. Older messages exist and were not archived. |
| `live_only` | Only messages observed live are archived. The conversation was never backfilled. |
| `compliance_verified` | The authorized enterprise compliance feed confirmed this record. **Only that feed can set this value** — browser capture is rejected if it claims it. |
| `reconciled` | A partial record was later completed from another observation. |
| `unknown` | Not yet determined. |

### The four coverage gaps, stated plainly

1. **Conversations never opened in this browser.** The extension archives a
   conversation when the employee opens it. It does not crawl the sidebar, click
   through conversations, or fetch history through undocumented endpoints.
2. **Other devices and browsers.** A conversation held on a phone, at home, or in
   a different browser profile is invisible to this installation.
3. **Deleted-before-capture conversations.** Anything removed upstream before it
   was ever opened cannot be recovered by the extension.
4. **Attachment bytes in historical conversations.** The rendered page usually
   shows a filename and a size, not the original file.

Gaps 1–3 are closed only by the authorized OpenAI Enterprise Compliance feed.
Gap 4 is closed only by an authorized export source.

---

## Live capture: why nothing is uploaded per token

The observer watches the page continuously but uploads only complete versions:

1. A user message is recognised only once it appears in the transcript.
2. An assistant message is tracked while it streams, **in memory only**.
3. A debounced `MutationObserver` waits for a configurable quiet period
   (`stable_response_quiet_ms`, default 2000 ms).
4. When generation visibly ends, or nothing relevant changes for that period,
   exactly one complete version is uploaded.
5. If the tab closes, the route changes or the browser suspends first, one
   record is persisted with `completion_status = partial`.
6. That partial record is reconciled when the conversation is opened again, or
   when compliance data arrives.

The practical consequence: a 2,000-token answer produces **one** upload, not one
per token, and never a stream of partial fragments.

---

## The backfill, precisely

When you open a conversation (or press **Archive current conversation now**):

1. The current scroll position is saved.
2. The transcript is scrolled upward in steps so ChatGPT loads older turns.
3. Scrolling stops at the top of the conversation, or at a safety limit:
   2,000 messages, 120 seconds, or 400 scroll steps (all server-configured).
4. Every visible message is parsed in order.
5. The original scroll position is restored — a test asserts this, including
   when parsing throws.
6. A non-blocking status appears: `Archived 84 messages from this conversation.`

The extension never clicks Send, Regenerate, Edit, Delete, Share, or any other
control on your behalf. It is strictly read-only with respect to your account.

---

## Server-side gates

Browser content extraction is fully implemented but **disabled by default**. It
activates only when both of these are true on the server:

```env
BROWSER_CONTENT_CAPTURE_ENABLED=true
OPENAI_WRITTEN_AUTHORIZATION_CONFIRMED=true
```

Neither can be overridden from the extension. The configuration is signed and
versioned; a tampered local cache is rejected by shape validation
(`capture_active` without both gates is treated as invalid), and every ingest
request is re-checked server-side regardless. A kill switch
(`KILL_SWITCH_ENABLED=true`) stops capture immediately without redeploying.

`OPENAI_WRITTEN_AUTHORIZATION_CONFIRMED` exists so that a written authorization
decision is recorded before any content is archived. Setting it is a policy act,
not a technical one.

---

## What the employee sees

The popup states current status without overclaiming. The options page carries a
prominent panel headed **"What this does not cover"** listing every gap above.
No screen in the product says "all history archived" — because it would not be
true.

---

## Related documents

- [PRIVACY_AND_EMPLOYEE_NOTICE.md](PRIVACY_AND_EMPLOYEE_NOTICE.md) — the notice given to employees
- [COMPLIANCE_ADAPTER.md](COMPLIANCE_ADAPTER.md) — how company-wide coverage is obtained
- [SECURITY.md](SECURITY.md) — controls and threat model
- [ARCHITECTURE.md](ARCHITECTURE.md) — how the pieces fit together
