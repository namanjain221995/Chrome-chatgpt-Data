# TechSara Managed ChatGPT Session Archive

A company-managed archive for approved ChatGPT workspace conversations: a
Manifest V3 Chrome extension, FastAPI, PostgreSQL 16, a PostgreSQL-backed worker,
and the existing private AWS S3 bucket `techsara-chatgpt` in `us-east-1`.

The initial production deployment is one Ubuntu EC2 instance. Cloudflare
proxies HTTPS directly to the TLS-enabled FastAPI container on port 443. Docker
Compose runs PostgreSQL, database migrations, FastAPI, the worker, nightly
backup, an optional authorized compliance poller, and private pgAdmin.

## Honest capture guarantees

The extension can archive the managed-workspace conversation currently open
after older messages load, then archive newly committed user messages and
completed assistant messages. It stores detectable edits and regenerations as
versions and queues retries offline. It does not persist every streaming token.

It never captures unsent drafts, keystrokes, passwords, cookies, ChatGPT session
tokens, hidden model reasoning, personal-workspace conversations, or other
websites. Installing it does not retroactively archive conversations that are
never opened. Broader coverage requires the separately authorized Enterprise
Compliance feed. See [CAPTURE_LIMITATIONS.md](docs/CAPTURE_LIMITATIONS.md).

These server gates remain false by default:

```env
BROWSER_CONTENT_CAPTURE_ENABLED=false
OPENAI_WRITTEN_AUTHORIZATION_CONFIRMED=false
TRAINING_EXPORT_ENABLED=false
```

## Production request path

```text
Managed Chrome extension
  -> HTTPS Cloudflare
  -> HTTPS Full (strict)
  -> FastAPI container :443 (Origin CA TLS)
       -> PostgreSQL 16 container
       -> PostgreSQL-backed worker
       -> AWS S3 techsara-chatgpt (EC2 role credentials)
```

PostgreSQL has no host port. pgAdmin is disabled by default and, under the
`admin` profile, binds only `127.0.0.1:5050` for an SSM port-forwarding session.
The extension never receives AWS credentials; it receives short-lived,
constraint-pinned upload URLs from FastAPI.

## Local development

Requirements: Python 3.12, Node 20, Docker Engine, Compose v2, and OpenSSL.

```bash
make setup
cp .env.example .env
make test
make test-integration
make compose-up
curl -fsS http://127.0.0.1:8000/health/ready | jq .
```

Local and CI tests use an in-memory storage double and botocore Stubber; no
object-storage server is started. A real-bucket round trip is an explicit manual
workflow using temporary AWS credentials and a dedicated
`integration-tests/...` prefix.

## Verification

`make verify` runs backend/extension lint and types, unit and PostgreSQL
integration tests, migration round trips, schema drift, deterministic extension
packaging, Compose topology checks, production S3 and direct-TLS guards,
secret/dependency/prohibited-service scans, docs checks, and deterministic
deployment bundle generation. It also builds the image and exercises local and
production-shaped Compose stacks, backup restore, and a short four-scenario k6
load smoke.

Useful focused commands:

```bash
make lint
make typecheck
make test
make test-integration
make migration-check
make extension-zip
make compose-config
make security-check
make test-compose
make test-production-compose
make restore-test
make load-test
make bundle
```

## Repository layout

```text
apps/chrome-extension/   MV3 TypeScript extension and sanitized fixture tests
services/backend/        FastAPI, SQLAlchemy, Alembic, worker and tests
packages/schemas/        generated shared JSON Schemas
deploy/systemd/          host service/timer units
scripts/                 bootstrap, deploy, backup, restore and verification
tests/                   Compose smoke and k6 capacity scenarios
docs/                    architecture, security and operations runbooks
```

## Manual production deployment

Start with:

1. [AWS_MANUAL_SETUP.md](docs/AWS_MANUAL_SETUP.md) — exact Console order, CLI
   verification, expected output, and rollback.
2. [AWS_CONSOLE_CHECKLIST.md](docs/AWS_CONSOLE_CHECKLIST.md) — review checklist.
3. [CLOUDFLARE_DNS_AND_TLS.md](docs/CLOUDFLARE_DNS_AND_TLS.md) — proxied DNS,
   Origin CA, Full (strict), direct API TLS, validation, and rotation.
4. [CHROME_ENTERPRISE_DEPLOYMENT.md](docs/CHROME_ENTERPRISE_DEPLOYMENT.md) —
   private extension publishing and managed policy.

Production uses the EC2 IAM role and the AWS credential provider chain. No AWS
access key belongs in source, `.env`, Compose, the extension, or normal CI.

## Operations and capacity

The design target is about 250 registered employees, 100 concurrently online
clients, and 50 actively syncing clients. Ingestion is batched and idempotent;
the worker claims jobs with `FOR UPDATE SKIP LOCKED`; direct uploads bypass the
API; database pool and resource limits are bounded for an initial 8 GiB host.
Production capacity must still be confirmed through the 5 → 25 → 75 → 150 →
250 staged rollout in [SCALING_250_USERS.md](docs/SCALING_250_USERS.md).

Nightly `pg_dump` archives and checksum manifests are uploaded to S3. A backup
is not accepted until a disposable restore succeeds. See
[BACKUP_AND_RESTORE.md](docs/BACKUP_AND_RESTORE.md).

## Legal prerequisite

Before either capture gate changes to true, record lawful basis, complete the
required privacy impact review, consult employee representatives where required,
publish the employee notice, and record written authorization. The technical
deployment does not grant that authorization.
