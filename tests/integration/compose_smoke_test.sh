#!/usr/bin/env bash
# =============================================================================
# End-to-end integration test against a real `docker compose up`.
#
# Proves the claims that unit tests cannot:
#   1. a clean stack starts from nothing
#   2. PostgreSQL becomes healthy
#   3. Alembic migrations apply to an empty database
#   4. the API reports ready
#   5. a batch ingests through the real HTTP surface
#   6. the worker writes raw JSON to S3-compatible storage
#   7. restarting every service loses no data
#   8. a backup restores into a clean database
#
# Destroys its own stack on exit, including volumes.
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${ROOT}"

# A fixed project name is deliberate: the teardown at the top of the run then
# reclaims ports and volumes from a previous run that was interrupted before its
# EXIT trap completed.
PROJECT="techsara-smoke"
COMPOSE=(docker compose -p "${PROJECT}" -f compose.yaml)
API_PORT="${API_PORT:-18080}"
HTTPS_PORT="$((API_PORT + 1))"
# Caddy issues a local certificate from its internal CA for `localhost`, so the
# test exercises the real TLS path with -k rather than bypassing the proxy.
BASE="https://localhost:${HTTPS_PORT}"
CURL=(curl -fsSk)
BUCKET="techsara-smoke-bucket"
PASSED=0
FAILED=0

green() { printf '\033[32m%s\033[0m\n' "$*"; }
red() { printf '\033[31m%s\033[0m\n' "$*"; }
step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

ok() { green "  PASS  $*"; PASSED=$((PASSED + 1)); }
bad() { red "  FAIL  $*"; FAILED=$((FAILED + 1)); }

cleanup() {
  step "tearing down"
  "${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
}
trap cleanup EXIT

export CADDY_HTTP_PORT="${API_PORT}"
export CADDY_HTTPS_PORT="$((API_PORT + 1))"
export MINIO_API_PORT="$((API_PORT + 2))"
export MINIO_CONSOLE_PORT="$((API_PORT + 3))"
export CADDY_DOMAIN="localhost"
export S3_BUCKET="${BUCKET}"
export ENVIRONMENT=development
export BROWSER_CONTENT_CAPTURE_ENABLED=true
export OPENAI_WRITTEN_AUTHORIZATION_CONFIRMED=true
export DEV_AUTH_ENABLED=true
export MANAGED_WORKSPACE_LABEL="TechSara's Workspace"
export ALLOWED_EMAIL_DOMAINS=example.com
export OIDC_CLIENT_ID=smoke-test-client
export OIDC_REQUIRED_HD=example.com
export POSTGRES_PASSWORD=smoke_test_password
export JWT_SECRET=smoke-test-signing-key-not-for-production-use
export CONFIG_SIGNING_KEY=smoke-test-config-key-not-for-production-use

compose_exec() { "${COMPOSE[@]}" exec -T "$@"; }

# ---------------------------------------------------------------------------
step "1. starting a clean stack"
"${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true

# Build the application image up front. Without this, a stale `:local` image
# from an earlier run is reused and the test silently validates old code.
if "${COMPOSE[@]}" build --quiet api 2>&1 | tail -2; then
  ok "application image built from the working tree"
else
  bad "image build failed"
  exit 1
fi
# minio-init is a one-shot: `--wait` treats any exited container as a failure,
# so the long-running services are waited on and the initialiser is run after.
if "${COMPOSE[@]}" up -d --build --wait --wait-timeout 300 postgres minio 2>&1 | tail -5; then
  ok "postgres and minio started"
else
  bad "stack failed to start"
  exit 1
fi

if "${COMPOSE[@]}" up --exit-code-from minio-init minio-init 2>&1 | tail -2; then
  ok "object storage bucket created"
else
  bad "bucket creation failed"
fi

# ---------------------------------------------------------------------------
step "2. PostgreSQL health"
if compose_exec postgres pg_isready -U techsara_app -d techsara_chat_archive >/dev/null 2>&1; then
  ok "postgres accepts connections"
