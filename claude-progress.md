# Refactor progress

**Project:** TechSara Managed ChatGPT Session Archive
**Started:** 2026-08-13
**Status:** all local verification passed; the branch is not deployable until
the final pushed commit also has a green GitHub Actions run.

## Architecture checklist

- [x] Preserve the Manifest V3 extension and honest capture boundaries.
- [x] Preserve FastAPI, PostgreSQL schema/migrations, idempotent ingestion, the
  PostgreSQL job queue, worker handlers, backups, and compliance adapter.
- [x] Publish only the FastAPI container's TLS listener on origin port 443.
- [x] Restrict origin ingress to Cloudflare source ranges in the manual network
  design; keep all database and administrative ports private.
- [x] Keep PostgreSQL unexposed and application-to-database networking internal.
- [x] Keep pgAdmin disabled by default under the `admin` profile and bind it to
  `127.0.0.1:5050` only.
- [x] Pin production storage to `techsara-chatgpt` in `us-east-1`, with no
  endpoint override, path-style addressing, or static access keys.
- [x] Preserve false defaults for browser capture authorization and training
  export.
- [x] Add optional compliance poller and nightly backup services.
- [x] Add resource limits appropriate to an initial 2 vCPU / 8 GiB host.

## Host and deployment checklist

- [x] Add Origin CA installation/validation and a Cloudflare proxied-DNS Full
  (strict) runbook without a host web server.
- [x] Add Ubuntu Docker installation and idempotent EBS bootstrap scripts.
- [x] Add SSM-to-root-file configuration rendering.
- [x] Refactor EC2 deployment with immutable image tags, pre-migration backup,
  migrations, readiness, application rollback, and direct TLS validation.
- [x] Add public/private binding, database, role identity, S3, and TLS deployment
  verification.
- [x] Write manual AWS setup in the mandated 26-step order and a Console
  checklist with CLI checkpoints and rollback notes.
- [x] Update the deterministic release bundle for direct container TLS.

## Test and CI checklist

- [x] Remove local object-storage server dependencies from Compose and CI.
- [x] Add botocore Stubber coverage for encrypted/checksummed AWS requests.
- [x] Add an explicitly dispatched real-S3 test restricted to a supplied
  `integration-tests/...` prefix and temporary OIDC credentials.
- [x] Add production Compose topology and S3 guard assertions.
- [x] Exercise production direct TLS with a disposable certificate.
- [x] Add repository policy scans, secret scanning, dependency audits, migration
  round trips, PostgreSQL integration tests, extension checks, and bundle output.
- [x] Run every required Make target and repair all local failures.
- [x] Run the Compose smoke and available load scenario.
- [ ] Commit and push the complete refactor.
- [ ] Monitor the pushed GitHub Actions run and repair every remote failure.
- [ ] Record the final green run URL/status and commit SHA.

## Verification results

`make verify` passed on the complete working tree on 2026-08-13:

- Ruff, mypy (58 files), ESLint, TypeScript, Bash syntax, ShellCheck 0.10.0,
  and actionlint 1.7.7 passed.
- Backend: 166 unit tests and 112 PostgreSQL integration tests passed. The 11
  SQLAlchemy transaction warnings are pre-existing test-rollback warnings.
- Extension: 105 tests passed; Manifest V3 and eight shared-schema fixtures
  validated; the ZIP reproduced byte-for-byte with SHA-256
  `722ef2cdda04c67b094549a82f418447d0d1df929907bc1aae63cb5c1f3db164`.
- Alembic upgraded an empty database, downgraded/re-upgraded, and reported no
  schema drift. A logical backup restored 41 tables at revision `0002_fts`.
- The production Compose smoke validated direct origin TLS, strict Host
  handling, root-owned certificate files, special-character database secrets,
  non-root API execution, exact S3 settings, worker startup, and loopback-only
  pgAdmin. The local Compose smoke passed all 14 checks.
- The four-scenario k6 smoke completed 63 requests with zero failed requests,
  zero backpressure, 245 accepted messages, p50 56.73 ms, and p95 112.2 ms.
- Retired-technology, prohibited-AWS-service, secret, pip-audit, and npm-audit
  checks passed with zero known dependency vulnerabilities.

The first load run exposed a concurrent first-request workspace insert race.
The policy service now performs an idempotent PostgreSQL insert and selects the
canonical row; a two-session regression test covers the race. A locally green
tree is not final completion; the remote Actions run must also be green.

GitHub Actions run `31636730931` passed six jobs but the container job found a
cold-runner defect: the production smoke requested `--pull never` before its
pinned PostgreSQL and pgAdmin images existed. The smoke now pulls only missing
third-party images while retaining the already-built local backend image. The
deprecated Node 20 checkout/upload action majors reported by that run were also
updated to their current Node 24 releases. A replacement run is pending.

The replacement run `31637210454` confirmed the image pull repair, then exposed
a Compose-version difference: its global `--wait` rejected the deliberately
healthcheck-free worker even though that worker was running. The production
smoke now waits explicitly for healthy API/PostgreSQL/pgAdmin states and a
running worker, which tests the intended contract without depending on that
Compose-version behavior. A further replacement run is pending.

## External steps still intentionally manual

- Secure and lifecycle-configure the existing S3 bucket.
- Create/review the prefix-scoped IAM policy and EC2 instance role.
- Launch and size EC2/EBS, attach the role, allocate the Elastic IP, and create
  the Cloudflare-source-only port 443 security group.
- Create SSM parameters and supply identity/compliance secrets.
- Create the proxied Cloudflare record and Origin CA certificate, then install
  it for the API container.
- Register the OIDC client and publish the private extension.
- Complete legal/privacy approvals and the staged pilot before capture gates
  can change.
