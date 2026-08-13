# Refactor progress

**Project:** TechSara Managed ChatGPT Session Archive
**This refactor:** Cloudflare Tunnel ingress + GitHub Actions → SSH → EC2 CI/CD
**Started:** 2026-08-13
**Status:** complete. `make verify` is green locally and GitHub Actions run
`31652706875` is green on commit `2727756d32882d429a49cb8134f40eedb70e0528`.
Production deployment is intentionally skipped until the SSM parameters and the
Cloudflare tunnel exist; see "Verification results".

## State before this refactor

The application layer was already complete and CI was green on
`64e05dab98b25dd5b1d0d1b7dbe64e0e6a04b2b0`: MV3 extension, FastAPI, PostgreSQL 16
with 25 tables and Alembic migrations, a `FOR UPDATE SKIP LOCKED` job queue,
presigned S3 uploads, backups with manifests, and the compliance adapter.

The **deployment** layer targeted a different architecture:

| Was | Now |
| --- | --- |
| Ubuntu host | Amazon Linux 2023, `ec2-user` |
| FastAPI terminating origin TLS on published port 443 | FastAPI `expose: 8000`, no host port |
| Cloudflare proxied DNS + an origin certificate key mounted into the API | Named Cloudflare Tunnel, no origin certificate |
| Security group open to Cloudflare ranges on 443 | No inbound application port at all |
| GitHub Actions → AWS OIDC → SSM `SendCommand` | GitHub Actions → SSH → EC2 |
| Deployment tarball bundle copied to the host | `git reset --hard <sha>` in the existing checkout |
| GHCR image pushed on tags | Image built on the instance, tagged with the commit SHA |
| `deploy_ec2.sh` | `deploy_production.sh` with `flock`, release records and rollback |

## Architecture conflicts found and resolved

- [x] Direct origin TLS on published port 443 contradicted "Cloudflare Tunnel is
  the only public application ingress". Removed the TLS listener, the
  certificate mounts and `install_origin_tls.sh`.
- [x] AWS/GitHub OIDC in `deploy.yml` and the real-S3 CI job contradicted
  "Do NOT use GitHub OIDC / AWS OIDC". Both removed; no workflow requests
  `id-token` any more.
- [x] SSM `SendCommand` deployment contradicted "Use SSH". Replaced.
- [x] The deployment bundle was a second, divergent way to put files on the
  host. Removed, so there is exactly one deployment path.
- [x] Ubuntu-specific `install_docker.sh` and `bootstrap_ec2_host.sh` could not
  run on Amazon Linux 2023. Rewritten for `dnf`.

## Defects found and fixed

- [x] **`scripts/secret_scan.sh` never ran three of its checks.** The pattern was
  passed positionally, so a pattern starting with `-----` was parsed by `grep`
  as options; the command failed, stderr was discarded and the empty result was
  reported as "ok". The private-key check had therefore never executed. Fixed
  with `-e`, and proven by planting a key, an AWS id and a tunnel token in a
  scanned file and observing all three findings.
- [x] **pgAdmin's `127.0.0.1:5050` binding never existed.** A container attached
  only to an `internal: true` Docker network cannot publish a host port: Docker
  accepts the `PortBindings` and silently never creates them, so the documented
  admin access could not have worked. pgAdmin now also joins a dedicated
  non-internal `admin` bridge. `verify_production_config.sh` now fails any
  service that publishes a port while attached only to internal networks, and
  the production smoke asserts the real binding at runtime.
- [x] **Readiness could block for ~60 s during an S3 outage.** `/health/ready`
  used the data-path S3 client (5 retries, 60 s read timeout). It now uses a
  dedicated probe client (1 attempt, 2 s connect, 3 s read) behind a 5 s
  asyncio timeout and a 60 s result cache guarded by a lock.
- [x] **Connection pools were unbounded relative to `max_connections`.** Added
  `Settings.max_expected_database_connections` and a production guardrail that
  refuses to start when pools could exceed `POSTGRES_MAX_CONNECTIONS` minus a
  15-connection reserve, plus the same assertion statically in CI.

## Compose and runtime checklist

- [x] `cloudflared` service: pinned by tag **and** digest, `--no-autoupdate`,
  token from a root-owned env file (never on the command line), loopback-only
  metrics port for a real `/ready` health check.
- [x] `api`: plain HTTP, `expose: 8000`, no `ports`, `${API_WORKERS}` honoured.
- [x] `postgres`: internal network only, no host port, `pg_isready` health check,
  `max_connections` shared with the application's pool budget.
- [x] `pgadmin`: `admin` profile, loopback only, never started by a deployment.
- [x] `compliance-poller`: `compliance` profile, started only when its flag is on.
- [x] `backup`: nightly `pg_dump` → gzip → SHA-256 manifest → S3.
- [x] Capture gates default false everywhere and are validated as literal
  `true`/`false` when rendered from SSM.

## Deployment checklist

