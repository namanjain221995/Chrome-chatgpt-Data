#!/usr/bin/env bash
# =============================================================================
# Deploy on the EC2 instance.
#
# Order matters and is deliberate:
#   1. render .env and secret files from SSM (root-owned, mode 0600)
#   2. pull the immutable image tag
#   3. take a database backup BEFORE touching the schema
#   4. run Alembic migrations
#   5. start the new containers
#   6. wait for health
#   7. on failure, roll the application images back to the previous tag
#
# Application images roll back cleanly. Database migrations do not: a migration
# that has already run stays run. That asymmetry is why step 3 exists, and it is
# documented in docs/PRODUCTION_DEPLOYMENT.md.
# =============================================================================
set -euo pipefail

PROJECT_NAME="${PROJECT_NAME:-techsara-chat-archive}"
APP_DIR="${APP_DIR:-/opt/${PROJECT_NAME}}"
DATA_ROOT="${DATA_ROOT:-/srv/${PROJECT_NAME}}"
AWS_REGION="${AWS_REGION:-$(curl -fsS -H "X-aws-ec2-metadata-token: $(curl -fsS -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' 2>/dev/null)" http://169.254.169.254/latest/meta-data/placement/region 2>/dev/null || echo us-east-1)}"
PARAM_PREFIX="/${PROJECT_NAME}"
COMPOSE=(docker compose -f compose.yaml -f compose.prod.yaml)
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-180}"

