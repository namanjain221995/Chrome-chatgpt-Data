# Architecture decision records

Each record states the decision, why it was taken, and what it costs.

---

## ADR-001: PostgreSQL as the durable job queue

**Status:** accepted

**Context.** Work must survive restarts: writing raw JSON to S3, building
snapshots, finalizing attachments, running exports, retention sweeps. SQS,
Lambda and ElastiCache are prohibited.

**Decision.** A `jobs` table claimed with `SELECT ... FOR UPDATE SKIP LOCKED`,
with `lock_token`-guarded completion and stale-lock recovery.

**Why this wins.** The decisive property is transactional enqueue: the capture
event and its archive job are inserted in the same transaction, so there is no
window in which an accepted message has no durable path to S3. An external queue
could not offer that without a two-phase dance.

**Cost.** Queue throughput is bounded by PostgreSQL. At the stress target
(100k messages/day ≈ 1.2/s average) this is comfortable. Beyond a sustained
~500 jobs/s the queue would need rethinking.

---

## ADR-002: Monthly partitioning, with a separate idempotency table

**Status:** accepted

**Context.** `capture_events` grows at the message rate. Retention by `DELETE`
on tens of millions of rows is slow and bloats the table.

**Decision.** Range-partition `capture_events`, `source_events` and
`audit_events` by month, each with a DEFAULT partition. Because a unique
constraint on a partitioned table must include the partition key — which would
make idempotency **month-local** — a separate non-partitioned `idempotency_keys`
table provides the global guarantee.

**Cost.** Partition maintenance is a scheduled job. The extra table needs its
own pruning. Both are automated and tested.

**Rejected alternative.** Partition-local idempotency. A retry that crossed a
month boundary would have created a duplicate — a silent correctness bug.

---

## ADR-003: Three-layer message identity

**Status:** accepted

**Context.** ChatGPT sometimes exposes a message id and sometimes does not.
Backfilling renumbers every message's position.

**Decision.** Match on `source_message_id`, then on a deterministic fingerprint
(conversation, role, normalised content hash, sequence neighbourhood, timestamp
bucket), then on **content identity** (same role, same normalised content, same
conversation).

**Why layer three exists.** Without it, opening a conversation and backfilling it
would duplicate every message already captured live, because the sequence index
shifted. This is covered by
`test_backfill_shift_does_not_duplicate_messages`.

**Cost.** Two genuinely distinct messages with byte-identical content and the
same role in one conversation collapse into one row with one version. Nothing is
lost but the repetition count. Documented in CAPTURE_LIMITATIONS.md.

---

## ADR-004: Enum values, not names, in the database

**Status:** accepted (after a bug)

**Context.** SQLAlchemy's `Enum` type persists the Python member **name** by
default. Every partial index and CHECK constraint in this schema is written
against the lowercase **value**.

**Decision.** `values_callable=lambda obj: [m.value for m in obj]` plus
`create_constraint=True` on every enum column.

**Why it matters.** Before this fix the database stored `PENDING` while
`uq_jobs_dedupe_active ... WHERE status IN ('pending','running')` matched
nothing. Job deduplication silently did not work, and the claim index was never
used. Caught by an integration test asserting that a duplicate dedupe key is
refused.

**Cost.** None. It also added real CHECK constraints, so an invalid enum value
is now impossible.

---

## ADR-005: Direct-to-S3 uploads with a pinned presigned PUT

**Status:** accepted

**Context.** Attachments up to 20 MiB from up to 100 concurrent clients.

**Decision.** Metadata to `/attachments/init`; bytes straight to S3 with a
presigned PUT whose signature covers bucket, key, content type **and exact
content length**; then `/attachments/complete`, which the backend acknowledges
only after verifying size and checksum. The worker re-downloads and re-hashes
before promoting quarantine → clean.

**Why pin the length.** Without it a client could presign a 1 KiB upload and then
PUT 5 GiB. Signing `Content-Length` makes S3 reject the mismatch.

**Cost.** Attachment bytes are downloaded once by the worker. Magic-byte
validation and EXIF stripping need them anyway.

---

## ADR-006: Server-authoritative, signed configuration

**Status:** accepted

**Context.** Requirement: "server-side capture gates cannot be bypassed by a
local extension setting."

**Decision.** The backend serves a versioned, HMAC-signed configuration. The
extension caches it, but every ingest request is re-checked server-side, and the
client independently rejects any cached document whose shape is impossible —
`capture_active` without both gates, personal-workspace capture set true, draft
capture set true, or an expired document.

**Cost.** A configuration change takes up to the cache TTL (15 minutes) to reach
a client. The kill switch is also enforced server-side, so an urgent stop is
immediate regardless of the cache.

---

## ADR-007: Separate builds for extension pages and worker/content scripts

**Status:** accepted (after a bug)

**Context.** A single Vite build with four entry points emitted shared chunks,
so `content-script.js` began with `import {...} from "./chunks/util.js"`.

**Decision.** Build popup and options together (they are ES modules), and build
the service worker and content script as separate self-contained bundles.

**Why it matters.** An MV3 content script is injected as a classic script with no
module loader. The import would have thrown at injection time, and capture would
have silently never started — with no error surfaced anywhere useful. The
manifest validator now fails the build if any import survives.

**Cost.** Three Vite invocations and some duplicated bytes between the worker and
the content script. Correctness is worth more than the kilobytes.

---

## ADR-008: One container image for four roles

**Status:** accepted

**Context.** API, worker, compliance poller and backup all need the application
code; backup also needs `pg_dump`.

**Decision.** One image, four commands.

**Cost.** The image carries `postgresql-client` for every role. In exchange there
is exactly one artifact to build, scan, sign, deploy and roll back, and no
possibility of a version skew between the API and its worker.

---

## ADR-009: Per-process rate limiting

**Status:** accepted

**Context.** Redis and ElastiCache are prohibited, but per-employee, per-device
and per-IP limits are required.

**Decision.** An in-process sliding window with bounded memory, with the per-key
budget divided by `API_WORKERS` so the fleet-wide limit matches the configured
value. Caddy applies coarse limits at the edge as a second layer.

**Cost.** The division is approximate: a client whose requests all land on one
worker sees exactly `limit / workers`. Documented in SECURITY.md. A PostgreSQL-
backed limiter was rejected as a write amplifier on the hot path.

---

## ADR-010: Configuration-driven compliance adapter

**Status:** accepted

**Context.** The brief forbids inventing undocumented endpoint paths.

**Decision.** Base URL, log path, files path and response field mapping are all
configuration. The adapter reports itself unconfigured and the poller stays idle
until they are supplied.

**Cost.** Enabling company-wide coverage requires a documented API contract from
the Enterprise agreement. That is the correct dependency: the alternative is
guessing at an API and shipping something that silently does not work.
