#!/usr/bin/env bash
# =============================================================================
# Assert the packaged extension contains only runtime files.
#
# The ZIP is what IT force-installs on ~250 managed profiles, so it must carry
# no credential, no environment file, no key material and no source map that
# would republish the original sources.
# =============================================================================
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

GREEN=$'\033[32m'; RED=$'\033[31m'; RESET=$'\033[0m'
[ -t 1 ] || { GREEN=""; RED=""; RESET=""; }

shopt -s nullglob
packages=(artifacts/techsara-chatgpt-extension-*.zip)
shopt -u nullglob

if [ "${#packages[@]}" -eq 0 ]; then
  echo "no extension package found in artifacts/; run: make extension-zip" >&2
  exit 1
fi

failures=0
pass() { printf '%sok     %s %s\n' "${GREEN}" "${RESET}" "$*"; }
fail() { printf '%sFAIL   %s %s\n' "${RED}" "${RESET}" "$*"; failures=$((failures + 1)); }

command -v unzip >/dev/null 2>&1 || { echo "unzip is required" >&2; exit 1; }

for package in "${packages[@]}"; do
  echo "Inspecting ${package}"
  listing="$(unzip -Z1 "${package}")"

  # --- Forbidden file shapes -------------------------------------------------
  forbidden="$(printf '%s\n' "${listing}" | grep -E \
    '(^|/)\.env($|\.)|\.map$|\.pem$|\.key$|\.p12$|\.pfx$|(^|/)\.git|(^|/)id_(rsa|ecdsa|ed25519)$|\.tar\.gz$' \
    || true)"
  if [ -n "${forbidden}" ]; then
    fail "forbidden files in the package:"
    printf '        %s\n' "${forbidden//$'\n'/$'\n        '}"
  else
    pass "no environment files, source maps or key material"
  fi

  # --- Required runtime files ------------------------------------------------
  for required in manifest.json service-worker.js content-script.js; do
    if printf '%s\n' "${listing}" | grep -qx "${required}"; then
      pass "contains ${required}"
    else
      fail "missing ${required}"
    fi
  done

  # --- Credential-shaped content --------------------------------------------
  work="$(mktemp -d)"
  # shellcheck disable=SC2064
  trap "rm -rf '${work}'" EXIT
  unzip -q "${package}" -d "${work}"

  # Patterns that would indicate a real credential was bundled at build time.
  # `AKIA`, provider key prefixes and a Cloudflare tunnel token shape.
  leaked="$(grep -rlE -e 'AKIA[0-9A-Z]{16}' \
                      -e 'sk-[A-Za-z0-9]{20,}' \
                      -e 'gh[pousr]_[A-Za-z0-9]{36,}' \
                      -e 'GOCSPX-[A-Za-z0-9_-]{20,}' \
                      -e 'TUNNEL_TOKEN' \
                      -e '-----BEGIN ([A-Z]+ )?PRIVATE KEY-----' \
                      -e 'postgres(ql)?(\+[a-z]+)?://[^:@/[:space:]]+:[^@/[:space:]]{8,}@' \
             "${work}" 2>/dev/null || true)"
  if [ -n "${leaked}" ]; then
    fail "credential-shaped content found in:"
    printf '        %s\n' "${leaked//$'\n'/$'\n        '}"
  else
    pass "no credential-shaped content"
  fi

  # The manifest must not have gained a broad content-script match.
  matches="$(python3 -c "
import json, sys
manifest = json.load(open('${work}/manifest.json', encoding='utf-8'))
for script in manifest.get('content_scripts', []):
    for pattern in script.get('matches', []):
        print(pattern)
")"
  bad_matches="$(printf '%s\n' "${matches}" | grep -vE '^https://(chatgpt\.com|chat\.openai\.com)/' || true)"
  if [ -n "${bad_matches}" ]; then
    fail "content script matches beyond ChatGPT: ${bad_matches}"
  else
    pass "content scripts are limited to ChatGPT origins"
  fi

  rm -rf "${work}"
  trap - EXIT

  size="$(wc -c < "${package}")"
  pass "package size ${size} bytes, $(printf '%s\n' "${listing}" | wc -l) files"
done

echo
if [ "${failures}" -gt 0 ]; then
  printf '%sextension package verification failed (%d problem(s))%s\n' "${RED}" "${failures}" "${RESET}"
  exit 1
fi
printf '%sextension package verification passed%s\n' "${GREEN}" "${RESET}"
