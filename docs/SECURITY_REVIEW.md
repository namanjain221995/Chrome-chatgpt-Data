# Adversarial security review

**Date:** 2026-08-12
**Scope:** the whole system — Chrome extension, backend API, worker, compliance
poller, database schema, Docker Compose, manual AWS deployment and operational
scripts.
**Method:** threat-model-driven code review plus targeted probes against the
running code. Every finding below was reproduced before it was fixed, and every
fix has a regression test.

---

## Summary

| ID | Severity | Finding | Status |
| --- | --- | --- | --- |
| F-01 | Medium | Unconstrained source identifiers reached S3 object keys and database values | **Fixed** |
| F-02 | Medium | The unauthenticated config endpoint disclosed the managed workspace label and id allowlist | **Fixed** |
| F-03 | Medium | SQLAlchemy persisted enum *names*, silently disabling every partial index and CHECK constraint | **Fixed** |
| F-04 | Low | An allowlisted workspace id was accepted without an observed verification signal | **Fixed** |
| F-05 | Low | `audit_events` stores a raw client IP alongside its hash | Accepted, documented |
| F-06 | Low | The `id_token` grant bypasses PKCE | Accepted, documented |
| F-07 | Informational | Per-process rate limiting is approximate | Accepted, documented |
| F-08 | Informational | Single instance is a single failure domain | Accepted by design |

No high or critical findings remained open at the end of the review.

---

## F-01 — Unconstrained source identifiers reached S3 keys (Medium, fixed)

**What was wrong.** `SourceId` was `str` with only a length bound. Both
`source_conversation_id` and `source_message_id` originate in the page, and
`source_conversation_id` is used as an S3 key segment.

**Reproduction.** Before the fix:

```
source_conversation_id = "../../backups/postgres/2026/01/01/evil\nX-Injected: 1"

raw_event_key(...) →
  'raw/events/year=2026/month=08/day=12/workspace=h
   /conversation=../../backups/postgres/2026/01/01/evil\nX-Injected: 1/e.json'
```

**Impact.** S3 treats keys as opaque strings and does not normalise `..`, and the
IAM policy is prefix-scoped, so this was **not** a path traversal and could not
write outside `raw/`. The real impact was a newline and arbitrary punctuation
inside object keys and database values: confusing keys, awkward tooling, and a
plausible log- or header-injection primitive for anything downstream that
concatenates a key into a line-oriented format.

**Fix.**
1. `SourceId` is now `^[A-Za-z0-9._:@-]+$`, so a hostile value is rejected at the
   schema boundary with a 422.
2. Defence in depth: `_key_segment()` in `app/services/storage.py` sanitises
   every segment of every key, so even a value that bypassed validation cannot
   shape a key.

**Tests.** `test_f01_source_identifiers_reject_control_characters`,
`test_f01_key_builders_sanitize_every_segment`.

---

## F-02 — Unauthenticated config disclosed workspace identifiers (Medium, fixed)

**What was wrong.** `GET /api/v1/config` is intentionally unauthenticated so the
extension can discover the kill switch and the privacy notice before anyone
signs in. It also returned `workspace_rules.managed_workspace_label` and
`managed_workspace_ids` to any caller on the internet.

**Impact.** Those values are the exact strings the client compares against when
deciding whether a page is the company workspace. Publishing them makes a
convincing spoof easier to build, and discloses internal configuration to
unauthenticated callers for no operational benefit.

**Fix.** The endpoint now serves a redacted document to anonymous callers — the
label is `null` and the id list is empty — and the full document only when a
valid company access token is presented. An invalid token is not an error; the
caller simply receives the public document. Because capture requires sign-in
anyway, a client that only ever sees the public document fails closed, which is
the correct behaviour.

Everything that makes the client safe is still in the public document: the kill
switch, `capture_active`, the privacy notice URL and the coverage statement.

**Tests.** `test_f02_public_config_withholds_workspace_identifiers`,
`test_f02_public_config_still_carries_the_safety_information`.

---

## F-03 — Enum names persisted instead of values (Medium, fixed)

