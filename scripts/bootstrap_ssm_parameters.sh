#!/usr/bin/env bash
# =============================================================================
# Create the SSM parameters a first deployment needs.
#
# Run from AWS CloudShell or any shell with administrator AWS credentials --
# NOT from the EC2 instance, whose role is deliberately read-only on Parameter
# Store.
#
#   ./scripts/bootstrap_ssm_parameters.sh --domain example.com
#
# Machine-only secrets are generated here with openssl and are never printed.
# Organisation values come from flags: nothing about your identity provider,
# your domain or your Cloudflare account is guessed.
#
# Idempotent. Configuration values are left alone unless --overwrite is given,
# and generated secrets are never replaced by --overwrite at all: rotating a
# database password behind a running cluster is a separate, confirmed action
# (--rotate-secrets). Re-running this script is always safe.
# =============================================================================
set -Eeuo pipefail
set +x

PROJECT="techsara-chat-archive"
REGION="${AWS_REGION:-us-east-1}"
DOMAIN=""
HOSTNAME_LABEL="archive"
WORKSPACE_LABEL="TechSara's Workspace"
OIDC_CLIENT_ID=""
PROMPT_TUNNEL_TOKEN=0
OVERWRITE=0
ROTATE_SECRETS=0

OIDC_PLACEHOLDER="pending-oidc-configuration"

usage() {
  sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'
  cat <<'EOF'

Options:
  --domain <d>            Company domain. Required. Sets the public URL
                          (https://archive.<d>), the required Google hosted
                          domain, and the allowed employee email domains.
  --hostname-label <l>    Subdomain for the archive (default: archive).
  --workspace-label <l>   Exact ChatGPT workspace label.
  --oidc-client-id <id>   Google Workspace OAuth client id. When omitted a
                          non-functional placeholder is written so the stack
                          can start; employee sign-in will not work until it
                          is replaced.
  --prompt-tunnel-token   Prompt (silently) for the Cloudflare Tunnel token.
                          Implied when the token is absent and the script is
                          run interactively, so it cannot be forgotten.
  --overwrite             Replace existing *configuration* values. Generated
                          secrets are never touched by this flag.
  --rotate-secrets        Also regenerate the machine secrets. Read the
                          warning below before using it on a live host.
  --region <r>            AWS region (default: us-east-1).
  --project <p>           SSM prefix (default: techsara-chat-archive).

Rotating postgres_password on a host whose database already exists will lock
the application out: PostgreSQL only reads POSTGRES_PASSWORD at initdb, so the
running cluster keeps the old password. Change it inside PostgreSQL first:

  ALTER ROLE techsara_app WITH PASSWORD '<new>';

then store the same value here and redeploy.
EOF
  exit "${1:-2}"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --domain) DOMAIN="${2:?}"; shift 2 ;;
    --hostname-label) HOSTNAME_LABEL="${2:?}"; shift 2 ;;
    --workspace-label) WORKSPACE_LABEL="${2:?}"; shift 2 ;;
    --oidc-client-id) OIDC_CLIENT_ID="${2:?}"; shift 2 ;;
    --prompt-tunnel-token) PROMPT_TUNNEL_TOKEN=1; shift ;;
    --overwrite) OVERWRITE=1; shift ;;
    --rotate-secrets) ROTATE_SECRETS=1; shift ;;
    --region) REGION="${2:?}"; shift 2 ;;
    --project) PROJECT="${2:?}"; shift 2 ;;
    --help|-h) usage 0 ;;
    *) echo "unknown argument: $1" >&2; usage ;;
  esac
done

die() { printf '[ssm-bootstrap] ERROR: %s\n' "$*" >&2; exit 1; }
log() { printf '[ssm-bootstrap] %s\n' "$*"; }

command -v aws >/dev/null || die "the AWS CLI is required"
command -v openssl >/dev/null || die "openssl is required"
[ -n "${DOMAIN}" ] || { echo "--domain is required" >&2; usage; }
case "${DOMAIN}" in
  *.*) ;;
  *) die "--domain should be a domain name, e.g. example.com" ;;
esac

identity="$(aws sts get-caller-identity --region "${REGION}" --query Arn --output text)" \
  || die "no usable AWS credentials"
log "acting as ${identity}"
log "region ${REGION}, prefix /${PROJECT}"

exists() {
  aws ssm get-parameter --name "/${PROJECT}/$1" --region "${REGION}" \
    --query 'Parameter.Name' --output text >/dev/null 2>&1
}

# put <name> <value> [type] [force]
# `force` is for values the operator supplied explicitly on the command line:
# that is unambiguous intent and does not also require --overwrite.
put() {
  local name="$1" value="$2" type="${3:-SecureString}" force="${4:-0}"
  if exists "${name}" && [ "${OVERWRITE}" -eq 0 ] && [ "${force}" -eq 0 ]; then
    log "keep   /${PROJECT}/${name} (already exists)"
    return 0
  fi
  aws ssm put-parameter \
    --name "/${PROJECT}/${name}" \
    --value "${value}" \
    --type "${type}" \
    --overwrite \
    --region "${REGION}" \
    --query 'Version' --output text >/dev/null
  log "set    /${PROJECT}/${name} (${type})"
}

