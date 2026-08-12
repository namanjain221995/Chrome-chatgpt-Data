# MASTER PROMPT FOR CLAUDE CODE

## TechSara Managed ChatGPT Session Archive

You are the principal software architect, senior security engineer, senior Chrome-extension engineer, senior Python/FastAPI engineer, senior PostgreSQL engineer, senior AWS/DevOps engineer, and QA lead for this repository.

Your assignment is to build a complete, production-oriented system that archives approved company ChatGPT workspace conversations for approximately 250 employees, with a design that can be vertically scaled beyond that. The solution consists of a managed Chrome Manifest V3 extension, a FastAPI backend, PostgreSQL running in Docker on a single Amazon EC2 instance, pgAdmin as an optional private administration tool, and Amazon S3 for files, images, immutable raw JSON, exports, and backups.

Read this entire prompt before changing any file.

---

# 1. Autonomous operating rules

1. Do not ask the user any architecture, naming, framework, or implementation questions.
2. Inspect the current repository first. Reuse good existing work and replace incomplete or unsafe work.
3. When information is missing, make the safest reasonable assumption, record it in `docs/ASSUMPTIONS.md`, and continue.
4. Maintain:
   - `CLAUDE.md` with permanent project instructions;
   - `claude-progress.md` with completed work, pending work, commands run, failures, and fixes;
   - `docs/DECISIONS.md` with architecture decision records.
5. Work in this order: inspect, plan, implement, test, security review, load test, document, package, and verify.
6. Do not stop after scaffolding. Implement working code.
7. Do not claim success until all required builds, tests, migrations, Docker health checks, and packaging steps pass.
8. Never commit secrets, private keys, OAuth client secrets, AWS keys, cookies, ChatGPT session tokens, or real employee content.
9. Do not automate a live ChatGPT account during tests. Use deterministic local DOM fixtures and mock APIs.
10. Never bypass access controls, anti-bot controls, rate limits, workspace boundaries, or browser security controls.
11. No prompt can guarantee defect-free software. Compensate with strict typing, tests, idempotency, failure recovery, security controls, and documentation.

---

# 2. Non-negotiable architecture

Use this architecture exactly unless a small implementation detail must change for correctness.

## Production compute

One Amazon EC2 Linux instance running Docker Compose.

Required Docker services:

1. `caddy`
   - Public reverse proxy.
   - TLS termination.
   - Only public service.
   - Expose ports 80 and 443.

2. `api`
   - Python 3.12.
   - FastAPI.
   - Pydantic v2.
   - SQLAlchemy 2 async.
   - asyncpg.
   - Alembic migrations.
   - Gunicorn with Uvicorn workers, or a similarly safe production process manager.

3. `worker`
   - Same application image as the API.
   - Uses a PostgreSQL-backed durable jobs table.
   - Claims jobs with `FOR UPDATE SKIP LOCKED`.
   - Handles archive writing, JSON export, retries, attachment finalization, cleanup, and compliance synchronization jobs.

4. `compliance-poller`
   - Same application image.
   - Disabled unless configured.
   - Polls the current authorized OpenAI Enterprise Compliance Logs interface on a schedule from EC2.
   - Never invent undocumented endpoint paths.
   - All endpoint-specific behavior must be isolated behind a documented adapter and environment configuration.

5. `postgres`
   - PostgreSQL 16.
   - Persistent encrypted EBS-backed host volume.
   - Not publicly exposed.
   - Accessible only on the private Docker network.

6. `pgadmin`
   - dpage/pgadmin4 or a maintained equivalent.
   - Optional Docker Compose profile named `admin`.
   - Never publicly exposed.
   - Bind only to `127.0.0.1` on the EC2 host, or keep it entirely on the private Docker network.
   - Document access through AWS Systems Manager Session Manager port forwarding.

7. `backup`
   - Scheduled PostgreSQL logical backups using `pg_dump` in custom or compressed format.
   - Uploads encrypted backup artifacts and checksums to S3.
   - Implements retention and a tested restore procedure.

Optional development-only service:

8. `minio`
   - Development profile only.
   - Provides local S3-compatible testing.
   - Must never be required in AWS production.

## AWS services allowed in version 1

Use:

- Amazon EC2.
- Amazon EBS.
- Amazon S3.
- AWS Identity and Access Management.
- AWS Systems Manager Session Manager.
- AWS Systems Manager Parameter Store standard parameters.
- Amazon CloudWatch logs and basic alarms where practical.
- Route 53 only when an existing hosted zone is supplied.

Do not use:

- Amazon DynamoDB.
- Amazon RDS.
- Amazon RDS Proxy.
- AWS Lambda.
- Amazon SQS.
- Amazon ECS.
- AWS Fargate.
- Amazon ElastiCache.
- Amazon API Gateway.
- Amazon Cognito unless an existing repository already depends on it and removing it would break an approved integration. The default authentication design must use company Google/OIDC directly.

