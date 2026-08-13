# TechSara Managed ChatGPT Session Archive

A company-managed archive for approved ChatGPT workspace conversations: a
Manifest V3 Chrome extension, FastAPI, PostgreSQL 16, a PostgreSQL-backed
worker, and the private AWS S3 bucket `techsara-chatgpt` in `us-east-1`.

Production is one Amazon Linux 2023 EC2 instance running Docker Compose. A
named Cloudflare Tunnel is the only public ingress: `cloudflared` holds
outbound-only connections to Cloudflare and forwards to `http://api:8000` on a
private Docker network, so the instance publishes no application port and needs
no origin certificate.

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
  -> HTTPS to Cloudflare (edge TLS, WAF, proxied DNS)
  -> Cloudflare Tunnel (outbound-only from the instance)
  -> cloudflared container
  -> http://api:8000  (FastAPI, private Docker network, expose only)
       -> PostgreSQL 16 container (internal network, no host port)
       -> PostgreSQL-backed worker
       -> AWS S3 techsara-chatgpt (EC2 instance role credentials)
```

The only host bindings are two loopback management endpoints: the tunnel's
`/ready` metrics endpoint on `127.0.0.1:2000`, and pgAdmin on `127.0.0.1:5050`
when the `admin` profile is deliberately started. The extension never receives
AWS credentials; it receives short-lived, constraint-pinned upload URLs from
FastAPI.

## CI/CD

```text
git push origin main -> CI (full suite) -> SSH to EC2 -> deploy exact SHA
                                                          migrate
                                                          health check
                                                          rollback on failure
```

GitHub needs exactly four secrets — `EC2_HOST`, `EC2_USER`, `EC2_SSH_PORT`,
`EC2_SSH_PRIVATE_KEY`. No AWS credential, no registry credential, no OIDC
federation. Every production application secret lives in AWS SSM Parameter
Store and is read on the instance with the EC2 instance role.

The backend image is built on the instance from the deployed commit and tagged
`techsara-chat-archive-backend:<full-git-sha>`; `latest` is never deployed. The
reasoning is in [SIMPLE_CICD.md](docs/SIMPLE_CICD.md).

## Local development

Requirements: Python 3.12, Node 20, Docker Engine, Compose v2.24+, and OpenSSL.

```bash
make setup
cp .env.example .env
make test
make test-integration
make compose-up
curl -fsS http://127.0.0.1:8000/health/ready | jq .
```

Local and CI tests use an in-memory storage double and botocore Stubber; no
object-storage server is started anywhere.

## Verification

`make verify` runs the same gate CI runs: backend and extension lint, formatting
and types; unit and PostgreSQL integration tests; migration round trips; schema
drift; deterministic extension packaging and package inspection; Compose
topology and production exposure guards; secret, dependency and
prohibited-technology scans; documentation checks; the image build; local and
production-shaped Compose stacks; a backup restore; and a short k6 load smoke.

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
```

## Repository layout

```text
apps/chrome-extension/   MV3 TypeScript extension and sanitized fixture tests
services/backend/        FastAPI, SQLAlchemy, Alembic, worker and tests
packages/schemas/        generated shared JSON Schemas
deploy/systemd/          optional host service/timer units
deploy/current-release   deployment metadata written on the instance (untracked)
scripts/                 bootstrap, deploy, rollback, backup, restore, verification
tests/                   Compose smoke and k6 capacity scenarios
docs/                    architecture, security and operations runbooks
.github/workflows/       ci.yml, deploy.yml, release.yml, test-ec2-connection.yml
```

## Deployment documentation

Read in this order:

1. [AWS_MANUAL_SETUP.md](docs/AWS_MANUAL_SETUP.md) — bucket, IAM role, instance,
   security group, SSM parameters, with CLI verification for each step.
2. [CLOUDFLARE_TUNNEL_SETUP.md](docs/CLOUDFLARE_TUNNEL_SETUP.md) — create the
   named tunnel, store its token in SSM, route the public hostname to
   `http://api:8000`.
3. [GITHUB_SECRETS.md](docs/GITHUB_SECRETS.md) — the four GitHub secrets, the
   optional host-key variable, and every SSM parameter.
4. [EC2_DEPLOYMENT.md](docs/EC2_DEPLOYMENT.md) — the exact first-deployment
   sequence and day-to-day operations.
5. [SIMPLE_CICD.md](docs/SIMPLE_CICD.md) — what the workflows do and why there
   is no registry.
6. [ROLLBACK.md](docs/ROLLBACK.md) — automatic and manual rollback, and the
   backward-compatible migration rules that make it safe.
7. [GOOGLE_OAUTH_SETUP.md](docs/GOOGLE_OAUTH_SETUP.md) — the Google Workspace
   sign-in client, and which parts need a Workspace administrator.
8. [CHROME_ENTERPRISE_DEPLOYMENT.md](docs/CHROME_ENTERPRISE_DEPLOYMENT.md) —
   private extension publishing and managed policy.

Production uses the EC2 IAM role and the AWS credential provider chain. No AWS
access key belongs in source, `.env`, Compose, the extension, or CI.

## Operations and capacity

The design target is about 250 registered employees, 100 concurrently online
clients, and 25–50 actively syncing clients. Ingestion is batched and
idempotent; the worker claims jobs with `FOR UPDATE SKIP LOCKED`; uploads bypass
the API through presigned URLs; connection pools are bounded and the application
refuses to start if they could exceed PostgreSQL `max_connections`. Measured
numbers and scaling thresholds are in [CAPACITY.md](docs/CAPACITY.md).

Nightly `pg_dump` archives and checksum manifests are uploaded to S3. A backup
is not accepted until a disposable restore succeeds. See
[BACKUP_RESTORE.md](docs/BACKUP_RESTORE.md).

## Legal prerequisite

Before either capture gate changes to true, record lawful basis, complete the
required privacy impact review, consult employee representatives where required,
publish the employee notice, and record written authorization. The technical
deployment does not grant that authorization.
