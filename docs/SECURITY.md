# Security

## Threat model

The data is confidential company conversation content plus employee identity.
The adversaries this design takes seriously:

| Adversary | Concern | Primary control |
| --- | --- | --- |
| A hostile web page | Reaching the extension's privileges or the backend token | Content script restricted to two origins, isolated world, no page storage access, no `innerHTML` |
| A curious employee | Reading colleagues' conversations | RBAC; no public search; every admin read audited |
| A compromised employee laptop | Exfiltrating archived content | Tokens are short-lived and device-bound; the queue is bounded; revoking a device is instant |
| An attacker on the network | Reading traffic | TLS everywhere outside the private Docker network; HSTS; S3 denies non-TLS |
| An attacker who reaches the instance | Reading the database | PostgreSQL is not publicly exposed; secrets are root-owned and readable only by each service group; EBS is encrypted |
| An insider with AWS access | Silent deletion | S3 versioning; the instance role has **no** `s3:DeleteObject`; audited deletion path only |
| An SSRF in the application | Stealing instance credentials | IMDSv2 required, hop limit 1 |
| A malicious upload | Malware, or a file lying about its type | Quarantine → verify checksum, size and magic bytes → clean; optional ClamAV profile |

Out of scope for version 1: a fully compromised AWS account, a malicious Chrome
build, and a hostile administrator with database and S3 access.

---

## Controls, mapped to requirements

### 1. Company workspace only, fail closed
Verification happens three times: the extension verifier, the API schema, and
the ingestion service. Each fails closed. With no configured workspace label or
id, the server refuses **everything** rather than guessing.

### 2–5. Notice, no stealth, no personal workspaces, no drafts or keystrokes
The popup and options page state what is captured. Personal workspaces are
rejected at three layers. The DOM adapter refuses to read `textarea`, `input`,
`contenteditable` and `form` elements, so drafts cannot be captured; a test
asserts draft text never appears in any extracted payload. No keyboard listener
exists in the codebase.

### 6. No cookie or session-token access
The manifest requests no `cookies` permission, and `scripts/validate-manifest.mjs`
fails the build if one appears. ESLint bans `document.cookie`, `localStorage`
and `sessionStorage` in extension source.

### 7. TLS everywhere
Cloudflare terminates TLS at the edge and sets HSTS. Traffic reaches the
instance through a named Cloudflare Tunnel: `cloudflared` dials out to
Cloudflare, so there is no inbound application port, no origin certificate and
no origin private key to protect or rotate. The only hop that is not TLS is
`cloudflared -> api:8000`, which is a private Docker network on the same host.
The S3 bucket policy denies any request with `aws:SecureTransport = false`. The
extension refuses a non-HTTPS backend URL from managed policy.

### 8. FastAPI, PostgreSQL and pgAdmin never publicly exposed
FastAPI uses `expose: 8000` and publishes no host port at all; PostgreSQL has no
host binding and sits on an `internal: true` Docker network. pgAdmin lives
behind the `admin` profile and binds to `127.0.0.1` only.
`scripts/verify_production_config.sh` asserts all of this statically, and the
production Compose smoke test asserts it again at runtime by inspecting the
actual container port bindings.

Note that a container attached only to an internal network cannot publish a host
port: Docker accepts the binding and silently never creates it. pgAdmin is
therefore also attached to a dedicated non-internal `admin` bridge so its
loopback binding genuinely exists. The topology check enforces this rule for
every service that publishes a port.

Access is via an SSH local port-forward — see
[PGADMIN_ACCESS.md](PGADMIN_ACCESS.md).

### 9. SSH is the only administrative path
Application traffic never uses SSH, and SSH never carries application traffic.
GitHub Actions authenticates with a dedicated key held in
`EC2_SSH_PRIVATE_KEY`, with host-key checking always enabled and the host key
pinned through the `EC2_SSH_HOST_KEY` repository variable;
`StrictHostKeyChecking=no` is never used. See
[GITHUB_SECRETS.md](GITHUB_SECRETS.md).

**Port 22 source range — an accepted trade-off.** GitHub-hosted runners
connect from a large, rotating IP pool, so pinning the security group to a
narrow source range is not achievable while deployment runs on them. Port 22
therefore stays reachable, and the control is authentication rather than
network scope:

* key-only authentication (`PasswordAuthentication no`, the Amazon Linux 2023
  default — verify with `sudo sshd -T | grep passwordauthentication`);
* no root login;
* a dedicated deployment key that is rotated as described in
  [GITHUB_SECRETS.md](GITHUB_SECRETS.md).

If a narrow range is required by policy, the options are a bastion host, a
self-hosted runner inside the VPC, or restricting to GitHub's published
`actions` ranges from `https://api.github.com/meta` and refreshing them on a
schedule. Note that **no application port is open in any of these cases**: the
Cloudflare Tunnel is outbound-only, so removing SSH exposure is the only
inbound question left to answer.

### 8a. Interactive API documentation is opt-in
`/docs` and `/openapi.json` are served outside production, and in production
only when `API_DOCS_ENABLED=true`. Swagger UI is unauthenticated: enabling it
publishes the complete admin, ingest and attachment surface, including request
schemas, to anyone who knows the hostname.

If it is needed in production, protect it at the edge rather than exposing it:

1. Zero Trust -> Access -> Applications -> **Add a self-hosted application**
2. Domain `archive.<company-domain>`, **Path** `docs` -- then add a second
   application for path `openapi.json`
