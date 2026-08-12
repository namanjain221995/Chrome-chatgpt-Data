# Chrome extension

Manifest V3, built for managed company Chrome profiles. It archives approved
company-workspace ChatGPT conversations and nothing else.

Source of truth for selectors and parsing:
[`apps/chrome-extension/src/modules/dom-adapter.ts`](../apps/chrome-extension/src/modules/dom-adapter.ts).
Everything else imports from it, so a ChatGPT UI change is a one-file fix plus a
new sanitized fixture.

## Never captured

Enforced in code, not just documented:

* raw keystrokes
* unsent drafts — the composer element is excluded from every extraction path
* passwords, cookies, `localStorage`, `sessionStorage`
* ChatGPT authentication or session tokens
* hidden model reasoning / chain-of-thought
* personal workspaces
* any site other than `chatgpt.com` and `chat.openai.com`

`host_permissions` is limited to those two origins. The content script runs in
an `ISOLATED` world, never assigns page HTML, and never reads page cookies or
storage. `scripts/verify_extension_package.sh` fails the build if the packaged
manifest ever widens its content-script matches.

## Fail-closed policy

The extension does not decide whether to capture. It fetches a signed runtime
policy from the backend and obeys it:

```text
GET /api/v1/config  ->  { capture_active, workspace_rules, limits, signature }
```

`src/modules/workspace-verifier.ts` returns "not verified" — meaning capture
nothing — for any of: no config, kill switch on, capture gates closed, URL not
approved, personal workspace detected, label mismatch, workspace id not
allowlisted, no signals, rules unconfigured.

Local extension settings and managed-storage values cannot turn capture on.
Both `BROWSER_CONTENT_CAPTURE_ENABLED` and
`OPENAI_WRITTEN_AUTHORIZATION_CONFIRMED` must be true **on the server**, and
`KILL_SWITCH_ENABLED` must be false, before `capture_active` is ever true.

## Live message capture

`src/modules/live-observer.ts`. Streaming tokens are never uploaded.

```text
employee sends a message
        v
committed user message appears in the transcript   (never the composer)
        v
user message recorded
        v
assistant begins streaming
        v
observed in memory only, DOM changes debounced
        v
streaming ends, or no relevant change for quietMs (default 2000 ms)
        v
one complete assistant version emitted
```

If the tab is hidden or unloaded mid-stream, the in-flight text is emitted once
with `completion_status = "partial"` rather than being lost. A later complete
capture supersedes it as a new version; it never overwrites history.

Idempotency keys are deterministic — derived from conversation, message and
content hash — so a retry after a network failure deduplicates server-side
instead of creating a duplicate row.

## Whole-current-conversation backfill

`src/modules/conversation-backfill.ts`. Best effort, bounded, and honest about
what it achieved.

```text
open a conversation
        v
detect the conversation id from the route
        v
record the exact original scroll position
        v
scroll the transcript upward in steps
        v
wait for lazy-loaded older turns to render
        v
stop at: top reached | no growth for N steps | maxMessages | maxSeconds | maxScrolls
        v
extract every loaded message
        v
normalize
        v
upload in batches
        v
restore the original scroll position
```

It never clicks Send, Regenerate, Edit, Delete or Share, never opens another
conversation, and never crawls the sidebar. Limits come from the server
configuration, not from the client.

### Capture completeness

Every conversation carries a completeness value, and the value is never
optimistic:

| Value | Meaning |
| --- | --- |
| `complete_current_page` | The scroll reached the beginning of this conversation and every rendered turn was captured. |
| `partial_scroll_limit` | A configured limit stopped the backfill before the beginning. |
| `live_only` | Only messages observed live were captured; no backfill ran. |
| `reconciled` | A later pass merged additional turns into an earlier partial capture. |
| `unknown` | The state could not be determined. |
| `compliance_verified` | **Only** ever set by the authorized enterprise compliance feed, never by browser capture. |

Also tracked per capture session: whether the beginning was reached, message
count, last successful synchronization, and capture errors.

