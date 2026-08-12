#!/usr/bin/env bash
# =============================================================================
# Start compose.prod.yaml with disposable bind mounts and throwaway secrets.
#
# Proves, without any production credential:
#   * the production file starts a working stack from file-based secrets
#   * FastAPI serves plain HTTP on the private network and publishes no port
#   * PostgreSQL publishes no port
#   * pgAdmin is loopback-only and only under the `admin` profile
#   * the backup role can reach PostgreSQL through its .pgpass
#   * the cloudflared service is configured but is NOT started, because a real
#     tunnel token is required and must never exist in CI
# =============================================================================
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${ROOT}"

PROJECT="techsara-production-smoke"
DATA_ROOT="$(mktemp -d)"
ENV_FILE="$(mktemp)"
COMPOSE=(docker compose -p "${PROJECT}" --env-file "${ENV_FILE}" -f compose.prod.yaml)

# The smoke stack reuses the image CI already built rather than rebuilding it.
export IMAGE_NAME=techsara-chat-archive-backend
export IMAGE_TAG=local

cat > "${ENV_FILE}" <<EOF
DATA_ROOT=${DATA_ROOT}
IMAGE_NAME=${IMAGE_NAME}
IMAGE_TAG=${IMAGE_TAG}
PGADMIN_PORT=15050
CLOUDFLARED_METRICS_PORT=12000
PUBLIC_BASE_URL=https://archive.example.com
ARCHIVE_HOSTNAME=archive.example.com
POSTGRES_DB=techsara_chat_archive
POSTGRES_USER=techsara_app
OIDC_ISSUER=https://accounts.google.com
OIDC_CLIENT_ID=production-smoke-client
OIDC_REQUIRED_HD=example.com
ALLOWED_EMAIL_DOMAINS=example.com
MANAGED_WORKSPACE_LABEL=Managed Workspace
PGADMIN_DEFAULT_EMAIL=dba@example.com
API_WORKERS=2
EOF

cleanup() {
  result=$?
  trap - EXIT
  if [ "${result}" -ne 0 ]; then
    "${COMPOSE[@]}" --profile admin logs --no-color --tail 120 >&2 || true
  fi
  "${COMPOSE[@]}" --profile admin --profile compliance down -v --remove-orphans >/dev/null 2>&1 || true
  # PostgreSQL creates root-owned files in the bind mount; remove them from a
  # container so the runner does not need sudo.
  docker run --rm --volume "${DATA_ROOT}:/data" --entrypoint sh postgres:16.14-alpine \
    -c 'find /data -mindepth 1 -depth -delete' >/dev/null 2>&1 || true
  rmdir "${DATA_ROOT}" 2>/dev/null || true
  rm -f "${ENV_FILE}"
  exit "${result}"
}
trap cleanup EXIT

mkdir -p "${DATA_ROOT}"/{postgres,backups,secrets,pgadmin}
chmod 0777 "${DATA_ROOT}/postgres" "${DATA_ROOT}/backups" "${DATA_ROOT}/pgadmin"
chmod 0755 "${DATA_ROOT}/secrets"

# A password with the characters that break naive URL building and .pgpass
# quoting, so the escaping is genuinely exercised.
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

# No cloudflared.env is written on purpose: `required: false` must keep the
# file valid, and the tunnel must stay unstarted without a real token.
"${COMPOSE[@]}" config --quiet
test ! -e "${DATA_ROOT}/secrets/cloudflared.env"

"${COMPOSE[@]}" --profile admin down -v --remove-orphans >/dev/null 2>&1 || true
"${COMPOSE[@]}" --profile admin up -d --no-build --pull missing postgres api worker pgadmin

