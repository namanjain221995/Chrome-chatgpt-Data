#!/usr/bin/env bash
# =============================================================================
# Render production configuration from AWS SSM Parameter Store.
#
# Produces, on the EC2 host only:
#   ${APP_DIR}/.env.production          non-secret runtime configuration (0600)
#   ${DATA_ROOT}/secrets/*              root-owned secret files (0440)
#   ${DATA_ROOT}/secrets/cloudflared.env  tunnel token for the cloudflared
#                                         service (0400)
#
# Credentials come from the EC2 instance role through the normal AWS SDK
# provider chain. This script never calls `aws configure`, never accepts a
# static access key, and never prints a parameter value.
# =============================================================================
set -Eeuo pipefail
# Defensive: a stray `set -x` from a caller would echo secret values.
set +x

PROJECT_NAME="${PROJECT_NAME:-techsara-chat-archive}"
APP_DIR="${APP_DIR:-/opt/${PROJECT_NAME}}"
DATA_ROOT="${DATA_ROOT:-/srv/${PROJECT_NAME}}"
AWS_REGION="${AWS_REGION:-us-east-1}"
PARAM_PREFIX="${SSM_PARAMETER_PREFIX:-/${PROJECT_NAME}}"
ENV_FILE="${ENV_FILE:-${APP_DIR}/.env.production}"

APP_GID=10001
POSTGRES_GID=70
PGADMIN_UID=5050

die() { printf '[ssm] ERROR: %s\n' "$*" >&2; exit 1; }
log() { printf '[ssm] %s\n' "$*"; }

[ "$(id -u)" -eq 0 ] || die "run as root (sudo $0)"
command -v aws >/dev/null || die "aws CLI is required on the instance"

install -d -o root -g root -m 0755 "${APP_DIR}"
install -d -o root -g "${APP_GID}" -m 0750 "${DATA_ROOT}/secrets"

# ---------------------------------------------------------------------------
# Parameter access
# ---------------------------------------------------------------------------

# Values pasted into the SSM console frequently carry a trailing space or an
# accidental leading one. A stray space in a tunnel token or a hostname fails
# far from its cause, so every value is trimmed on the way in. Interior spaces
# -- a workspace label, for instance -- are preserved.
trim() {
  local v="$1"
  v="${v#"${v%%[![:space:]]*}"}"
  v="${v%"${v##*[![:space:]]}"}"
  printf '%s' "${v}"
}

get_parameter() {
  local raw
  raw="$(aws ssm get-parameter --name "${PARAM_PREFIX}/$1" --with-decryption \
    --region "${AWS_REGION}" --query 'Parameter.Value' --output text 2>/dev/null)" || return 1
  trim "${raw}"
}

required_parameter() {
  local value
  if ! value="$(get_parameter "$1")" || [ -z "${value}" ] || [ "${value}" = "None" ]; then
    die "required SSM parameter missing or empty: ${PARAM_PREFIX}/$1"
  fi
  printf '%s' "${value}"
}

optional_parameter() {
  local value
  value="$(get_parameter "$1")" || value=""
  [ "${value}" = "None" ] && value=""
  printf '%s' "${value}"
}

# A value destined for an env file must be a single line; a newline would
# silently truncate the variable and shift everything after it.
single_line() {
  local name="$1" value="$2"
  case "${value}" in
    *$'\n'*) die "SSM parameter ${PARAM_PREFIX}/${name} must not contain a newline" ;;
  esac
  printf '%s' "${value}"
}

write_secret_file() {
  local name="$1" value="$2" gid="${3:-${APP_GID}}" mode="${4:-0440}"
  local path="${DATA_ROOT}/secrets/${name}"
  ( umask 077; printf '%s' "${value}" > "${path}" )
  chown root:"${gid}" "${path}"
  chmod "${mode}" "${path}"
}

# ---------------------------------------------------------------------------
# Secret material
# ---------------------------------------------------------------------------

log "reading ${PARAM_PREFIX}/* from SSM in ${AWS_REGION}"

postgres_password="$(required_parameter postgres_password)"
postgres_user="$(single_line postgres_user "$(required_parameter postgres_user)")"
postgres_db="$(single_line postgres_db "$(required_parameter postgres_db)")"

write_secret_file postgres_password "${postgres_password}"
# The PostgreSQL container reads the same password as gid 70.
write_secret_file postgres_server_password "${postgres_password}" "${POSTGRES_GID}"
write_secret_file jwt_secret "$(required_parameter jwt_secret)"
write_secret_file config_signing_key "$(required_parameter config_signing_key)"
write_secret_file oidc_client_secret "$(optional_parameter oidc_client_secret)"
write_secret_file openai_compliance_api_key "$(optional_parameter openai_compliance_api_key)"
# pgAdmin is an optional profile: write the file so the bind mount resolves to a
# file rather than a directory, even when the parameter is absent.
write_secret_file pgadmin_password "$(optional_parameter pgadmin_password)" "${PGADMIN_UID}"

