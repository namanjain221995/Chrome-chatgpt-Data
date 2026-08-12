#!/usr/bin/env bash
# =============================================================================
# Verify a running production host.
#
#   sudo ./scripts/verify_production.sh
#
# Read-only: it starts nothing, changes nothing and prints no secret. Every
# check reports pass/fail and the script exits non-zero if any required check
# failed, so it is usable both by a human and by monitoring.
# =============================================================================
set -Eeuo pipefail
set +x

PROJECT_NAME="${PROJECT_NAME:-techsara-chat-archive}"
APP_DIR="${APP_DIR:-/opt/${PROJECT_NAME}}"
DATA_ROOT="${DATA_ROOT:-/srv/${PROJECT_NAME}}"
ENV_FILE="${ENV_FILE:-${APP_DIR}/.env.production}"
MIN_FREE_DISK_PERCENT="${MIN_FREE_DISK_PERCENT:-15}"
MIN_FREE_MEMORY_MB="${MIN_FREE_MEMORY_MB:-256}"

GREEN=$'\033[32m'; RED=$'\033[31m'; YELLOW=$'\033[33m'; RESET=$'\033[0m'
[ -t 1 ] || { GREEN=""; RED=""; YELLOW=""; RESET=""; }

failures=0
warnings=0

pass() { printf '%sok     %s %s\n' "${GREEN}" "${RESET}" "$*"; }
fail() { printf '%sFAIL   %s %s\n' "${RED}" "${RESET}" "$*"; failures=$((failures + 1)); }
warn() { printf '%swarn   %s %s\n' "${YELLOW}" "${RESET}" "$*"; warnings=$((warnings + 1)); }

cd "${APP_DIR}" 2>/dev/null || { fail "application directory ${APP_DIR} not found"; exit 1; }
[ -f "${ENV_FILE}" ] || { fail "${ENV_FILE} not found; run scripts/fetch_ssm_secrets.sh"; exit 1; }

env_value() { sed -n "s/^$1=//p" "${ENV_FILE}" | tail -n 1; }

PUBLIC_BASE_URL="$(env_value PUBLIC_BASE_URL)"
ARCHIVE_HOSTNAME="$(env_value ARCHIVE_HOSTNAME)"
POSTGRES_USER="$(env_value POSTGRES_USER)"
POSTGRES_DB="$(env_value POSTGRES_DB)"
S3_BUCKET="$(env_value S3_BUCKET)"
AWS_REGION="$(env_value AWS_REGION)"
CLOUDFLARED_METRICS_PORT="$(env_value CLOUDFLARED_METRICS_PORT)"
COMPOSE=(docker compose --env-file "${ENV_FILE}" -f compose.prod.yaml)

echo "Production verification"
echo "======================="

# --- Docker and Compose ----------------------------------------------------
if docker info >/dev/null 2>&1; then
  pass "Docker daemon is running ($(docker version --format '{{.Server.Version}}' 2>/dev/null))"
else
  fail "Docker daemon is not reachable"
fi

if compose_version="$(docker compose version --short 2>/dev/null)"; then
  pass "Docker Compose plugin ${compose_version}"
else
  fail "the Docker Compose plugin is not installed"
fi

if "${COMPOSE[@]}" config --quiet 2>/dev/null; then
  pass "compose.prod.yaml validates"
else
  fail "compose.prod.yaml does not validate"
fi

# --- Containers ------------------------------------------------------------
state() {
  local id
  id="$("${COMPOSE[@]}" ps -q "$1" 2>/dev/null || true)"
  [ -n "${id}" ] || { printf 'missing'; return; }
  docker inspect -f '{{.State.Status}}' "${id}" 2>/dev/null || printf 'unknown'
}

for service in postgres api worker cloudflared backup; do
  status="$(state "${service}")"
  if [ "${status}" = "running" ]; then
    pass "${service} container is running"
  else
    fail "${service} container is ${status}"
  fi
done

# pgAdmin must NOT be running as part of a normal production posture.
if [ "$(state pgadmin)" = "running" ]; then
  warn "pgAdmin is running; stop it when the maintenance window ends"
