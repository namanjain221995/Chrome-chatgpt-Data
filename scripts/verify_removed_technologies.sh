#!/usr/bin/env bash
# Fail when any retired runtime or infrastructure implementation returns.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

proxy_name="$(printf '\143\141\144\144\171')"
iac_name="$(printf '\164\145\162\162\141\146\157\162\155')"
iac_alt_name="$(printf '\157\160\145\156\164\157\146\165')"
iac_short_name="$(printf '\164\157\146\165')"
store_name="$(printf '\155\151\156\151\157')"
web_server_name="$(printf '\156\147\151\156\170')"
failures=0

check_word() {
  local word="$1" hits=""
  hits="$(git grep -inI -- "${word}" -- . ':!Makefile' 2>/dev/null || true)"
  if [ -n "${hits}" ]; then
    printf 'retired technology reference found (%s):\n%s\n' "${word}" "${hits}" >&2
    failures=$((failures + 1))
  fi

  hits="$(grep -inI -- "${word}" Makefile 2>/dev/null | grep -v "verify-no-${word}" || true)"
  if [ -n "${hits}" ]; then
    printf 'unexpected Makefile reference found (%s):\n%s\n' "${word}" "${hits}" >&2
    failures=$((failures + 1))
  fi
}

check_word "${proxy_name}"
check_word "${iac_name}"
check_word "${iac_alt_name}"
check_word "${iac_short_name}"
check_word "${store_name}"
check_word "${web_server_name}"

iac_files="$({ git ls-files | while IFS= read -r file; do [ ! -e "${file}" ] || printf '%s\n' "${file}"; done; } \
  | grep -Ei '\.tf($|\.)|\.tfvars$|\.tfstate' || true)"
if [ -n "${iac_files}" ]; then
  printf 'retired infrastructure files found:\n%s\n' "${iac_files}" >&2
  failures=$((failures + 1))
fi

[ "${failures}" -eq 0 ] || exit 1
echo "retired technology scan passed"