# .pgpass for the backup container. Backslashes and colons are the only
# characters libpq treats specially in this file.
pgpass_password="${postgres_password//\\/\\\\}"
pgpass_password="${pgpass_password//:/\\:}"
write_secret_file pgpass "$(printf 'postgres:5432:*:%s:%s\n' "${postgres_user}" "${pgpass_password}")"

# Cloudflare Tunnel token. Passed to cloudflared through an env file rather
# than the command line, so it never appears in `docker inspect`, `ps` output
# or container logs.
#
# ALLOW_MISSING_TUNNEL is set only by `deploy_production.sh --without-tunnel`,
# the deliberate bring-up path used before the Cloudflare tunnel exists. The
# token is required for every normal deployment.
tunnel_token="$(optional_parameter cloudflare_tunnel_token)"
if [ -n "${tunnel_token}" ]; then
  tunnel_token="$(single_line cloudflare_tunnel_token "${tunnel_token}")"
  write_secret_file cloudflared.env "$(printf 'TUNNEL_TOKEN=%s\n' "${tunnel_token}")" 0 0400
elif [ "${ALLOW_MISSING_TUNNEL:-false}" = "true" ]; then
  # Remove any stale file so the tunnel cannot start with an old token.
  rm -f "${DATA_ROOT}/secrets/cloudflared.env"
  log "no tunnel token in SSM; continuing without public ingress as requested"
else
  die "required SSM parameter missing or empty: ${PARAM_PREFIX}/cloudflare_tunnel_token"
fi
unset tunnel_token postgres_password pgpass_password

# ---------------------------------------------------------------------------
# Non-secret runtime configuration
# ---------------------------------------------------------------------------

public_base_url="$(single_line public_base_url "$(required_parameter public_base_url)")"
case "${public_base_url}" in
  https://*) ;;
  *) die "public_base_url must start with https:// (Cloudflare terminates TLS)" ;;
esac
# The API's TrustedHostMiddleware pins this; deriving it removes a parameter
# that could drift out of step with the public URL.
archive_hostname="${public_base_url#https://}"
archive_hostname="${archive_hostname%%/*}"
archive_hostname="${archive_hostname%%:*}"
[ -n "${archive_hostname}" ] || die "could not derive a hostname from public_base_url"

image_tag="${IMAGE_TAG:-}"
if [ -z "${image_tag}" ]; then
  image_tag="$(git -C "${APP_DIR}" rev-parse HEAD 2>/dev/null || true)"
fi
[ -n "${image_tag}" ] || die "IMAGE_TAG is not set and the checkout has no HEAD commit"

default_to() { [ -n "$1" ] && printf '%s' "$1" || printf '%s' "$2"; }

oidc_issuer="$(default_to "$(optional_parameter oidc_issuer)" "https://accounts.google.com")"
oidc_client_id="$(single_line oidc_client_id "$(required_parameter oidc_client_id)")"
oidc_required_hd="$(single_line oidc_required_hd "$(required_parameter oidc_required_hd)")"
allowed_email_domains="$(single_line allowed_email_domains "$(required_parameter allowed_email_domains)")"
managed_workspace_label="$(single_line managed_workspace_label "$(required_parameter managed_workspace_label)")"
pgadmin_email="$(default_to "$(optional_parameter pgadmin_email)" "pgadmin-not-configured@invalid")"

# Capture gates: absent means false. They are never inferred from anything else.
browser_capture="$(default_to "$(optional_parameter browser_content_capture_enabled)" "false")"
written_authorization="$(default_to "$(optional_parameter openai_written_authorization_confirmed)" "false")"
training_export="$(default_to "$(optional_parameter training_export_enabled)" "false")"
kill_switch="$(default_to "$(optional_parameter kill_switch_enabled)" "false")"
compliance_poll="$(default_to "$(optional_parameter compliance_poll_enabled)" "false")"

umask 077
cat > "${ENV_FILE}" <<EOF
# Generated by scripts/fetch_ssm_secrets.sh from ${PARAM_PREFIX}/*.
# Do not edit by hand and never commit. Secret values are NOT in this file;
# they are root-owned files under ${DATA_ROOT}/secrets referenced by *_FILE.
ENVIRONMENT=production
PROJECT_NAME=${PROJECT_NAME}
DATA_ROOT=${DATA_ROOT}

# Immutable release identity: the exact deployed Git commit.
IMAGE_NAME=${IMAGE_NAME:-techsara-chat-archive-backend}
IMAGE_TAG=${image_tag}

# Public identity. Cloudflare terminates TLS; the tunnel is the only ingress.
PUBLIC_BASE_URL=${public_base_url}
ARCHIVE_HOSTNAME=${archive_hostname}

