# Build progress and final report

**Project:** TechSara Managed ChatGPT Session Archive
**Completed:** 2026-08-12
**Status:** complete — `make verify` passes; the end-to-end compose smoke test
passes 23/23 checks against a real stack.

---

## 1. What was built

| Component | Location | State |
| --- | --- | --- |
| Chrome MV3 extension | `apps/chrome-extension/` | 12 modules, 105 tests, reproducible ZIP |
| FastAPI backend | `services/backend/app/` | 15 endpoints, 269 tests |
| Database | `services/backend/alembic/` | 25 tables, 2 migrations, monthly partitioning |
| Durable job queue | `app/services/jobs.py` | `FOR UPDATE SKIP LOCKED`, stale-lock recovery |
| Worker | `app/workers/worker.py` | 9 handlers, bounded concurrency, graceful shutdown |
| Compliance poller | `app/workers/compliance_poller.py` | Implemented, idle until authorized |
| Shared schemas | `packages/schemas/` | 19 JSON Schemas generated from the Pydantic models |
| Docker Compose | `compose.yaml`, `compose.prod.yaml` | 9 services, both configurations validate |
| Terraform | `infra/terraform/` | 9 files; fmt and validate clean |
| Scripts | `scripts/` | backup, restore, verify, deploy, bundle, secret scan |
| CI/CD | `.github/workflows/` | ci, release, deploy |
| Documentation | `docs/`, root | 21 documents, ~3,400 lines |

**Code:** ~10,400 lines of Python, ~4,700 of TypeScript, ~5,900 of tests.

### The 12 extension modules

`workspace-verifier`, `route-observer`, `dom-adapter`, `conversation-backfill`,
`live-observer`, `message-normalizer`, `attachment-observer`, `offline-queue`,
`auth-client`, `managed-config`, `popup`, `options/status` — plus `api-client`
and a `sync-engine` in the service worker.

### The 15 endpoints

`GET /health/live`, `GET /health/ready`, `GET /api/v1/config`,
`POST /api/v1/auth/exchange`, `POST /api/v1/devices/register`,
`POST /api/v1/conversations/upsert`, `POST /api/v1/messages/batch`,
`POST /api/v1/capture-events/batch`, `POST /api/v1/attachments/init`,
`POST /api/v1/attachments/complete`, `POST /api/v1/feedback`,
`GET /api/v1/sync/status`, `GET /api/v1/admin/health-summary`,
`POST /api/v1/admin/exports`, `GET /api/v1/admin/exports/{export_id}`
(plus `POST /api/v1/admin/devices/revoke`).

---

## 2. Verification results

All commands run on Ubuntu 24.04 (aarch64), Python 3.12.3, Node 18.19.1,
Docker 29.2.1, Terraform 1.9.8.

| Check | Command | Result |
| --- | --- | --- |
| Python lint | `ruff check app tests` | pass |
| Python format | `ruff format --check` | pass, 71 files |
| Python types | `mypy app` | pass, 58 files, 0 issues |
| TypeScript lint | `eslint --max-warnings 0` | pass |
| TypeScript types | `tsc --noEmit` | pass |
| Shell syntax | `bash -n` on every script | pass |
| Backend unit tests | `pytest -m "not integration"` | **158 passed** |
| Backend full suite | `pytest` | **269 passed** |
| Extension tests | `vitest run` | **105 passed** |
| Migrations from empty | `alembic upgrade head` | pass |
| Model/schema drift | `alembic check` | no operations detected |
| Migration round trip | `downgrade -1 && upgrade head` | pass |
| Shared schema drift | `generate_schemas.py` + ajv | pass, 8 payloads |
| Extension build | `vite build` ×3 | pass |
| Manifest validation | `validate-manifest.mjs` | pass, 0 warnings |
| Extension package | `package.mjs` | reproducible, identical SHA-256 across runs |
| Compose (dev) | `docker compose config` | pass |
| Compose (prod) | `-f compose.yaml -f compose.prod.yaml config` | pass |
| Terraform format | `terraform fmt -check -recursive` | pass |
| Terraform validate | `terraform validate` | pass |
| Prohibited AWS services | `verify_no_prohibited_aws_services.sh` | **10/10 checks pass** |
| Secret scan | `secret_scan.sh` | pass |
| npm audit | `npm audit --omit=dev` | 0 vulnerabilities |
| Documentation | `docs_check.sh` | pass |
| **End-to-end smoke test** | `compose_smoke_test.sh` | **23/23 checks pass** |

**Total automated tests: 374** (269 backend + 105 extension), plus 23
end-to-end checks against a live stack.

### The end-to-end smoke test, in full

Against a real `docker compose up` with PostgreSQL 16, MinIO, the API, the
worker and Caddy:

```
PASS  application image built from the working tree
PASS  postgres and minio started
PASS  object storage bucket created
PASS  postgres accepts connections
PASS  migrations applied
PASS  schema has 41 tables            (25 logical + 16 partitions)
PASS  capture_events has 5 partitions
PASS  API is ready through Caddy on :18081 (TLS)
PASS  plain HTTP redirects to HTTPS (301)
PASS  signed config reports capture_active
PASS  config states drafts are never captured
PASS  anonymous config withholds workspace identifiers
PASS  minted a test session token
PASS  message batch accepted
PASS  replayed batch recognised as duplicate
PASS  capture event marked archived
PASS  raw object key recorded: raw/events/year=2026/month=08/day=12/workspace=…
PASS  1 raw object(s) present in object storage
PASS  1 conversation snapshot(s) written
PASS  message count survived the restart (1)
PASS  API recovered after restart
PASS  backup created (187909 bytes)
PASS  restore reproduced 1 message(s)
```

---

## 3. Bugs found and fixed during the build

Each was reproduced before being fixed, and each now has a regression test.

### Enum names persisted instead of values (would have broken deduplication)

SQLAlchemy's `Enum` persists the Python member *name* by default. Every partial
index and CHECK constraint in this schema targets the lowercase *value*. The
database stored `PENDING` while `uq_jobs_dedupe_active … WHERE status IN
('pending','running')` matched nothing, so job deduplication silently did not
work and the claim index was never used.
**Fix:** `values_callable` plus `create_constraint=True`; migration regenerated.
**Test:** `test_dedupe_key_prevents_duplicate_live_jobs`.

### Content script emitted an ES import (capture would never have started)

A single Vite build with four entry points produced
`import {…} from "./chunks/util.js"` at the top of `content-script.js`. An MV3
content script is injected as a classic script with no module loader, so it
would have thrown on every page load, with no error surfaced anywhere useful.
**Fix:** separate self-contained builds for the worker and the content script;
the manifest validator now fails the build if an import survives.

### Every S3 write failed against MinIO (blinded local and CI testing)

MinIO answers `NotImplemented` to `x-amz-server-side-encryption: AES256` unless
it has a KMS backend. Real AWS is unaffected, but locally and in CI *every*
archive write failed, so the whole raw-JSON-to-storage path was untested.
**Fix:** send SSE headers only when talking to real S3; the bucket policy still
denies unencrypted uploads there.
**Tests:** four in `TestEncryptionArguments`, plus the smoke test now proves the
path end to end.

### Idempotency key included the sequence index

A backfill renumbers every message, so a retry produced a different key and the
backend saw a new message. **Fix:** derive the key from stable material only.
**Test:** `test_keeps_the_key_stable_when_the_sequence_index_shifts_after_a_backfill`.

### Backfill duplicated already-captured messages

Same root cause, server side: the fingerprint includes a sequence neighbourhood,
so a shifted index looked like a new message. **Fix:** a third identity layer —
identical normalised content for the same role in the same conversation is the
same message. **Test:** `test_backfill_shift_does_not_duplicate_messages`.

### Others

- `RouteObserver` restored a *bound* copy of `history.pushState`, so repeated
  start/stop left a wrapper behind.
- Queue eviction ordering used a composite number that exceeded
  `Number.MAX_SAFE_INTEGER`.
- `sha256Hex` rejected cross-realm `ArrayBuffer`s from `File.arrayBuffer()`.
- The smoke test validated nothing over plain HTTP, because Caddy correctly
  301-redirects it; it now uses TLS and asserts the redirect explicitly.
- The worker inherited the API's HTTP health check and reported unhealthy.

---

## 4. Security review

Four findings were fixed and four accepted with documented rationale. Full
detail in [docs/SECURITY_REVIEW.md](docs/SECURITY_REVIEW.md).

| ID | Severity | Finding | Status |
| --- | --- | --- | --- |
| F-01 | Medium | Unconstrained source identifiers reached S3 keys and database values | Fixed |
| F-02 | Medium | Unauthenticated config disclosed the managed workspace label and id allowlist | Fixed |
| F-03 | Medium | Enum names persisted, disabling partial indexes and CHECK constraints | Fixed |
| F-04 | Low | An allowlisted workspace id was accepted without an observed signal | Fixed |
| F-05 | Low | Raw client IP stored alongside its hash in the audit trail | Accepted |
| F-06 | Low | The `id_token` grant bypasses PKCE (still fully verified) | Accepted |
| F-07 | Info | Per-process rate limiting is approximate | Accepted |
| F-08 | Info | Single instance is a single failure domain | Accepted by design |

No high or critical findings remained open.

---

## 5. Generated artifacts

| Artifact | Path |
| --- | --- |
| Extension package | `artifacts/techsara-chatgpt-archive-extension-1.0.0.zip` (75,943 bytes) |
| Extension checksum | `artifacts/techsara-chatgpt-archive-extension-1.0.0.zip.sha256` |
| Deployment bundle | `artifacts/techsara-chat-archive-deploy-<tag>.tar.gz` |
| Bundle checksum | `artifacts/techsara-chat-archive-deploy-<tag>.tar.gz.sha256` |
| Container image | `techsara/chat-archive-backend:local` (built locally; CI pushes `ghcr.io/<org>/…-backend:<git-sha>`) |

