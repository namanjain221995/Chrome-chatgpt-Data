# Testing

## Layers

| Layer | Count | Needs | Command |
| --- | --- | --- | --- |
| Backend unit | 166 | nothing | `make test-backend` |
| Backend integration | 112 | PostgreSQL | `make test-integration` |
| Extension | 105 | nothing | `make test-extension` |
| Compose smoke | 14 checks | Docker | `make test-compose` |
| Production Compose | secrets and bindings | Docker | `make test-production-compose` |
| Load smoke | 4 scenarios | Docker | `make load-test` |

`make verify` runs every row in this table, the migration round trip, schema
drift, image build, direct origin TLS, security checks, and deterministic artifacts.

## Backend unit tests

No database, no network, no S3. Fast enough to run on every save.

- `test_core_primitives.py` — hashing, fingerprints, filename safety, HTML
  sanitisation, log redaction
- `test_auth_and_policy.py` — OIDC verification with **local test keys**
  (nonce, audience, issuer, expiry, hosted domain, forged signature), RBAC,
  capture gates, workspace fail-closed, signed configuration
- `test_schemas_and_limits.py` — strict schemas, batch caps, attachment
  validation, rate limiting
- `test_services_offline.py` — S3 key builders, export splitting, EXIF
  stripping, partition helpers, the compliance adapter

## Backend integration tests

Real PostgreSQL, in a transaction that is always rolled back, with an in-memory
S3 double that implements the same surface as the real client.

- `test_ingest.py` — conversation upsert idempotency, completeness that never
  downgrades, message identity through all three layers, checksum rejection,
  edits and regenerations as new versions, partial → complete reconciliation,
  batch isolation, out-of-order arrival, partitioned writes
- `test_jobs_and_workers.py` — claiming, dedupe keys, priority, backoff, stale
  locks, **concurrent claiming across two sessions**, raw JSON reaching storage
  before completion, snapshot integrity hashes, the attachment pipeline
  (including a `.png` that is really a shell script), EXIF stripping into a
  separate curated copy, partition maintenance
- `test_api.py` — health, security headers, the signed config, sign-in with
  local keys, refresh rotation and single use, device revocation invalidating a
  live token, personal-workspace rejection over HTTP, body limits, RBAC, error
  contract, rate limiting, audit rows
- `test_governance.py` — retention, legal hold surviving a hard-delete policy,
  export gating and approval, whole-conversation splitting, compliance import
  ordering and tombstones

### The tests that encode the important guarantees

| Guarantee | Test |
| --- | --- |
| Drafts are never captured | `test_never_reads_the_composer…` (extension) |
| A backfill does not duplicate messages | `test_backfill_shift_does_not_duplicate_messages` |
| Raw JSON reaches storage before completion | `test_raw_json_reaches_storage_before_completion` |
| A storage failure leaves the event unarchived | `test_storage_failure_leaves_the_event_unarchived` |
| Two workers never claim the same job | `test_skip_locked_prevents_double_delivery` |
| A stale lock cannot be completed by the dead worker | `test_stale_locks_are_recovered` |
| Legal hold survives a hard-delete policy | `test_legal_hold_survives_retention` |
| Personal workspaces are refused | three tests, one per layer |
| Server gates cannot be bypassed locally | `server-side gates cannot be bypassed locally` (extension) |
| A lying MIME type is rejected | `test_content_type_lying_is_detected` |
| An employee cannot complete another's attachment | `test_another_employee_cannot_complete_someone_elses_attachment` |
| Validation errors never echo content | `test_validation_errors_do_not_echo_content` |

## Extension tests

jsdom plus sanitized fixtures. **No test ever contacts a live ChatGPT account.**

- `dom-adapter.test.ts` — user, assistant and tool messages; code with language;
  table cells; nested lists; headings; quotes; citations; attachments; generated
  images; branch counters; streaming state; hostile markup; an unrecognised page
  yielding nothing; a 200-message transcript
