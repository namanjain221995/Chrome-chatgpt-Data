#!/usr/bin/env bash
# Render production configuration and root-owned secret files from SSM.
set -euo pipefail

PROJECT_NAME="${PROJECT_NAME:-techsara-chat-archive}"
APP_DIR="${APP_DIR:-/opt/${PROJECT_NAME}}"
DATA_ROOT="${DATA_ROOT:-/srv/${PROJECT_NAME}}"
AWS_REGION="${AWS_REGION:-us-east-1}"
PARAM_PREFIX="${SSM_PARAMETER_PREFIX:-/${PROJECT_NAME}}"

[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)" >&2; exit 1; }
command -v aws >/dev/null || { echo "aws CLI is required" >&2; exit 1; }
mkdir -p "${APP_DIR}" "${DATA_ROOT}/secrets"
chown root:10001 "${DATA_ROOT}/secrets"
chmod 0750 "${DATA_ROOT}/secrets"

get_parameter() {
  aws ssm get-parameter --name "${PARAM_PREFIX}/$1" --with-decryption \
    --region "${AWS_REGION}" --query 'Parameter.Value' --output text 2>/dev/null
}

required_parameter() {
  local value
  value="$(get_parameter "$1")" || {
    echo "required SSM parameter missing: ${PARAM_PREFIX}/$1" >&2
    exit 1
  }
  [ -n "${value}" ] || { echo "SSM parameter is empty: ${PARAM_PREFIX}/$1" >&2; exit 1; }
  printf '%s' "${value}"
}

optional_parameter() {
  get_parameter "$1" || true
}

write_secret() {
  local name="$1" value="$2" required="${3:-true}" service_gid="${4:-10001}"
  if [ "${required}" = "true" ] && [ -z "${value}" ]; then
    echo "required secret is empty: ${PARAM_PREFIX}/${name}" >&2
    exit 1
  fi
  umask 077
  printf '%s' "${value}" > "${DATA_ROOT}/secrets/${name}"
  chown root:"${service_gid}" "${DATA_ROOT}/secrets/${name}"
  chmod 0440 "${DATA_ROOT}/secrets/${name}"
}

postgres_password="$(required_parameter postgres_password)"
postgres_user="$(required_parameter postgres_user)"
write_secret postgres_password "${postgres_password}"
write_secret postgres_server_password "${postgres_password}" true 70
write_secret jwt_secret "$(required_parameter jwt_secret)"
write_secret config_signing_key "$(required_parameter config_signing_key)"
write_secret pgadmin_password "$(required_parameter pgadmin_password)" true 5050
write_secret oidc_client_secret "$(optional_parameter oidc_client_secret)" false
write_secret openai_compliance_api_key "$(optional_parameter openai_compliance_api_key)" false
pgpass_password="${postgres_password//\\/\\\\}"
pgpass_password="${pgpass_password//:/\\:}"
printf 'postgres:5432:*:%s:%s\n' "${postgres_user}" "${pgpass_password}" \
  > "${DATA_ROOT}/secrets/pgpass"
chown root:10001 "${DATA_ROOT}/secrets/pgpass"
chmod 0440 "${DATA_ROOT}/secrets/pgpass"

umask 077
cat > "${APP_DIR}/.env" <<EOF
# Generated from ${PARAM_PREFIX}; do not edit or commit.
ENVIRONMENT=production
AWS_REGION=us-east-1
S3_BUCKET=techsara-chatgpt
S3_ENDPOINT_URL=
S3_USE_PATH_STYLE=false
DATA_ROOT=${DATA_ROOT}
IMAGE_REPOSITORY=$(required_parameter image_repository)
IMAGE_TAG=${IMAGE_TAG:-$(required_parameter image_tag)}
PUBLIC_BASE_URL=$(required_parameter public_base_url)
ARCHIVE_HOSTNAME=$(required_parameter archive_hostname)
POSTGRES_DB=$(required_parameter postgres_db)
POSTGRES_USER=${postgres_user}
ALLOWED_EMAIL_DOMAINS=$(required_parameter allowed_email_domains)
OIDC_ISSUER=$(required_parameter oidc_issuer)
OIDC_CLIENT_ID=$(required_parameter oidc_client_id)
OIDC_REQUIRED_HD=$(required_parameter oidc_required_hd)
EXTENSION_IDS=$(optional_parameter extension_ids)
ADMIN_ORIGINS=$(optional_parameter admin_origins)
MANAGED_WORKSPACE_LABEL=$(required_parameter managed_workspace_label)
MANAGED_WORKSPACE_IDS=$(optional_parameter managed_workspace_ids)
BROWSER_CONTENT_CAPTURE_ENABLED=$(optional_parameter browser_content_capture_enabled)
OPENAI_WRITTEN_AUTHORIZATION_CONFIRMED=$(optional_parameter openai_written_authorization_confirmed)
TRAINING_EXPORT_ENABLED=$(optional_parameter training_export_enabled)
COMPLIANCE_POLL_ENABLED=$(optional_parameter compliance_poll_enabled)
OPENAI_COMPLIANCE_BASE_URL=$(optional_parameter openai_compliance_base_url)
OPENAI_COMPLIANCE_LOG_PATH=$(optional_parameter openai_compliance_log_path)
OPENAI_COMPLIANCE_FILES_PATH=$(optional_parameter openai_compliance_files_path)
PGADMIN_DEFAULT_EMAIL=$(required_parameter pgadmin_email)
S3_ENCRYPTION_MODE=$(optional_parameter s3_encryption_mode)
S3_KMS_KEY_ID=$(optional_parameter s3_kms_key_id)
API_WORKERS=$(optional_parameter api_workers)
WORKER_CONCURRENCY=$(optional_parameter worker_concurrency)
BACKUP_RETENTION_DAYS=$(optional_parameter backup_retention_days)
RAW_RETENTION_DAYS=$(optional_parameter raw_retention_days)
EOF
chmod 0600 "${APP_DIR}/.env"

# Empty optional booleans are replaced with safe defaults without exposing any
# secret value.
sed -i \
  -e 's/^BROWSER_CONTENT_CAPTURE_ENABLED=$/BROWSER_CONTENT_CAPTURE_ENABLED=false/' \
  -e 's/^OPENAI_WRITTEN_AUTHORIZATION_CONFIRMED=$/OPENAI_WRITTEN_AUTHORIZATION_CONFIRMED=false/' \
  -e 's/^TRAINING_EXPORT_ENABLED=$/TRAINING_EXPORT_ENABLED=false/' \
  -e 's/^COMPLIANCE_POLL_ENABLED=$/COMPLIANCE_POLL_ENABLED=false/' \
  -e 's/^S3_ENCRYPTION_MODE=$/S3_ENCRYPTION_MODE=SSE-S3/' \
  -e 's/^API_WORKERS=$/API_WORKERS=3/' \
  -e 's/^WORKER_CONCURRENCY=$/WORKER_CONCURRENCY=2/' \
  -e 's/^BACKUP_RETENTION_DAYS=$/BACKUP_RETENTION_DAYS=90/' \
  -e 's/^RAW_RETENTION_DAYS=$/RAW_RETENTION_DAYS=365/' \
  "${APP_DIR}/.env"

echo "rendered ${APP_DIR}/.env and ${DATA_ROOT}/secrets from SSM"
