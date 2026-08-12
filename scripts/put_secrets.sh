#!/usr/bin/env bash
# =============================================================================
# Write secrets into SSM Parameter Store as SecureString.
#
# Run this from an administrator workstation after the manual IAM/SSM setup.
# Values are written directly as SecureString parameters and never enter an
# infrastructure state file.
#
#   scripts/put_secrets.sh --project techsara-chat-archive --region us-east-1
#
# Values are read from a TTY prompt (never echoed) or generated. Nothing is
# written to shell history, to a file, or to the process argument list.
# =============================================================================
set -euo pipefail

PROJECT="techsara-chat-archive"
REGION="${AWS_REGION:-us-east-1}"
GENERATE=0

while [ $# -gt 0 ]; do
  case "$1" in
    --project) PROJECT="${2:?}"; shift 2 ;;
    --region) REGION="${2:?}"; shift 2 ;;
    --generate) GENERATE=1; shift ;;
    --help|-h)
      sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

command -v aws >/dev/null || { echo "aws CLI is required" >&2; exit 1; }
command -v openssl >/dev/null || { echo "openssl is required" >&2; exit 1; }

put() {
  local name="$1" value="$2"
  aws ssm put-parameter \
    --name "/${PROJECT}/${name}" \
    --value "${value}" \
    --type SecureString \
    --overwrite \
    --region "${REGION}" \
    --output text --query 'Version' >/dev/null
  echo "  set /${PROJECT}/${name}"
}

prompt_secret() {
  local name="$1" description="$2" value=""
  printf '%s\n' "${description}" >&2
  printf '  value for %s (leave empty to skip): ' "${name}" >&2
  read -rs value
  printf '\n' >&2
  printf '%s' "${value}"
}

echo "Writing secrets to SSM under /${PROJECT} in ${REGION}"
echo

if [ "${GENERATE}" -eq 1 ]; then
  echo "generating strong random values for the machine-only secrets"
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
echo "Operator-supplied secrets (from your identity provider / OpenAI agreement):"
value="$(prompt_secret oidc_client_secret 'Google Workspace OAuth client secret for the archive backend.')"
[ -n "${value}" ] && put oidc_client_secret "${value}"

value="$(prompt_secret openai_compliance_api_key 'OpenAI Enterprise Compliance API key. Leave empty until authorized.')"
[ -n "${value}" ] && put openai_compliance_api_key "${value}"

echo
echo "Done. Verify with:"
echo "  aws ssm get-parameters-by-path --path /${PROJECT} --region ${REGION} --query 'Parameters[].Name'"
echo
echo "Rotation: re-run this script, then redeploy so the containers pick up new files:"
echo "  aws ssm send-command --document-name AWS-RunShellScript --targets Key=instanceids,Values=<id> \\"
echo "    --parameters 'commands=[\"cd /opt/${PROJECT} && sudo IMAGE_TAG=<current-sha> ./scripts/deploy_ec2.sh\"]' --region ${REGION}"
