# The TechSara ChatGPT Conversation Archive — An Engineer's Walkthrough

*Written for the person who inherits this. Read section 8 before you change anything.*

---

## 1. What this system is

Roughly 250 employees use ChatGPT for work. The company needs an auditable archive of those conversations, and it needs one that cannot quietly become surveillance. Those two goals pull against each other, and every design decision here is a resolution of that tension.

The constraints are hard: one EC2 host, no container registry, no external queue, no AWS credentials outside the instance role, no public port, and a legal requirement that capture stays off until written authorization exists.

The shape of the solution: a Chrome MV3 extension *reports* what is rendered on the page; a FastAPI backend *decides* whether any of it may be stored. Policy lives in exactly one place server-side, is re-derived on every write, and defaults to refusing. Bytes go to S3; metadata, hashes and keys go to PostgreSQL, which also serves as the durable job queue. A named Cloudflare Tunnel is the only way in.

---

## 2. The map

### Runtime topology

```
                        ┌──────────────────────────────────────┐
   Employee browser     │           Cloudflare edge            │
  ┌──────────────────┐  └───────────────┬──────────────────────┘
  │ content script   │                  ▲  outbound-initiated
  │  (ISOLATED world)│                  │  tunnel, no inbound port
  │  · dom-adapter   │                  │
  │  · verifier      │       ┌──────────┴──────────────────────────────────┐
  └────────┬─────────┘       │        EC2 (Amazon Linux 2023)              │
           │ runtime msg     │  ┌───────────────────────────────────────┐  │
  ┌────────▼─────────┐       │  │ cloudflared  (network: egress only)   │  │
  │ service worker   │       │  │ digest-pinned, --no-autoupdate        │  │
  │  · signed config │       │  │ 127.0.0.1:2000 metrics (loopback)     │  │
  │  · OIDC/PKCE     │       │  └──────────────┬────────────────────────┘  │
  │  · IndexedDB queue│──────┼─────────────────▼─── http://api:8000 ─────┐ │
  └────────┬─────────┘  HTTPS│  ┌───────────────────────────────────────┐│ │
           │                 │  │ api  (gunicorn/uvicorn, expose:8000)  ││ │
           │ presigned PUT   │  │   networks: backend + egress          ││ │
           │ (bytes only)    │  └───────┬───────────────────┬───────────┘│ │
           │                 │          │                   │            │ │
           │                 │  ┌───────▼──────────┐  ┌─────▼──────────┐ │ │
           │                 │  │ postgres 16      │  │ worker         │ │ │
           │                 │  │ networks:        │  │ claims jobs    │ │ │
           │                 │  │   backend ONLY   │◄─┤ FOR UPDATE     │ │ │
           │                 │  │ (internal:true)  │  │ SKIP LOCKED    │ │ │
           │                 │  └───────▲──────────┘  └─────┬──────────┘ │ │
           │                 │          │                   │            │ │
           │                 │  ┌───────┴──────────┐        │            │ │
           │                 │  │ migrate (oneshot)│        │            │ │
           │                 │  │ backup (loop)    │        │            │ │
           │                 │  │ compliance-poller│        │            │ │
           │                 │  │   [profile]      │        │            │ │
           │                 │  │ pgadmin [profile]│        │            │ │
           │                 │  │   127.0.0.1:5050 │        │            │ │
           │                 │  │   backend+admin  │        │            │ │
           │                 │  └──────────────────┘        │            │ │
           │                 └──────────────────────────────┼────────────┘ │
           │                                                │              │
           └────────────────────────────────────────────────┼──────────────┘
                                                            ▼
                              ┌──────────────────────────────────────────┐
                              │  S3  techsara-chatgpt  (us-east-1)       │
                              │  raw/events/   normalized/conversations/ │
                              │  attachments/{quarantine,clean,curated}/ │
                              │  exports/jsonl/   backups/               │
                              │  reached via EC2 instance role only      │
                              └──────────────────────────────────────────┘

   Secrets: SSM /techsara-chat-archive/*  ──►  fetch_ssm_secrets.sh  ──►
     ${DATA_ROOT}/secrets/*  (root-owned files, 0440)  +  .env.production (0600, no values)
```

### Subsystems

**`apps/chrome-extension/src/content/` + `modules/`** — the page half. Runs in an ISOLATED world on two ChatGPT origins. Owns: DOM knowledge (`dom-adapter.ts` is the *only* file that knows a ChatGPT selector), workspace verification (`workspace-verifier.ts`), stability-based emission (`live-observer.ts`), bounded scroll backfill (`conversation-backfill.ts`), and user-supplied file capture (`attachment-observer.ts`). It holds no credentials and makes no network calls.

**`apps/chrome-extension/src/background/`** — the MV3 service worker. Owns managed-policy lookup, the signed server config cache, OIDC/PKCE sign-in, the IndexedDB offline queue, and every backend HTTP call. Woken by two alarms (flush 1 min, config 10 min) and by incoming runtime messages.

**`services/backend/app/core/`** — the trust boundary. `config.py` turns env vars into a validated `Settings` singleton that *refuses to import* on unsafe production configuration. Also: structured logging with two-tier redaction, HMAC/SHA-256 primitives, OIDC verification plus first-party HS256 tokens, an in-process rate limiter, bleach sanitisation, and the async engine. Deliberately contains no business policy.

**`services/backend/app/api/`** — the entire HTTP surface: middleware chain, the dependency graph that turns a bearer token into a live `Principal` (user + org + device), four v1 routers, health probes, self-hosted Swagger. Owns authn/authz/rate limiting/error envelope. Owns no business logic — every route calls into `services/`.

**`services/backend/app/services/`** — where the product lives. `policy.py` is the capture decision. `ingest.py` (996 lines, the largest file) is the write path: idempotency, three-tier message identity, append-only versioning, partial→complete reconciliation. `storage.py` is the *only* S3 client. `attachments.py`, `exports.py`, `snapshots.py`, `retention.py`, `partitions.py`, `runtime_config.py`, `accounts.py`, `audit.py`, `compliance_import.py`.

**`services/backend/app/models/` + `alembic/`** — 25 tables, the `_enum()` VARCHAR+CHECK helper, three monthly RANGE-partitioned event tables, two migrations (`0001_initial`, `0002_fts`).

**`services/backend/app/workers/` + `services/jobs.py`** — a PostgreSQL-backed durable queue (no Redis, no Celery), one worker process, nine handlers, and an optional default-disabled compliance poller with its HTTP adapter in `app/adapters/openai_compliance.py`.

