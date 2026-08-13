#!/usr/bin/env bash
# Verify every required document exists and carries real content, and that the
# capture-limitations document actually states the limitations it must state.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}" || exit 1

RED=$'\033[31m'; GREEN=$'\033[32m'; RESET=$'\033[0m'
[ -t 1 ] || { RED=""; GREEN=""; RESET=""; }

MIN_LINES=25
missing=0

REQUIRED=(
  README.md
  CLAUDE.md
  claude-progress.md
  docs/ARCHITECTURE.md
  docs/ASSUMPTIONS.md
  docs/DECISIONS.md
  docs/AWS_MANUAL_SETUP.md
  docs/AWS_CONSOLE_CHECKLIST.md
  docs/CLOUDFLARE_TUNNEL_SETUP.md
  docs/CHROME_ENTERPRISE_DEPLOYMENT.md
  docs/CHROME_EXTENSION.md
  docs/LOCAL_DEVELOPMENT.md
  docs/EC2_DEPLOYMENT.md
  docs/SIMPLE_CICD.md
  docs/GITHUB_SECRETS.md
  docs/GOOGLE_OAUTH_SETUP.md
  docs/ROLLBACK.md
  docs/SECURITY.md
  docs/SECURITY_REVIEW.md
  docs/PRIVACY_AND_EMPLOYEE_NOTICE.md
  docs/CAPTURE_LIMITATIONS.md
  docs/COMPLIANCE_ADAPTER.md
  docs/DATABASE.md
  docs/PGADMIN_ACCESS.md
  docs/BACKUP_RESTORE.md
  docs/DISASTER_RECOVERY.md
  docs/MONITORING.md
  docs/CAPACITY.md
  docs/INCIDENT_RUNBOOK.md
  docs/TESTING.md
  docs/OPERATIONS.md
)

echo "Documentation check"
echo "==================="

for doc in "${REQUIRED[@]}"; do
  if [ ! -f "${doc}" ]; then
    echo "${RED}missing${RESET} ${doc}"
    missing=$((missing + 1))
    continue
  fi
  lines="$(wc -l < "${doc}")"
  if [ "${lines}" -lt "${MIN_LINES}" ]; then
    echo "${RED}thin   ${RESET} ${doc} (${lines} lines, expected >= ${MIN_LINES})"
    missing=$((missing + 1))
  else
    printf "%sok     %s %s (%s lines)\n" "${GREEN}" "${RESET}" "${doc}" "${lines}"
  fi
done

# The capture-limitations document must make the honest statements prominently.
echo
echo "Required statements in docs/CAPTURE_LIMITATIONS.md:"
REQUIRED_PHRASES=(
  "currently opened conversation"
  "does not archive unsent"
  "cannot guarantee"
  "hidden model reasoning"
  "may not be recoverable"
)
if [ -f docs/CAPTURE_LIMITATIONS.md ]; then
  for phrase in "${REQUIRED_PHRASES[@]}"; do
    if grep -qi "${phrase}" docs/CAPTURE_LIMITATIONS.md; then
      printf "%sok     %s \"%s\"\n" "${GREEN}" "${RESET}" "${phrase}"
    else
      printf "%smissing%s \"%s\"\n" "${RED}" "${RESET}" "${phrase}"
      missing=$((missing + 1))
    fi
  done
fi

# No runbook may point at a script or document that the tunnel/SSH refactor
# removed: a stale instruction is how an operator ends up deploying the old
# architecture by hand.
#
# Two files are deliberately exempt, because naming what was superseded is
# their entire job: claude-progress.md is the change log and docs/DECISIONS.md
# holds the ADRs, which must record what each decision replaced and why. Every
# file an operator would actually follow stays covered.
HISTORY_EXEMPT=(
  ':!claude-progress.md'
  ':!docs/DECISIONS.md'
)
echo
echo "Stale references to removed files:"
RETIRED=(
  'deploy_ec2\.sh'
  'install_origin_tls\.sh'
  'verify_deployment\.sh'
  'deploy_bundle\.sh'
  'restore_test_local\.sh'
  'PRODUCTION_DEPLOYMENT\.md'
  'BACKUP_AND_RESTORE\.md'
  'SCALING_250_USERS\.md'
  'CLOUDFLARE_DNS_AND_TLS\.md'
  'GITHUB_AWS_OIDC_SETUP\.md'
  'origin\.key'
  'Origin CA'
  'ssm send-command'
)
for pattern in "${RETIRED[@]}"; do
  hits="$(git grep -InE -e "${pattern}" -- '*.md' 'Makefile' '.github' 'scripts' 'tests' \
    'compose*.yaml' '.env.example' \
    ':!scripts/docs_check.sh' "${HISTORY_EXEMPT[@]}" 2>/dev/null || true)"
  if [ -n "${hits}" ]; then
    printf "%sstale  %s %s\n" "${RED}" "${RESET}" "${pattern}"
    printf '        %s\n' "${hits//$'\n'/$'\n        '}"
    missing=$((missing + 1))
  else
    printf "%sok     %s no reference to %s\n" "${GREEN}" "${RESET}" "${pattern}"
  fi
done

# The architecture the documents describe must be the one that is implemented.
echo
echo "Current architecture is documented:"
declare -A MUST_MENTION=(
  ["docs/CLOUDFLARE_TUNNEL_SETUP.md"]="http://api:8000"
  ["docs/SIMPLE_CICD.md"]="deploy_production.sh"
  ["docs/GITHUB_SECRETS.md"]="EC2_SSH_PRIVATE_KEY"
  ["docs/ROLLBACK.md"]="previous-release"
  ["docs/EC2_DEPLOYMENT.md"]="/opt/techsara-chat-archive"
  ["docs/CAPACITY.md"]="max_connections"
  ["docs/BACKUP_RESTORE.md"]="test_restore.sh"
  ["docs/CHROME_EXTENSION.md"]="complete_current_page"
  ["docs/GOOGLE_OAUTH_SETUP.md"]="chromiumapp.org"
)
for doc in "${!MUST_MENTION[@]}"; do
  phrase="${MUST_MENTION[${doc}]}"
  if [ -f "${doc}" ] && grep -qF "${phrase}" "${doc}"; then
    printf "%sok     %s %s mentions \"%s\"\n" "${GREEN}" "${RESET}" "${doc}" "${phrase}"
  else
    printf "%smissing%s %s must mention \"%s\"\n" "${RED}" "${RESET}" "${doc}" "${phrase}"
    missing=$((missing + 1))
  fi
done

echo
if [ "${missing}" -gt 0 ]; then
  echo "${RED}documentation check failed (${missing} problem(s))${RESET}"
  exit 1
fi
echo "${GREEN}documentation check passed${RESET}"
