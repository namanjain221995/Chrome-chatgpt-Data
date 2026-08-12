#!/usr/bin/env bash
# End-to-end Compose proof for PostgreSQL, migrations, FastAPI and the worker.
# AWS calls are deliberately outside this suite; CI unit tests stub botocore and
# an explicitly dispatched test covers a dedicated real-S3 prefix.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${ROOT}"

PROJECT="techsara-smoke"
COMPOSE=(docker compose -p "${PROJECT}" -f compose.yaml)
API_PORT="${API_PORT:-18080}"
BASE="http://127.0.0.1:${API_PORT}"
PASSED=0
FAILED=0

ok() { printf 'PASS  %s\n' "$*"; PASSED=$((PASSED + 1)); }
bad() { printf 'FAIL  %s\n' "$*" >&2; FAILED=$((FAILED + 1)); }
cleanup() { "${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true; }
trap cleanup EXIT

export API_PORT
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

"${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
"${COMPOSE[@]}" build --quiet api
ok "application image built from the working tree"

"${COMPOSE[@]}" up -d --wait --wait-timeout 180 postgres api worker
ok "PostgreSQL, API and worker started"
"${COMPOSE[@]}" exec -T postgres pg_isready -U techsara_app -d techsara_chat_archive
ok "PostgreSQL accepts connections"

table_count="$("${COMPOSE[@]}" exec -T postgres psql -U techsara_app \
  -d techsara_chat_archive -tAc \
  "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'" | tr -d ' \r')"
if [ "${table_count}" -ge 25 ]; then
  ok "migrations created ${table_count} tables"
else
  bad "only ${table_count} tables"
fi

curl -fsS "${BASE}/health/live" | grep -q '"status":"ok"'
ok "liveness is healthy on loopback"
curl -fsS "${BASE}/health/ready" | grep -q '"status":"ok"'
ok "readiness is healthy on loopback"

config_json="$(curl -fsS "${BASE}/api/v1/config")"
printf '%s' "${config_json}" | grep -q '"capture_active":[[:space:]]*true'
ok "signed runtime config reports capture active in the isolated smoke stack"
if printf '%s' "${config_json}" | grep -q 'TechSara'; then
  bad "anonymous runtime config exposed a workspace label"
else
  ok "anonymous runtime config withholds workspace identifiers"
fi

# Stop the worker before inserting a capture job: object writes are tested with
# the AWS SDK stub and by the explicitly authorized real-prefix workflow.
"${COMPOSE[@]}" stop worker >/dev/null

TOKEN="$("${COMPOSE[@]}" exec -T api python -c "
import asyncio
from app.core.config import get_settings
from app.core.security import VerifiedIdentity
from app.db.session import session_scope
from app.services import accounts

async def main():
    settings = get_settings()
    async with session_scope() as session:
        org = await accounts.get_or_create_organization(session)
        identity = VerifiedIdentity(subject='smoke', issuer=settings.oidc_issuer,
            email='smoke@example.com', email_verified=True, hosted_domain='example.com')
        user = await accounts.get_or_create_user(session, organization=org, identity=identity)
        device = await accounts.get_or_create_device(session, user=user, organization=org,
            device_fingerprint='smoke' * 8)
        token, *_ = await accounts.issue_session(session, user=user, organization=org,
            device=device, settings=settings)
        print(token)
asyncio.run(main())
" 2>/dev/null | tail -1)"
if [ -n "${TOKEN}" ]; then
  ok "test session token minted"
else
  bad "token mint failed"
  exit 1
fi

TEXT="Compose smoke message"
SHA="$(printf '%s' "${TEXT}" | sha256sum | awk '{print $1}')"
NOW="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
payload=$(cat <<JSON
{"workspace":{"source_workspace_id":null,"label":"TechSara's Workspace","kind":"managed_company","verified":true,"verification_signals":["workspace_label_match"]},"client":{"extension_version":"1.0.0","adapter_version":"2024.1","schema_version":"1.0","device_fingerprint":"smokesmokesmokesmokesmokesmoke12","captured_at":"${NOW}"},"messages":[{"idempotency_key":"k-smoke-000000000001","source_conversation_id":"smoke-conv-1","source_message_id":"smoke-msg-1","role":"user","sequence_index":0,"text":"${TEXT}","parts":[],"citations":[],"completion_status":"complete","is_edit":false,"is_regeneration":false,"branch_selected":true,"content_sha256":"${SHA}","attachment_client_ids":[]}]}
JSON
)

response="$(curl -fsS -X POST "${BASE}/api/v1/messages/batch" \
  -H "Authorization: Bearer ${TOKEN}" -H 'Content-Type: application/json' -d "${payload}")"
printf '%s' "${response}" | grep -q '"accepted":[[:space:]]*1'
ok "message batch accepted"

replay="$(curl -fsS -X POST "${BASE}/api/v1/messages/batch" \
  -H "Authorization: Bearer ${TOKEN}" -H 'Content-Type: application/json' -d "${payload}")"
printf '%s' "${replay}" | grep -q '"duplicate":[[:space:]]*1'
ok "message replay deduplicated"

before="$("${COMPOSE[@]}" exec -T postgres psql -U techsara_app -d techsara_chat_archive \
  -tAc 'SELECT count(*) FROM messages' | tr -d ' \r')"
"${COMPOSE[@]}" restart postgres api >/dev/null
for _ in $(seq 1 60); do
  curl -fsS "${BASE}/health/ready" >/dev/null 2>&1 && break
  sleep 2
done
after="$("${COMPOSE[@]}" exec -T postgres psql -U techsara_app -d techsara_chat_archive \
  -tAc 'SELECT count(*) FROM messages' | tr -d ' \r')"
if [ "${before}" = "${after}" ] && [ "${after}" -ge 1 ]; then
  ok "data survived restart"
else
  bad "data changed"
fi

dump_file="$(mktemp)"
trap 'rm -f "${dump_file}"; cleanup' EXIT
"${COMPOSE[@]}" exec -T postgres pg_dump -U techsara_app -d techsara_chat_archive -Fc \
  > "${dump_file}"
if [ -s "${dump_file}" ]; then
  ok "pg_dump created a non-empty backup"
else
  bad "backup empty"
fi
"${COMPOSE[@]}" exec -T postgres createdb -U techsara_app restore_check
"${COMPOSE[@]}" exec -T postgres pg_restore -U techsara_app -d restore_check --no-owner \
  < "${dump_file}"
restored="$("${COMPOSE[@]}" exec -T postgres psql -U techsara_app -d restore_check \
  -tAc 'SELECT count(*) FROM messages' | tr -d ' \r')"
if [ "${restored}" = "${after}" ]; then
  ok "restore reproduced ${restored} message rows"
else
  bad "restore mismatch"
fi

printf 'passed: %s\nfailed: %s\n' "${PASSED}" "${FAILED}"
[ "${FAILED}" -eq 0 ]
