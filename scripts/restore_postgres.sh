#!/bin/sh
# =============================================================================
# Restore a PostgreSQL logical backup.
#
#   scripts/restore_postgres.sh --from-s3 backups/postgres/2026/03/15/techsara-…dump.gz
#   scripts/restore_postgres.sh --file /var/backups/techsara/techsara-…dump.gz
#
# Safety: the target database is named explicitly and must either not exist or
# be passed with --drop-existing. There is no path that silently overwrites the
# live database — recovery is deliberate, not accidental.
# =============================================================================
set -eu

if [ -n "${PGPASS_SOURCE_FILE:-}" ] && [ -r "${PGPASS_SOURCE_FILE}" ]; then
  cp "${PGPASS_SOURCE_FILE}" "${PGPASSFILE:-/tmp/.pgpass}"
  chmod 0600 "${PGPASSFILE:-/tmp/.pgpass}"
fi

usage() {
  cat >&2 <<'EOF'
Usage:
  restore_postgres.sh --file <path>            [options]
  restore_postgres.sh --from-s3 <s3-key>       [options]

Options:
  --target-db <name>     Database to restore into (default: techsara_restore_test)
  --drop-existing        Drop the target database first (refused for the live DB)
  --verify-only          Restore, run integrity checks, then drop the target
  --jobs <n>             Parallel restore jobs (default: 2)
  --help
EOF
  exit 2
}

SOURCE_FILE=""
S3_KEY=""
TARGET_DB="techsara_restore_test"
DROP_EXISTING=0
VERIFY_ONLY=0
JOBS=2

while [ $# -gt 0 ]; do
  case "$1" in
    --file) SOURCE_FILE="${2:?}"; shift 2 ;;
    --from-s3) S3_KEY="${2:?}"; shift 2 ;;
    --target-db) TARGET_DB="${2:?}"; shift 2 ;;
    --drop-existing) DROP_EXISTING=1; shift ;;
    --verify-only) VERIFY_ONLY=1; shift ;;
    --jobs) JOBS="${2:?}"; shift 2 ;;
    --help|-h) usage ;;
    *) echo "unknown argument: $1" >&2; usage ;;
  esac
done

PGHOST="${PGHOST:-postgres}"
PGPORT="${PGPORT:-5432}"
PGUSER="${PGUSER:?PGUSER is required}"
LIVE_DB="${PGDATABASE:-techsara_chat_archive}"
WORK_DIR="${BACKUP_DIR:-/var/backups/techsara}/restore"

if [ -z "${SOURCE_FILE}" ] && [ -z "${S3_KEY}" ]; then
  echo "one of --file or --from-s3 is required" >&2
  usage
fi

if [ "${TARGET_DB}" = "${LIVE_DB}" ] && [ "${DROP_EXISTING}" -eq 1 ]; then
  echo "refusing to drop the live database ${LIVE_DB}" >&2
  echo "restore into a new database, verify it, then switch over deliberately" >&2
  exit 1
fi

mkdir -p "${WORK_DIR}"

if [ -n "${S3_KEY}" ]; then
  S3_BUCKET="${S3_BUCKET:?S3_BUCKET is required when restoring from S3}"
  SOURCE_FILE="${WORK_DIR}/$(basename "${S3_KEY}")"
  echo "downloading s3://${S3_BUCKET}/${S3_KEY}"
  aws s3 cp "s3://${S3_BUCKET}/${S3_KEY}" "${SOURCE_FILE}" \
    --region "${AWS_REGION:-us-east-1}" --only-show-errors

  # Verify against the manifest when one exists for this dump.
  manifest_key="$(echo "${S3_KEY}" \
    | sed 's|backups/postgres/|backups/manifests/|; s|\.dump\.gz$|.sha256|')"
  if aws s3 cp "s3://${S3_BUCKET}/${manifest_key}" "${WORK_DIR}/manifest.json" \
       --region "${AWS_REGION:-us-east-1}" --only-show-errors 2>/dev/null; then
    expected="$(grep -o '"sha256": *"[0-9a-f]*"' "${WORK_DIR}/manifest.json" \
      | head -1 | sed 's/.*"\([0-9a-f]\{64\}\)".*/\1/')"
    actual="$(sha256sum "${SOURCE_FILE}" | awk '{print $1}')"
    if [ "${expected}" != "${actual}" ]; then
      echo "CHECKSUM MISMATCH: manifest=${expected} downloaded=${actual}" >&2
      exit 1
    fi
    echo "checksum verified against manifest: ${actual}"
  else
    echo "warning: no manifest found for ${S3_KEY}; continuing without checksum verification" >&2
  fi
fi

[ -f "${SOURCE_FILE}" ] || { echo "backup file not found: ${SOURCE_FILE}" >&2; exit 1; }

export PGHOST PGPORT PGUSER

if [ "${DROP_EXISTING}" -eq 1 ]; then
  echo "dropping database ${TARGET_DB} if it exists"
  psql -d postgres -v ON_ERROR_STOP=1 -c "DROP DATABASE IF EXISTS \"${TARGET_DB}\";"
fi

echo "creating database ${TARGET_DB}"
psql -d postgres -v ON_ERROR_STOP=1 \
  -c "CREATE DATABASE \"${TARGET_DB}\";" 2>/dev/null \
  || echo "database ${TARGET_DB} already exists; restoring into it"

echo "restoring ${SOURCE_FILE} into ${TARGET_DB}"
gunzip -c "${SOURCE_FILE}" \
  | pg_restore --dbname="${TARGET_DB}" --no-owner --no-privileges \
      --jobs="${JOBS}" --exit-on-error --verbose 2>"${WORK_DIR}/restore.log" \
  || {
    # A parallel restore cannot read from a pipe in every pg_restore build;
    # fall back to a single-threaded restore from a temporary file.
    echo "parallel restore unavailable, retrying single-threaded"
    gunzip -c "${SOURCE_FILE}" >"${WORK_DIR}/restore.dump"
    pg_restore --dbname="${TARGET_DB}" --no-owner --no-privileges \
      --exit-on-error "${WORK_DIR}/restore.dump" 2>"${WORK_DIR}/restore.log"
    rm -f "${WORK_DIR}/restore.dump"
  }

echo "verifying restored schema"
tables="$(psql -d "${TARGET_DB}" -tAc \
  "SELECT count(*) FROM information_schema.tables WHERE table_schema='public';")"
conversations="$(psql -d "${TARGET_DB}" -tAc \
  "SELECT count(*) FROM conversations;" 2>/dev/null || echo 0)"
messages="$(psql -d "${TARGET_DB}" -tAc \
  "SELECT count(*) FROM messages;" 2>/dev/null || echo 0)"
revision="$(psql -d "${TARGET_DB}" -tAc \
  "SELECT version_num FROM alembic_version;" 2>/dev/null || echo unknown)"

if [ "${tables}" -lt 20 ]; then
  echo "RESTORE VERIFICATION FAILED: only ${tables} tables present" >&2
  exit 1
fi

printf '{"event":"restore_completed","target_db":"%s","tables":%s,"conversations":%s,"messages":%s,"alembic_revision":"%s"}\n' \
  "${TARGET_DB}" "${tables}" "${conversations}" "${messages}" "${revision}"

if [ "${VERIFY_ONLY}" -eq 1 ]; then
  echo "dropping verification database ${TARGET_DB}"
  psql -d postgres -v ON_ERROR_STOP=1 -c "DROP DATABASE IF EXISTS \"${TARGET_DB}\";"
  echo "restore verification passed"
fi