3. Policy: **Allow**, Include -> *Emails ending in* `@<company-domain>`
4. Set `/techsara-chat-archive/api_docs_enabled` to `true` and redeploy

Access authenticates against the same Google Workspace directory as the
application, and every visit is logged in Zero Trust. Do not put an Access
policy on the whole hostname: the extension calls `/api/v1/*` with a bearer
token and would be blocked by the interactive login.

The API logs `api_docs_exposed` at WARNING on every start while this is on, and
`scripts/verify_production.sh` reports it, so it cannot become invisible
background state.

### 9a. No application port is ever published
The security group needs no rule for 80, 443, 8000, 5432 or 5050. Nothing on
the host listens on them, so such a rule grants reach without granting access
and should be deleted. `scripts/verify_production.sh` fails if any private
service ever becomes bound to a public address.

### 10–11. S3 block public access and encryption
Block-all-public-access, `BucketOwnerEnforced` ownership (ACLs disabled),
versioning, SSE-S3 by default with optional SSE-KMS, and a bucket policy that
denies unencrypted uploads.

### 12. Least-privilege IAM
The instance role can read and write only the archive prefixes, read only its
own SSM parameter path, and write only its own log group.
**`s3:DeleteObject` is deliberately absent.**

### 13. Short-lived presigned URLs
Upload URLs expire in 300 seconds and are pinned to bucket, key, content type
and exact content length — a client cannot inflate an upload past the policy
limit after the presign. Download URLs are issued only to authorized roles and
every issuance is audited.

### 14–16. Input validation, output encoding, SQL injection
Every request is validated by a Pydantic model with `extra="forbid"`. HTML is
allowlist-sanitised twice (extension and backend). All database access goes
through SQLAlchemy with bound parameters; there is no string-built SQL over user
input.

### 17. Rate limiting and body-size limits
Per-user, per-device and per-IP sliding windows, plus a 1-second burst window.
Because the API runs `API_WORKERS` Gunicorn workers, the per-key budget is
divided by the worker count so the fleet-wide limit matches the configured
value. Cloudflare supplies coarse edge controls; middleware enforces the
authoritative origin body-size limit.

### 18. Auditing
Every admin read, export, approval, deletion, device revocation and
authentication decision writes an `audit_events` row with actor, action,
resource, outcome, hashed IP and correlation id. Content keys are stripped from
audit details.

### 19. Log redaction
`app/core/logging.py` removes credential-shaped keys, suppresses content keys
unless `LOG_MESSAGE_CONTENT=true` (refused in production), and scrubs
bearer tokens, JWTs, AWS keys and presigned signatures from free text.

### 20–21. Secret scanning and dependency checks
`make secret-scan` runs locally and in CI, alongside gitleaks, `pip-audit` and
`npm audit`. Dependencies are pinned to exact versions.

### 22–25. Retention, export gating and training
Legal hold blocks deletion at the database level (a CHECK constraint forbids a
soft-deleted row that is also under hold). Curated export requires
`TRAINING_EXPORT_ENABLED=true` **and** an explicit approval row per conversation.
No conversation is sent to a third-party model. Assistant output is never
treated as approved training data without human approval.

---

## Authentication

Authorization Code with PKCE through `chrome.identity.launchWebAuthFlow`:

1. The extension generates `code_verifier`, `state` and `nonce`; the verifier
   never leaves the extension and never appears in a URL.
2. The provider redirect is rejected unless `state` matches and the attempt is
   under ten minutes old.
3. The **backend** exchanges the code and verifies the ID token: signature
   against the provider JWKS, issuer, audience, expiry, issued-at, nonce,
   `email_verified`, hosted domain and the allowed-domain list.
4. The backend issues its own short-lived access token (30 minutes) and an
   opaque refresh token stored only as a SHA-256 hash.
5. Refresh tokens are single-use: using one clears the stored hash.

An email address supplied by the extension is never proof of identity. A
`session_id` claim is checked against the device row, so a token minted before a
revoke-and-relogin cycle stops working immediately.

The development identity provider (`app/core/devauth.py`) generates its RSA key
in memory and every entry point calls `assert_dev_auth_allowed`, which raises
unconditionally when `ENVIRONMENT=production` — even if `DEV_AUTH_ENABLED=true`.

## Authorization

| Role | May do |
| --- | --- |
| `employee` | Ingest their own capture, read their own sync status |
| `support` | Read the admin health summary; **no message content** |
| `compliance_admin` | Read content, manage legal holds, revoke devices, run exports |
| `security_reviewer` | Read content and audit records for investigations |
| `data_curator` | Approve records and run curated exports |

---

## Container hardening

- Non-root (uid 10001), read-only root filesystem, `/tmp` as a size-limited tmpfs
- `cap_drop: ALL`, `no-new-privileges:true`
- One image for API, worker, poller and backup — one thing to scan and roll back
- No Docker socket is ever mounted into an application container
- Resource limits so one service cannot starve the others

## Production configuration guardrails

`Settings` refuses to start in production when: dev auth is enabled, a default
or short secret is in use, the database URL still holds a placeholder, an S3
endpoint override is set, the bucket/region is wrong, path-style addressing is
enabled, the public URL is not HTTPS, message-content
logging is on, no allowed domains are configured, or the OIDC client id is
unset. Failing to boot is the correct behaviour; running insecurely is not.

---

## Reporting a vulnerability

Email `security@<company-domain>` with steps to reproduce. Do not open a public
issue. See [INCIDENT_RUNBOOK.md](INCIDENT_RUNBOOK.md) for the response process.
