# Permanent project instructions

## System boundary

This repository archives authorized conversations for roughly 250 employees.
Production is one Amazon Linux 2023 EC2 host (`ec2-user`, application at
`/opt/techsara-chat-archive`) running Docker Compose: PostgreSQL 16, one-shot
migrations, FastAPI, a PostgreSQL-backed worker, `cloudflared`, nightly backups,
an optional authorized compliance poller, an optional private pgAdmin, and AWS
S3 bucket `techsara-chatgpt` in `us-east-1`.

A named Cloudflare Tunnel is the only public ingress. Deployment is GitHub
Actions → SSH → EC2. There is no container registry, no GitHub OIDC and no AWS
OIDC.

## Invariants

1. Never capture unsent drafts, keystrokes, passwords, cookies, page storage,
   ChatGPT session tokens, hidden model reasoning, personal workspaces, or other
   websites.
2. Fail closed. Missing configuration, weak workspace evidence, either disabled
   server gate, or the kill switch means capture nothing.
3. The server decides policy. Local extension settings cannot enable capture.
4. Never claim complete history from browser capture. Only the authorized
   compliance feed may mark coverage `compliance_verified`.
5. Never invent an upstream compliance endpoint or response field.
6. Never commit a secret or log content, cookies, tokens, authorization headers,
   passwords, presigned URLs, or credentials.
7. Production AWS access comes only from the EC2 instance role and normal SDK
   credential provider chain. The extension receives no AWS credential, and
   GitHub Actions holds no AWS credential.
8. No application port is published on the host. FastAPI uses `expose: 8000`
   and PostgreSQL has no host binding. The only host bindings that may exist
   are loopback-only management endpoints: the `cloudflared` metrics port
   (`127.0.0.1:2000`) and, under the `admin` profile only,
   `127.0.0.1:5050` for pgAdmin.
9. Public ingress is the named Cloudflare Tunnel routing to `http://api:8000`.
   The tunnel image is pinned by tag and digest, runs `--no-autoupdate`, and
   receives its token through a root-owned env file rendered from SSM — never
   on the command line. Quick Tunnels are forbidden.
10. Capture gates and training export default false and stay false during
    development, deployment, migration, and rollback unless separately approved.
11. No test automates a live ChatGPT account. Use sanitized DOM fixtures only.
12. The four GitHub secrets `EC2_HOST`, `EC2_USER`, `EC2_SSH_PORT` and
    `EC2_SSH_PRIVATE_KEY` are the only secrets GitHub needs. Every production
    application secret lives in AWS SSM under `/techsara-chat-archive/`.
13. Never use `StrictHostKeyChecking=no`. Host keys are pinned through the
    `EC2_SSH_HOST_KEY` repository variable, or learned once and reported with
    a warning.
14. Deployments never run `docker system prune -a`, never remove a volume, and
    never downgrade the schema automatically.

## Sources of truth

| Concern | Location |
| --- | --- |
| DOM selectors and parsing | `apps/chrome-extension/src/modules/dom-adapter.ts` |
| Capture decisions | `services/backend/app/services/policy.py` |
| API wire models | `services/backend/app/schemas/` |
| Generated schemas | `packages/schemas/` |
| Production guards | `services/backend/app/core/config.py` |
| S3 operations and keys | `services/backend/app/services/storage.py` |
| Job claiming and retry | `services/backend/app/services/jobs.py` |
| Worker handlers | `services/backend/app/workers/handlers.py` |
| Production topology | `compose.prod.yaml` |
| Topology assertions | `scripts/verify_production_config.sh` |
| Deployment | `scripts/deploy_production.sh` |
| Rollback | `scripts/rollback_production.sh` |
| Runtime configuration from SSM | `scripts/fetch_ssm_secrets.sh` |

## Change rules

For endpoints, use strict Pydantic request/response models, enforce policy before
writes, audit administration, regenerate shared schemas, and add rejection tests.

For database changes, edit the model, generate/review an Alembic revision, and
run `make migration-check`. Enums use the project `_enum()` helper so database
values match partial indexes and constraints. Migrations must be backward
compatible for one release, because rollback runs the previous application
against the newer schema; the rules are in `docs/ROLLBACK.md`.

For extension changes, keep selectors centralized, add sanitized fixtures,
version parsing changes, and preserve separate self-contained worker/content
bundles. Never assign page HTML or read page cookies/storage.

For storage changes, keep bytes out of FastAPI, pin presigned constraints, store
metadata/hashes/keys in PostgreSQL, enforce encryption/checksums, and keep all
tests network-free.

For deployment changes, preserve root-owned secret files, Cloudflare Tunnel as
the only ingress, no published application port, internal database networking,
SHA-tagged immutable images, the deployment lock, pre-migration backup, and
documented rollback. Anything that publishes a host port must sit on a
non-internal Docker network, or Docker accepts the binding and silently never
creates it.

For connection-pool changes, keep
`(DATABASE_POOL_SIZE + DATABASE_MAX_OVERFLOW) * (API_WORKERS + worker + poller)`
below `POSTGRES_MAX_CONNECTIONS` minus the 15-connection reserve. The
production guardrail in `app/core/config.py` refuses to start otherwise.

## Required gate

Run `make verify` before merge. Do not disable or weaken a check to make it pass.
Python uses Ruff and typed definitions; TypeScript is strict; shell scripts must
pass `bash -n` and shellcheck; workflows must pass actionlint.
Error messages should state the failure and safe next action.

When capture behavior changes, update `docs/CAPTURE_LIMITATIONS.md` in the same
change. That document is a promise to employees and auditors.

When a script or document is renamed or removed, update every reference in the
same change; `scripts/docs_check.sh` fails on a stale reference so nobody can
follow an old runbook into the previous architecture.