else
  bad "postgres never became ready"
fi

# ---------------------------------------------------------------------------
step "3. Alembic migrations on an empty database"
if "${COMPOSE[@]}" run --rm migrate 2>&1 | tail -3; then
  ok "migrations applied"
else
  bad "migrations failed"
fi

table_count="$(compose_exec postgres psql -U techsara_app -d techsara_chat_archive -tAc \
  "SELECT count(*) FROM information_schema.tables WHERE table_schema='public';" | tr -d ' \r')"
if [ "${table_count}" -ge 25 ]; then
  ok "schema has ${table_count} tables"
else
  bad "schema has only ${table_count} tables"
fi

partitions="$(compose_exec postgres psql -U techsara_app -d techsara_chat_archive -tAc \
  "SELECT count(*) FROM pg_inherits i JOIN pg_class p ON p.oid=i.inhparent WHERE p.relname='capture_events';" | tr -d ' \r')"
if [ "${partitions}" -ge 4 ]; then
  ok "capture_events has ${partitions} partitions"
else
  bad "capture_events has only ${partitions} partitions"
fi

# ---------------------------------------------------------------------------
step "4. API readiness"
"${COMPOSE[@]}" up -d --wait --wait-timeout 180 api worker caddy 2>&1 | tail -3 || true

ready=0
for _ in $(seq 1 60); do
  if "${CURL[@]}" "${BASE}/health/ready" >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 2
done
if [ "${ready}" -eq 1 ]; then
  ok "API is ready through Caddy on :${HTTPS_PORT} (TLS)"
else
  bad "API never became ready"
  "${COMPOSE[@]}" logs --tail 40 api caddy || true
  exit 1
fi

# Plain HTTP must redirect, never serve. A test that silently followed a 301
# would validate nothing, so this is asserted explicitly.
http_status="$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:${API_PORT}/api/v1/config" || echo 000)"
if [ "${http_status}" = "301" ] || [ "${http_status}" = "308" ]; then
  ok "plain HTTP redirects to HTTPS (${http_status})"
else
  bad "plain HTTP returned ${http_status} instead of a redirect"
fi

config_json="$("${CURL[@]}" "${BASE}/api/v1/config")"
if echo "${config_json}" | grep -q '"capture_active":[[:space:]]*true'; then
  ok "signed config reports capture_active"
else
  bad "signed config does not report capture_active"
fi
if echo "${config_json}" | grep -q '"capture_unsent_drafts":[[:space:]]*false'; then
  ok "config states drafts are never captured"
else
  bad "config is missing the draft guarantee"
fi
if echo "${config_json}" | grep -q "TechSara"; then
  bad "anonymous config discloses the managed workspace label (F-02)"
else
  ok "anonymous config withholds workspace identifiers"
fi

# ---------------------------------------------------------------------------
step "5. batch ingest through HTTP"
TOKEN="$(compose_exec api python -c "
import uuid, asyncio
from app.core.config import get_settings
from app.core.security import Role, create_access_token
from app.db.session import session_scope
from app.services import accounts
from app.core.security import VerifiedIdentity

async def main():
    settings = get_settings()
    async with session_scope() as session:
        org = await accounts.get_or_create_organization(session)
        identity = VerifiedIdentity(
            subject='smoke-subject', issuer=settings.oidc_issuer,
            email='smoke@example.com', email_verified=True, hosted_domain='example.com')
        user = await accounts.get_or_create_user(session, organization=org, identity=identity)
        device = await accounts.get_or_create_device(
            session, user=user, organization=org, device_fingerprint='smoke'*8)
        token, _, _, _, _ = await accounts.issue_session(
            session, user=user, organization=org, device=device, settings=settings)
        print(token)

asyncio.run(main())
" 2>/dev/null | tail -1)"

if [ -n "${TOKEN}" ]; then
  ok "minted a test session token"