- `capture-behaviour.test.ts` — workspace verification (nine fail-closed cases),
  tamper-resistant configuration, live-observer stability (nothing emitted while
  streaming, exactly one emission when stable, partial on page hide, never twice),
  route changes, backfill scroll restoration and honest completeness, message
  normalisation and idempotency-key stability across a backfill
- `queue-and-attachments.test.ts` — queue bounds by count, bytes and age;
  retry and drop; durability across a reopen; attachment capture by input, paste
  and drop; size and MIME refusal; PKCE state validation; API error
  classification; the backend token never reaching a storage host; diagnostics
  redaction

## Deployment and topology checks

| Check | What it proves |
| --- | --- |
| `scripts/verify_production_config.sh` | Statically, from `docker compose config`: FastAPI exposes 8000 and publishes nothing, PostgreSQL publishes nothing, pgAdmin is loopback-only behind the `admin` profile, the tunnel image is digest-pinned with `--no-autoupdate` and no `--token` on the command line, the tunnel cannot reach PostgreSQL, no service uses a floating tag or a static AWS key, the capture gates are false, and the connection-pool budget fits inside `max_connections`. It also fails any service that publishes a port while attached only to internal networks — Docker accepts that binding and then silently never creates it. |
| `tests/integration/production_compose_smoke_test.sh` | The same contract at runtime, by inspecting real container port bindings, plus file-based secrets, a special-character database password, non-root execution, the backup role's `.pgpass`, and that `cloudflared` stays unstarted without a token. |
| `scripts/verify_extension_package.sh` | The packaged ZIP contains the runtime files and no `.env`, source map, key material, credential-shaped string or widened content-script match. |
| `scripts/secret_scan.sh` | Credential shapes across everything git can publish. Gitignored key material is reported as a warning, since it cannot reach a commit. |
| `scripts/docs_check.sh` | Every required document exists, the capture-limitations promises are still present, no document points at a removed script, and each runbook still describes the implemented architecture. |
| `make lint-shell` | `bash -n` and ShellCheck over every script. |
| `make lint-workflows` | actionlint over the workflows and the composite action. |

## Compose smoke test

```bash
make test-compose
```

Fourteen checks against a real stack: clean start, PostgreSQL health, migrations
on an empty database, direct loopback API readiness, signed config, a real HTTP
batch ingest, replay deduplication, a full restart with no data loss, and a
backup that restores into a clean database. The worker starts successfully and
is then stopped before ingestion so an offline test never attempts an AWS write.
AWS requests are covered by Stubber and the explicit dedicated-prefix workflow.
The stack is destroyed on exit.

## Load tests

`make load-test` uses a pinned k6 container and an isolated local API/database
stack to run short sustained, burst, attachment-metadata, and status-polling
scenarios. It writes ignored reports under `artifacts/`. The full five-minute
capacity profile remains available in `tests/load/k6-ingest.js` for a dedicated
load environment with an out-of-band access token. See
[CAPACITY.md](CAPACITY.md). Never run it against production or
a live ChatGPT account.

## Fixtures

`apps/chrome-extension/tests/fixtures/transcripts.ts` holds hand-written
approximations of the ChatGPT transcript structure. They contain no real
employee content, no cookies and no tokens. When the product's markup changes,
add a fixture and a failing test **before** changing selectors.

## Writing a new test

1. Name it after the guarantee, not the function:
   `test_legal_hold_survives_retention`, not `test_retention_2`.
2. Assert the behaviour, not the implementation.
3. For a bug fix, write the failing test first — several tests here exist
   because they caught a real bug (enum values, content-script imports,
   idempotency-key stability).
4. Integration tests get `@pytest.mark.integration` so they skip cleanly without
   a database.

## Coverage

Coverage is reported but not gated on a number. The gate is the guarantee table
above: every claim this system makes to an employee or an auditor has a test.