A browser extension cannot archive conversations an employee never opens. That
limitation is stated plainly to employees and auditors in
[CAPTURE_LIMITATIONS.md](CAPTURE_LIMITATIONS.md), and `scripts/docs_check.sh`
fails the build if that document stops saying so.

## Edits, regenerations, branches

* An edited prompt produces a new **message version**, marked `is_edit`. The
  original version is retained.
* A regenerated answer produces a new version marked `is_regeneration`.
* When the UI exposes a branch selector, the selected branch is recorded on the
  version (`branch_selected`) and the conversation's branches are stored in
  `conversation_branches`.
* Ordering is preserved with an explicit `sequence_index`, not by insertion
  order.

## Attachments

`src/modules/attachment-observer.ts` captures only File/Blob objects the
employee explicitly hands to ChatGPT:

* file picker (`<input type="file">`)
* clipboard paste
* drag and drop

It never reads the filesystem and never re-fetches a URL to reconstruct a file.
For attachments whose bytes the page does not expose, only metadata is stored
(`attachment_state = metadata_only`).

```text
extension computes SHA-256 with Web Crypto
        v
POST /api/v1/attachments/init  (name, size, mime, sha256, message link)
        v
backend validates size, content type, extension, ownership and the
conversation/message relationship, then returns a short-lived presigned PUT
        v
browser uploads the bytes directly to S3
        v
POST /api/v1/attachments/complete
        v
worker verifies the checksum, then promotes quarantine/ -> clean/
```

Bytes never transit FastAPI. The presigned URL pins bucket, key, content type,
content length and the SHA-256 checksum. No AWS credential exists in the page
or the extension, and no S3 object is ever public.

## Offline queue

`src/modules/offline-queue.ts`, backed by IndexedDB — deliberately not
`chrome.storage.sync`, which would replicate message bodies to a personal
Google account.

Bounded on three axes, oldest evicted first: 10 000 items, 50 MiB, 7 days.
Retries use exponential backoff with a maximum attempt count; a permanently
rejected item is dropped rather than retried forever.

## Employee-facing UI

* **Popup:** current capture status, workspace verification verdict in plain
  language, last successful sync, queue depth, and the company notice.
* **Options page:** what is and is not captured, who to contact, and a link to
  the employee notice. It contains no switch that can enable capture.

## Build and package

```bash
make extension-build     # tsc --noEmit + three Vite bundles
make extension-zip       # deterministic ZIP + SHA-256 + package verification
```

Separate, self-contained bundles are produced for the service worker, the
content script and the extension pages; none of them shares a chunk, because
an MV3 service worker cannot load a shared chunk at runtime.

The ZIP is byte-reproducible: entries are sorted, timestamps pinned, no OS
metadata. Building the same commit twice yields the same SHA-256, which is what
lets IT verify that the artifact they push through Chrome Enterprise is the one
CI built. CI proves this by packaging twice and comparing.

Artifact name: `techsara-chatgpt-extension-<git-sha>.zip`, uploaded by
`ci.yml`. Releases attach `techsara-chatgpt-extension-<tag>.zip` plus its
`.sha256`.

`scripts/verify_extension_package.sh` asserts the ZIP contains
`manifest.json`, `service-worker.js` and `content-script.js`, and contains no
`.env`, no source map, no key material, and no credential-shaped string.

## Tests

No test ever drives a live ChatGPT account. `apps/chrome-extension/tests`
covers parsing and behaviour against sanitized DOM fixtures in
`tests/fixtures/transcripts.ts`:

* `dom-adapter.test.ts` — selector and parsing behaviour, composer exclusion
* `capture-behaviour.test.ts` — streaming stabilization, partial-on-unload,
  edits, regenerations, backfill limits and completeness values
* `queue-and-attachments.test.ts` — queue bounds, eviction, backoff, attachment
  hashing and the presigned-upload handshake

```bash
make test-extension
```

## Deployment to managed profiles

See [CHROME_ENTERPRISE_DEPLOYMENT.md](CHROME_ENTERPRISE_DEPLOYMENT.md) for
force-install policy, the managed-storage schema, and the pilot sequence.
After the first packaged build, put the extension id into
`/techsara-chat-archive/extension_ids` and redeploy so the backend's CORS
allowlist accepts it.
