# Database

PostgreSQL 16 in Docker on the EC2 instance. Not publicly exposed; reachable
only on the private Docker network and, for administrators, through SSM port
forwarding.

## Tables

### Identity
| Table | Purpose | Key constraints |
| --- | --- | --- |
| `organizations` | One row per company | unique `slug` |
| `workspaces` | Observed ChatGPT workspaces | unique (organization, workspace_hash) |
| `users` | Employees | unique (organization, email) |
| `user_identities` | External OIDC subjects | unique (issuer, subject) |
| `devices` | Browser profiles | unique (user, fingerprint); refresh token stored as SHA-256 only |

### Conversation content
| Table | Purpose | Key constraints |
| --- | --- | --- |
| `conversations` | One per ChatGPT conversation | unique (organization, workspace, source_conversation_id) |
| `conversation_branches` | Detected alternative paths | unique (conversation, branch_key) |
| `messages` | Stable turn identity | partial unique (conversation, source_message_id) where not null; unique (conversation, fingerprint) |
| `message_versions` | **Append-only** content | unique (message, version_number); unique (message, normalized_sha256) |
| `message_parts` | Code, tables, lists, quotes | unique (version, part_index) |

### Attachments
| Table | Purpose |
| --- | --- |
| `attachments` | State machine: pending → quarantine → clean / rejected / expired / metadata_only |
| `message_attachments` | Links a verified attachment to its message |

### Events (monthly partitioned)
| Table | Purpose |
| --- | --- |
| `capture_events` | Immutable raw client payloads |
| `source_events` | Raw upstream compliance events |
| `audit_events` | Append-only administrative trail |

### Global uniqueness (not partitioned)
| Table | Purpose |
| --- | --- |
| `idempotency_keys` | One row per accepted client idempotency key |
| `source_event_keys` | One row per imported upstream event id |

### Operations and governance
| Table | Purpose |
| --- | --- |
| `jobs`, `job_attempts` | Durable queue and its forensics |
| `sync_checkpoints` | Compliance poller cursors |
| `feedback` | Employee and reviewer feedback |
| `training_approvals` | Explicit export approval, per conversation |
| `exports` | Export runs and their manifests |
| `retention_policies` | Retain days, grace days, action |
| `legal_holds` | Named holds that block deletion |
| `alembic_version` | Schema revision |

## Versioning model

`messages` never carries content. Content lives in `message_versions`:

```
messages(id, fingerprint, role, sequence_index, current_version_id, version_count)
   └── message_versions(id, version_number, plain_text, content_sha256, is_edit,
                        is_regeneration, completion_status, captured_at)
          └── message_parts(part_index, kind, language, text, structured)
```

An edit or a regeneration appends a row and moves `current_version_id`. Nothing
is overwritten, so the original is always recoverable.

## Partitioning

`capture_events`, `source_events` and `audit_events` are RANGE-partitioned by
`created_at`, one partition per month, plus a DEFAULT partition so a write can
never fail for lack of one.

```sql
-- What actually stores a row
SELECT tableoid::regclass, count(*) FROM capture_events GROUP BY 1 ORDER BY 1;

-- Partitions of a parent
SELECT c.relname FROM pg_inherits i
  JOIN pg_class c ON c.oid = i.inhrelid
  JOIN pg_class p ON p.oid = i.inhparent
 WHERE p.relname = 'capture_events' ORDER BY 1;
```

The `maintain_partitions` job creates the current month plus three ahead, every
six hours. Dropping an old partition is a metadata-only operation — but
`drop_partitions_older_than` defaults to `dry_run=True`, because dropping a
partition is irreversible and a legal hold may cover the period.

**Consequence, handled deliberately:** a UNIQUE constraint on a partitioned table
must include the partition key. That would make idempotency month-local, so the
non-partitioned `idempotency_keys` table carries the global guarantee instead.

## Indexes that matter

| Index | Why |
| --- | --- |
| `ix_jobs_claim` (partial, `status='pending'`) | The queue claim query |
| `uq_jobs_dedupe_active` (partial unique) | One live job per dedupe key |
| `ix_messages_conversation_sequence` | Ordered transcript reads |
| `ix_message_versions_conv_norm` | Content-identity fallback matching |
| `ix_conversations_snapshot_stale` (partial) | Finding conversations needing a snapshot |
| `ix_attachments_pending_expiry` (partial) | Expiring abandoned uploads |
| `ix_message_versions_search_tsv` (GIN) | Authorized administrative search |

## Full-text search

Migration `0002_fts` adds a stored generated column:

```sql
search_tsv tsvector GENERATED ALWAYS AS (to_tsvector('simple', plain_text)) STORED
```

with a GIN index. There is **no public search endpoint**. Search is for
authorized administrators, and every use is audited.

## Migrations

```bash
make migrate           # apply
make migration-check   # apply, assert no drift, downgrade -1, re-upgrade
cd services/backend && alembic revision --autogenerate -m "description"
```

`alembic check` failing means the models and the migrations disagree — fix it
before merging, never in production.

## Sizing at the stress target

100,000 messages/day:

| Table | Rows/day | Bytes/row (approx) | Growth/day |
| --- | --- | --- | --- |
| `messages` | 100k | ~400 | ~40 MB |
| `message_versions` | 110k | ~2 KB | ~220 MB |
| `message_parts` | 300k | ~500 | ~150 MB |
| `capture_events` | 100k | ~3 KB | ~300 MB |
| `audit_events` | ~5k | ~500 | ~2 MB |

Roughly 700 MB/day before compression, so the 100 GiB data volume holds about
four months at full stress. Real usage for 250 employees is far below this;
see [CAPACITY.md](CAPACITY.md) for the monitoring thresholds
that tell you when to grow.

## Retention

Two stages, always in this order:

1. **Soft delete.** `deleted_at` and `deletion_reason` are set. Rows under legal
   hold are skipped, and a CHECK constraint makes a held-and-deleted row
   impossible.
2. **Hard delete.** Only for a policy with `action='hard_delete'`, only after
   `grace_days`, and only for rows not under hold. Cascades remove messages,
   versions, parts and attachment links. Every run writes an audit row.

## Connection budget

```
API_WORKERS × (DATABASE_POOL_SIZE + DATABASE_MAX_OVERFLOW)
  + WORKER_CONCURRENCY + 1 (compliance poller) + 5 (headroom)
```

Defaults: 3 × (20 + 20) + 2 + 1 + 5 = **128** against `max_connections = 200`.
Raising `API_WORKERS` without checking this is the classic way to exhaust
PostgreSQL connections.
