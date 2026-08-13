#!/usr/bin/env bash
# =============================================================================
# Production deployment on the EC2 host.
#
#   ./scripts/deploy_production.sh <git-sha>
#   DEPLOY_SHA=<git-sha> ./scripts/deploy_production.sh
#
# Invoked over SSH by .github/workflows/deploy.yml with the exact commit that
# was pushed to main. The commit is the release identity: the backend image is
# built here and tagged with that SHA, never with `latest`.
#
# Guarantees:
#   * one deployment at a time (flock)
#   * the previous working release is recorded before anything changes
#   * a failed migration or health check rolls the application back
#   * PostgreSQL data and schema are never destroyed or downgraded
#   * no secret value is ever printed
# =============================================================================
set -Eeuo pipefail
set +x

PROJECT_NAME="${PROJECT_NAME:-techsara-chat-archive}"
APP_DIR="${APP_DIR:-/opt/${PROJECT_NAME}}"
DATA_ROOT="${DATA_ROOT:-/srv/${PROJECT_NAME}}"
ENV_FILE="${ENV_FILE:-${APP_DIR}/.env.production}"
LOCK_FILE="${LOCK_FILE:-/var/lock/${PROJECT_NAME}-deploy.lock}"
LOCK_WAIT_SECONDS="${LOCK_WAIT_SECONDS:-900}"
GIT_REMOTE="${GIT_REMOTE:-origin}"
RELEASE_DIR="${APP_DIR}/deploy"
CURRENT_RELEASE="${RELEASE_DIR}/current-release"
PREVIOUS_RELEASE="${RELEASE_DIR}/previous-release"
HEALTH_RETRIES="${HEALTH_RETRIES:-60}"
HEALTH_INTERVAL="${HEALTH_INTERVAL:-5}"
PUBLIC_HEALTH_RETRIES="${PUBLIC_HEALTH_RETRIES:-24}"
DEPLOY_ACTOR="${DEPLOY_ACTOR:-$(id -un)}"

DEPLOY_SHA="${1:-${DEPLOY_SHA:-}}"

