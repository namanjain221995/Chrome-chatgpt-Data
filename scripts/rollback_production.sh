#!/usr/bin/env bash
# =============================================================================
# Roll the application back to a previously deployed commit.
#
#   ./scripts/rollback_production.sh <previous_sha>
#   ./scripts/rollback_production.sh            # uses deploy/previous-release
#
# Application only. The database schema is deliberately left at head:
# migrations in this project are written to be backward compatible for one
# release, so the previous application runs against the newer schema. An
# automatic downgrade would risk data loss and is never performed here.
# See docs/ROLLBACK.md.
# =============================================================================
set -Eeuo pipefail
set +x

PROJECT_NAME="${PROJECT_NAME:-techsara-chat-archive}"
APP_DIR="${APP_DIR:-/opt/${PROJECT_NAME}}"
DATA_ROOT="${DATA_ROOT:-/srv/${PROJECT_NAME}}"
ENV_FILE="${ENV_FILE:-${APP_DIR}/.env.production}"
LOCK_FILE="${LOCK_FILE:-/var/lock/${PROJECT_NAME}-deploy.lock}"
LOCK_WAIT_SECONDS="${LOCK_WAIT_SECONDS:-900}"
RELEASE_DIR="${APP_DIR}/deploy"
CURRENT_RELEASE="${RELEASE_DIR}/current-release"
PREVIOUS_RELEASE="${RELEASE_DIR}/previous-release"
HEALTH_RETRIES="${HEALTH_RETRIES:-60}"
HEALTH_INTERVAL="${HEALTH_INTERVAL:-5}"

