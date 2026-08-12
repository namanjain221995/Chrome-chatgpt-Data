#!/usr/bin/env bash
# =============================================================================
# Write the production parameters into AWS SSM Parameter Store.
#
# Run this from an administrator workstation after the manual IAM setup. Secret
# values are written as SecureString; plain configuration is written as String
# so it is readable in the console without decryption.
#
#   scripts/put_secrets.sh --generate            # machine secrets, then prompts
#   scripts/put_secrets.sh --config-only         # only non-secret settings
#
# Secret values are read from a TTY prompt and never echoed, never written to a
# file, and never placed on the process argument list of anything but the AWS
# CLI call itself.
# =============================================================================
set -Eeuo pipefail

PROJECT="techsara-chat-archive"
REGION="${AWS_REGION:-us-east-1}"
GENERATE=0
CONFIG_ONLY=0

while [ $# -gt 0 ]; do
  case "$1" in
    --project) PROJECT="${2:?}"; shift 2 ;;
    --region) REGION="${2:?}"; shift 2 ;;
    --generate) GENERATE=1; shift ;;
    --config-only) CONFIG_ONLY=1; shift ;;
    --help|-h) sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

command -v aws >/dev/null || { echo "aws CLI is required" >&2; exit 1; }
command -v openssl >/dev/null || { echo "openssl is required" >&2; exit 1; }

put() {
  local name="$1" value="$2" type="${3:-SecureString}"
  aws ssm put-parameter \
    --name "/${PROJECT}/${name}" \
    --value "${value}" \
    --type "${type}" \
    --overwrite \
    --region "${REGION}" \
    --output text --query 'Version' >/dev/null
  echo "  set /${PROJECT}/${name} (${type})"
}

prompt_secret() {
  local name="$1" description="$2" value=""
  printf '%s\n' "${description}" >&2
  printf '  value for %s (leave empty to skip): ' "${name}" >&2
  read -rs value
  printf '\n' >&2
  printf '%s' "${value}"
}

prompt_plain() {
  local name="$1" description="$2" value=""
  printf '%s\n' "${description}" >&2
  printf '  value for %s (leave empty to skip): ' "${name}" >&2
  read -r value
  printf '%s' "${value}"
}

echo "Writing parameters to SSM under /${PROJECT} in ${REGION}"
echo

if [ "${CONFIG_ONLY}" -eq 0 ]; then
  echo "--- Machine secrets (SecureString) ---"
  if [ "${GENERATE}" -eq 1 ]; then
    echo "generating strong random values"
    put postgres_password "$(openssl rand -base64 36 | tr -d '\n/+=' | head -c 40)"
    put jwt_secret "$(openssl rand -base64 48 | tr -d '\n')"
    put config_signing_key "$(openssl rand -base64 48 | tr -d '\n')"
    put pgadmin_password "$(openssl rand -base64 24 | tr -d '\n/+=' | head -c 24)"
  else
    value="$(prompt_secret postgres_password 'PostgreSQL application password (40+ random characters).')"
    [ -n "${value}" ] && put postgres_password "${value}"
    value="$(prompt_secret jwt_secret 'Backend session signing key. Generate with: openssl rand -base64 48')"
    [ -n "${value}" ] && put jwt_secret "${value}"
    value="$(prompt_secret config_signing_key 'Runtime config signing key. Generate with: openssl rand -base64 48')"
    [ -n "${value}" ] && put config_signing_key "${value}"
    value="$(prompt_secret pgadmin_password 'pgAdmin login password (administrators only).')"
    [ -n "${value}" ] && put pgadmin_password "${value}"
  fi

  echo
  echo "--- Operator-supplied secrets (SecureString) ---"
  value="$(prompt_secret cloudflare_tunnel_token \
    'Cloudflare Tunnel token from Zero Trust > Networks > Tunnels (see docs/CLOUDFLARE_TUNNEL_SETUP.md). REQUIRED before the first deployment.')"
  [ -n "${value}" ] && put cloudflare_tunnel_token "${value}"

  value="$(prompt_secret oidc_client_secret 'Google Workspace OAuth client secret for the archive backend.')"
  [ -n "${value}" ] && put oidc_client_secret "${value}"

  value="$(prompt_secret openai_compliance_api_key 'OpenAI Enterprise Compliance API key. Leave empty until authorized.')"
  [ -n "${value}" ] && put openai_compliance_api_key "${value}"
fi

echo
echo "--- Configuration (String) ---"
value="$(prompt_plain public_base_url 'Public HTTPS base URL served by the Cloudflare Tunnel, e.g. https://archive.example.com')"
[ -n "${value}" ] && put public_base_url "${value}" String

value="$(prompt_plain postgres_user 'PostgreSQL role name, e.g. techsara_app')"
[ -n "${value}" ] && put postgres_user "${value}" String

value="$(prompt_plain postgres_db 'PostgreSQL database name, e.g. techsara_chat_archive')"
[ -n "${value}" ] && put postgres_db "${value}" String

value="$(prompt_plain oidc_client_id 'Google Workspace OAuth client id.')"
[ -n "${value}" ] && put oidc_client_id "${value}" String

value="$(prompt_plain oidc_required_hd 'Required Google hosted domain, e.g. example.com')"
[ -n "${value}" ] && put oidc_required_hd "${value}" String

value="$(prompt_plain allowed_email_domains 'Comma-separated employee email domains.')"
[ -n "${value}" ] && put allowed_email_domains "${value}" String

value="$(prompt_plain managed_workspace_label 'Exact ChatGPT workspace label that identifies the company workspace.')"
[ -n "${value}" ] && put managed_workspace_label "${value}" String

value="$(prompt_plain openai_workspace_id 'OpenAI Enterprise workspace id, if known. Optional.')"
[ -n "${value}" ] && put openai_workspace_id "${value}" String

value="$(prompt_plain pgadmin_email 'pgAdmin login email for administrators. Optional.')"
[ -n "${value}" ] && put pgadmin_email "${value}" String

value="$(prompt_plain extension_ids 'Chrome extension id(s), comma separated. Set after the first packaged build.')"
[ -n "${value}" ] && put extension_ids "${value}" String

cat <<EOF

Done. Verify the parameter set with:
  aws ssm get-parameters-by-path --path /${PROJECT} --region ${REGION} \\
    --query 'Parameters[].Name' --output table

Capture gates are deliberately NOT written here. They default to false and must
be set explicitly, and only after written authorization:
  aws ssm put-parameter --name /${PROJECT}/browser_content_capture_enabled \\
    --value true --type String --overwrite --region ${REGION}

Rotation: re-run this script, then redeploy so containers pick up new files:
  ssh ec2-user@<host> "sudo /opt/${PROJECT}/scripts/deploy_production.sh \\
    \$(git rev-parse HEAD)"
EOF