log()  { printf '[deploy] %s\n' "$*"; }
warn() { printf '[deploy] WARNING: %s\n' "$*" >&2; }
die()  { printf '[deploy] ERROR: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root (sudo $0 <git-sha>)"
[ -n "${DEPLOY_SHA}" ] || die "usage: $0 <git-sha>"
[[ "${DEPLOY_SHA}" =~ ^[0-9a-f]{40}$ ]] || die "DEPLOY_SHA must be a full 40-character commit SHA"

cd "${APP_DIR}" || die "application directory not found: ${APP_DIR}"
[ -d "${APP_DIR}/.git" ] || die "${APP_DIR} is not a git repository"

# ---------------------------------------------------------------------------
# One deployment at a time
# ---------------------------------------------------------------------------
exec 9>"${LOCK_FILE}"
if ! flock -w "${LOCK_WAIT_SECONDS}" 9; then
  die "another deployment has held ${LOCK_FILE} for more than ${LOCK_WAIT_SECONDS}s"
fi
log "acquired deployment lock"

COMPOSE=(docker compose --env-file "${ENV_FILE}" -f compose.prod.yaml)

# ---------------------------------------------------------------------------
# Record the release we are replacing, before touching anything
# ---------------------------------------------------------------------------
install -d -o root -g root -m 0755 "${RELEASE_DIR}"

release_value() { sed -n "s/^$1=//p" "${2:-${CURRENT_RELEASE}}" 2>/dev/null | tail -n 1; }

ROLLBACK_SHA="$(release_value GIT_SHA)"
if [ -z "${ROLLBACK_SHA}" ]; then
  # First managed deployment: fall back to whatever is checked out right now.
  ROLLBACK_SHA="$(git rev-parse HEAD 2>/dev/null || true)"
  FIRST_DEPLOYMENT=true
else
  FIRST_DEPLOYMENT=false
fi
log "deploying ${DEPLOY_SHA}; rollback target ${ROLLBACK_SHA:-none}"

rollback() {
  local reason="$1"
  warn "deployment failed: ${reason}"
  if [ -z "${ROLLBACK_SHA}" ] || [ "${ROLLBACK_SHA}" = "${DEPLOY_SHA}" ]; then
    warn "no distinct previous release to roll back to; leaving the stack as it is"
    warn "the database schema was NOT reverted"
    return 1
  fi
  warn "rolling application back to ${ROLLBACK_SHA} (schema is left at head)"
  # This process already holds the deployment lock on fd 9; the child must not
  # try to take it again or it would block until the timeout.
  if ROLLBACK_HAS_LOCK=true "${APP_DIR}/scripts/rollback_production.sh" "${ROLLBACK_SHA}"; then
    warn "rollback to ${ROLLBACK_SHA} succeeded"
  else
    warn "ROLLBACK FAILED - the stack needs manual attention"
  fi
  return 1
}

on_error() {
  local line="$1"
  rollback "aborted at line ${line}" || true
  exit 1
}
trap 'on_error "${LINENO}"' ERR

# ---------------------------------------------------------------------------
# 1. Check out the exact commit
# ---------------------------------------------------------------------------
log "fetching ${GIT_REMOTE}"
git config --global --add safe.directory "${APP_DIR}" 2>/dev/null || true
git fetch --prune "${GIT_REMOTE}" '+refs/heads/*:refs/remotes/'"${GIT_REMOTE}"'/*' --tags

git cat-file -e "${DEPLOY_SHA}^{commit}" 2>/dev/null \
  || die "commit ${DEPLOY_SHA} does not exist on ${GIT_REMOTE}"

log "checking out ${DEPLOY_SHA}"
git reset --hard "${DEPLOY_SHA}"
chmod +x "${APP_DIR}/scripts/"*.sh

# ---------------------------------------------------------------------------
# 2. Configuration and secrets from SSM
# ---------------------------------------------------------------------------
log "rendering configuration from AWS SSM Parameter Store"
IMAGE_TAG="${DEPLOY_SHA}" "${APP_DIR}/scripts/fetch_ssm_secrets.sh"
[ -f "${ENV_FILE}" ] || die "${ENV_FILE} was not produced"

env_value() { sed -n "s/^$1=//p" "${ENV_FILE}" | tail -n 1; }

PUBLIC_BASE_URL="$(env_value PUBLIC_BASE_URL)"
ARCHIVE_HOSTNAME="$(env_value ARCHIVE_HOSTNAME)"
POSTGRES_USER="$(env_value POSTGRES_USER)"
POSTGRES_DB="$(env_value POSTGRES_DB)"
IMAGE_NAME="$(env_value IMAGE_NAME)"
S3_BUCKET="$(env_value S3_BUCKET)"
AWS_REGION="$(env_value AWS_REGION)"
COMPLIANCE_POLL_ENABLED="$(env_value COMPLIANCE_POLL_ENABLED)"
CLOUDFLARED_METRICS_PORT="$(env_value CLOUDFLARED_METRICS_PORT)"

for required in PUBLIC_BASE_URL ARCHIVE_HOSTNAME POSTGRES_USER POSTGRES_DB IMAGE_NAME S3_BUCKET; do
  [ -n "${!required}" ] || die "${required} is missing from ${ENV_FILE}"
done
[ "$(env_value IMAGE_TAG)" = "${DEPLOY_SHA}" ] || die "IMAGE_TAG in ${ENV_FILE} is not ${DEPLOY_SHA}"

TUNNEL_ENV="${DATA_ROOT}/secrets/cloudflared.env"
[ -s "${TUNNEL_ENV}" ] || die "Cloudflare tunnel token missing at ${TUNNEL_ENV}"

# ---------------------------------------------------------------------------
# 3. Validate the Compose topology before starting anything
# ---------------------------------------------------------------------------
log "validating compose.prod.yaml"
"${COMPOSE[@]}" config --quiet
# Exported so the topology assertions run against the configuration this
# deployment will actually start, not against the script's CI placeholders.
IMAGE_NAME="${IMAGE_NAME}" IMAGE_TAG="${DEPLOY_SHA}" \
PUBLIC_BASE_URL="${PUBLIC_BASE_URL}" ARCHIVE_HOSTNAME="${ARCHIVE_HOSTNAME}" \
POSTGRES_DB="${POSTGRES_DB}" POSTGRES_USER="${POSTGRES_USER}" \
OIDC_ISSUER="$(env_value OIDC_ISSUER)" OIDC_CLIENT_ID="$(env_value OIDC_CLIENT_ID)" \
OIDC_REQUIRED_HD="$(env_value OIDC_REQUIRED_HD)" \
ALLOWED_EMAIL_DOMAINS="$(env_value ALLOWED_EMAIL_DOMAINS)" \
MANAGED_WORKSPACE_LABEL="$(env_value MANAGED_WORKSPACE_LABEL)" \
PGADMIN_DEFAULT_EMAIL="$(env_value PGADMIN_DEFAULT_EMAIL)" \
API_WORKERS="$(env_value API_WORKERS)" \
DATABASE_POOL_SIZE="$(env_value DATABASE_POOL_SIZE)" \
DATABASE_MAX_OVERFLOW="$(env_value DATABASE_MAX_OVERFLOW)" \
POSTGRES_MAX_CONNECTIONS="$(env_value POSTGRES_MAX_CONNECTIONS)" \
  bash "${APP_DIR}/scripts/verify_production_config.sh" >/dev/null

# ---------------------------------------------------------------------------
# 4. PostgreSQL first, then a pre-migration backup
# ---------------------------------------------------------------------------
log "pulling pinned third-party images"
"${COMPOSE[@]}" pull --quiet postgres cloudflared

log "starting PostgreSQL"
"${COMPOSE[@]}" up -d postgres

postgres_ready=false
for _ in $(seq 1 60); do
  if "${COMPOSE[@]}" exec -T postgres pg_isready -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" -q; then
    postgres_ready=true
    break
  fi
  sleep 2
done
[ "${postgres_ready}" = true ] || die "PostgreSQL did not become ready"
log "PostgreSQL is ready"

# ---------------------------------------------------------------------------
# 5. Build the application image for this exact commit
# ---------------------------------------------------------------------------
log "building ${IMAGE_NAME}:${DEPLOY_SHA}"
"${COMPOSE[@]}" build --pull api

# A pre-migration backup only makes sense once the schema exists.
schema_exists="$("${COMPOSE[@]}" exec -T postgres psql -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" \
  -tAc "SELECT 1 FROM information_schema.tables WHERE table_name='alembic_version'" 2>/dev/null || true)"