log()  { printf '[rollback] %s\n' "$*"; }
warn() { printf '[rollback] WARNING: %s\n' "$*" >&2; }
die()  { printf '[rollback] ERROR: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root (sudo $0 <previous_sha>)"
cd "${APP_DIR}" || die "application directory not found: ${APP_DIR}"
[ -d "${APP_DIR}/.git" ] || die "${APP_DIR} is not a git repository"

# Reading a release record must tolerate its absence: the first deployment on a
# host has no current-release file, and `sed` exits 2 on a missing file, which
# `set -o pipefail` would turn into an aborted deployment.
release_value() {
  local key="$1" file="$2"
  [ -f "${file}" ] || return 0
  sed -n "s/^${key}=//p" "${file}" | tail -n 1
}

TARGET_SHA="${1:-}"
if [ -z "${TARGET_SHA}" ]; then
  TARGET_SHA="$(release_value GIT_SHA "${PREVIOUS_RELEASE}")"
  [ -n "${TARGET_SHA}" ] || die "no target given and ${PREVIOUS_RELEASE} has no GIT_SHA"
  log "no target supplied; using ${PREVIOUS_RELEASE}"
fi
[[ "${TARGET_SHA}" =~ ^[0-9a-f]{40}$ ]] || die "target must be a full 40-character commit SHA"

# When invoked from deploy_production.sh the lock is already held by the parent
# process, which owns this file descriptor; take it only when standalone.
if [ "${ROLLBACK_HAS_LOCK:-false}" != "true" ]; then
  exec 9>"${LOCK_FILE}"
  flock -w "${LOCK_WAIT_SECONDS}" 9 || die "another deployment holds ${LOCK_FILE}"
fi

git config --global --add safe.directory "${APP_DIR}" 2>/dev/null || true
git cat-file -e "${TARGET_SHA}^{commit}" 2>/dev/null \
  || die "commit ${TARGET_SHA} is not present in the local repository"

log "restoring application code to ${TARGET_SHA}"
git reset --hard "${TARGET_SHA}"
chmod +x "${APP_DIR}/scripts/"*.sh

log "re-rendering configuration for ${TARGET_SHA}"
IMAGE_TAG="${TARGET_SHA}" "${APP_DIR}/scripts/fetch_ssm_secrets.sh"

COMPOSE=(docker compose --env-file "${ENV_FILE}" -f compose.prod.yaml)
env_value() { sed -n "s/^$1=//p" "${ENV_FILE}" | tail -n 1; }

IMAGE_NAME="$(env_value IMAGE_NAME)"
ARCHIVE_HOSTNAME="$(env_value ARCHIVE_HOSTNAME)"
PUBLIC_BASE_URL="$(env_value PUBLIC_BASE_URL)"
CLOUDFLARED_METRICS_PORT="$(env_value CLOUDFLARED_METRICS_PORT)"
COMPLIANCE_POLL_ENABLED="$(env_value COMPLIANCE_POLL_ENABLED)"

# Roll back into the same ingress posture the host is already in. A host
# deployed with --without-tunnel has no tunnel token, so starting cloudflared
# here would fail a rollback that is otherwise fine.
PUBLIC_INGRESS="$(release_value PUBLIC_INGRESS "${CURRENT_RELEASE}")"
PUBLIC_INGRESS="${PUBLIC_INGRESS:-cloudflare-tunnel}"
if [ ! -s "${DATA_ROOT}/secrets/cloudflared.env" ]; then
  PUBLIC_INGRESS=none
fi
[ "${PUBLIC_INGRESS}" = "cloudflare-tunnel" ] \
  || warn "rolling back without public ingress: no Cloudflare tunnel on this host"

"${COMPOSE[@]}" config --quiet

# The previous image is normally still on the host, because deployments prune
# only dangling layers. Rebuild it if it has been cleaned up.
if docker image inspect "${IMAGE_NAME}:${TARGET_SHA}" >/dev/null 2>&1; then
  log "reusing the existing image ${IMAGE_NAME}:${TARGET_SHA}"
else
  warn "image ${IMAGE_NAME}:${TARGET_SHA} is not on this host; rebuilding it"
  "${COMPOSE[@]}" build api
fi

log "recreating application services at ${TARGET_SHA}"
if [ "${PUBLIC_INGRESS}" = "cloudflare-tunnel" ]; then
  "${COMPOSE[@]}" up -d --no-build --remove-orphans api worker backup cloudflared
else
  "${COMPOSE[@]}" up -d --no-build --remove-orphans api worker backup
fi
if [ "${COMPLIANCE_POLL_ENABLED}" = "true" ]; then
  "${COMPOSE[@]}" --profile compliance up -d --no-build compliance-poller
fi

log "verifying the restored release"
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

tunnel_ready=false
if [ "${PUBLIC_INGRESS}" = "cloudflare-tunnel" ]; then
  for _ in $(seq 1 20); do
    if curl -fsS --max-time 5 "http://127.0.0.1:${CLOUDFLARED_METRICS_PORT:-2000}/ready" >/dev/null 2>&1; then
      tunnel_ready=true
      break
    fi
    sleep "${HEALTH_INTERVAL}"
  done
fi

# Record the outcome whether or not it worked, so the next operator can see
# exactly what state the host is in.
if [ -f "${CURRENT_RELEASE}" ]; then
  cp -f "${CURRENT_RELEASE}" "${PREVIOUS_RELEASE}"
fi
cat > "${CURRENT_RELEASE}" <<EOF
GIT_SHA=${TARGET_SHA}
IMAGE=${IMAGE_NAME}:${TARGET_SHA}
IMAGE_ID=$(docker image inspect --format '{{.Id}}' "${IMAGE_NAME}:${TARGET_SHA}" 2>/dev/null || echo unknown)
DEPLOYED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)
DEPLOYED_BY=rollback
PUBLIC_BASE_URL=${PUBLIC_BASE_URL}
ROLLED_BACK=true
PUBLIC_INGRESS=${PUBLIC_INGRESS}
API_HEALTHY=${api_ready}
TUNNEL_HEALTHY=${tunnel_ready}
EOF
chmod 0644 "${CURRENT_RELEASE}"

if [ "${api_ready}" != true ]; then
  "${COMPOSE[@]}" logs --no-color --tail 120 api >&2 || true
  die "rollback to ${TARGET_SHA} did not become healthy; manual intervention required"
fi
if [ "${PUBLIC_INGRESS}" = "cloudflare-tunnel" ] && [ "${tunnel_ready}" != true ]; then
  "${COMPOSE[@]}" logs --no-color --tail 80 cloudflared >&2 || true
  die "rollback API is healthy but the Cloudflare tunnel is not connected"
fi

"${COMPOSE[@]}" ps
log "rollback to ${TARGET_SHA} completed; database schema left at head"
