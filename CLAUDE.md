# Permanent project instructions

## System boundary

This repository archives authorized conversations for roughly 250 employees.
Production is one Ubuntu EC2 host with Docker Compose, PostgreSQL 16, FastAPI
terminating origin TLS directly, a PostgreSQL-backed worker, private optional
pgAdmin, nightly backups, an optional authorized compliance poller, and AWS S3 bucket
`techsara-chatgpt` in `us-east-1`.

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
   credential provider chain. The extension receives no AWS credential.
8. PostgreSQL has no host port. FastAPI publishes only TLS port `443`; its EC2
   security-group rule accepts only Cloudflare source ranges. Optional pgAdmin
   binds only `127.0.0.1:5050` and is reached through SSM.
9. Cloudflare proxied DNS uses Full (strict) and the API container reads its
   root-owned Origin CA key through a read-only mount.
10. Capture gates and training export default false and stay false during
    development, deployment, migration, and rollback unless separately approved.
11. No test automates a live ChatGPT account. Use sanitized DOM fixtures only.

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

## Change rules

For endpoints, use strict Pydantic request/response models, enforce policy before
writes, audit administration, regenerate shared schemas, and add rejection tests.

For database changes, edit the model, generate/review an Alembic revision, and
run `make migration-check`. Enums use the project `_enum()` helper so database
values match partial indexes and constraints.

For extension changes, keep selectors centralized, add sanitized fixtures,
version parsing changes, and preserve separate self-contained worker/content
bundles. Never assign page HTML or read page cookies/storage.

For storage changes, keep bytes out of FastAPI, pin presigned constraints, store
metadata/hashes/keys in PostgreSQL, enforce encryption/checksums, and keep all
tests network-free except the explicitly authorized dedicated-prefix workflow.

For deployment changes, preserve root-owned secret files, direct origin TLS,
Cloudflare-only port 443 ingress, internal database networking, immutable
application tags, pre-migration backup, and documented rollback.

## Required gate

Run `make verify` before merge. Do not disable or weaken a check to make it pass.
Python uses Ruff and typed definitions; TypeScript is strict; shell scripts must
pass `bash -n`. Error messages should state the failure and safe next action.

When capture behavior changes, update `docs/CAPTURE_LIMITATIONS.md` in the same
change. That document is a promise to employees and auditors.
