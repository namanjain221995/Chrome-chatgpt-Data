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
cd "${ROOT}" || exit 1

RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RESET=$'\033[0m'
[ -t 1 ] || { RED=""; GREEN=""; YELLOW=""; RESET=""; }

findings=0

EXCLUDES=(
  --exclude-dir=.git --exclude-dir=node_modules --exclude-dir=.venv
  --exclude-dir=dist --exclude-dir=artifacts --exclude-dir=__pycache__
  --exclude-dir=.ruff_cache --exclude-dir=.pytest_cache
  --exclude-dir=.mypy_cache --exclude-dir=coverage
  --exclude=package-lock.json --exclude=secret_scan.sh
)

# Content scanning covers exactly the files git can publish: everything already
# tracked, plus untracked files that .gitignore does not cover. Gitignored
# material cannot reach a commit, and is reported separately by the
# key-shaped-file checks below rather than being pattern-matched here.
SCAN_LIST="$(mktemp)"
trap 'rm -f "${SCAN_LIST}"' EXIT
if git rev-parse --git-dir >/dev/null 2>&1; then
  git ls-files -z --cached --others --exclude-standard \
    | tr '\0' '\n' \
    | grep -v -e '^scripts/secret_scan.sh$' -e 'package-lock.json$' \
    > "${SCAN_LIST}" || true
  SCAN_MODE=git
else
  SCAN_MODE=tree
fi

# Documented, intentionally non-secret placeholders.
ALLOWLIST='devonly_|REPLACE|replace-me|changeme|example\.com|smoke-test|not-for-production|test-client|placeholder|your-|<.*>|xxxx|aaaa|strongpassword'

# Test suites deliberately contain secret-*shaped* fixtures (a sample JWT, a
# throwaway database URL). Shape heuristics skip them; the patterns that match
# only real credential formats (AWS keys, private keys, provider tokens) do not.
TEST_PATHS='/tests?/|_test\.|\.test\.|conftest\.py|/fixtures/'

