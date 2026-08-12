# Architecture

## The shape of the system

```
┌──────────────────────────────────────────────────────────────────────────┐
│ Employee's managed Chrome                                                │
│                                                                          │
│  content script (isolated world, chatgpt.com only)                       │
│    workspace-verifier ─ route-observer ─ dom-adapter                     │
│    live-observer ─ conversation-backfill ─ attachment-observer           │
│                     │ observations only, no network                      │
│                     ▼                                                    │
│  service worker (MV3)                                                    │
│    auth-client (PKCE) ─ managed-config ─ offline-queue (IndexedDB)       │
│    message-normalizer ─ api-client ─ sync-engine                         │
└───────────────┬──────────────────────────────────────┬───────────────────┘
                │ HTTPS (batched, idempotent)          │ presigned PUT
                ▼                                      │
        ┌───────────────┐                              │
        │  Cloudflare   │  edge TLS, WAF, proxied DNS  │
        └───────┬───────┘                              │
                │ tunnel (outbound-only from EC2)      │
                ▼                                      ▼
┌────────────────────────────────────────┐   ┌──────────────────────────────┐
│ EC2 (one instance)                     │   │ Amazon S3 (private)          │
│                                        │   │                              │
│  Docker Compose                        │   │  raw/events/…                │
│  cloudflared ──▶ http://api:8000       │   │  normalized/conversations/…  │
│    (egress network only)               │   │  attachments/quarantine|clean│
│    │                                   │   │  exports/jsonl/…             │
│  api (FastAPI, expose 8000) ───────────┼──▶│  backups/postgres/…          │
│    │                                   │   └──────────────────────────────┘
│    ▼                                   │
│  postgres:16  (internal network only)  │
│    ▲     ▲                             │            ▲
│    │     │                             │            │
│  worker  compliance-poller ────────────┼────────────┘  (authorized feed)
│  backup ── nightly pg_dump ────────────┘
│  pgadmin (profile: admin, 127.0.0.1 only)
└────────────────────────────────────────┘
```

A named Cloudflare Tunnel is the only public ingress. `cloudflared` holds
outbound-only connections to the Cloudflare edge and forwards to
`http://api:8000` over the private Docker `egress` network, so the instance
publishes no application port at all and needs no origin certificate. FastAPI
uses `expose: 8000`, never `ports:`. PostgreSQL is on an `internal: true`
network with no host binding. pgAdmin is an optional `admin` profile bound to
`127.0.0.1:5050`, reached through an SSH local port-forward.

The only host ports that exist are two loopback management endpoints: the
tunnel's own `/ready` metrics endpoint on `127.0.0.1:2000`, and pgAdmin on
`127.0.0.1:5050` when it is deliberately started.

---

## Why these choices

### One EC2 instance instead of managed services

The constraint is explicit: no RDS, no Lambda, no SQS, no ECS, no ElastiCache,
no DynamoDB, no API Gateway. That forces a design where PostgreSQL provides
durability, queueing and search, and the instance provides compute. The
consequences are honest ones:

- **Single point of failure.** One instance means one thing to lose. That is why
  backups are tested, not merely taken, and why the data lives on a separate EBS
  volume that survives an instance rebuild.
- **Vertical scaling only.** Growth means a bigger instance, then a split
  application/database topology. Thresholds are in
  [CAPACITY.md](CAPACITY.md).
- **Operational simplicity.** One `docker compose up`, one systemd unit, one
  backup script. A small team can actually run this.

### PostgreSQL as the job queue

`FOR UPDATE SKIP LOCKED` gives exactly-once-ish delivery with no extra service.
Jobs are rows, so enqueueing an archive job and storing the capture event happen
in **the same transaction** — the property that makes "raw JSON reaches S3
before the job is marked complete" enforceable rather than hopeful.

Stale locks are recovered by comparing `locked_at` against
`WORKER_STALE_LOCK_SECONDS`. Recovery clears the lock token, which also
invalidates any late completion from the worker that died — so a zombie cannot
mark a job done that another worker has already redone.

### S3 for bytes, PostgreSQL for structure

File bytes never pass through FastAPI. The extension gets a presigned PUT
pinned to bucket, key, content type and exact content length, and uploads
directly. The backend acknowledges only after it has verified size, checksum and
magic bytes.

PostgreSQL holds normalised text (for authorized operational search), structure
(parts, versions, branches) and integrity hashes. S3 holds the immutable raw
event JSON, the normalized conversation snapshots, the attachment bytes and the
backups.

