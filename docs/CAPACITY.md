# Scaling to 250 employees

## The envelope this is engineered for

| Dimension | Target |
| --- | --- |
| Registered employees | 250 |
| Simultaneously online clients | 100 |
| Concurrent active sync clients | 50 |
| Messages per business day | 100,000 (stress target) |
| Batch size | ≤ 100 items or 2 MiB |
| Sustained ingestion | 10 requests/second |
| Burst ingestion | 25 requests/second |
| Attachments | Direct to S3, ≤ 20 MiB each |
| Test fixture scale | ≥ 1,000,000 message rows |

100,000 messages/day is roughly 1.2 messages/second averaged, or about 12/second
in a busy hour. The 10/s sustained and 25/s burst targets are request rates, and
each request carries up to 100 messages — so the design has considerable headroom
over the message target.

## What makes it fit on one instance

| Control | Where |
| --- | --- |
| Connection pooling inside each API process | `DATABASE_POOL_SIZE`, `DATABASE_MAX_OVERFLOW` |
| Batch ingest (100 messages per request) | `MAX_BATCH_ITEMS` |
| Composite indexes on the hot paths | `ix_messages_conversation_sequence`, `ix_jobs_claim` |
| Durable queue instead of synchronous S3 writes | `jobs` table |
| Backpressure when the queue saturates | `JOB_QUEUE_BACKPRESSURE_THRESHOLD` (503 + Retry-After) |
| Client batching, compression and jittered retries | Extension offline queue |
| Attachments bypass the API entirely | Presigned S3 PUT |
| Edge and application rate limits | Cloudflare controls + `CompositeRateLimiter` |
| Bounded worker concurrency | `WORKER_CONCURRENCY` |
| Monthly partitions on the high-volume tables | Keeps index maintenance bounded |

## Sizing at rest

`t3a.large` — 2 vCPU, 8 GiB:

| Component | Memory | Notes |
| --- | --- | --- |
| PostgreSQL | 2-3 GiB | `shared_buffers=2GB`, `work_mem=24MB` |
| API (3 workers) | ~1.5 GiB | ~500 MiB per worker |
| Worker | ~500 MiB | Concurrency 2 |
| Compliance poller | ~200 MiB | Idle unless configured |
| cloudflared | ~100 MiB | Limited to 256 MiB in `compose.prod.yaml` |
| Backup | ~200 MiB | Peaks during a dump |
| OS and Docker | ~800 MiB | |
| **Headroom** | **~1.5 GiB** | pgAdmin stays stopped |

TLS is terminated by Cloudflare, so the origin does no certificate work; the
tunnel connector's cost is the ~100 MiB above. pgAdmin (~300 MiB) is an opt-in
profile for exactly this reason.

## Connection budget

Each application *process* owns its own SQLAlchemy pool, so pools multiply by
process count, not by request concurrency:

```
(DATABASE_POOL_SIZE + DATABASE_MAX_OVERFLOW)
  × (API_WORKERS + job worker + optional compliance poller)
= (12 + 4) × (3 + 1 + 1) = 80        against max_connections = 120
```

A reserve of 15 connections is held back for PostgreSQL's own superuser slots,
the nightly `pg_dump`, an incident `psql` session and the optional pgAdmin
profile, leaving 105 usable.

Raising `API_WORKERS` without recomputing this is the standard way to exhaust
PostgreSQL connections, so the arithmetic is enforced rather than documented:

* `Settings.max_expected_database_connections` computes it, and the production
  guardrail in `app/core/config.py` **refuses to start** when the worst case
  exceeds `POSTGRES_MAX_CONNECTIONS` minus the reserve.
* `scripts/verify_production_config.sh` asserts the same inequality statically
  in CI, against the `max_connections` that `compose.prod.yaml` configures.
* The production Compose smoke asserts it once more at runtime.