The extension ZIP is byte-reproducible: building the same commit twice produces
an identical SHA-256, verified in CI.

---

## 6. Administrator-supplied configuration

### Secrets (via `scripts/put_secrets.sh`, never committed)

| Parameter | Source |
| --- | --- |
| `postgres_password` | generate (`--generate`) |
| `jwt_secret` | generate — `openssl rand -base64 48` |
| `config_signing_key` | generate — `openssl rand -base64 48` |
| `pgadmin_password` | generate |
| `oidc_client_secret` | Google Cloud Console, OAuth client |
| `openai_compliance_api_key` | OpenAI Enterprise agreement (optional) |

### Non-secret configuration (SSM or Terraform variables)

`aws_region`, `s3_bucket`, `public_base_url`, `caddy_domain`, `caddy_email`,
`allowed_email_domains`, `oidc_client_id`, `oidc_issuer`, `oidc_required_hd`,
`extension_ids` (after the first build), `managed_workspace_label` and/or
`managed_workspace_ids`, `image_repository`, `image_tag`.

### The two policy decisions

`browser_content_capture_enabled` and `openai_written_authorization_confirmed`
are both **false** until an authorized person sets them. Nothing is captured
until both are true.

---

## 7. Deployment order

```
1.  cd infra/terraform && terraform apply
2.  ./scripts/put_secrets.sh --project techsara-chat-archive --region <region>
3.  Point DNS at the Elastic IP
4.  Create the Google OAuth client; put the id in SSM
5.  ./scripts/deploy_bundle.sh, copy it to the instance
6.  sudo IMAGE_TAG=<sha> ./scripts/deploy_ec2.sh   (+ enable the systemd unit)
7.  make extension-zip → publish → register the extension id in SSM and Terraform
8.  Redeploy so CORS and S3 CORS include the extension origin
9.  Enable the two capture gates and redeploy
10. Verify capture, then confirm backups and EBS snapshots
```

Step-by-step with verification at each stage:
[docs/AWS_STEP_BY_STEP.md](docs/AWS_STEP_BY_STEP.md).

---

## 8. Known limitations, stated honestly

### What the extension cannot do (limits of the approach, not the build)

1. **Conversations never opened in this browser are not archived.** No sidebar
   crawling, no undocumented endpoints. Company-wide coverage needs the
   authorized compliance feed.
2. **Other devices and browsers are invisible** to a given installation.
3. **Historical attachment bytes are often unavailable** — the page shows a tile,
   not a file. Recorded honestly as `metadata_only`.
4. **Hidden model reasoning is never captured.** It is not rendered.
5. **DOM selectors are best-effort.** ChatGPT's markup is not a public contract.
   All selectors live in one version-stamped adapter with fixture tests, so a
   product change is a single-file fix — but it *will* need fixing occasionally.

### What is implemented but inactive until configured

6. **The compliance poller** is complete and idle. It needs a base URL, a log
   path, a field map and an API key from the Enterprise agreement. No endpoint
   path was invented.
7. **Curated export** requires `TRAINING_EXPORT_ENABLED=true` plus an explicit
   approval row per conversation. Both default to off.
8. **ClamAV scanning** is an optional compose profile, off by default because it
   needs ~1.5 GiB that an 8 GiB instance does not have spare.

### Operational limits

9. **Single instance.** One failure domain, ~4-hour documented recovery,
   vertical scaling only. Thresholds in
   [docs/SCALING_250_USERS.md](docs/SCALING_250_USERS.md).
10. **RPO is 24 hours** from nightly logical backups. WAL archiving would reduce
    it to minutes and is the first recommended improvement.
11. **Rate limiting is per-process**, divided by the worker count.
12. **A deployment restarts the API**, so there is a brief gap. Clients queue
    locally and lose nothing.

### Not run in this environment

13. **The k6 load test** is written and covers the full 250-employee envelope,
    but was not executed here: it needs a dedicated load environment, and the
    brief forbids running it against production. Run it before go-live and
    record the report at `artifacts/load-test-report.md`.
14. **`terraform plan` against real AWS** was not run — no credentials in this
    environment. `fmt` and `validate` both pass, and CI runs a plan when
    credentials are available.
15. **Live Chrome installation** was not exercised: the brief forbids automating
    a live ChatGPT account. The extension is validated against sanitized DOM
    fixtures, its manifest, and a reproducible package build.

---

## 9. Recommended next steps

1. Run the load test in a dedicated environment and record the report.
2. Enable WAL archiving to cut the RPO.
3. Add an admin API for legal holds, which today are applied through SQL and are
   therefore outside the audit trail.
4. Add a canary that exercises the full ingest path hourly and alerts on
   silence — capture stopping quietly (because the markup changed) is the
   failure most likely to go unnoticed.
5. Enable S3 Object Lock on the `backups/` prefix.
6. Schedule a quarterly disaster-recovery rehearsal and a DOM adapter review.