# AWS. The EC2 instance role supplies credentials through the SDK chain;
# no access key is ever written here.
AWS_REGION=us-east-1
S3_BUCKET=techsara-chatgpt
S3_ENDPOINT_URL=
S3_USE_PATH_STYLE=false
S3_ENCRYPTION_MODE=$(default_to "$(optional_parameter s3_encryption_mode)" "SSE-S3")
S3_KMS_KEY_ID=$(optional_parameter s3_kms_key_id)
S3_HEALTH_CACHE_SECONDS=$(default_to "$(optional_parameter s3_health_cache_seconds)" "60")

# PostgreSQL
POSTGRES_DB=${postgres_db}
POSTGRES_USER=${postgres_user}
POSTGRES_MAX_CONNECTIONS=$(default_to "$(optional_parameter postgres_max_connections)" "120")
DATABASE_POOL_SIZE=$(default_to "$(optional_parameter database_pool_size)" "12")
DATABASE_MAX_OVERFLOW=$(default_to "$(optional_parameter database_max_overflow)" "4")
DATABASE_POOL_TIMEOUT_SECONDS=$(default_to "$(optional_parameter database_pool_timeout_seconds)" "30")
DATABASE_POOL_RECYCLE_SECONDS=$(default_to "$(optional_parameter database_pool_recycle_seconds)" "1800")
DATABASE_POOL_PRE_PING=true

# Identity
OIDC_ISSUER=${oidc_issuer}
OIDC_CLIENT_ID=${oidc_client_id}
OIDC_REQUIRED_HD=${oidc_required_hd}
ALLOWED_EMAIL_DOMAINS=${allowed_email_domains}
EXTENSION_IDS=$(optional_parameter extension_ids)
ADMIN_ORIGINS=$(optional_parameter admin_origins)

# Workspace scoping
MANAGED_WORKSPACE_LABEL=${managed_workspace_label}
MANAGED_WORKSPACE_IDS=$(optional_parameter managed_workspace_ids)
OPENAI_WORKSPACE_ID=$(optional_parameter openai_workspace_id)

# Capture gates. Both must be true before any message content is accepted.
BROWSER_CONTENT_CAPTURE_ENABLED=${browser_capture}
OPENAI_WRITTEN_AUTHORIZATION_CONFIRMED=${written_authorization}
KILL_SWITCH_ENABLED=${kill_switch}
TRAINING_EXPORT_ENABLED=${training_export}
AUTO_ARCHIVE_CURRENT_OPEN_CHAT=$(default_to "$(optional_parameter auto_archive_current_open_chat)" "true")
ATTACHMENT_CAPTURE_ENABLED=$(default_to "$(optional_parameter attachment_capture_enabled)" "true")

# Optional authorized compliance feed. No endpoint path is assumed.
COMPLIANCE_POLL_ENABLED=${compliance_poll}
OPENAI_COMPLIANCE_BASE_URL=$(optional_parameter openai_compliance_base_url)
OPENAI_COMPLIANCE_LOG_PATH=$(optional_parameter openai_compliance_log_path)
OPENAI_COMPLIANCE_FILES_PATH=$(optional_parameter openai_compliance_files_path)

# Runtime sizing
API_WORKERS=$(default_to "$(optional_parameter api_workers)" "3")
WORKER_CONCURRENCY=$(default_to "$(optional_parameter worker_concurrency)" "2")
LOG_LEVEL=$(default_to "$(optional_parameter log_level)" "INFO")
BACKUP_INTERVAL_SECONDS=$(default_to "$(optional_parameter backup_interval_seconds)" "86400")
BACKUP_RETENTION_DAYS=$(default_to "$(optional_parameter backup_retention_days)" "90")
RAW_RETENTION_DAYS=$(default_to "$(optional_parameter raw_retention_days)" "365")

# Optional admin profile (never started by a deployment).
PGADMIN_DEFAULT_EMAIL=${pgadmin_email}
PGADMIN_PORT=$(default_to "$(optional_parameter pgadmin_port)" "5050")
CLOUDFLARED_METRICS_PORT=$(default_to "$(optional_parameter cloudflared_metrics_port)" "2000")
EOF

chown root:root "${ENV_FILE}"
chmod 0600 "${ENV_FILE}"

# Fail closed rather than silently deploying with capture enabled by accident.
for gate in BROWSER_CONTENT_CAPTURE_ENABLED OPENAI_WRITTEN_AUTHORIZATION_CONFIRMED \
            TRAINING_EXPORT_ENABLED; do
  value="$(sed -n "s/^${gate}=//p" "${ENV_FILE}")"
  case "${value}" in
    true|false) ;;
    *) die "${gate} must be exactly 'true' or 'false' (got an unexpected value)" ;;
  esac
done

log "wrote ${ENV_FILE} (0600) and $(find "${DATA_ROOT}/secrets" -maxdepth 1 -type f | wc -l) secret files"
log "capture gates: browser=${browser_capture} written_authorization=${written_authorization}"