else
  bad "could not mint a token"
  exit 1
fi

TEXT="Integration test message for the compose smoke test"
SHA="$(printf '%s' "${TEXT}" | sha256sum | awk '{print $1}')"
NOW="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

payload=$(cat <<JSON
{
  "workspace": {
    "source_workspace_id": null,
    "label": "TechSara's Workspace",
    "kind": "managed_company",
    "verified": true,
    "verification_signals": ["workspace_label_match"]
  },
  "client": {
    "extension_version": "1.0.0",
    "adapter_version": "2024.1",
    "schema_version": "1.0",
    "device_fingerprint": "smokesmokesmokesmokesmokesmoke12",
    "captured_at": "${NOW}"
  },
  "messages": [{
    "idempotency_key": "k-smoke-000000000001",
    "source_conversation_id": "smoke-conv-1",
    "source_message_id": "smoke-msg-1",
    "role": "user",
    "sequence_index": 0,
    "text": "${TEXT}",
    "parts": [],
    "citations": [],
    "completion_status": "complete",
    "is_edit": false,
    "is_regeneration": false,
    "branch_selected": true,
    "content_sha256": "${SHA}",
    "attachment_client_ids": []
  }]
}
JSON
)

response="$("${CURL[@]}" -X POST "${BASE}/api/v1/messages/batch" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H 'Content-Type: application/json' \
  -d "${payload}" 2>&1)" || true

if echo "${response}" | grep -q '"accepted": *1'; then
  ok "message batch accepted"
else
  bad "message batch was not accepted: ${response}"
fi

# Replaying the identical batch must be recognised, not duplicated.
replay="$("${CURL[@]}" -X POST "${BASE}/api/v1/messages/batch" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H 'Content-Type: application/json' \
  -d "${payload}" 2>&1)" || true
if echo "${replay}" | grep -q '"duplicate": *1'; then
  ok "replayed batch recognised as duplicate"
else
  bad "replay was not deduplicated: ${replay}"
fi

# ---------------------------------------------------------------------------
step "6. worker writes raw JSON to object storage"
archived=0
for _ in $(seq 1 45); do
  count="$(compose_exec postgres psql -U techsara_app -d techsara_chat_archive -tAc \
    "SELECT count(*) FROM capture_events WHERE archived_at IS NOT NULL;" | tr -d ' \r')"
  if [ "${count}" -ge 1 ]; then
    archived=1
    break
  fi
  sleep 2
done

if [ "${archived}" -eq 1 ]; then
  ok "capture event marked archived"
else
  bad "worker never archived the capture event"
  echo "--- diagnostic: jobs ---"
  compose_exec postgres psql -U techsara_app -d techsara_chat_archive -c \
    "SELECT kind, status, attempts, left(coalesce(error_summary,''),120) FROM jobs ORDER BY created_at DESC LIMIT 10;" || true
  echo "--- diagnostic: capture events ---"
  compose_exec postgres psql -U techsara_app -d techsara_chat_archive -c \
    "SELECT kind, status, archived_at, raw_s3_key FROM capture_events ORDER BY created_at DESC LIMIT 5;" || true
  echo "--- diagnostic: worker ---"
  "${COMPOSE[@]}" ps worker || true
  "${COMPOSE[@]}" logs --tail 40 worker || true
fi

raw_key="$(compose_exec postgres psql -U techsara_app -d techsara_chat_archive -tAc \
  "SELECT raw_s3_key FROM capture_events WHERE raw_s3_key IS NOT NULL LIMIT 1;" | tr -d ' \r')"
if [ -n "${raw_key}" ]; then
  ok "raw object key recorded: ${raw_key}"
else
  bad "no raw object key recorded"
fi

objects="$("${COMPOSE[@]}" run --rm --entrypoint /bin/sh minio-init -c "
  mc alias set local http://minio:9000 minioadmin minioadmin >/dev/null 2>&1
  mc ls --recursive local/${BUCKET}/raw/ 2>/dev/null | wc -l" 2>/dev/null | tail -1 | tr -d ' \r')"