`POSTGRES_MAX_CONNECTIONS` is a single value used both for PostgreSQL's
`-c max_connections` and for the application's budget check, so the two cannot
drift apart. Pool behaviour is otherwise bounded by
`DATABASE_POOL_TIMEOUT_SECONDS` (30 s), `DATABASE_POOL_RECYCLE_SECONDS`
(1800 s) and `DATABASE_POOL_PRE_PING` (on), all configurable from SSM.

## Running the load test

```bash
# Generate a token out of band; never commit one
export ACCESS_TOKEN="$(python scripts/load_test_token.py)"
k6 run -e BASE_URL=https://archive-load.example.com tests/load/k6-ingest.js
```

Four scenarios run together: sustained 10 req/s, a ramp to 25 req/s, attachment
init calls, and status polling. Thresholds are gates, not decoration:

| Threshold | Value |
| --- | --- |
| p50 (sustained) | < 300 ms |
| p95 (sustained) | < 1200 ms |
| p99 (sustained) | < 2500 ms |
| p95 (burst) | < 2500 ms |
| p95 (status polling) | < 500 ms |
| Failed requests | < 1% |
| Backpressure responses | < 5% |

Never run this against production or against a live ChatGPT account.

## What to record with every run

The report template captures latency and throughput automatically. Record these
alongside it:

```bash
# Database connections
psql -c "SELECT count(*), state FROM pg_stat_activity GROUP BY state;"

# Queue depth and lag
psql -c "SELECT status, count(*), min(run_after) FROM jobs GROUP BY status;"

# Instance
docker stats --no-stream
iostat -x 5 3         # await on the data volume
```

## Growth thresholds

Act when a measurement crosses these, sustained over 15 minutes:

| Signal | Threshold | Action |
| --- | --- | --- |
| CPU | > 70% | Move to `t3a.xlarge` (4 vCPU / 16 GiB) |
| CPU credit balance (t-class) | < 30 | Move off burstable to `m6a.large` |
| Memory | > 85% | Raise instance size, or lower `API_WORKERS` |
| p95 ingest latency | > 2 s | Profile queries; check `pg_stat_statements` |
| Queue depth | > 10,000 pending | Raise `WORKER_CONCURRENCY` to 4 |
| Queue depth | > 50,000 pending | Backpressure engages automatically; investigate |
| Oldest pending job | > 15 minutes | Worker is falling behind; scale it |
| Database connections | > 80% of `max_connections` | Lower pool sizes before raising `max_connections`; `verify_production.sh` warns at this threshold |
| EBS `await` | > 20 ms | Raise gp3 IOPS/throughput |
| Data volume | > 80% | Grow the volume (online) |

## The growth path

1. **Tune** (no downtime): raise `WORKER_CONCURRENCY`, adjust pool sizes, raise
   gp3 IOPS.
2. **Grow the instance** (~5 minutes downtime): stop, change type, start.
   `t3a.large → t3a.xlarge` doubles CPU and memory and is enough for roughly
   500-750 employees.
3. **Split application and database** (planned migration): move PostgreSQL to
   its own instance with its own EBS volume. The application already talks to it
   over the network, so this is a `DATABASE_URL` change plus a data migration.
   Appropriate beyond ~1,000 employees.
4. **Scale the application horizontally**: multiple application instances behind
   a load balancer, sharing one database. The job queue already supports many
   workers safely (`FOR UPDATE SKIP LOCKED`). Note that the in-process rate
   limiter becomes per-instance at this point and would need revisiting.

## Honest limits

One instance means one failure domain. This design accepts that in exchange for
operational simplicity, and compensates with tested backups, a separate data
volume that survives an instance rebuild, and a documented 4-hour recovery.

No single instance handles every possible pattern. If 250 employees all backfill
1,000-message conversations simultaneously, the queue will grow and backpressure
will engage — which is the designed behaviour, not a failure. Clients hold their
work locally and drain when capacity returns.
