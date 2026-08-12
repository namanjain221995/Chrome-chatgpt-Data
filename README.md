# TechSara Managed ChatGPT Session Archive

A company-managed archive of approved conversations from the TechSara ChatGPT
workspace: a Manifest V3 Chrome extension, a FastAPI backend, PostgreSQL 16 in
Docker on a single EC2 instance, and Amazon S3 for files, immutable raw JSON,
exports and backups.

Built for ~250 employees, engineered and load-tested to a 100,000-message/day
stress target, and designed to scale vertically from there.

---

## What it does, stated honestly

**It archives the conversation you currently have open**, including earlier
messages once they load, and **every new message** you send or receive in the
managed company workspace.

**It never captures** unsent drafts, keystrokes, passwords, cookies, ChatGPT
session tokens, hidden model reasoning, personal-workspace conversations, or
anything on another website.

**It cannot claim complete workspace history.** A browser extension sees what a
browser renders. Conversations never opened in this browser are not archived by
the extension; company-wide coverage requires the authorized OpenAI Enterprise
Compliance feed.

Read [docs/CAPTURE_LIMITATIONS.md](docs/CAPTURE_LIMITATIONS.md) before approving
this system. It is the honest-scope document, and everything in it is enforced
in code and covered by tests.

---

## Architecture at a glance

```
Managed Chrome ──HTTPS──▶ Caddy :443 ──▶ FastAPI ──▶ PostgreSQL 16
     │                    (only public   │            (private network only)
     │                     service)      ├──▶ worker ──▶ S3 (raw, snapshots,
     │                                   │                  attachments, exports)
     └──presigned PUT───────────────────────────────▶ S3
                                         ├──▶ compliance-poller (optional)
                                         ├──▶ backup (nightly pg_dump → S3)
                                         └──▶ pgadmin (profile: admin, 127.0.0.1)
```

One EC2 instance, one Docker Compose stack, one container image for the API,
worker, poller and backup. No DynamoDB, RDS, RDS Proxy, Lambda, SQS, ECS,
Fargate, ElastiCache or API Gateway — proven by
`make verify-no-prohibited-aws-services`.

Details: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

---

## Quick start

```bash
make setup          # backend virtualenv + npm install
cp .env.example .env
make compose-up     # postgres, minio, api, worker, caddy

curl -s http://localhost:8080/health/ready | jq
curl -s http://localhost:8080/api/v1/config | jq '.config.policy'
```

Capture is **off** by default, exactly as in production. To enable it locally,
set both gates in `.env` and restart:

```env
BROWSER_CONTENT_CAPTURE_ENABLED=true
OPENAI_WRITTEN_AUTHORIZATION_CONFIRMED=true
```

Full guide: [docs/LOCAL_DEVELOPMENT.md](docs/LOCAL_DEVELOPMENT.md).

---

## Repository layout

```
.
├── apps/chrome-extension/     MV3 extension (TypeScript, React for UI only)
│   ├── src/modules/           workspace-verifier, route-observer, dom-adapter,
│   │                          live-observer, conversation-backfill,
│   │                          message-normalizer, attachment-observer,
│   │                          offline-queue, auth-client, managed-config, api-client
│   ├── src/background/        MV3 service worker and sync engine
│   ├── src/content/           content script (approved ChatGPT URLs only)
│   └── tests/                 105 tests against sanitized DOM fixtures
├── services/backend/          FastAPI, SQLAlchemy 2 async, Alembic, worker, poller
│   ├── app/api/v1/            versioned endpoints
│   ├── app/models/            25 tables, 3 monthly-partitioned
│   ├── app/services/          ingest, jobs, storage, policy, exports, retention
│   └── tests/                 260 tests (unit + integration)
├── packages/schemas/          JSON Schemas generated from the Pydantic models
├── deploy/                    Caddyfile, systemd units
├── infra/terraform/           EC2, EBS, S3, IAM, SSM, security group, alarms
├── scripts/                   backup, restore, verify, deploy, secret scan
├── tests/                     compose smoke test, k6 load tests
└── docs/                      22 documents
```

---

## Commands

```bash
make help                 # every target
make verify               # everything CI runs; the gate before merging

make test                 # unit tests, no external services
make test-integration     # starts PostgreSQL, migrates, runs integration tests
make test-compose         # full docker compose smoke test

make extension-zip        # reproducible extension package
make bundle               # deployment bundle for the instance
make migration-check      # migrations match models, and round-trip
make security-check       # prohibited services, secret scan, dependency audit
```

