#!/usr/bin/env bash
# Verify every required document exists and carries real content, and that the
# capture-limitations document actually states the limitations it must state.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

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
  docs/AWS_STEP_BY_STEP.md
  docs/CHROME_ENTERPRISE_DEPLOYMENT.md
  docs/LOCAL_DEVELOPMENT.md
  docs/PRODUCTION_DEPLOYMENT.md
  docs/SECURITY.md
  docs/SECURITY_REVIEW.md
  docs/PRIVACY_AND_EMPLOYEE_NOTICE.md
  docs/CAPTURE_LIMITATIONS.md
  docs/COMPLIANCE_ADAPTER.md
  docs/DATABASE.md
  docs/PGADMIN_ACCESS.md
  docs/BACKUP_AND_RESTORE.md
  docs/DISASTER_RECOVERY.md
  docs/MONITORING.md
  docs/SCALING_250_USERS.md
  docs/INCIDENT_RUNBOOK.md
  docs/TESTING.md
  docs/OPERATIONS.md
  scripts/aws-console-checklist.md
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

echo
if [ "${missing}" -gt 0 ]; then
  echo "${RED}documentation check failed (${missing} problem(s))${RESET}"
  exit 1
fi
echo "${GREEN}documentation check passed${RESET}"