else
  pass "pgAdmin is not running"
fi

# --- PostgreSQL ------------------------------------------------------------
if "${COMPOSE[@]}" exec -T postgres pg_isready -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" -q 2>/dev/null; then
  pass "PostgreSQL accepts connections"
else
  fail "PostgreSQL is not accepting connections"
fi

mounted="$("${COMPOSE[@]}" exec -T postgres sh -c 'ls -1 /var/lib/postgresql/data/PG_VERSION 2>/dev/null' 2>/dev/null || true)"
if [ -n "${mounted}" ] && [ -f "${DATA_ROOT}/postgres/PG_VERSION" ]; then
  pass "PostgreSQL data directory is on the ${DATA_ROOT}/postgres bind mount"
else
  fail "PostgreSQL data directory does not look like the expected bind mount"
fi

connections="$("${COMPOSE[@]}" exec -T postgres psql -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" \
  -tAc 'SELECT count(*) FROM pg_stat_activity' 2>/dev/null | tr -d '[:space:]' || true)"
max_connections="$("${COMPOSE[@]}" exec -T postgres psql -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" \
  -tAc 'SHOW max_connections' 2>/dev/null | tr -d '[:space:]' || true)"
if [ -n "${connections}" ] && [ -n "${max_connections}" ]; then
  pass "PostgreSQL connections ${connections}/${max_connections}"
  if [ "${connections}" -gt $(( max_connections * 80 / 100 )) ]; then
    warn "connection usage is above 80% of max_connections"
  fi
else
  warn "could not read PostgreSQL connection counters"
fi

# --- API -------------------------------------------------------------------
if "${COMPOSE[@]}" exec -T api curl -fsS --max-time 5 \
     -H "Host: ${ARCHIVE_HOSTNAME}" http://127.0.0.1:8000/health/live 2>/dev/null \
     | grep -q '"status":"ok"'; then
  pass "API liveness (internal)"
else
  fail "API liveness (internal) failed"
fi

ready_body="$("${COMPOSE[@]}" exec -T api curl -fsS --max-time 10 \
  -H "Host: ${ARCHIVE_HOSTNAME}" http://127.0.0.1:8000/health/ready 2>/dev/null || true)"
if printf '%s' "${ready_body}" | grep -q '"status":"ok"'; then
  pass "API readiness (internal)"
else
  fail "API readiness (internal) failed"
fi
if printf '%s' "${ready_body}" | grep -q '"object_storage":true'; then
  pass "API reports object storage reachable"
else
  warn "API readiness does not report object storage as reachable"
fi

# --- Cloudflare tunnel -----------------------------------------------------
if curl -fsS --max-time 5 "http://127.0.0.1:${CLOUDFLARED_METRICS_PORT:-2000}/ready" >/dev/null 2>&1; then
  pass "cloudflared reports a ready tunnel connection"
else
  fail "cloudflared /ready did not answer on 127.0.0.1:${CLOUDFLARED_METRICS_PORT:-2000}"
fi

if [ -n "${PUBLIC_BASE_URL}" ]; then
  if curl -fsS --max-time 15 "${PUBLIC_BASE_URL}/health/ready" 2>/dev/null | grep -q '"status":"ok"'; then
    pass "public endpoint ${PUBLIC_BASE_URL}/health/ready"
  else
    fail "public endpoint ${PUBLIC_BASE_URL}/health/ready failed"
  fi
fi

# --- Network exposure ------------------------------------------------------
if command -v ss >/dev/null 2>&1; then
  if ss -lnt 2>/dev/null | grep -Eq '(0\.0\.0\.0|\[::\]):(8000|5432|5050|2000)\b'; then
    fail "a private service is listening on a public address"
    ss -lnt | grep -E '(0\.0\.0\.0|\[::\]):(8000|5432|5050|2000)\b' >&2 || true
  else
    pass "no private service is bound to a public address"
  fi
else
  warn "ss is unavailable; skipped the listening-socket check"
fi

