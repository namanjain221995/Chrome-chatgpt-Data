#!/bin/sh
# Scheduled backup loop for the `backup` compose service.
#
# A plain loop rather than cron: it inherits the container environment and the
# instance profile directly, logs to the container's stdout like every other
# service, and stops cleanly when compose sends SIGTERM.
set -eu

INTERVAL="${BACKUP_INTERVAL_SECONDS:-86400}"
SCRIPT_DIR="$(dirname "$0")"

term() {
  echo '{"event":"backup_loop_stopping"}'
  exit 0
}
trap term TERM INT

echo "{\"event\":\"backup_loop_started\",\"interval_seconds\":${INTERVAL}}"

# Wait for PostgreSQL to accept connections before the first attempt.
until pg_isready -h "${PGHOST:-postgres}" -U "${PGUSER:-techsara_app}" >/dev/null 2>&1; do
  sleep 5
done

while true; do
  if sh "${SCRIPT_DIR}/backup_postgres.sh"; then
    :
  else
    echo '{"event":"backup_failed","action":"will retry next cycle"}' >&2
  fi
  sleep "${INTERVAL}" &
  wait $!
done
