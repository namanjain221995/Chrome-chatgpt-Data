# Assumptions

Every decision made without an explicit instruction, and why it is the safest
reasonable choice. Anything here can be changed by configuration unless noted.

## Identity and tenancy

| # | Assumption | Rationale | How to change |
| --- | --- | --- | --- |
| A1 | Single tenant: one organization, slug `techsara` | The brief describes one company with ~250 employees. Multi-tenancy would add joins and risk with no stated benefit. | The schema already carries `organization_id` everywhere; provisioning a second org is additive. |
| A2 | Google Workspace is the identity provider | The brief names Google/OIDC as the default. | `OIDC_ISSUER` + `OIDC_JWKS_URL` accept any compliant provider. |
| A3 | Roles are assigned by an administrator directly in `users.roles` | No role-management UI was requested for v1. | Add an admin endpoint; RBAC is already enforced. |
| A4 | A "device" is a browser profile, identified by a random per-install id | Identifies the install, not the person or the hardware. | — |

## Capture policy

| # | Assumption | Rationale |
| --- | --- | --- |
| A5 | Both capture gates default to **false** | The brief requires fail-closed until written authorization is confirmed. |
| A6 | With no workspace label or id configured, the server refuses all capture | Guessing which workspace is "the company one" is exactly the mistake that would archive personal conversations. |
| A7 | A workspace label match must be accompanied by a strong signal | A label alone is user-visible text; requiring a signal reduces spoofing risk. |
| A8 | `PERSONAL_WORKSPACE_CAPTURE_ENABLED` is never honoured, even if set true | The brief states personal workspaces are never captured. The variable exists to document the invariant. |
| A9 | Stable-response quiet period defaults to 2000 ms | Stated in the brief. Configurable server-side. |
| A10 | Backfill limits default to 2,000 messages / 120 s / 400 scrolls | Bounded work that finishes while an employee waits. |

## Data model

| # | Assumption | Rationale |
| --- | --- | --- |
| A11 | UUIDv4 primary keys generated in Python | No `pgcrypto` extension needed; works identically in tests. |
| A12 | Enums are VARCHAR + CHECK, not native PostgreSQL enums | Adding a value is an ordinary migration rather than a type mutation. |
| A13 | `capture_events`, `source_events` and `audit_events` are monthly range-partitioned | ~100k rows/day at the stress target; retention becomes a metadata-only `DROP TABLE`. |
| A14 | Idempotency keys live in a **separate non-partitioned** table | A unique constraint on a partitioned table must include the partition key, which would make idempotency month-local — a correctness bug. |
| A15 | Idempotency keys are pruned after `max(4 × OFFLINE_QUEUE_MAX_AGE_DAYS, 30)` days | The client cannot retry older than its own queue horizon. |
| A16 | Content identity is a third fallback for message matching | A backfill renumbers every message; without this the archive would fill with duplicates. Documented in ARCHITECTURE.md. |
| A17 | Full-text search uses the `simple` configuration | Language-neutral; conversations are multilingual. |

## Storage

| # | Assumption | Rationale |
| --- | --- | --- |
| A18 | One bucket with prefixes, not one bucket per data class | The brief prefers this for cost and simplicity. Separate buckets remain configurable. |
| A19 | SSE-S3 by default, SSE-KMS optional | Lowest cost that still satisfies encryption-at-rest. |
| A20 | The instance role has no `s3:DeleteObject` | Deletion must be a deliberate, audited act. Lifecycle rules handle expiry. |
| A21 | Attachment bytes are re-downloaded by the worker for verification | Checksum verification and magic-byte validation both require the bytes; a 20 MiB ceiling bounds the cost. |
| A22 | EXIF stripping is implemented in pure Python for JPEG and PNG | Avoids a heavyweight image dependency in a security-sensitive container. Other formats are copied unchanged and no claim is made. |

## Runtime

| # | Assumption | Rationale |
| --- | --- | --- |
| A23 | Rate limiting is per-process, divided by `API_WORKERS` | Redis/ElastiCache is prohibited. The division makes the fleet-wide limit match the configured value. |
| A24 | The worker schedules its own periodic jobs | Avoids a separate cron container; jobs coalesce on a dedupe key. |
| A25 | Backups run as a loop in a sidecar, not cron | Inherits the container environment and the instance profile; logs like every other service. A systemd timer alternative is provided. |
| A26 | Ubuntu 24.04 LTS is the host OS | Long support window, current Docker packages, SSM Agent available. |
| A27 | The stack starts through a systemd unit | Survives reboots and instance stop/start without manual intervention. |

## Chrome extension

| # | Assumption | Rationale |
| --- | --- | --- |
| A28 | Vite builds pages and worker/content scripts separately | A content script is a classic script: an emitted `import` would throw on injection and capture would silently never start. |
| A29 | The ZIP is built byte-deterministically in Node | IT can verify that the artifact they push is the one CI built. |
| A30 | DOM selectors are best-effort and version-stamped | ChatGPT's markup is not a public contract. All selectors live in one adapter with fixture tests; a product change is a single-file fix. |
| A31 | React is used only for popup and options | Content extraction stays framework-independent, as required. |

## Infrastructure

| # | Assumption | Rationale |
| --- | --- | --- |
| A32 | Default VPC and its first subnet unless overridden | Lets a first deployment succeed without networking work. |
| A33 | `t3a.large` x86_64 default | 2 vCPU / 8 GiB as specified; ARM64 `t4g.large` is supported and all images are multi-arch. |
| A34 | Terraform manages only non-secret SSM parameters | Terraform state stores values in plaintext; secrets are written by `scripts/put_secrets.sh`. |
| A35 | EBS snapshots are documented, not Terraform-managed | AWS Backup / DLM policy is usually an account-level concern owned by a different team. |
| A36 | Route 53 record only when a zone id is supplied | The brief restricts Route 53 to an existing hosted zone. |

## Compliance adapter

| # | Assumption | Rationale |
| --- | --- | --- |
| A37 | No endpoint path is invented | The brief forbids it. Base URL, log path and files path are all configuration. |
| A38 | Response field names are configurable via `OPENAI_COMPLIANCE_FIELD_MAP` | The documented shape is supplied with the Enterprise agreement, not guessable. |
| A39 | Only the compliance feed may set `compliance_verified` | Browser capture cannot vouch for company-wide completeness. |
| A40 | Deletion events become tombstones, not deletions | An archive that silently drops records on an upstream delete is not an archive. |

## Testing

| # | Assumption | Rationale |
| --- | --- | --- |
| A41 | No test touches a live ChatGPT account | Required by the brief. All DOM tests use sanitized fixtures. |
| A42 | Integration tests skip when `TEST_DATABASE_URL` is unset | `make test` works on a laptop with nothing running; `make verify` starts the database itself. |
| A43 | Load tests target a dedicated environment | Never production, never a live account. |