# --- Machine-only secrets ----------------------------------------------------
# Generated here, never displayed, never stored anywhere but Parameter Store.
#
# These are deliberately NOT covered by --overwrite. Regenerating
# postgres_password on a host whose cluster already exists locks the
# application out, because PostgreSQL only reads POSTGRES_PASSWORD at initdb;
# regenerating jwt_secret invalidates every employee session. Rotation is a
# separate, explicit decision.
put_generated_secret() {
  local name="$1" value="$2"
  if exists "${name}"; then
    if [ "${ROTATE_SECRETS}" -eq 1 ]; then
      log "ROTATE /${PROJECT}/${name} (was present)"
      put "${name}" "${value}" SecureString 1
    else
      log "keep   /${PROJECT}/${name} (generated secret, --rotate-secrets to replace)"
    fi
    return 0
  fi
  put "${name}" "${value}"
}

if [ "${ROTATE_SECRETS}" -eq 1 ]; then
  cat >&2 <<'EOF'
[ssm-bootstrap] WARNING: --rotate-secrets will replace the machine secrets.
        If this database already exists, rotating postgres_password locks the
        application out until you also run, inside PostgreSQL:
            ALTER ROLE <user> WITH PASSWORD '<the new value>';
        Rotating jwt_secret signs every employee out.
EOF
  printf '[ssm-bootstrap] type ROTATE to continue: ' >&2
  read -r confirm
  [ "${confirm}" = "ROTATE" ] || die "not confirmed; nothing was changed"
fi

put_generated_secret postgres_password "$(openssl rand -base64 36 | tr -d '\n/+=' | head -c 40)"
put_generated_secret jwt_secret "$(openssl rand -base64 48 | tr -d '\n')"
put_generated_secret config_signing_key "$(openssl rand -base64 48 | tr -d '\n')"
put_generated_secret pgadmin_password "$(openssl rand -base64 24 | tr -d '\n/+=' | head -c 24)"

# --- Configuration -----------------------------------------------------------
public_base_url="https://${HOSTNAME_LABEL}.${DOMAIN}"

put postgres_user "techsara_app" String
put postgres_db "techsara_chat_archive" String
put public_base_url "${public_base_url}" String
put oidc_issuer "https://accounts.google.com" String
put oidc_required_hd "${DOMAIN}" String
put allowed_email_domains "${DOMAIN}" String
put managed_workspace_label "${WORKSPACE_LABEL}" String
put pgadmin_email "pgadmin@${DOMAIN}" String

if [ -n "${OIDC_CLIENT_ID}" ]; then
  # Supplied explicitly on the command line, so it replaces the placeholder
  # without needing --overwrite as well.
  put oidc_client_id "${OIDC_CLIENT_ID}" String 1
else
  put oidc_client_id "${OIDC_PLACEHOLDER}" String
fi

# --- Capture gates: explicitly false ----------------------------------------
# Written rather than left absent so the value is visible in the console and an
# auditor can see the decision, not an omission.
put browser_content_capture_enabled "false" String
put openai_written_authorization_confirmed "false" String
put training_export_enabled "false" String
put kill_switch_enabled "false" String
put compliance_poll_enabled "false" String

# --- Cloudflare tunnel token -------------------------------------------------
# Prompt whenever the token is missing and there is a terminal to prompt on.
# Requiring --prompt-tunnel-token made forgetting the flag a silent failure:
# the script reported success and the archive stayed unreachable.
if [ "${PROMPT_TUNNEL_TOKEN}" -eq 0 ] && [ -t 0 ] && ! exists cloudflare_tunnel_token; then
  log "no cloudflare_tunnel_token is stored yet"
  PROMPT_TUNNEL_TOKEN=1
fi

if [ "${PROMPT_TUNNEL_TOKEN}" -eq 1 ]; then
  printf 'Cloudflare Tunnel token (input hidden, leave empty to skip): ' >&2
  read -rs tunnel_token
  printf '\n' >&2
  if [ -n "${tunnel_token}" ]; then
    # Prompted for explicitly, and tunnel tokens are rotated far more often
    # than they are created, so always store what was just typed.
    put cloudflare_tunnel_token "${tunnel_token}" SecureString 1
    unset tunnel_token
  else
    log "no tunnel token supplied"
  fi
fi

echo
log "parameters now present:"
aws ssm get-parameters-by-path --path "/${PROJECT}" --region "${REGION}" \
  --query 'Parameters[].Name' --output text | tr '\t' '\n' | sed 's|^|         |'

echo
missing=0
if ! exists cloudflare_tunnel_token; then
  missing=1
  cat >&2 <<EOF
[ssm-bootstrap] STILL REQUIRED for public ingress:
        /${PROJECT}/cloudflare_tunnel_token

        Create the tunnel first (docs/CLOUDFLARE_TUNNEL_SETUP.md), route
        ${public_base_url} to http://api:8000, then re-run this script with
        --prompt-tunnel-token.

        Until then, deploy the internal stack with:
            sudo ./scripts/deploy_production.sh --without-tunnel <full-sha>
EOF
fi

if [ "$(aws ssm get-parameter --name "/${PROJECT}/oidc_client_id" \
          --region "${REGION}" --query 'Parameter.Value' --output text)" \
     = "${OIDC_PLACEHOLDER}" ]; then
  missing=1
  cat >&2 <<EOF
[ssm-bootstrap] PLACEHOLDER IN USE:
        /${PROJECT}/oidc_client_id = ${OIDC_PLACEHOLDER}

        The stack will start, but no employee can sign in until this holds a
        real Google Workspace OAuth client id. Replace it with:
            aws ssm put-parameter --name /${PROJECT}/oidc_client_id \\
              --value '<client-id>' --type String --overwrite --region ${REGION}
EOF
fi

[ "${missing}" -eq 0 ] && log "all parameters for a full deployment are present"
log "public base URL is ${public_base_url}"