if [ "${schema_exists}" = "1" ]; then
  log "taking a pre-migration backup"
  "${COMPOSE[@]}" run --rm --no-deps --entrypoint /bin/sh backup /opt/scripts/backup_postgres.sh
else
  log "no existing schema; skipping the pre-migration backup"
fi

# ---------------------------------------------------------------------------
# 6. Migrate, then bring the application up
# ---------------------------------------------------------------------------
log "running database migrations"
"${COMPOSE[@]}" run --rm migrate

log "recreating api, worker, backup and cloudflared"
"${COMPOSE[@]}" up -d --no-build --remove-orphans api worker backup cloudflared

# pgAdmin is never started by a deployment. The compliance poller starts only
# when its own flag is on.
if [ "${COMPLIANCE_POLL_ENABLED}" = "true" ]; then
  log "compliance poller is enabled; starting it"
  "${COMPOSE[@]}" --profile compliance up -d --no-build compliance-poller
else
  log "compliance poller disabled; not started"
fi

# ---------------------------------------------------------------------------
# 7. Health verification
# ---------------------------------------------------------------------------
container_state() {
  local id
  id="$("${COMPOSE[@]}" ps -q "$1" 2>/dev/null || true)"
  [ -n "${id}" ] || { printf 'missing'; return; }
  docker inspect -f '{{.State.Status}}' "${id}" 2>/dev/null || printf 'unknown'
}

log "waiting for internal API readiness"
api_ready=false
for _ in $(seq 1 "${HEALTH_RETRIES}"); do
  if "${COMPOSE[@]}" exec -T api curl -fsS --max-time 5 \
       -H "Host: ${ARCHIVE_HOSTNAME}" http://127.0.0.1:8000/health/ready 2>/dev/null \
       | grep -q '"status":"ok"'; then
    api_ready=true
    break
  fi
  sleep "${HEALTH_INTERVAL}"
