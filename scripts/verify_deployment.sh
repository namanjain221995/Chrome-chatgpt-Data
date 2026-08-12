#!/usr/bin/env bash
# Verify direct TLS, private bindings, health, database and S3 identity.
set -euo pipefail

PROJECT_NAME="${PROJECT_NAME:-techsara-chat-archive}"
APP_DIR="${APP_DIR:-/opt/${PROJECT_NAME}}"
cd "${APP_DIR}"

env_value() {
  local key="$1"
  sed -n "s/^${key}=//p" .env | tail -n 1
}

POSTGRES_USER="$(env_value POSTGRES_USER)"
POSTGRES_DB="$(env_value POSTGRES_DB)"
PUBLIC_BASE_URL="$(env_value PUBLIC_BASE_URL)"
ARCHIVE_HOSTNAME="$(env_value ARCHIVE_HOSTNAME)"
if [[ -z "${POSTGRES_USER}" || -z "${POSTGRES_DB}" || -z "${PUBLIC_BASE_URL}" || -z "${ARCHIVE_HOSTNAME}" ]]; then
  echo "POSTGRES_USER, POSTGRES_DB, PUBLIC_BASE_URL and ARCHIVE_HOSTNAME must be set in .env" >&2
  exit 1
fi

COMPOSE=(docker compose -f compose.prod.yaml)
"${COMPOSE[@]}" config --quiet
"${COMPOSE[@]}" exec -T postgres pg_isready -U "${POSTGRES_USER}" -d "${POSTGRES_DB}"
curl -fkSs -H "Host: ${ARCHIVE_HOSTNAME}" https://127.0.0.1:443/health/live | grep -q '"status":"ok"'
curl -fkSs -H "Host: ${ARCHIVE_HOSTNAME}" https://127.0.0.1:443/health/ready | grep -q '"status":"ok"'
aws sts get-caller-identity --region us-east-1 >/dev/null
aws s3api head-bucket --bucket techsara-chatgpt --region us-east-1
curl -fsS "${PUBLIC_BASE_URL}/health/ready" | grep -q '"status":"ok"'

if ss -lnt | grep -Eq '0\.0\.0\.0:(8000|5050|5432)|\[::\]:(8000|5050|5432)'; then
  echo "a private service is listening on a public address" >&2
  exit 1
fi
ss -lnt | grep -Eq '0\.0\.0\.0:443|\[::\]:443' || {
  echo "the TLS API is not listening on public port 443" >&2
  exit 1
}
echo "deployment verification passed"