# --- AWS -------------------------------------------------------------------
if identity="$(aws sts get-caller-identity --region "${AWS_REGION}" --query 'Arn' --output text 2>/dev/null)"; then
  pass "AWS identity ${identity}"
  case "${identity}" in
    *:assumed-role/*) pass "credentials come from an assumed instance role" ;;
    *) warn "AWS identity is not an assumed role; confirm no static key is in use" ;;
  esac
else
  fail "aws sts get-caller-identity failed; the instance role may be missing"
fi

if aws s3api head-bucket --bucket "${S3_BUCKET}" --region "${AWS_REGION}" >/dev/null 2>&1; then
  pass "S3 HeadBucket on ${S3_BUCKET}"
else
  fail "S3 HeadBucket on ${S3_BUCKET} failed"
fi

for key in AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY; do
  if grep -q "^${key}=" "${ENV_FILE}" 2>/dev/null; then
    fail "${key} is present in ${ENV_FILE}; production must use the instance role"
  else
    pass "${key} is absent from ${ENV_FILE}"
  fi
done

# --- Capture gates ---------------------------------------------------------
for gate in BROWSER_CONTENT_CAPTURE_ENABLED OPENAI_WRITTEN_AUTHORIZATION_CONFIRMED; do
  value="$(env_value "${gate}")"
  if [ "${value}" = "false" ]; then
    pass "${gate}=false"
  else
    warn "${gate}=${value} - browser content capture is ACTIVE; confirm this is authorized"
  fi
done

# --- Host resources --------------------------------------------------------
disk_used="$(df --output=pcent "${DATA_ROOT}" 2>/dev/null | tail -1 | tr -dc '0-9')"
if [ -n "${disk_used}" ]; then
  free_percent=$((100 - disk_used))
  if [ "${free_percent}" -ge "${MIN_FREE_DISK_PERCENT}" ]; then
    pass "disk free ${free_percent}% on ${DATA_ROOT}"
  else
    fail "disk free ${free_percent}% on ${DATA_ROOT} is below ${MIN_FREE_DISK_PERCENT}%"
  fi
else
  warn "could not read disk utilisation for ${DATA_ROOT}"
fi

available_mb="$(awk '/^MemAvailable:/ {print int($2/1024)}' /proc/meminfo 2>/dev/null || true)"
if [ -n "${available_mb}" ]; then
  if [ "${available_mb}" -ge "${MIN_FREE_MEMORY_MB}" ]; then
    pass "memory available ${available_mb} MiB"
  else
    fail "memory available ${available_mb} MiB is below ${MIN_FREE_MEMORY_MB} MiB"
  fi
else
  warn "could not read available memory"
fi

# --- Release identity ------------------------------------------------------
release_file="${APP_DIR}/deploy/current-release"
if [ -f "${release_file}" ]; then
  release_sha="$(sed -n 's/^GIT_SHA=//p' "${release_file}" | tail -1)"
  checkout_sha="$(git -C "${APP_DIR}" rev-parse HEAD 2>/dev/null || echo unknown)"
  pass "recorded release ${release_sha}"
  if [ "${release_sha}" = "${checkout_sha}" ]; then
    pass "checkout matches the recorded release"
  else
    fail "checkout ${checkout_sha} does not match the recorded release ${release_sha}"
  fi
  running_image="$(docker inspect -f '{{.Config.Image}}' "$("${COMPOSE[@]}" ps -q api 2>/dev/null)" 2>/dev/null || true)"
  if [ -n "${running_image}" ]; then
    pass "api container image ${running_image}"
    case "${running_image}" in
      *:latest) fail "the API is running a floating :latest tag" ;;
    esac
  fi
else
  warn "no ${release_file}; this host has not been deployed by scripts/deploy_production.sh"
fi

echo
if [ "${failures}" -gt 0 ]; then
  printf '%sverification failed: %d failure(s), %d warning(s)%s\n' "${RED}" "${failures}" "${warnings}" "${RESET}"
  exit 1
fi
printf '%sverification passed with %d warning(s)%s\n' "${GREEN}" "${warnings}" "${RESET}"
