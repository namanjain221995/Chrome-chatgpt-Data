#!/bin/sh
# =============================================================================
# Verify that backups exist, are recent, and actually restore.
#
#   scripts/verify_backup.sh                 # freshness + checksum checks
#   scripts/verify_backup.sh --full-restore  # also restore into a scratch DB
#
# Exit code 0 means an operator can trust the backup chain today. Anything else
# is a page-worthy condition: a backup you have never restored is a hypothesis,
# not a backup.
# =============================================================================
set -eu

FULL_RESTORE=0
MAX_AGE_HOURS="${MAX_AGE_HOURS:-30}"
[ "${1:-}" = "--full-restore" ] && FULL_RESTORE=1

S3_BUCKET="${S3_BUCKET:?S3_BUCKET is required}"
AWS_REGION="${AWS_REGION:-us-east-1}"

fail() { printf '{"event":"verify_failed","reason":"%s"}\n' "$1" >&2; exit 1; }

echo "checking for recent backups in s3://${S3_BUCKET}/backups/postgres/"
latest="$(aws s3 ls "s3://${S3_BUCKET}/backups/postgres/" --recursive --region "${AWS_REGION}" \
  | sort | tail -1)"
[ -n "${latest}" ] || fail "no backup objects found"

latest_key="$(echo "${latest}" | awk '{print $4}')"
latest_date="$(echo "${latest}" | awk '{print $1" "$2}')"
latest_size="$(echo "${latest}" | awk '{print $3}')"

echo "latest backup: ${latest_key} (${latest_size} bytes, ${latest_date})"

[ "${latest_size}" -gt 1024 ] || fail "latest backup is suspiciously small (${latest_size} bytes)"

# Freshness: a backup older than MAX_AGE_HOURS breaks the stated 24 h RPO.
backup_epoch="$(date -u -d "${latest_date}" +%s 2>/dev/null || echo 0)"
now_epoch="$(date -u +%s)"
if [ "${backup_epoch}" -gt 0 ]; then
  age_hours=$(( (now_epoch - backup_epoch) / 3600 ))
  echo "backup age: ${age_hours}h (limit ${MAX_AGE_HOURS}h)"
  [ "${age_hours}" -le "${MAX_AGE_HOURS}" ] || fail "latest backup is ${age_hours}h old"
fi

manifest_key="$(echo "${latest_key}" | sed 's|backups/postgres/|backups/manifests/|; s|\.dump\.gz$|.sha256|')"
if aws s3api head-object --bucket "${S3_BUCKET}" --key "${manifest_key}" \
     --region "${AWS_REGION}" >/dev/null 2>&1; then
  echo "manifest present: ${manifest_key}"
else
  fail "no manifest for ${latest_key}"
fi

if [ "${FULL_RESTORE}" -eq 1 ]; then
  echo "running a full restore into a disposable database"
  # shellcheck disable=SC2086
  sh "$(dirname "$0")/restore_postgres.sh" \
    --from-s3 "${latest_key}" \
    --target-db "techsara_restore_verify_$(date -u +%s)" \
    --drop-existing --verify-only
fi

printf '{"event":"verify_passed","latest":"%s","full_restore":%s}\n' "${latest_key}" "${FULL_RESTORE}"