**`packages/schemas/`** — 19 JSON Schemas generated from the backend Pydantic models by `scripts/generate_schemas.py`. `make schema-check` regenerates and `git diff --exit-code`s; `validate-schemas.mjs` validates extension-shaped payloads against them. Drift on either side fails the build.

**`compose.prod.yaml` + `scripts/` + `.github/workflows/`** — one image running five roles, three Docker networks, and the deploy/rollback/backup/verify machinery.

### How they connect

`Settings` flows from `core/config.py` into `services/policy.py` (capture decisions), `services/storage.py` (S3), and `services/runtime_config.py` (the signed document the extension fetches). Every ingest route funnels through `services/ingest.py:95 build_context`, which is the single choke point where policy runs before any write. The write path enqueues jobs on the *same session* as the domain rows, so a job can never reference a rolled-back row. The worker reaches back into `storage.py`, `snapshots.py`, `exports.py`, `retention.py`.

---

## 3. The data model

Twenty-five tables. Think of them in six groups.

**Identity.** An `Organization` (single-tenant today, slug hardcoded `"techsara"` at `services/backend/app/services/accounts.py:26`) owns `Workspaces`, `Users` and their `Devices`. A `Workspace` is the archive's notion of "a ChatGPT enterprise workspace" — its identity is a `workspace_hash`, a truncated SHA-256 of `"{org_slug}|{workspace_id_or_lowercased_label}"` (`app/services/policy.py:48-51`). That hash, not the workspace name, is what appears in every S3 key. A `Device` carries a `refresh_token_hash` (never the token), a rotation counter, a `session_id` and a `revoked_at` kill switch. `UserIdentity` maps an OIDC (issuer, subject) pair onto a user.

**The content tree.** `Conversation → Message → MessageVersion → MessagePart`, with `ConversationBranch` off to the side.

The load-bearing idea is **append-only versioning**: `messages` is stable turn identity; `message_versions` is append-only and nothing is ever overwritten. An edit or a regeneration inserts a new version and moves `messages.current_version_id`. `Message.version_count` and `UNIQUE(message_id, version_number)` keep the ordering honest; `is_edit`/`is_regeneration` record the cause.

A message has **three identities**, in priority order, because ChatGPT does not reliably expose a stable id: (1) `source_message_id`, backed by a *partial* unique index that only applies when it is non-null; (2) a deterministic fingerprint over conversation + role + normalised content + `sequence_index // 5` + a 300-second timestamp bucket — the coarsening is deliberate, so a message keeps its fingerprint when a backfill shifts its position; (3) content identity, any same-role message in the conversation with the same `normalized_sha256`.

Two hashes per version: `content_sha256` is the exact SHA-256 of the raw client text (the anti-tamper check); `normalized_sha256` is over NFKC-normalised, whitespace-collapsed, casefolded text (the dedupe key). `UNIQUE(message_id, normalized_sha256)` is what makes an identical re-capture collapse instead of duplicating.

`Conversation.capture_completeness` is the honesty field — a monotonic ladder `unknown < live_only < partial_scroll_limit < complete_current_page < reconciled < compliance_verified` (`app/services/ingest.py:339-346`). It can only ever go up, and an incoming `compliance_verified` from the browser is discarded outright.

**Attachments.** `Attachment` is a state machine expressed as three separate S3 key columns — `quarantine_s3_key`, `clean_s3_key`, `curated_s3_key` — plus paired *declared* vs *verified* size/hash/MIME columns, so the worker can compare what the client claimed against what the bytes actually are. States: `pending → quarantine → clean`, with `rejected`, `expired` and `metadata_only` as terminal side states. `MessageAttachment` binds a verified object to its message.

**Event tables (monthly RANGE-partitioned).** `capture_events` holds the verbatim client payload as JSONB plus its hash — the immutable receipt. `source_events` holds upstream compliance events. `audit_events` holds administrative reads, exports, auth decisions and retention actions. All three grow at message rate, so retention is a metadata-only `DROP TABLE` on an old partition.

This forces one subtlety worth internalising: **PostgreSQL requires a unique constraint on a partitioned table to include the partition key**, which would make idempotency month-local. So two tiny *non-partitioned* side tables carry global uniqueness instead: `idempotency_keys` keyed `(organization_id, idempotency_key)`, and `source_event_keys` keyed `(organization_id, source, source_event_id)`. Every partitioned parent also has a DEFAULT partition created in migration 0001, so an insert can never fail merely because next month's partition is missing.

**Governance.** `Feedback` (the curation signal), `TrainingApproval` (default-never-export; a curated export requires an approved, conversation-level, `contains_secrets=false` row), `Export` (kind, filters, split ratios, manifest hash), `RetentionPolicy` (`retain_days`/`grace_days`/`action`, with exactly one default per org enforced by a unique partial index), `LegalHold`.

Legal hold is enforced *at the database level*: a CHECK constraint makes `legal_hold = true AND deleted_at IS NOT NULL` impossible on `conversations` and `messages` (`app/models/conversation.py:120-123`, `:219-222`).