healthy=0
for _ in $(seq 1 90); do
  api_id="$("${COMPOSE[@]}" ps -q api)"
  postgres_id="$("${COMPOSE[@]}" ps -q postgres)"
  pgadmin_id="$("${COMPOSE[@]}" ps -q pgadmin)"
  worker_id="$("${COMPOSE[@]}" ps -q worker)"
  api_health="$(docker inspect -f '{{.State.Health.Status}}' "${api_id}" 2>/dev/null || true)"
  postgres_health="$(docker inspect -f '{{.State.Health.Status}}' "${postgres_id}" 2>/dev/null || true)"
  pgadmin_state="$(docker inspect -f '{{.State.Status}}' "${pgadmin_id}" 2>/dev/null || true)"
  worker_state="$(docker inspect -f '{{.State.Status}}' "${worker_id}" 2>/dev/null || true)"
  if [ "${api_health}" = healthy ] && [ "${postgres_health}" = healthy ] \
      && [ "${pgadmin_state}" = running ] && [ "${worker_state}" = running ]; then
    healthy=1
    break
  fi
  sleep 2
done
test "${healthy}" -eq 1

# Readiness over plain HTTP on the private network, with the Host header the
# Cloudflare Tunnel will forward.
"${COMPOSE[@]}" exec -T api curl -fsS \
  -H 'Host: archive.example.com' http://127.0.0.1:8000/health/ready | grep -q '"status":"ok"'

# Host bindings that actually exist, as "port/proto=ip:port" pairs. An exposed
# but unpublished port has no binding; Docker renders that as either a null
# entry or an absent key depending on version, so the entries are what get
# compared rather than the raw JSON.
host_bindings() {
  docker inspect \
    -f '{{range $p, $conf := .NetworkSettings.Ports}}{{range $conf}}{{$p}}={{.HostIp}}:{{.HostPort}} {{end}}{{end}}' \
    "$("${COMPOSE[@]}" ps -q "$1")"
}

# Neither the API nor PostgreSQL may be reachable from the host at all.
for private_service in api postgres worker; do
  bindings="$(host_bindings "${private_service}")"
  test -z "${bindings}" || {
    echo "${private_service} unexpectedly publishes host ports: ${bindings}" >&2
    exit 1
  }
done

# pgAdmin is bound to loopback only, and the binding genuinely exists: a
# container attached only to an internal network would silently get none.
pgadmin_binding="$(host_bindings pgadmin)"
case "${pgadmin_binding}" in
  *"80/tcp=127.0.0.1:15050"*) ;;
  *) echo "pgAdmin is not loopback-bound: ${pgadmin_binding:-<no binding at all>}" >&2; exit 1 ;;
esac
case "${pgadmin_binding}" in
  *0.0.0.0*|*"[::]"*) echo "pgAdmin is bound to a public address: ${pgadmin_binding}" >&2; exit 1 ;;
esac

test "$("${COMPOSE[@]}" exec -T api id -u)" = "10001"
"${COMPOSE[@]}" exec -T api python -c "
from app.core.config import get_settings
s = get_settings()
assert s.aws_region == 'us-east-1'
assert s.s3_bucket == 'techsara-chatgpt'
assert not s.s3_endpoint_url
assert 'p%40ss%3A%2F%25%20word' in s.database_url
assert s.max_expected_database_connections <= s.postgres_max_connections - 15
assert not s.browser_capture_active
"

# cloudflared must not be running: no token was provided.
test "$("${COMPOSE[@]}" ps --status running --services | sort | tr '\n' ' ')" = \
  "api pgadmin postgres worker "

# The backup role reaches PostgreSQL through the mounted .pgpass.
# The quoted variables expand inside the container, not in this shell.
# shellcheck disable=SC2016
"${COMPOSE[@]}" run --rm --no-deps --entrypoint /bin/sh backup -c \
  'cp "$PGPASS_SOURCE_FILE" "$PGPASSFILE"; chmod 0600 "$PGPASSFILE"; exec pg_dump --host=postgres --username=techsara_app --dbname=techsara_chat_archive --schema-only' \
  >/dev/null

echo "production Compose tunnel topology, secrets, identity and private pgAdmin smoke passed"