**What was wrong.** SQLAlchemy's `Enum` type persists the Python member *name*
by default. Every partial index and CHECK constraint in this schema is written
against the lowercase *value*.

**Reproduction.** The database stored `PENDING` while the deduplication index read

```sql
WHERE dedupe_key IS NOT NULL AND status IN ('pending','running')
```

so the index matched nothing. Enqueueing the same dedupe key twice created two
jobs. The claim index (`ix_jobs_claim`, partial on `status = 'pending'`) was
never used either, so every claim was a sequential scan.

**Impact.** Silent duplicate work — the same conversation snapshot rebuilt many
times, the same archive job run twice — and a claim query that degrades badly as
the jobs table grows. No data corruption, because the handlers are idempotent,
but a real correctness and performance defect that would have surfaced only
under load.

**Fix.** `_enum()` in `app/models/identity.py` now passes
`values_callable=lambda obj: [m.value for m in obj]` and `create_constraint=True`,
so values are persisted and an invalid enum value is impossible. Migration 0001
was regenerated.

**Test.** `test_dedupe_key_prevents_duplicate_live_jobs` — it fails against the
old behaviour.

---

## F-04 — Allowlisted id accepted without an observed signal (Low, fixed)

**What was wrong.** When `MANAGED_WORKSPACE_IDS` was configured, a client that
merely *asserted* an allowlisted id was accepted. The label path already required
a strong verification signal; the id path did not.

**Impact.** Low. An attacker would still need a valid company session and
knowledge of the allowlisted id (which F-02 stopped disclosing). But the two
paths should hold the same standard.

**Fix.** The id path now also requires a strong signal
(`workspace_id_match` or `workspace_label_match`), so the client must report
having observed the id rather than only claiming it.

**Test.** `test_f04_allowlisted_id_still_requires_an_observed_signal`.

---

## F-05 — Raw client IP in the audit trail (Low, accepted)

`audit_events` stores both `source_ip` (INET) and `ip_hash`. A raw IP is personal
data in several jurisdictions.

**Accepted**, because a security investigation genuinely needs the address, and
the audit trail is already the most access-restricted table in the system.
Documented in [PRIVACY_AND_EMPLOYEE_NOTICE.md](PRIVACY_AND_EMPLOYEE_NOTICE.md).
If your jurisdiction requires otherwise, drop the `source_ip` column: `ip_hash`
alone still supports correlation.

---

## F-06 — The `id_token` grant bypasses PKCE (Low, accepted)

`POST /api/v1/auth/exchange` accepts `grant_type=id_token` in addition to the
PKCE authorization-code flow.

**Accepted.** The token is still fully verified — signature against the provider
JWKS, issuer, audience, expiry, `email_verified`, hosted domain and the allowed
domain list — and the audience check means a token minted for a different OAuth
client is refused. It exists so tests and the local identity provider can
exercise the real verification path. If your threat model excludes it, remove
the branch from `_identity_from_request`; the extension only uses PKCE.

---

## F-07 — Rate limiting is per-process (Informational, accepted)

Redis and ElastiCache are prohibited, so limiting is in-process with the per-key
budget divided by `API_WORKERS`. A client whose requests happen to land on one
worker sees `limit / workers` rather than the full limit.

**Accepted and documented.** Cloudflare supplies coarse edge controls as a
second layer.
If exactness ever matters more than the prohibition, a PostgreSQL-backed limiter
is possible at the cost of a write on every request.

---

## F-08 — Single instance is a single failure domain (Informational, accepted)

Accepted by design, and compensated with a separate encrypted data volume that
survives an instance rebuild, tested logical backups, S3 versioning, and a
documented 4-hour recovery. See [DISASTER_RECOVERY.md](DISASTER_RECOVERY.md).

---

## Areas reviewed and found sound

**Authentication.** PKCE with `state` and `nonce` validated; the verifier never
leaves the extension and never appears in a URL; the backend performs the token
exchange and full ID-token verification; refresh tokens are stored only as
SHA-256 hashes and are single-use; a `session_id` claim bound to the device row
means revoking a device invalidates a live access token immediately (test:
`test_register_device_and_revoke_it`).