- [x] `scripts/prepare_server_storage.sh` — idempotent directory layout.
- [x] `scripts/fetch_ssm_secrets.sh` — renders `.env.production` (0600) and
  root-owned secret files; derives `ARCHIVE_HOSTNAME` from `public_base_url`.
- [x] `scripts/deploy_production.sh` — `flock`, release records, exact-SHA
  checkout, SSM render, topology validation, pre-migration backup, migration,
  service recreation, six health checks, automatic rollback, dangling-only prune.
- [x] `scripts/rollback_production.sh` — reuses the previous SHA-tagged image,
  records the outcome honestly, never downgrades the schema.
- [x] `scripts/verify_production.sh` — Docker, Compose, containers, PostgreSQL,
  API, tunnel, public URL, listening sockets, AWS identity, S3, capture gates,
  disk, memory and release identity.
- [x] `scripts/test_restore.sh` — local mode and `--from-s3-latest`.

## CI/CD checklist

- [x] `ci.yml` — seven jobs, `workflow_call` enabled, `contents: read` only.
- [x] `deploy.yml` — `push` to `main`, calls CI, `production-deploy` concurrency
  with `cancel-in-progress: false`, environment `production`, SSH.
- [x] `release.yml` — tag `v*`, extension ZIP + checksum, `contents: write` on
  the publishing job only.
- [x] `test-ec2-connection.yml` — manual, prints only non-sensitive facts.
- [x] `.github/actions/ec2-ssh` — key at mode 600, pinned or reported host key,
  `StrictHostKeyChecking yes` always.
- [x] Extension artifact renamed to `techsara-chatgpt-extension-<git-sha>.zip`
  and inspected by `scripts/verify_extension_package.sh`.

## Verification results

`make verify` passed on the complete working tree on 2026-08-13:

- Ruff (lint and format), mypy over 58 source files, ESLint, TypeScript,
  `bash -n`, ShellCheck 0.10.0 and actionlint 1.7.7 all passed.
- Backend: 178 unit tests and 112 PostgreSQL integration tests passed. The 11
  SQLAlchemy warnings are the pre-existing test-rollback warnings.
- Extension: 105 tests across three files; Manifest V3 validated with zero
  warnings; eight shared-schema payloads validated; the ZIP reproduced
  byte-for-byte at SHA-256
  `2671f3c4bca6caec8914bd08dfbdad5ddcbe77c61ce0f4bc571dfe28cfdc891a`, and the
  package inspection found no environment file, source map, key material,
  credential-shaped string or widened content-script match.
- Alembic upgraded an empty database, downgraded one revision, re-upgraded and
  reported no drift. The restore test reproduced 41 tables at revision
  `0002_fts` with matching row counts.
- The production Compose smoke passed the tunnel topology contract: no host
  binding on `api`, `postgres` or `worker`; a real loopback binding on pgAdmin;
  `cloudflared` correctly unstarted without a token; file-based secrets, a
  special-character database password, non-root execution and the backup
  `.pgpass` path all verified. The development Compose smoke passed all 14
  checks.
- k6 four-scenario smoke: 65 requests, 0 failed, 255 messages accepted,
  0 duplicates, 0 rejected, backpressure rate 0, p50 55.32 ms, p95 85.66 ms.
- Retired-technology, prohibited-AWS-service, secret, pip-audit and npm-audit
  scans passed with zero known dependency vulnerabilities.

### GitHub Actions

The first push exposed two CI problems, both now fixed:

1. The production smoke compared the raw `NetworkSettings.Ports` JSON. Docker
   renders an exposed-but-unpublished port as `{"5432/tcp":null}` locally and
   as `{}` on the runner, so the assertion failed on a healthy stack. It now
   asserts the property that actually matters — that no host binding exists.
2. A push to `main` started `ci.yml` and `deploy.yml`'s nested CI in the same
   concurrency group, so one was always cancelled. `ci.yml` now runs on pull
   requests, `workflow_call` and manual dispatch only.

Run `31652706875` on commit `2727756d32882d429a49cb8134f40eedb70e0528` is green:
all seven CI jobs plus the deploy job.

The deploy job connected to the instance over SSH and ran its preflight. SSH,
passwordless sudo, Docker, the git checkout, the AWS CLI and the EC2 instance
role all verified. The eleven required SSM parameters and
`/srv/techsara-chat-archive` do not exist yet, so the deployment was **skipped
with the exact list of what to create** rather than half-applied. Nothing on
the host was changed.

## External steps still intentionally manual

- Create the Cloudflare Tunnel, copy its token into SSM, and route the public
  hostname to `http://api:8000`.
- Create the SSM parameters, including `public_base_url`.
- Confirm the EC2 instance role can reach S3 and Parameter Store.
- Pin `EC2_SSH_HOST_KEY` as a repository variable after verifying the
  fingerprint against the EC2 system log.
- Register the OIDC client and publish the private extension.
- Complete legal/privacy approvals and the staged pilot before either capture
  gate changes.