---

## Deploying

```
terraform apply → put_secrets.sh → DNS → OIDC client → bundle → deploy
  → build the extension → register its id → redeploy
  → enable the capture gates → verify → backups and snapshots
```

Each step, with verification: [docs/AWS_STEP_BY_STEP.md](docs/AWS_STEP_BY_STEP.md).

---

## Documentation

| Document | Read it when |
| --- | --- |
| [CAPTURE_LIMITATIONS.md](docs/CAPTURE_LIMITATIONS.md) | Before approving the system |
| [PRIVACY_AND_EMPLOYEE_NOTICE.md](docs/PRIVACY_AND_EMPLOYEE_NOTICE.md) | Before telling employees |
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | Understanding the design |
| [SECURITY.md](docs/SECURITY.md) | Reviewing controls and the threat model |
| [SECURITY_REVIEW.md](docs/SECURITY_REVIEW.md) | The adversarial review and its findings |
| [AWS_STEP_BY_STEP.md](docs/AWS_STEP_BY_STEP.md) | Deploying the first time |
| [CHROME_ENTERPRISE_DEPLOYMENT.md](docs/CHROME_ENTERPRISE_DEPLOYMENT.md) | Rolling out the extension |
| [LOCAL_DEVELOPMENT.md](docs/LOCAL_DEVELOPMENT.md) | Working on the code |
| [PRODUCTION_DEPLOYMENT.md](docs/PRODUCTION_DEPLOYMENT.md) | Shipping a change |
| [DATABASE.md](docs/DATABASE.md) | Working with the schema |
| [PGADMIN_ACCESS.md](docs/PGADMIN_ACCESS.md) | Needing database access |
| [BACKUP_AND_RESTORE.md](docs/BACKUP_AND_RESTORE.md) | Setting up or testing backups |
| [DISASTER_RECOVERY.md](docs/DISASTER_RECOVERY.md) | Something is badly broken |
| [MONITORING.md](docs/MONITORING.md) | Setting up alerting |
| [SCALING_250_USERS.md](docs/SCALING_250_USERS.md) | Capacity planning |
| [INCIDENT_RUNBOOK.md](docs/INCIDENT_RUNBOOK.md) | During an incident |
| [OPERATIONS.md](docs/OPERATIONS.md) | Routine operations |
| [COMPLIANCE_ADAPTER.md](docs/COMPLIANCE_ADAPTER.md) | Enabling company-wide coverage |
| [TESTING.md](docs/TESTING.md) | Adding tests |
| [ASSUMPTIONS.md](docs/ASSUMPTIONS.md) | Wondering why something is the way it is |
| [DECISIONS.md](docs/DECISIONS.md) | Wondering what was rejected and why |

---

## Legal and ethical prerequisites

This system archives employee conversations. Before enabling capture:

1. Record the lawful basis for monitoring in your jurisdiction.
2. Complete a data protection impact assessment where required.
3. Consult works councils or employee representatives where required.
4. Give employees the privacy notice **before** capture begins.
5. Record the written authorization decision, then set
   `OPENAI_WRITTEN_AUTHORIZATION_CONFIRMED=true`.

The system is fail-closed until step 5 by design. `BROWSER_CONTENT_CAPTURE_ENABLED`
and `OPENAI_WRITTEN_AUTHORIZATION_CONFIRMED` both default to false, cannot be
overridden by any local extension setting, and are re-checked server-side on
every request.

---

## Status

| Component | State |
| --- | --- |
| Chrome extension | Complete; 105 tests; reproducible ZIP |
| Backend API | Complete; 15 endpoints; 260 tests |
| Database | 25 tables; 2 migrations; monthly partitioning |
| Worker and job queue | Complete; `FOR UPDATE SKIP LOCKED`; stale-lock recovery |
| Compliance adapter | Implemented; idle until authorized and configured |
| Infrastructure | Terraform validated; fmt clean |
| CI/CD | Three workflows: ci, release, deploy |
| Documentation | 22 documents |

Known limitations are stated in [CAPTURE_LIMITATIONS.md](docs/CAPTURE_LIMITATIONS.md)
and in the final report in [claude-progress.md](claude-progress.md). They are
limitations of what a browser extension can honestly do, not gaps in the
implementation.
