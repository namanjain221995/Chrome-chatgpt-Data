#!/bin/sh
# =============================================================================
# PostgreSQL logical backup -> gzip -> SHA-256 manifest -> S3.
#
# Runs inside the `backup` container, which shares the application image (so it
# has a matching pg_dump) and the instance profile (so it can write to S3
# without any stored credential).
#
# The manifest is uploaded only after the dump object, and the local copy is
# removed only after both succeed: a failure therefore leaves evidence rather
# than a silent gap.
# =============================================================================
set -eu

BACKUP_DIR="${BACKUP_DIR:-/var/backups/techsara}"
S3_BUCKET="${S3_BUCKET:?S3_BUCKET is required}"
AWS_REGION="${AWS_REGION:-us-east-1}"
PGHOST="${PGHOST:-postgres}"
PGPORT="${PGPORT:-5432}"
PGUSER="${PGUSER:?PGUSER is required}"
PGDATABASE="${PGDATABASE:?PGDATABASE is required}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-90}"
APP_NAME="${APP_NAME:-techsara-chat-archive}"

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
day_path="$(date -u +%Y/%m/%d)"
dump_name="techsara-${timestamp}.dump.gz"
manifest_name="techsara-${timestamp}.sha256"
dump_path="${BACKUP_DIR}/${dump_name}"
manifest_path="${BACKUP_DIR}/${manifest_name}"

log() {
  printf '{"event":"%s","ts":"%s","backup":"%s"}\n' "$1" "$(date -u +%FT%TZ)" "${dump_name}"
}

mkdir -p "${BACKUP_DIR}"

log backup_started

# --format=custom keeps parallel restore and selective restore available;
# gzip on top is what the retention tiers and the S3 lifecycle rules expect.
if ! pg_dump \
  --host="${PGHOST}" \
  --port="${PGPORT}" \
  --username="${PGUSER}" \
  --dbname="${PGDATABASE}" \
  --format=custom \
  --compress=0 \
  --no-owner \
  --no-privileges \
  --verbose 2>"${BACKUP_DIR}/last-dump.log" | gzip -9 >"${dump_path}"; then
  log backup_dump_failed
  tail -20 "${BACKUP_DIR}/last-dump.log" >&2
  exit 1
fi

if [ ! -s "${dump_path}" ]; then
  log backup_empty
  exit 1
fi

size_bytes="$(wc -c <"${dump_path}" | tr -d ' ')"
checksum="$(sha256sum "${dump_path}" | awk '{print $1}')"

cat >"${manifest_path}" <<EOF
{
  "app": "${APP_NAME}",
  "database": "${PGDATABASE}",
  "created_at": "$(date -u +%FT%TZ)",
  "dump_object": "backups/postgres/${day_path}/${dump_name}",
  "sha256": "${checksum}",
  "size_bytes": ${size_bytes},
  "format": "pg_dump custom, gzip -9",
  "pg_dump_version": "$(pg_dump --version | head -1)"
}
EOF

log backup_uploading

aws s3 cp "${dump_path}" \
  "s3://${S3_BUCKET}/backups/postgres/${day_path}/${dump_name}" \
  --region "${AWS_REGION}" \
  --sse AES256 \
  --only-show-errors

aws s3 cp "${manifest_path}" \
  "s3://${S3_BUCKET}/backups/manifests/${day_path}/${manifest_name}" \
  --region "${AWS_REGION}" \
  --sse AES256 \
  --only-show-errors

# Verify the uploaded object is the size we produced before trusting it.
remote_size="$(aws s3api head-object \
  --bucket "${S3_BUCKET}" \
  --key "backups/postgres/${day_path}/${dump_name}" \
  --region "${AWS_REGION}" \
  --query 'ContentLength' --output text 2>/dev/null || echo 0)"

if [ "${remote_size}" != "${size_bytes}" ]; then
  log backup_upload_size_mismatch
  echo "local=${size_bytes} remote=${remote_size}" >&2
  exit 1
fi

# Local copies are a convenience for a fast restore; S3 is the durable store.
find "${BACKUP_DIR}" -name 'techsara-*.dump.gz' -mtime +2 -delete 2>/dev/null || true
find "${BACKUP_DIR}" -name 'techsara-*.sha256' -mtime +2 -delete 2>/dev/null || true

printf '{"event":"backup_completed","ts":"%s","object":"backups/postgres/%s/%s","sha256":"%s","bytes":%s,"retention_days":%s}\n' \
  "$(date -u +%FT%TZ)" "${day_path}" "${dump_name}" "${checksum}" "${size_bytes}" "${RETENTION_DAYS}"
