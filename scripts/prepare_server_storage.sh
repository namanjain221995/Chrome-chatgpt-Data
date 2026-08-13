#!/usr/bin/env bash
# =============================================================================
# Create the persistent directory layout on the EC2 host.
#
# Safe to run repeatedly: it only creates what is missing and re-asserts
# ownership and permissions. It never touches the contents of the PostgreSQL
# data directory or the backup staging area.
#
#   /opt/techsara-chat-archive          application source (git checkout)
#   /srv/techsara-chat-archive/postgres PostgreSQL data (never deleted here)
#   /srv/techsara-chat-archive/backups  local backup staging before S3 upload
#   /srv/techsara-chat-archive/secrets  root-owned secret files from SSM
#   /srv/techsara-chat-archive/pgadmin  optional admin-profile state
# =============================================================================
set -Eeuo pipefail

PROJECT_NAME="${PROJECT_NAME:-techsara-chat-archive}"
APP_DIR="${APP_DIR:-/opt/${PROJECT_NAME}}"
DATA_ROOT="${DATA_ROOT:-/srv/${PROJECT_NAME}}"

# The application image runs as uid/gid 10001; the PostgreSQL alpine image runs
# as gid 70. Secret files are root-owned and group-readable by exactly the
# service that needs them.
APP_GID=10001
POSTGRES_GID=70
PGADMIN_UID=5050

log() { printf '[storage] %s\n' "$*"; }
die() { printf '[storage] ERROR: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root (sudo $0)"

log "application directory ${APP_DIR}"
install -d -o root -g root -m 0755 "${APP_DIR}"

log "data root ${DATA_ROOT}"
install -d -o root -g root -m 0755 "${DATA_ROOT}"

# PostgreSQL manages its own permissions inside the data directory; create it
# only if absent so an existing cluster is never disturbed.
if [ -d "${DATA_ROOT}/postgres" ]; then
  log "postgres data directory already exists; leaving it untouched"
else
  log "creating postgres data directory"
  install -d -o "${POSTGRES_GID}" -g "${POSTGRES_GID}" -m 0700 "${DATA_ROOT}/postgres"
fi

log "backup staging directory"
install -d -o "${APP_GID}" -g "${APP_GID}" -m 0750 "${DATA_ROOT}/backups"

log "secrets directory"
install -d -o root -g "${APP_GID}" -m 0750 "${DATA_ROOT}/secrets"

log "pgadmin state directory (optional admin profile)"
install -d -o "${PGADMIN_UID}" -g root -m 0750 "${DATA_ROOT}/pgadmin"

# Deployment metadata lives beside the checkout and must survive `git reset`.
if [ -d "${APP_DIR}/.git" ]; then
  install -d -o root -g root -m 0755 "${APP_DIR}/deploy"
fi

log "deployment lock directory"
install -d -o root -g root -m 0755 /var/lock

df -h "${DATA_ROOT}" | tail -n +1
log "storage layout ready"