log() { printf '[deploy] %s\n' "$*"; }
die() { printf '[deploy] ERROR: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root (sudo)"
cd "${APP_DIR}" || die "application directory ${APP_DIR} not found"

# --- 1. Configuration and secrets -----------------------------------------

param() {
  aws ssm get-parameter --name "${PARAM_PREFIX}/$1" --with-decryption \
    --region "${AWS_REGION}" --query 'Parameter.Value' --output text 2>/dev/null || echo ""
}

write_secret() {
  local name="$1" value="$2"
  [ -n "${value}" ] || die "secret ${name} is empty in SSM"
  [ "${value}" != "REPLACE_ME_WITH_scripts_put_secrets_sh" ] \
    || die "secret ${name} is still the placeholder; run scripts/put_secrets.sh first"
  printf '%s' "${value}" > "${DATA_ROOT}/secrets/${name}"
  chmod 0400 "${DATA_ROOT}/secrets/${name}"
  chown root:root "${DATA_ROOT}/secrets/${name}"
}

log "reading configuration from SSM ${PARAM_PREFIX}"
mkdir -p "${DATA_ROOT}/secrets"
chmod 0700 "${DATA_ROOT}/secrets"

POSTGRES_PASSWORD="$(param postgres_password)"
write_secret postgres_password "${POSTGRES_PASSWORD}"
write_secret jwt_secret "$(param jwt_secret)"
write_secret config_signing_key "$(param config_signing_key)"

# Optional secrets: an empty file is fine, the feature is simply off.
printf '%s' "$(param oidc_client_secret)" > "${DATA_ROOT}/secrets/oidc_client_secret"
printf '%s' "$(param openai_compliance_api_key)" > "${DATA_ROOT}/secrets/openai_compliance_api_key"
chmod 0400 "${DATA_ROOT}/secrets/oidc_client_secret" "${DATA_ROOT}/secrets/openai_compliance_api_key"

# pg_dump reads the password from a pgpass file rather than the environment,
# so it never appears in `ps` output.
printf 'postgres:5432:*:%s:%s\n' "$(param postgres_user || echo techsara_app)" "${POSTGRES_PASSWORD}" \
  > "${DATA_ROOT}/secrets/pgpass"
chmod 0400 "${DATA_ROOT}/secrets/pgpass"

IMAGE_TAG="${IMAGE_TAG:-$(param image_tag)}"
[ -n "${IMAGE_TAG}" ] && [ "${IMAGE_TAG}" != "unset" ] || die "IMAGE_TAG is not set"

PREVIOUS_TAG="$(grep -E '^IMAGE_TAG=' .env 2>/dev/null | cut -d= -f2- || echo '')"
log "deploying tag ${IMAGE_TAG} (previous: ${PREVIOUS_TAG:-none})"

umask 077
cat > .env <<EOF
# Rendered by scripts/deploy_ec2.sh from SSM. Do not edit by hand.
# Secret VALUES are not here: they live in ${DATA_ROOT}/secrets as 0400 files.
ENVIRONMENT=$(param environment)
AWS_REGION=${AWS_REGION}
DATA_ROOT=${DATA_ROOT}
IMAGE_REPOSITORY=$(param image_repository)
IMAGE_TAG=${IMAGE_TAG}
S3_BUCKET=$(param s3_bucket)
S3_ENCRYPTION_MODE=$(param s3_encryption_mode)
S3_KMS_KEY_ID=$(param s3_kms_key_id)
PUBLIC_BASE_URL=$(param public_base_url)
CADDY_DOMAIN=$(param caddy_domain)
CADDY_EMAIL=$(param caddy_email)
POSTGRES_DB=$(param postgres_db || echo techsara_chat_archive)
POSTGRES_USER=$(param postgres_user || echo techsara_app)
ALLOWED_EMAIL_DOMAINS=$(param allowed_email_domains)
OIDC_ISSUER=$(param oidc_issuer)
OIDC_CLIENT_ID=$(param oidc_client_id)
OIDC_REQUIRED_HD=$(param oidc_required_hd)
EXTENSION_IDS=$(param extension_ids)
ADMIN_ORIGINS=$(param admin_origins)
MANAGED_WORKSPACE_LABEL=$(param managed_workspace_label)
MANAGED_WORKSPACE_IDS=$(param managed_workspace_ids)
BROWSER_CONTENT_CAPTURE_ENABLED=$(param browser_content_capture_enabled || echo false)
OPENAI_WRITTEN_AUTHORIZATION_CONFIRMED=$(param openai_written_authorization_confirmed || echo false)
AUTO_ARCHIVE_CURRENT_OPEN_CHAT=$(param auto_archive_current_open_chat || echo true)
ATTACHMENT_CAPTURE_ENABLED=$(param attachment_capture_enabled || echo true)
KILL_SWITCH_ENABLED=$(param kill_switch_enabled || echo false)
COMPLIANCE_POLL_ENABLED=$(param compliance_poll_enabled || echo false)
OPENAI_COMPLIANCE_BASE_URL=$(param openai_compliance_base_url)
OPENAI_COMPLIANCE_LOG_PATH=$(param openai_compliance_log_path)
OPENAI_COMPLIANCE_FILES_PATH=$(param openai_compliance_files_path)
TRAINING_EXPORT_ENABLED=$(param training_export_enabled || echo false)
RAW_RETENTION_DAYS=$(param raw_retention_days || echo 365)
BACKUP_RETENTION_DAYS=$(param backup_retention_days || echo 90)
API_WORKERS=$(param api_workers || echo 3)
WORKER_CONCURRENCY=$(param worker_concurrency || echo 2)
PGADMIN_DEFAULT_EMAIL=$(param pgadmin_email)
PGADMIN_DEFAULT_PASSWORD=$(param pgadmin_password)
EOF
chmod 0600 .env

# --- 2. Pull -------------------------------------------------------------

log "pulling images"
"${COMPOSE[@]}" pull --quiet || die "image pull failed for tag ${IMAGE_TAG}"

# --- 3. Pre-migration backup ---------------------------------------------

if "${COMPOSE[@]}" ps --status running --services 2>/dev/null | grep -q '^postgres$'; then
  log "taking a pre-migration backup"
  if ! "${COMPOSE[@]}" exec -T backup sh /opt/scripts/backup_postgres.sh; then
    die "pre-migration backup failed; refusing to migrate"
  fi
else
  log "postgres is not running yet; skipping the pre-migration backup (first deploy)"
fi

# --- 4. Migrate ----------------------------------------------------------

log "starting postgres"
"${COMPOSE[@]}" up -d postgres
for _ in $(seq 1 60); do
  "${COMPOSE[@]}" exec -T postgres pg_isready -q && break
  sleep 2
done

log "running database migrations"
"${COMPOSE[@]}" run --rm migrate || die "migrations failed; application not restarted"

# --- 5. Start ------------------------------------------------------------

log "starting application services"
"${COMPOSE[@]}" up -d --remove-orphans

# --- 6. Health -----------------------------------------------------------

log "waiting for readiness (up to ${HEALTH_TIMEOUT}s)"
healthy=0
for _ in $(seq 1 "${HEALTH_TIMEOUT}"); do
  if "${COMPOSE[@]}" exec -T api curl -fsS http://127.0.0.1:8000/health/ready >/dev/null 2>&1; then
    healthy=1
    break
  fi
  sleep 1
done

if [ "${healthy}" -ne 1 ]; then
  log "health check FAILED"
  "${COMPOSE[@]}" logs --tail 80 api >&2 || true

  if [ -n "${PREVIOUS_TAG}" ] && [ "${PREVIOUS_TAG}" != "${IMAGE_TAG}" ]; then
    log "rolling application images back to ${PREVIOUS_TAG}"
    sed -i "s|^IMAGE_TAG=.*|IMAGE_TAG=${PREVIOUS_TAG}|" .env
    "${COMPOSE[@]}" up -d api worker compliance-poller
    log "rollback complete; NOTE: database migrations were NOT reverted"
  fi
  die "deployment failed health check"
fi

# --- 7. Report -----------------------------------------------------------

log "deployment healthy"
"${COMPOSE[@]}" ps
log "image tag ${IMAGE_TAG} is live"