Run a final repository-wide check proving that none of the prohibited services appears as an active dependency, infrastructure resource, IAM permission, environment variable, or production code path.

## Why pgAdmin is present

pgAdmin is only a database administration interface. It is not the database. PostgreSQL itself must run in the `postgres` Docker container on EC2. pgAdmin must connect to that PostgreSQL service through the private Docker network.

---

# 3. Product goal and capture boundaries

Build a company-managed system that can archive approved conversations from the company-managed ChatGPT workspace.

The system must capture, when available and authorized:

- Conversation ID.
- Conversation URL.
- Conversation title.
- Workspace identity or verified company-workspace marker.
- Employee identity using the company login.
- User messages.
- Assistant messages.
- Visible tool messages and citations.
- Message order.
- Message timestamps when exposed.
- Code blocks, headings, lists, tables, links, and sanitized rich text.
- Edited prompts as new message versions.
- Regenerated assistant answers as new message versions.
- Selected branch and branch relationships when detectable.
- Partial answer status if a tab closes before generation finishes.
- Images and files uploaded by the user when the browser supplies a `File` or `Blob` object.
- Attachment metadata for files already displayed in a historical chat.
- Generated-image metadata and original files only through an authorized source that permits export.
- User feedback such as useful, incorrect, approved, rejected, and notes.

The system must never capture:

- Unsent drafts.
- Raw keystrokes.
- Password fields.
- Browser cookies.
- ChatGPT authentication/session tokens.
- Hidden chain-of-thought or private model reasoning.
- Personal-workspace conversations.
- Content from a workspace that cannot be verified as the configured company workspace.
- Data from unrelated websites.

---

# 4. Honest historical-capture behavior

Implement the following behavior and document it clearly in the extension UI and administrator guide.

## Current open conversation

On first authenticated run, if the employee is on a verified company ChatGPT conversation page, the extension may archive the complete currently open conversation.

To do this safely:

1. Detect the current conversation route.
2. Save the employee's current scroll position.
3. Incrementally scroll upward to cause older messages in that conversation to load.
4. Stop when the beginning is reached or a configurable safety limit is reached.
5. Parse all visible messages in order.
6. Restore the original scroll position.
7. Show a non-blocking status such as `Archived 84 messages from this conversation`.
8. Never click Send, regenerate, delete, edit, share, or any other action on behalf of the employee.

## Other historical conversations

A browser extension cannot truthfully guarantee access to every other historical ChatGPT conversation merely because it was installed. Therefore:

- Capture an old conversation when the employee opens that conversation.
- Track which conversation IDs have already been archived.
- Provide a `Historical Archive Progress` page showing captured conversation count and last capture time.
- Provide a safe guided mode telling the employee to open older company conversations, after which the extension archives each opened conversation.
- Do not automatically crawl the sidebar, click every conversation, bypass product controls, or scrape inaccessible account history.
- Do not claim that the extension alone has archived all workspace history.

## Enterprise-wide historical and cross-device coverage

Implement an optional EC2-based `compliance-poller` adapter for the authorized OpenAI Enterprise Compliance Platform. It is the preferred source for company-wide records, cross-device activity, and conversations not opened in this Chrome browser.

Requirements:

- Disabled by default until valid credentials and documented endpoint configuration are supplied.
- Store a durable cursor/checkpoint in PostgreSQL.
- Write every raw source event to S3 before advancing the checkpoint.
- Use overlapping time windows and event IDs for safe deduplication.
- Retry with exponential backoff and jitter.
- Never log credentials or raw sensitive payloads to ordinary application logs.
- Preserve tombstones/deletion events when the source emits them.
- Expose last-success, last-event-time, lag, error count, and cursor health in the admin status endpoint.

---

# 5. Live capture behavior

The extension observes the page continuously, but it must not permanently upload every second or every streaming token.

Implement this sequence:

1. Detect a committed user message only after it appears in the conversation transcript.
2. Detect an assistant message while it streams.
3. Use a debounced `MutationObserver`.
4. Keep changing partial text in memory only.
5. Treat a response as stable after generation visibly ends or no relevant content changes for a configurable quiet period, default 2 seconds.
6. Upload one complete message version.
7. If the page closes, route changes, or browser suspends before completion, persist one record with `completion_status=partial`.
8. Reconcile that partial version when the same conversation is opened again or when compliance data arrives.

Do not capture the text typed into the composer before it is sent.

---

# 6. Chrome extension requirements

Build a Manifest V3 extension in TypeScript.

Recommended stack:

- TypeScript with strict mode.
- Vite or another deterministic build tool.
- React only for popup/options/admin pages; content extraction code must remain framework-independent.
- Zod or JSON Schema for shared payload validation.
- IndexedDB for a bounded encrypted-at-rest-by-browser offline queue. Do not use `chrome.storage.sync` for message bodies.
- Manifest V3 service worker.
- Content script limited to approved ChatGPT URL patterns.

Required extension modules:

1. `workspace-verifier`
   - Receives company workspace rules from the backend.
   - Verifies managed workspace markers conservatively.
   - Fails closed when uncertain.

2. `route-observer`
   - Handles ChatGPT single-page navigation.
   - Detects conversation ID changes without requiring a full reload.

3. `dom-adapter`
   - All ChatGPT DOM selectors and parsing heuristics live in one adapter boundary.
   - Supports adapter versions.
   - Includes diagnostics without collecting excess content.
   - Is tested against saved sanitized fixtures.

4. `conversation-backfill`
   - Captures the complete currently opened conversation through controlled scrolling.
   - Restores scroll position.
   - Has time, message-count, and retry limits.

5. `live-observer`
   - MutationObserver with debouncing and stable-response detection.
   - Detects user, assistant, and visible tool message roles.

6. `message-normalizer`
   - Produces plain text, sanitized HTML, and structured parts.
   - Preserves code language, table cells, list hierarchy, links, citations, and attachment references.
   - Never executes page HTML.

7. `attachment-observer`
   - Listens for `change`, `paste`, and `drop` events.
   - Captures only File/Blob objects explicitly supplied to ChatGPT by the employee.
   - Computes SHA-256.
   - Requests a short-lived S3 presigned URL from the backend.
   - Uploads directly to S3.
   - Links the attachment to the correct committed message.
   - Supports cancellation and retry.
   - Does not store AWS credentials.

8. `offline-queue`
   - IndexedDB.
   - Bounded by item count, byte count, and age.
   - Exponential backoff with jitter.
   - Idempotency key on every event.
   - Flushes using service-worker alarms and online events.

9. `auth-client`
   - Company Google Workspace/OIDC login using Authorization Code with PKCE through `chrome.identity.launchWebAuthFlow` or another browser-appropriate secure OIDC flow.
   - Backend validates issuer, audience, signature, expiry, nonce, and allowed hosted domain.
   - Never trust an email address sent by the extension without token validation.

10. `managed-config`
    - Supports `chrome.storage.managed` for organization policy.
    - Backend configuration has a signed/versioned representation.
    - Includes remote kill switch.

11. `popup`
    - Login status.
    - Verified workspace.
    - Current conversation ID.
    - Current archive status.
    - Last successful sync.
    - Offline queue size.
    - Backend health.
    - Privacy notice link.
    - `Archive current conversation now` action.

12. `options/status`
    - Diagnostic information safe for support.
    - No raw message content in ordinary diagnostics.
    - Historical archive progress.

Required production flags:

```env
BROWSER_CONTENT_CAPTURE_ENABLED=false
OPENAI_WRITTEN_AUTHORIZATION_CONFIRMED=false
AUTO_ARCHIVE_CURRENT_OPEN_CHAT=true
CAPTURE_PERSONAL_WORKSPACES=false
CAPTURE_UNSENT_DRAFTS=false
```

Browser content extraction must activate only when both of the first two flags are true on the server-side signed configuration. Implement the feature fully, but fail closed until the company confirms its authorization and policy requirements. Never allow a local extension toggle to override the server-side gates.

---

# 7. Backend requirements

Build an async FastAPI application.

Required endpoints, with versioned paths:

- `GET /health/live`
- `GET /health/ready`
- `GET /api/v1/config`
- `POST /api/v1/auth/exchange`
- `POST /api/v1/devices/register`
- `POST /api/v1/conversations/upsert`
- `POST /api/v1/messages/batch`
- `POST /api/v1/capture-events/batch`
- `POST /api/v1/attachments/init`
- `POST /api/v1/attachments/complete`
- `POST /api/v1/feedback`
- `GET /api/v1/sync/status`
- `GET /api/v1/admin/health-summary`
- `POST /api/v1/admin/exports`
- `GET /api/v1/admin/exports/{export_id}`

Behavior:

1. Validate every request against strict schemas.
2. Enforce JWT authentication and company domain.
3. Enforce role-based authorization for employee, support, compliance-admin, security-reviewer, and data-curator.
4. Enforce CORS/origin allowlists for the known extension ID and approved admin origin.
5. Rate-limit per employee, device, and IP.
6. Accept message batches to reduce request count.
7. Use idempotency keys and unique database constraints.
8. Return per-item accepted, duplicate, rejected, and retryable statuses.
9. Store raw accepted capture events in PostgreSQL JSONB and enqueue durable archive jobs in the same transaction.
10. Worker writes immutable raw JSON to S3, records checksum and object version, then marks the job complete.
11. Acknowledge uploaded attachments only after checksum, size, object metadata, employee, conversation, and message linkage are verified.
12. Never proxy large file bodies through FastAPI. Use S3 presigned PUT URLs.
13. Limit presigned URL lifetime to 5 minutes by default.
14. Enforce allowed MIME types, file extensions, and maximum sizes server-side.
15. Use content-disposition and metadata safely; sanitize filenames.
16. Do not expose S3 buckets publicly.
17. Generate short-lived presigned GET URLs only for authorized administrative use.
18. Add structured logs with correlation IDs, but redact credentials, authorization headers, message text, and file contents from normal logs.