**The queue.** `jobs` + `job_attempts`. Three purpose-built partial indexes: `uq_jobs_dedupe_active` (unique on `dedupe_key` only while status is `pending`/`running` — so a key is *coalescing*, not permanent dedupe), `ix_jobs_claim` (covering the claim's ORDER BY, `WHERE status = 'pending'`), and `ix_jobs_stale_locks`.

All three depend on the `_enum()` helper (`app/models/identity.py:28-44`) persisting the enum *value* (`'pending'`) rather than the member *name* (`'PENDING'`), via `values_callable`. Remove that and nothing errors — the queue just silently never claims a job again.

---

## 4. The critical path

One assistant answer, from the DOM to a curated export.

**1. The DOM mutates; nothing happens.** `apps/chrome-extension/src/modules/live-observer.ts:79` — the MutationObserver callback only re-arms a debounce timer built at `:63` with the server's `stable_response_quiet_ms`. This is the entire reason there is no per-token network traffic.

**2. Quiet reached; the whole transcript is re-parsed.** `live-observer.ts:123 emitStable()` calls `extractConversation` on the full document — there is no incremental parsing. It bails if the conversation id drifted (`:126`), records a first sighting silently, and emits exactly once when the signature is unchanged *and* `isStreaming === false` (`:149-152`).

**3. Parsing refuses drafts structurally.** `apps/chrome-extension/src/modules/dom-adapter.ts:467` — `extractMessage` opens with `isInsideComposer(element)`, an `element.closest()` against `FORBIDDEN_CONTAINERS` (`:92-98`): textarea, input, `[contenteditable="true"]`, `[data-testid="prompt-textarea"]`, form. The whole turn is dropped, not filtered.

**4. Workspace verified, in the page.** `apps/chrome-extension/src/modules/workspace-verifier.ts:55` — nine ordered fail-closed exits. Re-run immediately before every report at `content-script.ts:99`, never cached.

**5. Cross to the worker.** `content-script.ts:104` sends `MESSAGES_CAPTURED`. `service-worker.ts:264` re-checks `captureAllowed(config)` — a genuine independent re-derivation of all four gates plus expiry.

**6. Normalisation.** `apps/chrome-extension/src/modules/message-normalizer.ts:120` — HTML through an inert DOMParser allowlist, truncation (1 MB text / 2 MB HTML / 500 parts), `content_sha256`, and the idempotency key at `:129-137` which **deliberately excludes `sequence_index`** so a later backfill replays as duplicates rather than new rows.

**7. Durable enqueue, then upload.** `apps/chrome-extension/src/background/sync-engine.ts:49` and `:82` write to IndexedDB *before* anything is sent. `:110 flush()` dispatches, branching on `ApiError.retryable` (`api-client.ts:37`).

**8. Authentication.** `services/backend/app/api/deps.py:87 get_principal` — decode HS256, load the live User/Organization/Device rows, enforce active user, org match, `revoked_at is None`, ownership, and session binding (`device.session_id == claims.session_id`, `:116-117`). That last check is what makes revoke-and-relogin invalidate already-minted tokens without a blacklist.

**9. Backpressure.** `services/backend/app/api/v1/ingest.py:30 _guard_backpressure` → 503 with `Retry-After: 60` when pending job depth crosses the threshold.

**10. THE GATE.** `services/backend/app/services/ingest.py:106-107`:

```python
assert_browser_capture_allowed(settings)
decision = await resolve_workspace(session, ctx.organization, workspace, settings)
```

Every ingest and attachment route funnels through `build_context`. No row is written before it returns.

**11. Per-item isolation and checksum.** `services/ingest.py:777` wraps each batch item in a SAVEPOINT. `:522-532` verifies the declared `content_sha256` against `sha256_hex(payload.text)` and rejects before any write.

**12. Identity and versioning.** `services/ingest.py:432 _find_message` resolves through the three tiers. `:620-656` promotes an existing partial version in place to `RECONCILED`; otherwise `:658-701` appends a new `MessageVersion` at `version_count + 1` and moves `current_version_id`.

**13. The crux — one transaction.** `services/ingest.py:711-748`: the immutable `CaptureEvent` row, the `ARCHIVE_RAW_EVENT` job, and `mark_snapshot_stale`'s `BUILD_CONVERSATION_SNAPSHOT` job all go in on the same session. `enqueue_job` (`services/jobs.py:82-85`) uses `ON CONFLICT DO NOTHING` against the partial live-dedupe index, so N edits to one conversation coalesce into one snapshot job.

**14. Claim with a fencing token.** `services/jobs.py:93 claim_jobs` — `SELECT … FOR UPDATE SKIP LOCKED` ordered `priority ASC, run_after, created_at`, then a guarded UPDATE per id stamping a fresh `lock_token` and incrementing `attempts`. `complete_job` and `fail_job` both require the token to match, so a resurrected worker cannot overwrite its successor's result. `workers/worker.py:71` commits the claim immediately.

**15. S3 before the DB flag.** `services/backend/app/workers/handlers.py:98-101` — `put_json` first, *then* `event.archived_at = utcnow()`. A crash between them leaves an orphan S3 object, which is the deliberately chosen failure mode over a DB row pointing at nothing. Then `:104-108` back-fills `raw_s3_key` onto the `MessageVersion`.

**16. Snapshot.** `handlers.py:117` → `services/snapshots.py:205 write_snapshot` — assemble the canonical document, stamp `integrity.sha256` computed over everything above it (`:146`), PUT to `normalized/conversations/year=/month=/workspace=/conversation=/snapshot-NNNNNN.json`, write `snapshot_version`/`snapshot_s3_key`/`snapshot_sha256` back and clear `snapshot_stale` (`:236-239`).

Every write goes through `services/storage.py:217 put_bytes`, which unconditionally spreads `_encryption_args()` (`:191`) and sets `ChecksumSHA256` (`:234`). There is no code path that writes an unencrypted or unchecksummed object.

**17. Attachments (parallel).** `POST /attachments/init` → `services/attachments.py:110` re-asserts `assert_attachment_capture_allowed` (`:118`), validates, and calls `storage.py:318 presign_put`, which signs Bucket, Key, ContentType, **ContentLength** and the SSE headers plus `ChecksumSHA256`. Signing the length is what stops a client inflating the upload after the presign. The browser PUTs straight to S3 via `api-client.ts:230`, which bypasses the bearer-token path entirely. `complete` HEADs and size-checks; the worker (`handlers.py:147`) re-downloads, re-hashes, sniffs magic bytes, copies to `attachments/clean/`, and writes a `curated/` derivative only if `strip_metadata` (`services/exif.py:76`) actually changed the bytes.

**18. Export.** `api/v1/admin.py:209 assert_export_allowed` → insert `Export` → enqueue `RUN_EXPORT`. `services/exports.py:82-99` applies two independent gates for a curated training export: the `TRAINING_EXPORT_ENABLED` server flag *and* a per-conversation approved, conversation-level, `contains_secrets=false` `TrainingApproval`, *and* completeness in `{complete_current_page, reconciled, compliance_verified}`. `split_for_conversation` (`:48`) is a pure function of the conversation UUID, so no message of a conversation can land in two splits. `GET /admin/exports/{id}` mints short-lived presigned GETs and writes an `EXPORT_DOWNLOADED` audit row.

---

## 5. How capture is gated

This is the product. Everything else is plumbing.

### The three server flags

`BROWSER_CONTENT_CAPTURE_ENABLED` and `OPENAI_WRITTEN_AUTHORIZATION_CONFIRMED` must both be true, and `KILL_SWITCH_ENABLED` false. All three default to the safe value at `app/core/config.py:115-121`. The reporting form is `Settings.browser_capture_active` (`:300-306`); the *enforcing* form is `app/services/policy.py:54-73`, which raises in a fixed order with distinct codes — `kill_switch_active` → `capture_disabled` → `authorization_not_confirmed` — so an operator can tell which gate is shut. Attachments layer a fourth flag on top (`:76-80`).

The second gate is not a technical toggle. It is a record of a policy act. Nothing in the system distinguishes it from the first at runtime; the distinction is entirely that flipping it is a statement someone signs their name to.

### Workspace verification, three times

**In the page** (`workspace-verifier.ts:55`): nine ordered refusals — `no_config`, `kill_switch`, `capture_gates_closed`, `url_not_approved`, `personal_workspace` (an explicit personal marker ends the decision), `rules_unconfigured`, `id_not_allowlisted`, `label_mismatch`, `no_signals`.

**At the contract edge** (`app/schemas/ingest.py:89-94`): `ConversationUpsertIn._reject_unverified_personal` rejects a personal workspace as a 422 before the handler is entered. `WorkspaceRef` defaults to `kind="unverified", verified=False` (`schemas/common.py:73-76`) — fail-closed by default.

**In the service** (`app/services/policy.py:83-145`): the client's `verified` flag is treated as an untrusted hint and the answer is re-derived. Personal is rejected unconditionally, with the comment at `:91-92` stating that `PERSONAL_WORKSPACE_CAPTURE_ENABLED` "is never honoured, in any environment, even if set to true." If the server has *neither* an id allowlist *nor* a label, everything is refused as `workspace_policy_unconfigured` — refuse rather than guess. When ids are configured the reported id must be allowlisted *and* the client must have reported a strong signal; otherwise an exact case-insensitive label match plus a strong signal.

### The signed configuration

`app/services/runtime_config.py:153 get_signed_config` returns an HMAC-SHA256-signed, versioned, 900-second-TTL document. Sub-gates are ANDed with the master gate before serialisation (`:47-52`), so there is no field the extension can set to widen its own permissions. The two never-flags are unrepresentable in the contract: `app/schemas/auth.py:78-79` types them `Literal[False]`.

Client-side, `managed-config.ts:101-122 isUsableConfig` re-validates a document read back out of `chrome.storage.local` and rejects wrong schema version, expiry, non-HTTPS base URL, either never-flag not literally `false`, `capture_active` without both gates, or `capture_active` with the kill switch. `:125-135 captureAllowed` then re-derives the runtime decision from the raw gate fields rather than trusting `capture_active`. The HMAC itself is *not* verified client-side (the client cannot hold the key); the signature must merely be a non-empty string.

### Can an employee turn capture on locally?

No, for four independent reasons.

1. **There is no local control surface.** Grepping `src/` for `storage.local.set` finds four writers: the config cache, the device fingerprint, the status blob, the archived-id list. Nothing in `ui/` writes storage. The complete verb list across popup and options is SIGN_IN, SIGN_OUT, ARCHIVE_CURRENT_CONVERSATION, FLUSH_QUEUE, REFRESH_CONFIG, `openOptionsPage()`.
2. **A doctored cache is structurally rejected** by `isUsableConfig`.
3. **Patching the client is irrelevant**, because `build_context` re-decides server-side using only server settings.
4. **The gates are not employee-writable**: they live in SSM, rendered by `scripts/fetch_ssm_secrets.sh` into a root-owned 0600 file, defaulting to `"false"` when the parameter is absent (`:172-177`).

The honest caveat: a patched extension could still *parse* the DOM and ship payloads to its own service worker. Refusal happens at `service-worker.ts:264` and again at the server. Nothing is stored — but "capture nothing" means "store nothing", not "read nothing", on a tampered client.

### Never claim complete history

`CaptureCompleteness.COMPLIANCE_VERIFIED` is discarded on both the update path (`app/services/ingest.py:353-355`) and the create path (`:309-313`), which merges against `UNKNOWN` for exactly this reason. Grep confirms the only writers in the entire application are `app/services/compliance_import.py:221` and `:234`. `GET /sync/status` returns a server-authored `COVERAGE_STATEMENT` verbatim (`app/services/runtime_config.py:32`), and the options page's "What this does not cover" block disclaims unopened conversations, other devices, drafts, hidden reasoning and metadata-only historical files.

---

## 6. Build, test, deploy, rollback

### What runs when

`make verify` (Makefile:241-259) is the merge gate, in order: lint → typecheck → test → test-integration → migration-check → schema-check → extension-build → extension-zip → compose-config → test-deploy-scripts → security-check → docs-check → build-image → test-production-compose → test-compose → restore-test → load-test.

CI (`.github/workflows/ci.yml`) runs seven independent jobs with no `needs` between them: **backend** (ruff, mypy, unit pytest), **extension** (lint, typecheck, vitest, build, manifest validation, schema validation, package twice and compare SHA-256, package verification), **database** (real PostgreSQL service; upgrade → `alembic check` → integration pytest → `downgrade -1` → re-upgrade → check → pg_dump/pg_restore round trip), **images-and-compose** (buildx, non-root assertion, topology check, shellcheck/actionlint, deploy-script tests, both Compose smokes, k6 smoke), **schemas** (regenerate + `git diff --exit-code`), **security** (removed technologies, prohibited AWS services, secret scan, gitleaks, pip-audit, npm audit), **docs**.

The test suite has three stacks and one organising rule: nothing in the gate touches AWS, a live ChatGPT account, or a real OIDC provider. S3 is `FakeStorageService` plus, separately, a botocore `Stubber` with `signature_version=UNSIGNED` that asserts the exact `PutObject` params including `ServerSideEncryption` and `ChecksumSHA256`. OIDC is a locally-keyed `LocalIdentityProvider`. The extension runs against hand-written sanitized fixtures in `apps/chrome-extension/tests/fixtures/transcripts.ts`, one of which contains the literal string `DRAFT: this must never be captured` so the invariant-1 test has something to fail on.

Integration isolation is transaction rollback, not truncation: `conftest.py:110-122` binds a sessionmaker to a connection inside a transaction and rolls back at teardown. Two tests deliberately escape this to prove concurrency behaviour, and clean up inline.

### What blocks what

`ci.yml` has no `push` trigger — it would race itself inside deploy.yml's concurrency group. `deploy.yml` declares `needs: validate` where `validate` is the whole of `ci.yml` as a reusable workflow, with `concurrency: production-deploy, cancel-in-progress: false` so a second push queues rather than interrupting a migration.

### Deploy

Push to `main` → CI → **Resolve** (`deploy.yml:44-65`: 40-hex, and `git merge-base --is-ancestor <sha> origin/main`) → **Configure SSH** (`.github/actions/ec2-ssh/action.yml`: key validated with `ssh-keygen -y` before use, host key pinned from a repo *variable* or learned once with a loud warning, `StrictHostKeyChecking yes` unconditionally) → **Preflight** (three severities: FATAL fails the build; NOTREADY *exits 0* and skips the deployment on a green build; NOTUNNEL downgrades to `--without-tunnel`) → **Sync** (`git reset --hard` as a separate root step, so the instance runs the deployed commit's deploy script) → **Deploy**.

`scripts/deploy_production.sh` then, holding a `flock` on `/var/lock/techsara-chat-archive-deploy.lock`:

1. Record the rollback target from `deploy/current-release` *before* anything changes.
2. Render SSM → `.env.production` (0600, **no secret values**, only `*_FILE` paths) + root-owned secret files. Assert `IMAGE_TAG == DEPLOY_SHA`.
3. `verify_production_config.sh` — the topology assertions (see below).
4. `up -d postgres`, poll `pg_isready` 60×2s.
5. `compose build --pull api` producing `IMAGE_NAME:<sha>` on the host. Pre-migration `pg_dump` only if `alembic_version` exists.
6. `compose run --rm migrate` → `alembic upgrade head`. Then `up -d api worker backup [cloudflared]`.
7. Health gates in order: internal `/health/ready` (60×5s, with a `Host:` header or TrustedHostMiddleware 400s it), worker/backup container state, cloudflared `/ready`, `s3api head-bucket`, then the public URL.
8. Rotate `current-release` → `previous-release`, write the new record, `docker image prune -f` — **dangling layers only**, with an explicit comment forbidding `system prune -a` because that would delete the previous release image and make fast rollback impossible.

The public health check is deliberately outside the rollback contract (`:359-362`): everything inside the host passed, so an unreachable public URL is an edge problem and rolling back would move a working stack backwards without touching the cause.

`scripts/verify_production_config.sh` asserts, against the resolved Compose config: the exact eight-service set; only cloudflared (loopback 2000) and admin-profile pgAdmin (loopback 5050) may publish ports; api uses `expose: ["8000"]` with no TLS flags; PostgreSQL is on `backend` only; the tunnel image is `@sha256:`-pinned with `--no-autoupdate`, no `trycloudflare`, no `--token`, on `egress` only; `backend.internal is True`; **any service publishing a port must be on a non-internal network** (Docker accepts the binding on an internal-only container and then silently never creates it — this shipped once); no `:latest`; no static AWS credentials; and the connection budget `(pool + overflow) × (API_WORKERS + 2) + 15 ≤ max_connections`.

### Rollback

`scripts/rollback_production.sh` restores the application only. **The schema is never reversed** — grep confirms no `alembic downgrade` in either script.

It resolves the target from argv or `previous-release`, requires the commit to already be local (no fetch), re-renders SSM at the target tag, reads the recorded `PUBLIC_INGRESS` posture, reuses `IMAGE_NAME:<target>` if present or rebuilds, recreates services, health-checks, and **always writes the release record with `API_HEALTHY`/`TUNNEL_HEALTHY` before deciding whether to die** — so the recorded state always matches reality.

**Can the previous app run against the newer schema?** For the one migration that exists, provably yes. `0002_fts` adds `message_versions.search_tsv` as a *nullable*, *generated* (`Computed(..., persisted=True)`) column plus a GIN index. The previous release's ORM has no such attribute, so its INSERTs never name it, and being generated it could not be written anyway. Nothing is renamed, narrowed or dropped.

But the guarantee is **review-only**. `make migration-check` runs upgrade → `alembic check` → `downgrade -1` → upgrade → check. That proves reversibility and model/schema agreement. Nothing anywhere runs the *previous release's* test suite against the *new* schema. `docs/ROLLBACK.md:114-117` says so honestly. The property holds today because there is one trivial migration, not because anything would catch a violation.

### Backup and restore

`scripts/backup_loop.sh` (the `backup` service entrypoint) waits on `pg_isready`, then loops. `backup_postgres.sh` does `pg_dump --format=custom --compress=0 | gzip -9`, computes SHA-256, writes a JSON manifest, uploads the dump *then* the manifest with `--sse AES256`, verifies the uploaded size, and only then prunes local copies older than two days.

`restore_postgres.sh` refuses `--drop-existing` against the live database, verifies the downloaded SHA-256 against the manifest, restores into a named target with a single-threaded fallback, and requires ≥20 public tables. `test_restore.sh --local` (wired into `make verify`) requires ≥25 tables *and byte-identical row counts* across four tables.

---

## 7. Invariant conformance

| # | Invariant | Enforced at | Verdict |
|---|---|---|---|
| 1 | No drafts, keystrokes, passwords, cookies, page storage, session tokens, hidden reasoning, personal workspaces, other sites | `dom-adapter.ts:92-98,467` (composer exclusion via `closest()`); `manifest.json:38-39` (only `storage`/`alarms`/`identity`, two host origins, ISOLATED world); `policy.py:93` (personal rejected); `schemas/auth.py:78-79` (`Literal[False]`) | **Enforced** for drafts and personal workspaces. **Convention-only** for the cookie/page-storage clause — verified by grep (zero occurrences of `document.cookie`, `localStorage`, `sessionStorage`, key listeners), and by an eslint rule whose `innerHTML` half matches only a variable literally named `element` (`.eslintrc.cjs:26-30`) |
| 2 | Fail closed | `policy.py:54-73` (ordered gate codes) and `:112-117` (`workspace_policy_unconfigured`); `schemas/common.py:73-76` (safe defaults); `managed-config.ts:101-135` | **Enforced** on the browser path. **Gap**: `compliance_import.py:81-83` auto-creates a `MANAGED_COMPANY`, `verified_at=now`, `capture_enabled=True` workspace from an upstream event with no human approval |
| 3 | Server decides policy; local settings cannot enable capture | `services/ingest.py:106`; `managed-config.ts:114-121`; no `storage.local.set` in any `ui/` file | **Enforced** — four independent mechanisms (§5) |
| 4 | Never claim complete history; only the compliance feed sets `compliance_verified` | `services/ingest.py:353-355`, `:309-313`; sole writers `compliance_import.py:221,234` | **Enforced.** Watch: `import_pending_source_events` filters only on org + `processed_at IS NULL`, not on `source == 'openai_compliance'` — it holds because there is exactly one producer, not because of an explicit guard |
| 5 | Never invent an upstream compliance endpoint or field | `adapters/openai_compliance.py` (all paths and the field map configurable, no hardcoded endpoint); `docs/COMPLIANCE_ADAPTER.md:116-145` records what was probed and why probing stopped | **Enforced** |
| 6 | Never commit a secret or log content/cookies/tokens/headers/passwords/presigned URLs/credentials | `core/logging.py:29-84` (two-tier key + regex redaction); `core/errors.py:138-145` (validation errors echo `loc`/`type` only); `services/audit.py:20-28` (detail denylist); `scripts/secret_scan.sh` in `make verify` | **Enforced with holes.** Key matching is exact-lowercase, never substring: `token` is redacted, `refresh_token_hash` is not; `download_url` is, `download_urls` (the actual `ExportOut` field name) is not. `_scrub` skips `bytes`, sets, dataclasses and Pydantic models entirely |
| 7 | Production AWS only from the instance role; no AWS credential to the extension or GitHub | `config.py:216-223` (region/bucket/endpoint pinned); `verify_production_config.sh:154-161`; `api-client.ts:230` (presigned URL only, bearer bypassed); `deploy.yml:70-74` (no AWS credential in any workflow) | **Enforced** |
| 8 | No published application port | `compose.prod.yaml:199-200`; `verify_production_config.sh:69-82`, `:128-141` | **Enforced** in production. **Violated in dev**: `compose.yaml:164-182` publishes pgAdmin on `127.0.0.1:5050` while attached only to the `internal: true` backend network — the exact trap prod documents. The verifier only parses `compose.prod.yaml` |
| 9 | Named Cloudflare Tunnel is the only ingress; pinned, `--no-autoupdate`, token via root-owned env file, no Quick Tunnels | `verify_production_config.sh:43-50,106-121`; `fetch_ssm_secrets.sh:128-138` (0400 root:root); `compose.prod.yaml:253-278` | **Enforced** |
| 10 | Capture gates and training export default false and stay false | `config.py:115-121,139`; `fetch_ssm_secrets.sh:172-177,263-271`; `compose.prod.yaml:57-62` | **Partially enforced.** Defaults are correct everywhere. But the deploy-time assertion is **vacuous** — see surprise #2. There is also no Python unit test on shipped defaults, because `conftest.py:26-27` forces both gates true for the whole suite; the sole automated proof is `production_compose_smoke_test.sh:154` |
| 11 | No test automates a live ChatGPT account | Structural: `tests/fixtures/transcripts.ts` is hand-written; `vitest.config.ts` pins a jsdom URL | **Enforced by absence.** No positive assertion exists |
| 12 | Four GitHub secrets only; app secrets in SSM | `deploy.yml:70-74`; `fetch_ssm_secrets.sh:98-177` | **Enforced in spirit.** `deploy.yml:74` reads `vars.EC2_SSH_HOST_KEY \|\| secrets.EC2_SSH_HOST_KEY` — a fifth secret *name*, though the value is a host public key |
| 13 | Never `StrictHostKeyChecking=no`; pin host keys | `.github/actions/ec2-ssh/action.yml:105` (`yes`, unconditional), `:64-85` (pin, else TOFU with `::warning::`) | **Enforced.** Repo-wide grep finds no `=no`. The pin itself is optional in practice — an unpinned first run trusts whatever answers |
| 14 | Never `system prune -a`, never remove a volume, never auto-downgrade the schema | `deploy_production.sh:404-408` (`image prune -f` only, with the reasoning in comments); `rollback_production.sh:8-12,165` | **Enforced.** Grep across both scripts finds no `prune -a`, `down -v`, `volume rm`, or `alembic downgrade`. Not covered by `deploy_scripts_test.sh` |

---

## 8. What surprised me / what to watch

Ranked by how likely they are to hurt you.

**1. The worker-side workspace check is dead code.** `apps/chrome-extension/src/background/service-worker.ts:267` tests `!workspace.verified || workspace.kind !== 'managed_company'` — but both callers construct that object literally with `verified: true, kind: 'managed_company'` (`:387-393`, `:401-407`). The condition can never fire. The file header claims "this worker decides — against the signed server configuration — whether anything may be stored", which is true for the policy gate at `:264` and false for the workspace dimension. **Code vs. documented promise.** The backend re-derives independently, so this is a lost defence-in-depth layer, not an open door — but do not treat it as a second opinion.

**2. The deploy-time capture-gate assertion is vacuous.** `scripts/verify_production_config.sh:38` runs `docker compose config` with **no `--env-file`**, and `deploy_production.sh:204-216` exports the pool/OIDC/host values but deliberately not the three gates. Since `compose.prod.yaml:57-62` spells them `${BROWSER_CONTENT_CAPTURE_ENABLED:-false}`, the resolved config always reads `false` and the assertions at `:177-181` always pass. At runtime the real value *is* applied. Nothing in the deploy path fails on gates=true: the renderer only checks the literal is `true`/`false`, and `verify_production.sh` only warns. The connection-budget check at `:186-191` *is* real, because those values are exported. **Invariant 10 is documented and defaulted, not gated.**

**3. Automatic rollback cannot succeed on a `--without-tunnel` host.** `rollback_production.sh:70` calls `fetch_ssm_secrets.sh` without `ALLOW_MISSING_TUNNEL`, and the renderer dies at `:137` when the tunnel token is absent — *before* the rollback script reaches its own `PUBLIC_INGRESS` downgrade logic at `:84-90`, which exists precisely for that case. `deploy_production.sh:120` then logs "ROLLBACK FAILED" and leaves the host on the new code with the new schema. Since `deploy.yml:113-115` routes every tunnel-less host down exactly this path, this is the rollback most likely to be exercised first.

**4. `content_sha256` does not hash the text that is stored.** It is verified against and stored from the *raw* client text (`services/ingest.py:524`, `:671`), while `plain_text` stores `clean_plain_text(payload.text)` (`:522`, `:669`), which collapses runs of spaces and tabs, strips control characters and trims. `char_count` is also `len(raw)`. Both fields are emitted side by side in the snapshot (`services/snapshots.py:159-160`), so **an auditor who recomputes SHA-256 over the snapshot's `text` field gets a mismatch for any message containing double spaces or tabs.** `normalized_sha256` is self-consistent; `content_sha256` is not.

**5. Flushing while signed out silently destroys the queue.** With no usable token, `api-client.ts:64` throws `ApiError(…, 401, retryable=false)`; `sync-engine.ts:129-137` treats non-retryable as terminal and deletes the item. The flush alarm runs every 60s regardless of sign-in state. Tokens live in `chrome.storage.session`, so **every browser restart begins signed-out** — a queue that survived the restart can be emptied within a minute, before the employee signs in.

**6. Attachment bytes almost certainly do not survive the content→worker hop.** `content-script.ts:191-198` sends an `ArrayBuffer` through `chrome.runtime.sendMessage`, which JSON-serialises — an ArrayBuffer becomes `{}`. That empty object is truthy, so the metadata-only branch at `service-worker.ts:418` is skipped and `uploadAttachment` PUTs `{}` to the presigned URL. No test can catch it: the fake `sendMessage` in `tests/setup.ts:103` never serialises. **Verify this in a real browser before trusting attachment capture at all.**

**7. Snapshot coalescing can strand an incomplete archive.** `mark_snapshot_stale` dedupes on `snapshot:{conversation_id}` and `enqueue_job` returns `None` when a live job exists. If a snapshot job is already RUNNING when a new message lands, no new job is created — and that running job then sets `snapshot_stale = False` (`snapshots.py:239`) even though it read the message list before the new message existed. Nothing re-enqueues stale snapshots; `PERIODIC_JOBS` has no snapshot sweep. The conversation is left with an S3 snapshot missing its newest turn *and* the flag cleared, so the admin `stale_snapshots` counter cannot see it.

**8. Retention never deletes anything from S3.** `storage.py:262 delete_object` has zero production callers. `hard_delete_grace_expired` removes the `Conversation` row and cascades, so the database loses the pointers while `raw/events/…`, `normalized/conversations/…` and all three attachment prefixes survive indefinitely. Physical deletion is entirely delegated to hand-configured S3 lifecycle rules that nothing in the repo provisions or asserts.

**9. The migration runs while the old API and worker are still live.** `deploy_production.sh:262` runs `alembic upgrade head` and only `:266` recreates the containers. Every deployment therefore creates a window where the *previous* release serves traffic against the *new* schema. `docs/ROLLBACK.md:93-95` frames backward compatibility purely as a rollback concern — a reviewer thinking "we never roll back" would still be wrong to relax the rules. Compounding this: `0002_fts:34-41` creates a GIN index **non-concurrently** and adds a stored generated column, which forces a full-table rewrite under `ACCESS EXCLUSIVE` — violating `docs/ROLLBACK.md:109-110`'s own sixth rule, on a table growing at ~100k rows/day.

**10. Identical turns collapse into one row, and the earlier one's position is rewritten.** Tier-3 content matching (`services/ingest.py:465-476`) matches any same-role message with the same normalised hash. A genuine second "yes" is matched to the first — and before returning `duplicate`, the update branch unconditionally overwrites the first message's `sequence_index` with the second occurrence's (`:606-607`), inside the committed SAVEPOINT. Intentional and test-pinned, but the archive is not a faithful transcript in that case.

**11. `verify_backup.sh` has zero callers.** Grep across the Makefile, `.github/`, `deploy/` and every shell script finds only its own docstring. `test_restore.sh --from-s3-latest` is likewise never invoked. The automated proof in `make verify` is "a dev-database dump round-trips inside a test container", not "the real S3 backup chain restores". The SHA-256 manifest every backup writes is checked only by a restore, and nothing restores automatically. Backups are also verified by **size only** at upload (`backup_postgres.sh:98-108`).

**12. `staging` is not a production dress rehearsal.** Production guardrails run only when `environment == "production"` (`config.py:204-205`). `staging` is a valid `Environment` value and gets none of them: dev auth stays enabled, Swagger and `/openapi.json` are served, `devonly` secrets are accepted, `LOG_MESSAGE_CONTENT=true` is permitted, and the pool arithmetic is never checked.

**13. `GET /config` accepts a revoked device's unexpired token.** `api/v1/auth.py:71-79` calls `decode_access_token` only — no user, org or device load — so a revoked device still unlocks the full `managed_workspace_label` and `managed_workspace_ids`, which is exactly the data `redact_for_public` exists to withhold.

**14. The `AUTH_DENIED` audit row is never persisted.** `api/v1/auth.py:150-160` records the audit then re-raises, and `get_db_session` rolls back on any exception. Failed logins leave a log line and no audit trail — the opposite of the code's evident intent, and the same pattern would swallow any audit written before a raise.

**15. Curated (EXIF-stripped) copies are never exported.** `services/exports.py:182` emits `a.clean_s3_key` — the byte-for-byte original — never `curated_s3_key`. An export consumer following the manifest gets images with camera and GPS metadata intact even though a sanitised derivative exists in S3. And `strip_metadata` covers only JPEG and PNG; WebP, GIF and PDF are in the allowlist and pass through unchanged.

**16. `COMPLIANCE_EXTRACT` exports are ungated by approval or completeness.** `exports.py:74-99` applies only the org, not-deleted and legal-hold filters for that kind. Full `plain_text` of every message in the organization is written to `exports/jsonl/…` behind the exporter role alone. `run_export` also buckets every record in memory before writing anything, with no streaming path and no cap on `conversation_ids`.

**17. `WORKER_CONCURRENCY` buys no concurrency, and one bad job can stall a batch for five minutes.** `worker.py:73-75` runs the claimed batch in a sequential loop on one shared session. A handler that raises a database error poisons that session; the `finally` commit is swallowed by `contextlib.suppress(Exception)` (`:129`), so the failure is never recorded, and the next job's `fail_job` raises `PendingRollbackError` out past `run_once`. The whole batch stays `running` with live locks until the 300s stale timeout, logged only as `worker_cycle_failed`.

**18. Compliance events can be stranded permanently.** In `compliance_poller.py:99-114`, `advance_checkpoint` and the `COMPLIANCE_SYNC` enqueue are both inside the try. An error after some events persisted leaves `SourceEvent` rows with `processed_at IS NULL` and no sync job. The next cycle re-fetches, `persist_event` returns False for all of them, `summary['new']` is 0, and the enqueue is skipped again. `COMPLIANCE_SYNC` is not in `PERIODIC_JOBS` and no admin endpoint enqueues it.

**19. `crypto.pseudonymize` defaults to `salt=""`.** `workspace_hash`, `email_hash`, `employee_id_hash`, `ip_hash` are all plain SHA-256 of a lowercased value. With a known ~250-person roster and a known org slug, the pseudonyms in S3 key prefixes are trivially enumerable offline.

**20. Anonymous rate limiting fails open.** `deps.py:63-73 client_ip` returns `None` in production whenever `CF-Connecting-IP` is absent or unparsable, and `CompositeRateLimiter.check` with no keys returns allowed (`ratelimit.py:114-115`). A request reaching the origin without that header gets no application-layer limit on `/config` or `/auth/exchange`. The compensating control is entirely the EC2 security group.

**21. Documented runbooks that would not work.** `docs/INCIDENT_RUNBOOK.md:34` — the SEV1 kill-switch procedure — runs `sed -i` on `/opt/techsara-chat-archive/.env`, a file no script ever creates (everything uses `.env.production`). The `sed` fails and the `grep` at `:81` returns nothing, so an operator halting a content-exposure incident would believe capture stopped when it has not. The systemd units have the same `.env` vs `.env.production` mismatch. `docs/AWS_MANUAL_SETUP.md` still describes the pre-tunnel architecture in three places (inbound 443 from Cloudflare ranges, "the API container's TLS port", a `tls` directory nothing creates), and contradicts `AWS_CONSOLE_CHECKLIST.md` on whether the instance has an SSH key pair.

**22. Nothing here has ever run in production.** `claude-progress.md:155-159` records that the first real deploy's preflight found the eleven SSM parameters and `/srv/techsara-chat-archive` missing and skipped the deployment. Every operational runbook is unexercised against a real instance — which explains the concentration of staleness at exactly that layer.

### Smaller things worth knowing

- `SecurityHeadersMiddleware` is the *innermost* layer (registered first at `main.py:100`), so any response short-circuited above it — the 413 from `BodySizeLimitMiddleware`, TrustedHost's 400, CORS preflight, an unhandled 500 — carries no CSP, no HSTS, no `X-Correlation-Id`.
- The `http_request` access log line has no correlation id: `middleware.py:55-56` resets the contextvars in the `finally` *before* emitting the log at `:60-66`.
- `check_database()` at `main.py:74` discards its return value, and `db/session.py:107-118` swallows every exception. The API starts and serves traffic with an unreachable PostgreSQL; only `/health/ready` notices.
- Roles come from the JWT and are never re-read from the row (`deps.py:48-50`). A demoted admin keeps admin access until the token expires.
- `FAILED` and `DEAD` are inverted from the intuitive reading (`jobs.py:204`): a `NonRetryableError` lands in DEAD, an exhausted retry lands in FAILED. Both are terminal, and nothing ever prunes or alerts on them.
- `recover_stale_jobs` ignores `max_attempts` and applies no backoff — a job that kills its worker before `fail_job` can run is re-queued immediately, forever.
- `_enum()` lives in `app/models/identity.py`, not `enums.py`. Five model modules import a leading-underscore private from a sibling domain module. Its `values_callable` is what makes seven partial indexes and CHECK constraints match; removing it fails silently.
- The `backup` service replaces (not extends) the anchor's `volumes:`, so it has none of the `/run/secrets/*` files despite inheriting every `*_FILE` env var. It also bind-mounts `./scripts` from the host working tree, so backup logic is not pinned to the release.
- `optional_host_permissions: ["https://*/*"]` in `manifest.json:40` is a declared ability to request every HTTPS origin. Nothing calls `chrome.permissions.request`, so nothing is granted — but no validator checks it, and `docs/CHROME_EXTENSION.md:25` tells the reader host permissions are limited to two origins.

---

## 9. Where to start reading

In this order. Each earns its place by making the next one legible.

1. **`CLAUDE.md`** — the 14 invariants and the sources-of-truth table. Fifteen minutes, and it tells you which single file owns each concern. Read the change rules too; they are the actual review checklist.

2. **`docs/CAPTURE_LIMITATIONS.md`** — the promise to employees and auditors. This is the product's thesis. `scripts/docs_check.sh` hard-fails the build if five specific honest phrases disappear from it, which tells you how seriously it is meant.

3. **`services/backend/app/services/policy.py`** — 230 lines that contain the entire capture decision. `assert_browser_capture_allowed` at `:54`, `evaluate_workspace_ref` at `:83`, `workspace_hash_for` at `:48`. If you understand only one file, make it this one.

4. **`services/backend/app/services/ingest.py`** — the write path, and the largest file in the repo. Read `build_context` (`:95`) first to see the choke point, then `_merge_completeness` (`:349`) for invariant 4, then `_find_message` (`:432`) for the identity model, then `ingest_message` (`:508`) end to end.

5. **`services/backend/app/core/config.py`** — ~74 settings, the `*_FILE` secret loading, and `_production_guardrails` (`:201-246`), which is the reason a misconfigured production process refuses to import at all. Note `browser_capture_active` at `:300` and `max_expected_database_connections` at `:309`.

6. **`apps/chrome-extension/src/modules/dom-adapter.ts`** and **`workspace-verifier.ts`** — the only file that knows ChatGPT's markup, and the only file that decides workspace identity in the page. `FORBIDDEN_CONTAINERS` at `dom-adapter.ts:92` is the entire mechanism behind the never-capture-drafts promise.

7. **`services/backend/app/models/conversation.py`** and **`events.py`** — the content tree with its partial unique indexes, and the module docstring at `events.py:3-12` explaining why idempotency lives in a separate non-partitioned table. Read `identity.py:28-44` for `_enum()` while you are here.

8. **`services/backend/app/services/jobs.py`** and **`app/workers/handlers.py`** — the durable queue and its nine consumers. `handlers.py:98-101` (S3 before the DB flag) is the single most load-bearing ordering in the system.

9. **`compose.prod.yaml`** and **`scripts/verify_production_config.sh`** — the runtime topology and the assertions that keep it honest. Read them together; the verifier is a commented explanation of why the compose file looks the way it does.

10. **`scripts/deploy_production.sh`** and **`docs/ROLLBACK.md`** — the release mechanism and its contract. Pay attention to the `flock` + `exec` handover (`:72-80`, `:154-159`) and to the explicit reasoning in the comments around the prune and the public health check.

11. **`docs/DECISIONS.md`** — thirteen ADRs. Read ADR-001 (transactional enqueue), ADR-002 (partition-local idempotency rejected), and especially ADR-004 and ADR-007, both marked "accepted (after a bug)". Those two are the most instructive pages in the repo: they document failures that were silent, not loud, which is the failure mode this codebase is most exposed to.