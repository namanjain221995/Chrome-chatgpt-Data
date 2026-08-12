#!/usr/bin/env bash
# Start the production Compose file with disposable bind mounts and test secrets.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${ROOT}"

PROJECT="techsara-production-smoke"
DATA_ROOT="$(mktemp -d)"
COMPOSE=(docker compose -p "${PROJECT}" -f compose.prod.yaml)

export DATA_ROOT
export HTTPS_PORT=18443
export PGADMIN_PORT=15050
export IMAGE_REPOSITORY=techsara/chat-archive-backend
export IMAGE_TAG=local
export PUBLIC_BASE_URL=https://archive.example.com
export ARCHIVE_HOSTNAME=archive.example.com
export POSTGRES_DB=techsara_chat_archive
export POSTGRES_USER=techsara_app
export OIDC_ISSUER=https://accounts.google.com
export OIDC_CLIENT_ID=production-smoke-client
export OIDC_REQUIRED_HD=example.com
export ALLOWED_EMAIL_DOMAINS=example.com
export MANAGED_WORKSPACE_LABEL='Managed Workspace'
export PGADMIN_DEFAULT_EMAIL=dba@example.com

cleanup() {
  result=$?
  trap - EXIT
  if [ "${result}" -ne 0 ]; then
    "${COMPOSE[@]}" --profile admin logs --no-color --tail 120 >&2 || true
  fi
  "${COMPOSE[@]}" --profile admin down -v --remove-orphans >/dev/null 2>&1 || true
  docker run --rm --volume "${DATA_ROOT}:/data" --entrypoint sh postgres:16.14-alpine \
    -c 'find /data -mindepth 1 -depth -delete' >/dev/null
  rmdir "${DATA_ROOT}"
  exit "${result}"
}
trap cleanup EXIT

mkdir -p "${DATA_ROOT}"/{postgres,backups,secrets,tls-input,pgadmin}
chmod 0777 "${DATA_ROOT}/postgres" "${DATA_ROOT}/backups" "${DATA_ROOT}/pgadmin"
chmod 0755 "${DATA_ROOT}/secrets"
openssl req -x509 -nodes -newkey rsa:2048 -days 8 \
  -subj '/CN=archive.example.com' \
  -addext 'subjectAltName=DNS:archive.example.com' \
  -keyout "${DATA_ROOT}/tls-input/origin.key" \
  -out "${DATA_ROOT}/tls-input/origin.pem" >/dev/null 2>&1
docker run --rm --pull never --user 0 --entrypoint /bin/bash \
  --volume "${ROOT}/scripts/install_origin_tls.sh:/install-origin-tls:ro" \
  --volume "${DATA_ROOT}:/data" techsara/chat-archive-backend:local \
  /install-origin-tls --data-root /data \
    --cert-file /data/tls-input/origin.pem --key-file /data/tls-input/origin.key \
  >/dev/null
tls_mode="$(docker run --rm --pull never --user 0 --entrypoint stat \
  --volume "${DATA_ROOT}:/data:ro" techsara/chat-archive-backend:local \
  -c '%a:%u:%g' /data/tls/origin.key)"
test "${tls_mode}" = "440:0:10001"

db_password='prod-smoke-p@ss:/% word'
printf '%s' "${db_password}" > "${DATA_ROOT}/secrets/postgres_password"
printf '%s' "${db_password}" > "${DATA_ROOT}/secrets/postgres_server_password"
printf '%s' 'j-test-only-production-smoke-signing-key-000000000000' > "${DATA_ROOT}/secrets/jwt_secret"
printf '%s' 'c-test-only-production-smoke-config-key-00000000000' > "${DATA_ROOT}/secrets/config_signing_key"
printf '%s' 'test-client-secret' > "${DATA_ROOT}/secrets/oidc_client_secret"
printf '%s' '' > "${DATA_ROOT}/secrets/openai_compliance_api_key"
printf '%s' 'test-pgadmin-password' > "${DATA_ROOT}/secrets/pgadmin_password"
pgpass_password="${db_password//\\/\\\\}"
pgpass_password="${pgpass_password//:/\\:}"
printf 'postgres:5432:*:techsara_app:%s\n' "${pgpass_password}" > "${DATA_ROOT}/secrets/pgpass"
chmod 0444 "${DATA_ROOT}/secrets/"*

"${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
"${COMPOSE[@]}" --profile admin up -d --pull never --wait --wait-timeout 180 \
  postgres api worker pgadmin

curl -fkSs -H 'Host: archive.example.com' \
  https://127.0.0.1:${HTTPS_PORT}/health/ready | grep -q '"status":"ok"'
test "$("${COMPOSE[@]}" exec -T api id -u)" = "10001"
"${COMPOSE[@]}" exec -T api python -c "
from app.core.config import get_settings
s = get_settings()
assert s.aws_region == 'us-east-1'
assert s.s3_bucket == 'techsara-chatgpt'
assert not s.s3_endpoint_url
assert 'p%40ss%3A%2F%25%20word' in s.database_url
"
test "$("${COMPOSE[@]}" ps --status running --services | sort | tr '\n' ' ')" = \
  "api pgadmin postgres worker "
# The quoted variables expand inside the container, not in this shell.
# shellcheck disable=SC2016
"${COMPOSE[@]}" run --rm --pull never --no-deps --entrypoint /bin/sh backup -c \
  'cp "$PGPASS_SOURCE_FILE" "$PGPASSFILE"; chmod 0600 "$PGPASSFILE"; exec pg_dump --host=postgres --username=techsara_app --dbname=techsara_chat_archive --schema-only' \
  >/dev/null

echo "production Compose secret, identity, database and private pgAdmin smoke passed"
