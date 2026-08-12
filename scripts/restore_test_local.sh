#!/usr/bin/env bash
# =============================================================================
# Local backup/restore proof.
#
# Backs up the running development database, restores it into a brand-new
# database, and compares row counts. This is the check that turns "we take
# backups" into "we have restored one today".
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

CONTAINER="${TEST_PG_CONTAINER:-techsara-test-pg}"
DB="${POSTGRES_DB:-techsara_chat_archive}"
USER="${POSTGRES_USER:-techsara_app}"
PASSWORD="${POSTGRES_PASSWORD:-devonly_change_me}"
RESTORE_DB="techsara_restore_check_$$"
DUMP="/tmp/techsara-restore-test-$$.dump"

green() { printf '\033[32m%s\033[0m\n' "$*"; }
red() { printf '\033[31m%s\033[0m\n' "$*"; }

cleanup() {
  rm -f "${DUMP}"
  docker exec -e PGPASSWORD="${PASSWORD}" "${CONTAINER}" \
    psql -U "${USER}" -d postgres -q -c "DROP DATABASE IF EXISTS ${RESTORE_DB};" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker ps --format '{{.Names}}' | grep -q "^${CONTAINER}$" || {
  red "container ${CONTAINER} is not running; run: make test-db-up"
  exit 1
}

echo "==> counting rows in the live database"
counts_before="$(docker exec -e PGPASSWORD="${PASSWORD}" "${CONTAINER}" psql -U "${USER}" -d "${DB}" -tAc "
  SELECT (SELECT count(*) FROM conversations) || ':' ||
         (SELECT count(*) FROM messages) || ':' ||
         (SELECT count(*) FROM message_versions) || ':' ||
         (SELECT count(*) FROM capture_events);" | tr -d ' \r')"
echo "    conversations:messages:versions:capture_events = ${counts_before}"

echo "==> taking a pg_dump backup"
docker exec -e PGPASSWORD="${PASSWORD}" "${CONTAINER}" \
  pg_dump -U "${USER}" -d "${DB}" -Fc --no-owner --no-privileges > "${DUMP}"
size="$(wc -c < "${DUMP}")"
[ "${size}" -gt 1024 ] || { red "backup is suspiciously small (${size} bytes)"; exit 1; }
checksum="$(sha256sum "${DUMP}" | awk '{print $1}')"
echo "    ${size} bytes, sha256 ${checksum}"

echo "==> restoring into a clean database ${RESTORE_DB}"
docker exec -e PGPASSWORD="${PASSWORD}" "${CONTAINER}" \
  psql -U "${USER}" -d postgres -q -c "CREATE DATABASE ${RESTORE_DB};"
docker exec -i -e PGPASSWORD="${PASSWORD}" "${CONTAINER}" \
  pg_restore -U "${USER}" -d "${RESTORE_DB}" --no-owner --no-privileges < "${DUMP}" >/dev/null 2>&1 || true

echo "==> verifying the restored database"
tables="$(docker exec -e PGPASSWORD="${PASSWORD}" "${CONTAINER}" psql -U "${USER}" -d "${RESTORE_DB}" -tAc \
  "SELECT count(*) FROM information_schema.tables WHERE table_schema='public';" | tr -d ' \r')"
counts_after="$(docker exec -e PGPASSWORD="${PASSWORD}" "${CONTAINER}" psql -U "${USER}" -d "${RESTORE_DB}" -tAc "
  SELECT (SELECT count(*) FROM conversations) || ':' ||
         (SELECT count(*) FROM messages) || ':' ||
         (SELECT count(*) FROM message_versions) || ':' ||
         (SELECT count(*) FROM capture_events);" | tr -d ' \r')"
revision="$(docker exec -e PGPASSWORD="${PASSWORD}" "${CONTAINER}" psql -U "${USER}" -d "${RESTORE_DB}" -tAc \
  "SELECT version_num FROM alembic_version;" | tr -d ' \r')"

echo "    tables:   ${tables}"
echo "    counts:   ${counts_after}"
echo "    revision: ${revision}"

[ "${tables}" -ge 25 ] || { red "restored schema has only ${tables} tables"; exit 1; }
[ "${counts_before}" = "${counts_after}" ] || {
  red "row counts differ: before=${counts_before} after=${counts_after}"
  exit 1
}

green "restore test passed: ${tables} tables, counts match, revision ${revision}"
