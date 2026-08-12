#!/usr/bin/env bash
# Deploy an immutable backend image on the EC2 host and roll application
# containers back if readiness fails. Schema changes are protected by a backup.
set -euo pipefail

PROJECT_NAME="${PROJECT_NAME:-techsara-chat-archive}"
APP_DIR="${APP_DIR:-/opt/${PROJECT_NAME}}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-180}"
COMPOSE=(docker compose -f compose.prod.yaml)

log() { printf '[deploy] %s\n' "$*"; }
die() { printf '[deploy] ERROR: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root (sudo)"
cd "${APP_DIR}" || die "application directory not found: ${APP_DIR}"

previous_tag="$(sed -n 's/^IMAGE_TAG=//p' .env 2>/dev/null || true)"
"${APP_DIR}/scripts/fetch_ssm_secrets.sh"
current_tag="$(sed -n 's/^IMAGE_TAG=//p' .env)"
[ -n "${current_tag}" ] || die "IMAGE_TAG is empty"
postgres_user="$(sed -n 's/^POSTGRES_USER=//p' .env)"
postgres_db="$(sed -n 's/^POSTGRES_DB=//p' .env)"
archive_hostname="$(sed -n 's/^ARCHIVE_HOSTNAME=//p' .env)"
log "deploying ${current_tag}; previous tag ${previous_tag:-none}"

"${COMPOSE[@]}" config --quiet
"${COMPOSE[@]}" pull postgres migrate api worker backup
"${COMPOSE[@]}" up -d postgres

for _ in $(seq 1 60); do
  "${COMPOSE[@]}" exec -T postgres pg_isready -q && break
  sleep 2
done
"${COMPOSE[@]}" exec -T postgres pg_isready -q || die "PostgreSQL did not become ready"

if "${COMPOSE[@]}" exec -T postgres psql -U "${postgres_user}" \
    -d "${postgres_db}" -tAc \
    "SELECT 1 FROM information_schema.tables WHERE table_name='alembic_version'" \
    | grep -q 1; then
  log "taking pre-migration backup"
  "${COMPOSE[@]}" run --rm --entrypoint /bin/sh backup /opt/scripts/backup_postgres.sh
fi

log "running migrations"
"${COMPOSE[@]}" run --rm migrate
log "starting API, worker and nightly backup"
"${COMPOSE[@]}" up -d --remove-orphans api worker backup

if grep -q '^COMPLIANCE_POLL_ENABLED=true$' .env; then
  "${COMPOSE[@]}" --profile compliance up -d compliance-poller
fi

healthy=0
for _ in $(seq 1 "${HEALTH_TIMEOUT}"); do
  if curl -fkSs -H "Host: ${archive_hostname}" https://127.0.0.1:443/health/ready \
      | grep -q '"status":"ok"'; then
    healthy=1
    break
  fi
  sleep 1
done

if [ "${healthy}" -ne 1 ]; then
  "${COMPOSE[@]}" logs --tail 100 api >&2 || true
  if [ -n "${previous_tag}" ] && [ "${previous_tag}" != "${current_tag}" ]; then
    log "readiness failed; rolling application images back to ${previous_tag}"
    sed -i "s/^IMAGE_TAG=.*/IMAGE_TAG=${previous_tag}/" .env
    "${COMPOSE[@]}" up -d api worker backup
  fi
  die "deployment failed readiness check; database migration was not reversed"
fi

"${COMPOSE[@]}" ps
log "deployment healthy at ${current_tag}"
