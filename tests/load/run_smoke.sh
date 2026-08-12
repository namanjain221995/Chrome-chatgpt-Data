#!/usr/bin/env bash
# Short, deterministic k6 proof against an isolated local PostgreSQL/API stack.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${ROOT}"

PROJECT="techsara-load-smoke"
API_PORT="${LOAD_API_PORT:-18081}"
BASE_URL="http://127.0.0.1:${API_PORT}"
COMPOSE=(docker compose -p "${PROJECT}" -f compose.yaml)

cleanup() {
  result=$?
  trap - EXIT
  if [ "${result}" -ne 0 ]; then
    "${COMPOSE[@]}" logs --no-color --tail 200 api postgres >&2 || true
  fi
  "${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
  exit "${result}"
}
trap cleanup EXIT

export API_PORT
export ENVIRONMENT=development
export BROWSER_CONTENT_CAPTURE_ENABLED=true
export OPENAI_WRITTEN_AUTHORIZATION_CONFIRMED=true
export DEV_AUTH_ENABLED=true
export MANAGED_WORKSPACE_LABEL="TechSara's Workspace"
export ALLOWED_EMAIL_DOMAINS=example.com
export OIDC_CLIENT_ID=load-smoke-client
export OIDC_REQUIRED_HD=example.com
export POSTGRES_PASSWORD=load_smoke_password
export JWT_SECRET=load-smoke-signing-key-not-for-production-use
export CONFIG_SIGNING_KEY=load-smoke-config-key-not-for-production-use

"${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
"${COMPOSE[@]}" up -d --wait --wait-timeout 180 postgres api

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
        identity = VerifiedIdentity(subject='load-smoke', issuer=settings.oidc_issuer,
            email='load@example.com', email_verified=True, hosted_domain='example.com')
        user = await accounts.get_or_create_user(session, organization=org, identity=identity)
        device = await accounts.get_or_create_device(session, user=user, organization=org,
            device_fingerprint='loadsmoke' * 4)
        token, *_ = await accounts.issue_session(session, user=user, organization=org,
            device=device, settings=settings)
        print(token)
asyncio.run(main())
" 2>/dev/null | tail -1)"
test -n "${TOKEN}"

mkdir -p artifacts
docker run --rm --network host --user "$(id -u):$(id -g)" \
  --volume "${ROOT}:/workspace" --workdir /workspace \
  --env "BASE_URL=${BASE_URL}" \
  --env "ACCESS_TOKEN=${TOKEN}" \
  --env LOAD_PROFILE=smoke \
  --env MESSAGES_PER_BATCH=5 \
  --env ATTACHMENT_METADATA_ONLY=true \
  grafana/k6:1.2.3@sha256:4f82892217f3110cb233e2b2622bcc97fabc70f14bd241fbfbfe7305105c68aa \
  run tests/load/k6-ingest.js

test -s artifacts/load-test-report.md
test -s artifacts/load-test-summary.json
echo "load smoke test passed"