---

# 8. PostgreSQL data model

Use UUID primary keys generated safely. Use UTC `TIMESTAMPTZ`. Add all foreign keys, checks, indexes, and uniqueness constraints.

Create at minimum:

- `organizations`
- `workspaces`
- `users`
- `user_identities`
- `devices`
- `conversations`
- `conversation_branches`
- `messages`
- `message_versions`
- `message_parts`
- `attachments`
- `message_attachments`
- `capture_events`
- `source_events`
- `sync_checkpoints`
- `jobs`
- `job_attempts`
- `feedback`
- `training_approvals`
- `exports`
- `audit_events`
- `retention_policies`
- `schema_migrations` through Alembic

Important requirements:

1. `conversations` unique on organization/workspace/source conversation ID.
2. `messages` unique on conversation/source message ID when available.
3. If no reliable source message ID exists, use a stable fingerprint based on conversation, role, normalized content hash, sequence neighborhood, and timestamp bucket.
4. `message_versions` must preserve edits and regenerated outputs instead of overwriting.
5. Store complete normalized text in PostgreSQL `TEXT` for searchable operational use.
6. Store original raw event payload in JSONB and immutable S3 JSON.
7. Store content SHA-256 for integrity.
8. Use PostgreSQL full-text search only for authorized admin/search use; do not create a public search interface.
9. Store large binary data only in S3, never as PostgreSQL bytea or base64 JSON.
10. Use a PostgreSQL durable job queue with status, priority, attempts, run-after, locked-at, locked-by, error summary, and dedupe key.
11. Claim jobs atomically using `FOR UPDATE SKIP LOCKED`.
12. Implement stale-lock recovery.
13. Partition high-volume `capture_events`, `source_events`, and `audit_events` monthly if implementation complexity remains reasonable. Otherwise provide a documented migration path and indexes appropriate for at least 100,000 messages per day.
14. Implement retention as policy-driven soft deletion followed by auditable physical deletion jobs where legally permitted.
15. Protect legal-hold records from deletion.

---

# 9. JSON and S3 design

Use one private production S3 bucket by default to reduce complexity, with clear prefixes. Support separate buckets through configuration.

Recommended keys:

```text
raw/events/year=YYYY/month=MM/day=DD/workspace=<hash>/conversation=<id>/<event-id>.json
normalized/conversations/year=YYYY/month=MM/workspace=<hash>/conversation=<id>/snapshot-<version>.json
attachments/quarantine/workspace=<hash>/conversation=<id>/<attachment-id>/<safe-name>
attachments/clean/workspace=<hash>/conversation=<id>/<attachment-id>/<safe-name>
exports/jsonl/<export-id>/part-00001.jsonl.gz
backups/postgres/YYYY/MM/DD/techsara-<timestamp>.dump.gz
backups/manifests/YYYY/MM/DD/techsara-<timestamp>.sha256
```

Required S3 controls:

- Block all public access.
- Bucket owner enforced.
- Versioning enabled.
- Default SSE-S3 encryption for low-cost deployment; support optional SSE-KMS through configuration.
- Deny non-TLS requests.
- CORS limited to exact extension origins and required PUT methods.
- Presigned uploads restricted by content length, key prefix, checksum where supported, and short expiry.
- Lifecycle policies for quarantine, raw archive, backups, and old exports.
- No public website hosting.

Normalized conversation JSON must include:

```json
{
  "schema_version": "1.0",
  "organization_id": "uuid",
  "workspace_id": "uuid",
  "conversation_id": "uuid",
  "source_conversation_id": "source-id",
  "title": "Conversation title",
  "capture_sources": ["chrome_extension"],
  "capture_completeness": "complete_current_page",
  "employee_id_hash": "sha256-value",
  "created_at": "UTC timestamp",
  "updated_at": "UTC timestamp",
  "messages": [],
  "attachments": [],
  "integrity": {
    "sha256": "hash"
  }
}
```

Use explicit completeness values such as:

- `complete_current_page`
- `partial_scroll_limit`
- `live_only`
- `compliance_verified`
- `reconciled`
- `unknown`

Never label a conversation as globally complete unless the authorized source supports that conclusion.

---

# 10. Images and files

Implement robust attachment handling.

Supported initial input types:

- PNG.
- JPEG.
- WebP.
- GIF, with active content handled safely.
- PDF.
- Plain text.
- CSV.
- JSON.
- Common Office formats if policy permits.

Behavior:

