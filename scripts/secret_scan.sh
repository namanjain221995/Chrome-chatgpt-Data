#!/usr/bin/env bash
# =============================================================================
# Secret scan.
#
# Catches credentials committed to the working tree. Deliberately noisy about
# real secret shapes and quiet about the documented placeholders, so that a
# genuine finding is never buried in false positives.
# =============================================================================
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

RED=$'\033[31m'; GREEN=$'\033[32m'; RESET=$'\033[0m'
[ -t 1 ] || { RED=""; GREEN=""; RESET=""; }

findings=0

EXCLUDES=(
  --exclude-dir=.git --exclude-dir=node_modules --exclude-dir=.venv
  --exclude-dir=dist --exclude-dir=artifacts --exclude-dir=__pycache__
  --exclude-dir=.terraform --exclude-dir=.ruff_cache --exclude-dir=.pytest_cache
  --exclude-dir=.mypy_cache --exclude-dir=coverage
  --exclude=package-lock.json --exclude=secret_scan.sh
)

# Documented, intentionally non-secret placeholders.
ALLOWLIST='devonly_|REPLACE|replace-me|changeme|example\.com|smoke-test|not-for-production|test-client|placeholder|your-|<.*>|xxxx|aaaa|minioadmin|strongpassword'

# Test suites deliberately contain secret-*shaped* fixtures (a sample JWT, a
# throwaway database URL). Shape heuristics skip them; the patterns that match
# only real credential formats (AWS keys, private keys, provider tokens) do not.
TEST_PATHS='/tests?/|_test\.|\.test\.|conftest\.py|/fixtures/'

scan() {
  local label="$1" pattern="$2" skip_tests="${3:-no}"
  local hits
  hits="$(grep -rniE "${pattern}" "${EXCLUDES[@]}" . 2>/dev/null | grep -viE "${ALLOWLIST}" || true)"
  if [ "${skip_tests}" = "skip-tests" ] && [ -n "${hits}" ]; then
    hits="$(printf '%s\n' "${hits}" | grep -vE "${TEST_PATHS}" || true)"
  fi
  if [ -n "${hits}" ]; then
    echo "${RED}FINDING${RESET} ${label}"
    echo "${hits}" | head -10 | sed 's/^/        /'
    findings=$((findings + 1))
  else
    echo "${GREEN}ok     ${RESET} ${label}"
  fi
}

echo "Secret scan"
echo "==========="

scan "AWS access key ids"        'AKIA[0-9A-Z]{16}'
scan "AWS secret access keys"    'aws_secret_access_key[[:space:]]*[=:][[:space:]]*["'"'"']?[A-Za-z0-9/+=]{40}'
scan "private keys"              '-----BEGIN (RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----'
scan "Google OAuth client secrets" 'GOCSPX-[A-Za-z0-9_-]{20,}'
scan "OpenAI API keys"           'sk-[A-Za-z0-9]{20,}'
scan "GitHub tokens"             'gh[pousr]_[A-Za-z0-9]{36,}'
scan "Slack tokens"              'xox[baprs]-[A-Za-z0-9-]{10,}'
scan "JWT literals"              'eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}' skip-tests
# A literal value only: anything starting with $ or containing {} is a variable.
scan "hardcoded passwords"       'password[[:space:]]*[=:][[:space:]]*["'"'"'][^"'"'"'$\{[:space:]][^"'"'"'[:space:]]{11,}["'"'"']' skip-tests
scan "database URLs with a password" 'postgres(ql)?(\+[a-z]+)?://[^:@/[:space:]$]+:[^@/[:space:]$\{]{8,}@' skip-tests

# A committed .env is the single most common real-world leak.
if [ -f .env ]; then
  echo "${RED}FINDING${RESET} a .env file exists in the working tree"
  echo "        .env is gitignored, but never commit it"
  findings=$((findings + 1))
else
  echo "${GREEN}ok     ${RESET} no .env file present"
fi

if git rev-parse --git-dir >/dev/null 2>&1; then
  if git ls-files --error-unmatch .env >/dev/null 2>&1; then
    echo "${RED}FINDING${RESET} .env is TRACKED BY GIT"
    findings=$((findings + 1))
  else
    echo "${GREEN}ok     ${RESET} .env is not tracked by git"
  fi
  tracked_secrets="$(git ls-files | grep -E '\.(pem|key|p12|pfx|jks)$|(^|/)(id_rsa|id_ed25519)$|\.tfvars$' | grep -v example || true)"
  if [ -n "${tracked_secrets}" ]; then
    echo "${RED}FINDING${RESET} secret-shaped files are tracked:"
    echo "${tracked_secrets}" | sed 's/^/        /'
    findings=$((findings + 1))
  else
    echo "${GREEN}ok     ${RESET} no secret-shaped files tracked"
  fi
fi

echo
if [ "${findings}" -gt 0 ]; then
  echo "${RED}secret scan failed with ${findings} finding(s)${RESET}"
  exit 1
fi
echo "${GREEN}secret scan passed${RESET}"