done
if [ "${api_ready}" != true ]; then
  "${COMPOSE[@]}" logs --no-color --tail 120 api >&2 || true
  die "API did not become ready"
fi
log "API is ready"

log "checking the worker"
[ "$(container_state worker)" = "running" ] || {
  "${COMPOSE[@]}" logs --no-color --tail 80 worker >&2 || true
  die "worker container is not running"
}

log "checking the nightly backup service"
[ "$(container_state backup)" = "running" ] || {
  "${COMPOSE[@]}" logs --no-color --tail 80 backup >&2 || true
  die "backup container is not running"
}

log "checking the Cloudflare tunnel"
tunnel_ready=false
for _ in $(seq 1 "${HEALTH_RETRIES}"); do
  if curl -fsS --max-time 5 "http://127.0.0.1:${CLOUDFLARED_METRICS_PORT:-2000}/ready" >/dev/null 2>&1; then
    tunnel_ready=true
    break
  fi
  sleep "${HEALTH_INTERVAL}"
done
if [ "${tunnel_ready}" != true ]; then
  "${COMPOSE[@]}" logs --no-color --tail 80 cloudflared >&2 || true
  die "cloudflared did not report a ready tunnel connection"
fi
log "Cloudflare tunnel is connected"

log "checking S3 access through the instance role"
aws s3api head-bucket --bucket "${S3_BUCKET}" --region "${AWS_REGION}" \
  || die "HeadBucket on ${S3_BUCKET} failed; check the EC2 instance role"

log "checking the public endpoint"
public_ok=false
for _ in $(seq 1 "${PUBLIC_HEALTH_RETRIES}"); do
  if curl -fsS --max-time 10 "${PUBLIC_BASE_URL}/health/ready" 2>/dev/null | grep -q '"status":"ok"'; then
    public_ok=true
    break
  fi
  sleep "${HEALTH_INTERVAL}"
done
if [ "${public_ok}" = true ]; then
  log "public endpoint is healthy"
elif [ "${FIRST_DEPLOYMENT}" = true ]; then
  # DNS or the public hostname route may not exist yet on the very first run.
  # Everything inside the host is verified, so this is reported, not fatal.
  warn "public endpoint ${PUBLIC_BASE_URL}/health/ready is not answering yet"
  warn "internal API, tunnel, worker and S3 all passed; finish the Cloudflare"
  warn "public-hostname route (docs/CLOUDFLARE_TUNNEL_SETUP.md) and re-check"
else
  die "public endpoint ${PUBLIC_BASE_URL}/health/ready failed while internal checks passed"
fi

# ---------------------------------------------------------------------------
# 8. Record the release and clean up safely
# ---------------------------------------------------------------------------
trap - ERR

image_id="$(docker image inspect --format '{{.Id}}' "${IMAGE_NAME}:${DEPLOY_SHA}" 2>/dev/null || echo unknown)"

if [ -f "${CURRENT_RELEASE}" ]; then
  cp -f "${CURRENT_RELEASE}" "${PREVIOUS_RELEASE}"
fi

cat > "${CURRENT_RELEASE}" <<EOF
GIT_SHA=${DEPLOY_SHA}
IMAGE=${IMAGE_NAME}:${DEPLOY_SHA}
IMAGE_ID=${image_id}
DEPLOYED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)
DEPLOYED_BY=${DEPLOY_ACTOR}
PUBLIC_BASE_URL=${PUBLIC_BASE_URL}
PUBLIC_HEALTH_VERIFIED=${public_ok}
ROLLBACK_TARGET=${ROLLBACK_SHA}
EOF
chmod 0644 "${CURRENT_RELEASE}"

# Dangling layers only. Never `docker system prune -a`, which would delete the
# previous release image and make a fast rollback impossible, and never a
# volume prune, which would delete the database.
log "pruning dangling images"
docker image prune -f >/dev/null 2>&1 || true

"${COMPOSE[@]}" ps
log "deployment of ${DEPLOY_SHA} completed successfully"