1. Capture original user-selected/pasted/dropped file bytes only when the browser provides them directly.
2. Compute SHA-256 in the extension using Web Crypto.
3. Send only metadata to `/attachments/init`.
4. Upload directly to S3 with a presigned URL.
5. Call `/attachments/complete` with checksum and linkage.
6. Backend verifies S3 metadata and size.
7. Store attachment metadata in PostgreSQL.
8. Keep `quarantine`, `clean`, and `rejected` states.
9. Include an optional ClamAV Docker Compose profile for malware scanning, but do not make the high-memory scanner mandatory on the smallest deployment.
10. Strip dangerous filename characters.
11. Do not trust MIME type supplied by the browser alone.
12. Implement magic-byte validation in the worker.
13. Remove EXIF metadata only in a separate curated copy; keep the exact raw original for authorized audit retention.
14. Do not place file bytes in conversation JSON.
15. Generated images or old historical attachments are not guaranteed to expose original bytes through the page. Store visible metadata and obtain originals only from an authorized export source.

---

# 11. Authentication and authorization

Use company Google Workspace OIDC directly by default.

Requirements:

- Authorization Code with PKCE.
- State and nonce validation.
- Backend verifies token signature using provider JWKS.
- Validate issuer, audience, expiry, issued-at, nonce, and authorized hosted domain.
- Allowlist company domains via environment configuration.
- Map external subject to internal user identity.
- Short-lived backend access token and rotating refresh/session mechanism appropriate for an extension.
- Store extension tokens in the safest available extension storage and never in page localStorage.
- Revoke device sessions from the admin backend.
- Record device registration and last-seen.
- Never use an employee email as proof of identity without cryptographic validation.

Provide a development-only local identity provider or signed test-token utility that cannot be enabled in production.

---

# 12. Docker Compose requirements

Create:

- `compose.yaml` for development.
- `compose.prod.yaml` for production overrides.
- `.env.example` without secrets.
- `deploy/Caddyfile`.
- `deploy/postgres/` initialization only when needed; use Alembic for schema.
- `deploy/systemd/techsara-chat-archive.service` to run Docker Compose after reboot.

Compose requirements:

- Named/private networks.
- Persistent host mounts under `/srv/techsara-chat-archive/` in production.
- Health checks for PostgreSQL, API, and Caddy.
- `restart: unless-stopped` or an equivalent reliable policy.
- Log rotation.
- Read-only root filesystems where feasible.
- Drop Linux capabilities where feasible.
- `no-new-privileges` where feasible.
- Non-root application containers.
- Resource limits/reservations documented.
- PostgreSQL not bound to a public host port.
- pgAdmin only under the `admin` profile and never bound to `0.0.0.0`.
- Production secrets loaded from SSM Parameter Store or root-owned files generated at deployment, never embedded in images.
- Graceful shutdown and database connection draining.

Suggested production sizing for the compose services on an 8 GiB EC2 instance:

- PostgreSQL shared buffers and work memory tuned conservatively.
- API 2-4 workers based on CPU and measurements.
- Worker concurrency 2 by default.
- pgAdmin stopped except during administration.
- Optional ClamAV stopped unless needed or the instance is upsized.

---

# 13. EC2 and infrastructure automation

Create both:

1. `infra/terraform/` for repeatable AWS provisioning.
2. `scripts/aws-console-checklist.md` for an administrator who prefers the AWS console.

Terraform must create or support:

- One S3 bucket with secure configuration.
- IAM EC2 role and instance profile.
- Least-privilege S3 prefix access.
- SSM managed-instance permissions.
- CloudWatch log permissions if enabled.
- Security group allowing 443 from the internet, 80 only for redirect/certificate issuance, and no public PostgreSQL/pgAdmin/SSH.
- EC2 instance.
- Encrypted gp3 EBS storage.
- Elastic IP.
- Basic CloudWatch alarms for instance status check, CPU, disk-space agent metric when configured, and API health when practical.
- Optional Route 53 record only when a hosted-zone ID and domain are provided.
- Standard SSM Parameter Store parameter names without committing secret values.

Do not place secret values in Terraform state. Provide a post-apply script using `aws ssm put-parameter --type SecureString` for secret entry.

Recommended default instance variables:

```text
architecture: x86_64 for broad container compatibility
instance_type: t3a.large or a configurable equivalent
vCPU: 2
memory: 8 GiB
root volume: 30 GiB encrypted gp3
data volume: 100 GiB encrypted gp3
```

Allow an ARM64 `t4g.large` profile when all container images are verified multi-architecture.

Generate cloud-init/user-data that:

- Installs Docker Engine and Compose plugin from official sources.
- Installs and enables SSM Agent when not already present.
- Mounts the encrypted data volume by filesystem UUID.
- Creates `/srv/techsara-chat-archive` with restrictive permissions.
- Installs the systemd unit.
- Does not embed secrets.

---

# 14. Backup and disaster recovery

Implement two layers.

## PostgreSQL logical backup

- Nightly `pg_dump`.
- Gzip or custom format.
- SHA-256 manifest.
- Upload to S3.
- Retention tiers configurable.
- Alert on missed or failed backup.
- Weekly automated restore test into a disposable local database in CI or a documented scheduled operational job.