if [ "${objects:-0}" -ge 1 ]; then
  ok "${objects} raw object(s) present in object storage"
else
  bad "no raw objects found in object storage"
fi

snapshots="$("${COMPOSE[@]}" run --rm --entrypoint /bin/sh minio-init -c "
  mc alias set local http://minio:9000 minioadmin minioadmin >/dev/null 2>&1
  mc ls --recursive local/${BUCKET}/normalized/ 2>/dev/null | wc -l" 2>/dev/null | tail -1 | tr -d ' \r')"
if [ "${snapshots:-0}" -ge 1 ]; then
  ok "${snapshots} conversation snapshot(s) written"
else
  bad "no conversation snapshots written"
fi

# ---------------------------------------------------------------------------
step "7. restart every service and prove no data loss"
before="$(compose_exec postgres psql -U techsara_app -d techsara_chat_archive -tAc \
  "SELECT count(*) FROM messages;" | tr -d ' \r')"

"${COMPOSE[@]}" restart postgres api worker >/dev/null 2>&1
sleep 5
for _ in $(seq 1 60); do
  compose_exec postgres pg_isready -U techsara_app -d techsara_chat_archive >/dev/null 2>&1 && break
  sleep 2
done

after="$(compose_exec postgres psql -U techsara_app -d techsara_chat_archive -tAc \
  "SELECT count(*) FROM messages;" | tr -d ' \r')"

if [ "${before}" = "${after}" ] && [ "${after}" -ge 1 ]; then
  ok "message count survived the restart (${after})"
else
  bad "message count changed across restart: ${before} -> ${after}"
fi

ready=0
for _ in $(seq 1 60); do
  "${CURL[@]}" "${BASE}/health/ready" >/dev/null 2>&1 && { ready=1; break; }
  sleep 2
done
[ "${ready}" -eq 1 ] && ok "API recovered after restart" || bad "API did not recover"

# ---------------------------------------------------------------------------
step "8. backup and restore into a clean database"
if compose_exec -e PGPASSWORD="${POSTGRES_PASSWORD}" postgres \
     pg_dump -U techsara_app -d techsara_chat_archive -Fc \
     > "/tmp/${PROJECT}-backup.dump" 2>/dev/null; then
  size="$(wc -c < "/tmp/${PROJECT}-backup.dump")"
  ok "backup created (${size} bytes)"
else
  bad "backup failed"
fi

compose_exec -e PGPASSWORD="${POSTGRES_PASSWORD}" postgres \
  psql -U techsara_app -d postgres -c "DROP DATABASE IF EXISTS restore_check;" >/dev/null 2>&1
compose_exec -e PGPASSWORD="${POSTGRES_PASSWORD}" postgres \
  psql -U techsara_app -d postgres -c "CREATE DATABASE restore_check;" >/dev/null 2>&1

if compose_exec -T -e PGPASSWORD="${POSTGRES_PASSWORD}" postgres \
     pg_restore -U techsara_app -d restore_check --no-owner < "/tmp/${PROJECT}-backup.dump" >/dev/null 2>&1; then
  restored="$(compose_exec postgres psql -U techsara_app -d restore_check -tAc \
    "SELECT count(*) FROM messages;" | tr -d ' \r')"
  if [ "${restored}" = "${after}" ]; then
    ok "restore reproduced ${restored} message(s)"
  else
    bad "restore mismatch: live=${after} restored=${restored}"
  fi
else
  bad "restore failed"
fi
rm -f "/tmp/${PROJECT}-backup.dump"

# ---------------------------------------------------------------------------
step "results"
echo "  passed: ${PASSED}"
echo "  failed: ${FAILED}"

if [ "${FAILED}" -gt 0 ]; then
  red "integration smoke test FAILED"
  exit 1
fi
green "integration smoke test passed (${PASSED} checks)"