scan() {
  local label="$1" pattern="$2" skip_tests="${3:-no}"
  local hits
  # `-e` is mandatory: several patterns below start with `-`, and passing them
  # positionally makes grep parse them as options, silently reporting "ok".
  if [ "${SCAN_MODE}" = git ]; then
    hits="$(xargs -a "${SCAN_LIST}" -d '\n' -r grep -niHE -e "${pattern}" 2>/dev/null \
      | grep -viE "${ALLOWLIST}" || true)"
  else
    hits="$(grep -rniE -e "${pattern}" "${EXCLUDES[@]}" . 2>/dev/null | grep -viE "${ALLOWLIST}" || true)"
  fi
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
scan "private keys"              '-----BEGIN ([A-Z]+ )?PRIVATE KEY-----'
scan "SSH private key material"  'PuTTY-User-Key-File|ssh-rsa AAAA[A-Za-z0-9+/]{100,} PRIVATE'
scan "Google OAuth client secrets" 'GOCSPX-[A-Za-z0-9_-]{20,}'
scan "OpenAI API keys"           'sk-[A-Za-z0-9]{20,}'
scan "OpenAI project keys"       'sk-(proj|svcacct|admin)-[A-Za-z0-9_-]{20,}'
scan "GitHub tokens"             'gh[pousr]_[A-Za-z0-9]{36,}'
scan "GitHub fine-grained tokens" 'github_pat_[A-Za-z0-9_]{60,}'
scan "Slack tokens"              'xox[baprs]-[A-Za-z0-9-]{10,}'
# Cloudflare: tunnel tokens are long base64 blobs assigned to a TUNNEL_TOKEN
# variable or passed to `cloudflared --token`; API tokens are 40 chars.
scan "Cloudflare tunnel tokens"  '(TUNNEL_TOKEN|--token)[[:space:]]*[=:[:space:]][[:space:]]*["'"'"']?ey[A-Za-z0-9+/=_-]{40,}' skip-tests
scan "Cloudflare API tokens"     'cloudflare[_-]?api[_-]?(token|key)[[:space:]]*[=:][[:space:]]*["'"'"']?[A-Za-z0-9_-]{37,}'
scan "JWT literals"              'eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}' skip-tests
# JWT/config signing keys assigned a literal value rather than read from a file.
scan "signing secrets"           '(jwt_secret|config_signing_key)[[:space:]]*[=:][[:space:]]*["'"'"'][^"'"'"'$\{[:space:]][^"'"'"'[:space:]]{15,}' skip-tests
# A literal value only: anything starting with $ or containing {} is a variable.
scan "hardcoded passwords"       'password[[:space:]]*[=:][[:space:]]*["'"'"'][^"'"'"'$\{[:space:]][^"'"'"'[:space:]]{11,}["'"'"']' skip-tests
scan "database URLs with a password" 'postgres(ql)?(\+[a-z]+)?://[^:@/[:space:]$]+:[^@/[:space:]$\{]{8,}@' skip-tests

# A committed environment file is the single most common real-world leak.
env_present=0
for candidate in .env .env.production .env.local; do
  if [ -f "${candidate}" ]; then
    echo "${RED}FINDING${RESET} ${candidate} exists in the working tree"
    echo "        it is gitignored, but production configuration belongs on the"
    echo "        EC2 host only - never in a developer checkout"
    findings=$((findings + 1))
    env_present=1
  fi
done
[ "${env_present}" -eq 0 ] && echo "${GREEN}ok     ${RESET} no environment file present"

if git rev-parse --git-dir >/dev/null 2>&1; then
  # `*.example` files are templates by convention. They are still subject to
  # every content check above, so a real value inside one is still a finding.
  tracked_env="$(git ls-files | grep -E '(^|/)\.env($|\.)' | grep -v '\.example$' || true)"
  if [ -n "${tracked_env}" ]; then
    echo "${RED}FINDING${RESET} an environment file is TRACKED BY GIT:"
    printf '        %s\n' "${tracked_env//$'\n'/$'\n        '}"
    findings=$((findings + 1))
  else
    echo "${GREEN}ok     ${RESET} no environment file is tracked by git"
  fi

  tracked_secrets="$(git ls-files | grep -E '\.(pem|key|p12|pfx|jks|ppk)$|(^|/)(id_rsa|id_ecdsa|id_ed25519)$' | grep -v example || true)"
  if [ -n "${tracked_secrets}" ]; then
    echo "${RED}FINDING${RESET} secret-shaped files are tracked:"
    printf '        %s\n' "${tracked_secrets//$'\n'/$'\n        '}"
    findings=$((findings + 1))
  else
    echo "${GREEN}ok     ${RESET} no secret-shaped files tracked"
  fi

  # A private key sitting in the working tree is only safe while .gitignore
  # keeps covering it. Untracked-but-ignored is reported loudly; untracked and
  # NOT ignored is a failure, because one `git add -A` would publish it.
  untracked_keys="$(git ls-files --others --exclude-standard \
    | grep -E '\.(pem|key|p12|pfx|jks|ppk)$|(^|/)(id_rsa|id_ecdsa|id_ed25519)$' || true)"
  if [ -n "${untracked_keys}" ]; then
    echo "${RED}FINDING${RESET} key-shaped files are present and NOT gitignored:"
    printf '        %s\n' "${untracked_keys//$'\n'/$'\n        '}"
    findings=$((findings + 1))
  else
    echo "${GREEN}ok     ${RESET} no unignored key-shaped files in the working tree"
  fi

  # Only report ignored files that really hold a private key: vendored CA
  # bundles are also called *.pem and contain certificates, not keys.
  ignored_keys=""
  while IFS= read -r candidate; do
    [ -f "${candidate}" ] || continue
    if grep -qlE -e '-----BEGIN ([A-Z]+ )?PRIVATE KEY-----' "${candidate}" 2>/dev/null; then
      ignored_keys="${ignored_keys}${candidate}"$'\n'
    fi
  done < <(git ls-files --others --ignored --exclude-standard \
    | grep -E '\.(pem|key|p12|pfx|jks|ppk)$|(^|/)(id_rsa|id_ecdsa|id_ed25519)$' || true)
  ignored_keys="${ignored_keys%$'\n'}"
  if [ -n "${ignored_keys}" ]; then
    echo "${YELLOW}warn   ${RESET} gitignored key material is present in this checkout:"
    printf '        %s\n' "${ignored_keys//$'\n'/$'\n        '}"
    echo "        git will not commit these, but keep private keys outside the"
    echo "        repository directory (see docs/SECURITY.md)"
  fi
fi

echo
if [ "${findings}" -gt 0 ]; then
  echo "${RED}secret scan failed with ${findings} finding(s)${RESET}"
  exit 1
fi
echo "${GREEN}secret scan passed${RESET}"