## EBS snapshots

- Document daily EBS snapshots through Amazon Data Lifecycle Manager or AWS Backup.
- Explain that EBS snapshots are not a substitute for tested PostgreSQL logical backups.
- Document restore to a replacement EC2 instance.

Create:

- `scripts/backup_postgres.sh`
- `scripts/restore_postgres.sh`
- `scripts/verify_backup.sh`
- `docs/BACKUP_AND_RESTORE.md`
- `docs/DISASTER_RECOVERY.md`

Define recovery targets as configurable goals, not guarantees. Suggested initial goals:

- RPO: 24 hours for logical backup, lower if WAL archiving is later enabled.
- RTO: 4 hours for a documented single-instance recovery.

---

# 15. Scale target for 250 employees

Engineer and test for this initial envelope:

- 250 registered employees.
- 100 simultaneously online extension clients.
- 50 concurrent active sync clients.
- 100,000 message records per business day as a stress target.
- Message batches up to 100 items or 2 MiB, whichever comes first.
- Direct-to-S3 attachment uploads.
- Short bursts of 25 ingestion requests per second.
- Sustained 10 ingestion requests per second during load testing.
- At least 1 million message rows in integration/performance test fixtures.

Performance controls:

- PostgreSQL connection pooling in the API process.
- Efficient batch inserts/upserts.
- Correct composite indexes.
- Prepared statements where beneficial.
- Durable job queue rather than synchronous S3 archive work.
- Backpressure when the job queue exceeds thresholds.
- Extension batching, compression when safe, and jittered retries.
- Attachment upload directly to S3.
- Caddy request limits and backend rate limits.
- Bounded worker concurrency.

Create k6 or Locust load tests and a documented test report template. Do not promise that one instance will handle every possible employee behavior. Provide vertical-scaling guidance to `t3a.xlarge` or a split application/database topology if measured CPU, memory, disk latency, connection saturation, or queue lag crosses defined thresholds.

---

# 16. Security and privacy requirements

1. Company workspace only; fail closed.
2. Clear employee notice before capture.
3. No stealth collection.
4. No personal-workspace capture.
5. No drafts or keystrokes.
6. No cookie or ChatGPT token access.
7. TLS everywhere outside the private Docker network.
8. PostgreSQL and pgAdmin never publicly exposed.
9. EC2 administration through SSM Session Manager; do not require public SSH.
10. S3 block public access.
11. Default encryption at rest.
12. Least-privilege IAM.
13. Short-lived presigned URLs.
14. Input validation and output encoding.
15. HTML sanitization.
16. Protection from SQL injection through parameterized ORM/SQL.
17. Rate limiting and body-size limits.
18. Audit every admin data access, export, approval, and deletion.
19. Redact sensitive payloads from logs.
20. Secret scanning in CI.
21. Dependency and container vulnerability checks where tools are available.
22. Content retention and legal-hold controls.
23. Curated/training export disabled by default.
24. Never send raw company conversations to a third-party model for classification unless an explicit approved integration is configured.
25. Do not automatically treat assistant output as correct training truth.

Include server-side gates:

```env
BROWSER_CONTENT_CAPTURE_ENABLED=false
OPENAI_WRITTEN_AUTHORIZATION_CONFIRMED=false
COMPLIANCE_POLL_ENABLED=false
TRAINING_EXPORT_ENABLED=false
ATTACHMENT_CAPTURE_ENABLED=true
PERSONAL_WORKSPACE_CAPTURE_ENABLED=false
```

---

# 17. Curated export

Implement an admin-only exporter that writes approved JSONL to S3.

Rules:

- Only records with explicit `training_approvals.status=approved` are eligible.
- Exclude records under legal hold unless policy specifically permits export.
- Exclude rejected, personal, secret-containing, unsupported, or incomplete records.
- Split by whole conversation, never individual messages, when creating train/validation/test sets.
- Include source IDs and checksums for traceability.
- Support prompt/answer pairs and multimodal attachment references.
- Never start model training automatically.

---

# 18. Repository structure

Create or adapt to this structure:

```text
.
├── CLAUDE.md
├── claude-progress.md
├── Makefile
├── compose.yaml
├── compose.prod.yaml
├── .env.example
├── apps/
│   └── chrome-extension/
├── services/
│   └── backend/
├── packages/
│   └── schemas/
├── deploy/
│   ├── Caddyfile
│   ├── systemd/
│   └── postgres/
├── infra/
│   └── terraform/
├── scripts/
├── tests/
│   ├── fixtures/
│   ├── integration/
│   ├── e2e/
│   └── load/
└── docs/
```

Use shared generated JSON Schemas between TypeScript and Python where practical. Avoid duplicated, drifting request/response definitions.

---

# 19. Testing requirements

## Extension tests