**Authorization.** Five roles with distinct capabilities; `support` cannot read
message content; every admin surface is role-gated and audited. Tested for both
the allow and the deny direction.

**Injection.** All database access is through SQLAlchemy with bound parameters.
The one raw-SQL path (partition DDL) builds identifiers from a fixed table tuple
and a formatted date, never from user input. HTML is allowlist-sanitised twice —
in the extension with an inert `DOMParser` document, and again server-side — and
a test asserts nothing executes during sanitisation.

**Attachment pipeline.** Presigned PUTs pin bucket, key, content type **and
exact content length**, so a client cannot inflate an upload after the presign.
The worker re-downloads, re-hashes and checks magic bytes before promoting
quarantine → clean; a `.png` that is actually a shell script is rejected
(`test_content_type_lying_is_detected`). An employee cannot complete another
employee's attachment (`test_another_employee_cannot_complete_someone_elses_attachment`).

**Secrets.** Never in images, compose files, environment variables or repository
state. Rendered at deploy time from SSM into root-owned `0440` files readable
only by the numeric group of the service that consumes each secret. The scanner
runs locally and in CI.

**Log redaction.** Credential-shaped keys are removed, content keys are
suppressed unless explicitly enabled (which production refuses), and bearer
tokens, JWTs, AWS keys and presigned signatures are scrubbed from free text.
Validation errors echo the field location and error type, never the value —
`test_validation_errors_do_not_echo_content` pastes a secret into a bad request
and asserts it does not come back.

**Extension privileges.** Three permissions (`storage`, `alarms`, `identity`),
two host permissions, isolated world, no `all_frames`. The manifest validator
fails the build on a forbidden permission, a host outside the approved origins,
a weakened CSP, or an ES import surviving in the content script. ESLint bans
`document.cookie`, `localStorage` and `sessionStorage` in extension source.

**Fail-closed behaviour.** Verified at three layers with tests for each: the
extension verifier (nine refusal cases), the API schema, and the ingestion
service. An unconfigured server refuses everything rather than guessing.

---

## Probes run

| Probe | Result |
| --- | --- |
| Path traversal through `source_conversation_id` into S3 keys | Contained by prefix scoping; hardened anyway (F-01) |
| Newline injection into object keys | Reproduced, fixed (F-01) |
| Anonymous read of workspace identifiers | Reproduced, fixed (F-02) |
| Duplicate job creation with the same dedupe key | Reproduced, fixed (F-03) |
| Forged ID token with a wrong signature | Rejected |
| ID token for a different audience | Rejected |
| ID token with a mismatched nonce | Rejected |
| Expired ID token | Rejected |
| Access token signed with another key | Rejected |
| Using a device token after revocation | Rejected |
| Replaying a used refresh token | Rejected |
| Personal-workspace batch over HTTP | Rejected at all three layers |
| Oversized request body | Rejected before parsing |
| Batch above the item cap | Rejected |
| `.png` containing a shell script | Rejected by magic-byte validation |
| Completing another employee's attachment | Rejected |
| Employee reading the admin summary | Rejected |
| Curated export with the flag off | Rejected |
| Legal-hold record under a hard-delete policy | Survived |
| Two workers claiming the same job | Never double-delivered |
| Dead worker completing a recovered job | Rejected by lock token |
| Secret pasted into an invalid request | Not echoed in the error |

---

## Recommendations for the next iteration

1. **WAL archiving** to reduce the RPO from 24 hours to minutes.
2. **An admin API for legal holds**, which today are applied through SQL and are
   therefore outside the audit trail.
3. **Automated dependency scanning on a schedule**, not only on pull requests.
4. **A canary that exercises the full ingest path hourly** and alerts on silence
   — the failure mode most likely to go unnoticed is capture quietly stopping
   because ChatGPT's markup changed.
5. **Object Lock on the `backups/` prefix** for ransomware resistance.
6. **Periodic review of the DOM adapter** against the current ChatGPT UI.