### Append-only message versions

`messages` is the identity of a turn; `message_versions` is append-only content.
An edited prompt or a regenerated answer adds a row and moves
`messages.current_version_id`. Nothing is overwritten, so an auditor can
reconstruct exactly what the employee saw and when.

---

## Request path: a message from page to archive

1. **Observe.** `live-observer` sees the transcript change and waits for quiet.
2. **Normalise.** `message-normalizer` sanitises the HTML, decomposes the parts,
   computes the content SHA-256 and derives a deterministic idempotency key.
3. **Queue.** The batch goes into IndexedDB first. Nothing is lost if the network
   or the service worker dies here.
4. **Send.** `sync-engine` posts a batch of up to 100 messages with the backend
   access token.
5. **Authorise.** The API validates the token, the device, the role and the rate
   limit; then re-verifies the workspace and the capture gates server-side.
6. **Persist.** In one transaction: the conversation upsert, the message
   identity, the new version, the structured parts, the immutable capture event,
   the idempotency key, and the archive + snapshot jobs.
7. **Archive.** The worker claims the archive job, writes the raw JSON to S3,
   records the key and version id, and only then marks the job complete.
8. **Snapshot.** A coalesced snapshot job rebuilds the normalized conversation
   JSON with an integrity hash.

Step 6 is the crux: because the capture event and its archive job are inserted
together, there is no window where an accepted message has no durable path to S3.

---

## Identity and idempotency

A message is identified, in order of preference:

1. `source_message_id` when ChatGPT exposes one — the strongest signal.
2. A deterministic fingerprint over conversation, role, normalised content hash,
   sequence neighbourhood and a 5-minute timestamp bucket.
3. **Content identity** — same role, same normalised content, same conversation.

Rule 3 exists for a specific real failure: a backfill renumbers every message,
so a re-captured message arrives with a different `sequence_index` and therefore
a different fingerprint. Without rule 3 the archive would fill with duplicates.
An integration test covers exactly this case.

Client-side, the idempotency key is derived from stable material (conversation,
source id, role, content hash) and deliberately **excludes** the sequence index,
so a retry after a backfill reuses the same key.

---

## Failure behaviour

| Failure | What happens |
| --- | --- |
| Network down | Items stay in IndexedDB; retried with jittered backoff |
| Service worker evicted | Queue is durable; an alarm restarts the flush |
| Tab closes mid-answer | Partial version persisted, reconciled later |
| Worker killed mid-job | Stale-lock recovery returns the job to pending |
| S3 unavailable | Archive job fails and retries; the event stays unarchived and visible in the admin summary |
| Queue saturated | API returns 503 `backpressure`; clients back off |
| Database down | API readiness fails; Cloudflare returns an origin error; clients queue locally |
| Bad item in a batch | SAVEPOINT isolates it; the rest of the batch still commits |
| Instance lost | Restore from the tested backup onto a replacement instance |

---

## Trust boundaries

1. **Page → content script.** Page HTML is read, never executed. Nothing from the
   page is trusted as markup or as a policy statement.
2. **Content script → service worker.** The content script has no credentials and
   no network access. It reports observations.
3. **Extension → backend.** The extension's claims about workspace, identity and
   policy are all re-decided server-side.
4. **Backend → S3.** The instance profile grants prefix-scoped read/write with
   **no delete**.
5. **Administrator → data.** Every admin read, export, approval and deletion is
   audited with actor, action, resource and correlation id.

---

## Data model summary

25 tables. The ones that carry the design:

- `conversations` — unique on (organization, workspace, source id)
- `messages` — stable turn identity; unique on (conversation, source id) and on
  (conversation, fingerprint)
- `message_versions` — append-only content, unique on (message, normalised hash)
- `message_parts` — code/table/list structure
- `attachments` — quarantine → clean state machine with verified checksums
- `capture_events` — immutable raw client payloads, **monthly partitioned**
- `source_events` — raw upstream compliance events, **monthly partitioned**
- `audit_events` — append-only admin trail, **monthly partitioned**
- `idempotency_keys` — non-partitioned, so idempotency is global not month-local
- `jobs` / `job_attempts` — the durable queue and its forensics

Full detail in [DATABASE.md](DATABASE.md).

---

## Deliberate non-goals

- No public search interface. Full-text search exists for authorized
  administrators only, and every use is audited.
- No automatic training. Curated export requires an explicit approval row per
  conversation and a server flag that is off by default.
- No third-party model classification of company conversations.
- No claim of complete workspace history from browser capture alone.