- DOM adapter unit tests for user, assistant, tool, code, tables, citations, images, files, edits, regenerated answers, branches, and streaming states.
- Route-change tests.
- Workspace fail-closed tests.
- Current-conversation scroll/backfill tests.
- Duplicate-event tests.
- Offline queue and retry tests.
- Attachment paste/drop/input tests.
- Personal workspace rejection tests.
- Server-side kill-switch tests.
- Manifest validation.
- Package build and deterministic ZIP.

## Backend tests

- Schema validation.
- OIDC token verification with local test keys.
- Domain enforcement.
- RBAC.
- Idempotent upsert.
- Duplicate and out-of-order events.
- Edited and regenerated versions.
- Partial-to-complete reconciliation.
- PostgreSQL job claiming and stale-lock recovery.
- S3 presign and completion validation using MinIO/local mocks.
- Checksum mismatch.
- Unauthorized attachment linkage.
- Rate limiting.
- Retention/legal hold.
- Export approval gates.
- Audit log creation.
- Migration from an empty database.
- Migration upgrade test from the previous schema revision.

## Integration tests

- Clean `docker compose up`.
- PostgreSQL health.
- Alembic migration.
- API readiness.
- Batch ingest.
- Worker writes raw JSON to local S3-compatible storage.
- Restart services and prove no data loss.
- Backup and restore into a clean PostgreSQL volume.

## Load tests

- 50 concurrent sync clients.
- Sustained 10 requests/second.
- Burst 25 requests/second.
- Mixed batches and attachment-init calls.
- Record p50, p95, p99, error rate, database connections, CPU, memory, queue depth, and disk latency.

Do not run load tests against a live production ChatGPT site.

---

# 20. CI/CD requirements

Create GitHub Actions workflows for:

- TypeScript lint, typecheck, test, and extension build.
- Python lint, typecheck, test, and package build.
- Docker image build.
- Docker Compose configuration validation.
- Alembic migration validation.
- Terraform fmt, validate, and plan without secret values.
- Secret scanning.
- Dependency audit.
- Integration test with PostgreSQL and MinIO service containers.
- Release artifact containing the extension ZIP and deployment bundle.

Production deployment workflow:

1. Build immutable images tagged with Git SHA.
2. Push to a configured registry, preferably Amazon ECR if allowed later, or GitHub Container Registry for the initial design.
3. Use SSM Run Command or Session Manager-based deployment script on EC2.
4. Pull images.
5. Run database backup.
6. Run Alembic migrations.
7. Start new services.
8. Check health.
9. Roll back application images on failure; document migration rollback constraints.

Do not expose Docker socket to application containers.

---

# 21. Documentation requirements

Create complete documentation:

- `README.md`
- `docs/ARCHITECTURE.md`
- `docs/AWS_STEP_BY_STEP.md`
- `docs/CHROME_ENTERPRISE_DEPLOYMENT.md`
- `docs/LOCAL_DEVELOPMENT.md`
- `docs/PRODUCTION_DEPLOYMENT.md`
- `docs/SECURITY.md`
- `docs/PRIVACY_AND_EMPLOYEE_NOTICE.md`
- `docs/CAPTURE_LIMITATIONS.md`
- `docs/COMPLIANCE_ADAPTER.md`
- `docs/DATABASE.md`
- `docs/PGADMIN_ACCESS.md`
- `docs/BACKUP_AND_RESTORE.md`
- `docs/DISASTER_RECOVERY.md`
- `docs/MONITORING.md`
- `docs/SCALING_250_USERS.md`
- `docs/INCIDENT_RUNBOOK.md`
- `docs/TESTING.md`
- `docs/OPERATIONS.md`

The capture-limitations document must state prominently:

- The extension can archive the complete currently opened conversation after loading its messages.
- It continuously archives new committed messages.
- It does not archive unsent text.
- It cannot guarantee all old conversations unless each is opened or an authorized enterprise compliance/export source supplies them.
- It cannot capture hidden model reasoning.
- Historical attachment originals may not be recoverable from the rendered page.

---

# 22. Required environment variables

Provide a complete `.env.example` similar to the following, with safe placeholders:

