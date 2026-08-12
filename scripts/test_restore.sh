#!/usr/bin/env bash
# =============================================================================
# Backup/restore proof.
#
#   scripts/test_restore.sh                 local: dump the dev database and
#                                           restore it into a fresh database
#   scripts/test_restore.sh --from-s3-latest  production host: restore the most
#                                           recent S3 backup into a throwaway
#                                           database and drop it again
#
# This is the check that turns "we take backups" into "we have restored one
# today". Neither mode ever writes to the live database: the restore target is
# always a new database name, and the S3 mode uses restore_postgres.sh
# --verify-only, which refuses to touch the production database.
# =============================================================================
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

MODE=local
while [ $# -gt 0 ]; do
  case "$1" in
    --local) MODE=local; shift ;;
    --from-s3-latest) MODE=s3; shift ;;
    --help|-h)
      sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [ "${MODE}" = "s3" ]; then
  # Runs on the EC2 host, inside the backup container, which already has the
  # instance role, pg_restore and the .pgpass file.
  PROJECT_NAME="${PROJECT_NAME:-techsara-chat-archive}"
  APP_DIR="${APP_DIR:-/opt/${PROJECT_NAME}}"
  ENV_FILE="${ENV_FILE:-${APP_DIR}/.env.production}"
  [ -f "${ENV_FILE}" ] || { echo "${ENV_FILE} not found" >&2; exit 1; }
  cd "${APP_DIR}"

  bucket="$(sed -n 's/^S3_BUCKET=//p' "${ENV_FILE}" | tail -1)"
  region="$(sed -n 's/^AWS_REGION=//p' "${ENV_FILE}" | tail -1)"
  echo "==> finding the most recent backup in s3://${bucket}/backups/postgres/"
  latest="$(aws s3 ls "s3://${bucket}/backups/postgres/" --recursive --region "${region}" \
    | sort -k1,2 | tail -1 | awk '{print $4}')"
  [ -n "${latest}" ] || { echo "no backup objects found" >&2; exit 1; }
  echo "    ${latest}"

  echo "==> restoring into a throwaway database and dropping it again"
  docker compose --env-file "${ENV_FILE}" -f compose.prod.yaml run --rm --no-deps \
    --entrypoint /bin/sh backup -c \
    "cp \"\$PGPASS_SOURCE_FILE\" \"\$PGPASSFILE\"; chmod 0600 \"\$PGPASSFILE\";
     exec sh /opt/scripts/restore_postgres.sh --from-s3 '${latest}' \
       --target-db techsara_restore_test --drop-existing --verify-only"
  echo "S3 restore verification passed for ${latest}"
  exit 0
fi

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