```env
ENVIRONMENT=development
APP_NAME=techsara-chat-archive
PUBLIC_BASE_URL=https://archive.example.com
API_BASE_PATH=/api/v1

POSTGRES_DB=techsara_chat_archive
POSTGRES_USER=techsara_app
POSTGRES_PASSWORD_FILE=/run/secrets/postgres_password
DATABASE_URL=postgresql+asyncpg://techsara_app:REPLACE@postgres:5432/techsara_chat_archive
DATABASE_POOL_SIZE=20
DATABASE_MAX_OVERFLOW=20

AWS_REGION=us-east-1
S3_BUCKET=replace-account-region-techsara-chat-archive
S3_ENDPOINT_URL=
S3_USE_PATH_STYLE=false
S3_ENCRYPTION_MODE=SSE-S3
S3_KMS_KEY_ID=
PRESIGNED_UPLOAD_TTL_SECONDS=300
MAX_ATTACHMENT_BYTES=20971520

OIDC_ISSUER=https://accounts.google.com
OIDC_CLIENT_ID=replace-me
OIDC_CLIENT_SECRET_FILE=/run/secrets/oidc_client_secret
ALLOWED_EMAIL_DOMAINS=example.com
EXTENSION_IDS=replace-after-build

MANAGED_WORKSPACE_LABEL=TechSara's Workspace
MANAGED_WORKSPACE_IDS=
BROWSER_CONTENT_CAPTURE_ENABLED=false
OPENAI_WRITTEN_AUTHORIZATION_CONFIRMED=false
AUTO_ARCHIVE_CURRENT_OPEN_CHAT=true
ATTACHMENT_CAPTURE_ENABLED=true
PERSONAL_WORKSPACE_CAPTURE_ENABLED=false
CAPTURE_UNSENT_DRAFTS=false

COMPLIANCE_POLL_ENABLED=false
OPENAI_COMPLIANCE_BASE_URL=
OPENAI_COMPLIANCE_LOG_PATH=
OPENAI_COMPLIANCE_FILES_PATH=
OPENAI_COMPLIANCE_API_KEY_FILE=/run/secrets/openai_compliance_api_key
COMPLIANCE_POLL_INTERVAL_SECONDS=300
COMPLIANCE_OVERLAP_SECONDS=600

TRAINING_EXPORT_ENABLED=false
RAW_RETENTION_DAYS=365
BACKUP_RETENTION_DAYS=90
OFFLINE_QUEUE_MAX_ITEMS=10000
OFFLINE_QUEUE_MAX_BYTES=52428800
OFFLINE_QUEUE_MAX_AGE_DAYS=7

LOG_LEVEL=INFO
LOG_MESSAGE_CONTENT=false
RATE_LIMIT_REQUESTS_PER_MINUTE=300
WORKER_CONCURRENCY=2
```

Do not use the example password inside production configuration.

---

# 23. Makefile or task commands

Provide consistent commands:

```text
make setup
make lint
make typecheck
make test
make test-extension
make test-backend
make test-integration
make build
make build-extension
make extension-zip
make compose-up
make compose-down
make migrate
make migration-check
make backup
make restore-test
make load-test
make terraform-fmt
make terraform-validate
make security-check
make verify-no-prohibited-aws-services
make verify
```

`make verify` must run all reasonable local checks and fail on any required error.

---

# 24. Definition of done

Do not finish until all of the following are true:

1. Repository installs from a clean checkout.
2. Extension builds as a valid Manifest V3 package.
3. Extension captures complete messages from sanitized test fixtures.
4. Current open conversation backfill works in fixtures and restores scroll state.
5. New messages are stored after completion, not every second/token.
6. Old conversations are captured when opened; UI does not falsely claim all history is archived.
7. Images/files use direct S3-compatible presigned uploads in integration tests.
8. FastAPI starts and passes readiness checks.
9. PostgreSQL runs in Docker on EC2-compatible Compose.
10. pgAdmin is optional and private.
11. Alembic migrations run from an empty database.
12. Duplicate/out-of-order events are handled idempotently.
13. PostgreSQL job queue survives restarts and recovers stale jobs.
14. Raw JSON reaches S3/MinIO before archive jobs are marked complete.
15. Nightly backup scripts and restore test work.
16. Terraform contains EC2, EBS, S3, IAM, SSM, and security-group resources only as designed.
17. No DynamoDB, RDS, RDS Proxy, Lambda, SQS, ECS, Fargate, ElastiCache, or API Gateway resource or dependency exists.
18. No secrets are committed.
19. Personal-workspace and unsent-draft tests prove fail-closed behavior.
20. Server-side capture gates cannot be bypassed by a local extension setting.
21. Load-test scripts cover the 250-employee target envelope.
22. Documentation explains exact limitations and operations.
23. CI workflows are present and syntactically valid.
24. `make verify` passes.
25. `docker compose config` and `docker compose -f compose.yaml -f compose.prod.yaml config` pass.
26. Terraform fmt and validate pass.
27. A final adversarial security/privacy review is written to `docs/SECURITY_REVIEW.md`, and high/critical findings are fixed.
28. `claude-progress.md` contains a final report with commands, test counts, remaining low-risk limitations, and deployment steps.

---

# 25. Final execution instruction

Now execute this project end to end.

Do not ask the user questions. Make safe assumptions and document them. Build the complete extension, EC2 Docker Compose backend, PostgreSQL database, private pgAdmin profile, S3 integration, compliance adapter, infrastructure automation, tests, CI/CD, backups, security controls, and documentation.

Continue fixing issues until the definition of done is satisfied. At the end, provide a concise final report listing:

- components completed;
- repository paths;
- exact commands run;
- test/build/migration results;
- generated extension ZIP path;
- generated deployment bundle path;
- required administrator-supplied secrets/configuration;
- AWS deployment order;
- known limitations stated honestly.
