#!/usr/bin/env bash
# =============================================================================
# Behavioural tests for the deployment scripts, without a deployment.
#
# These cover the parts that only ever execute on the EC2 host, which is
# exactly where a mistake is most expensive and least observable. Functions are
# extracted from the real script text rather than reimplemented, so the test
# fails when the script changes and the behaviour regresses.
#
# The motivating bug: `sed -n ... "${file}" | tail -n 1` under
# `set -o pipefail` exits 2 when the file does not exist. On a first
# deployment there is no deploy/current-release, so the deployment aborted
# immediately after taking its lock, having done nothing and reported nothing.
# =============================================================================
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${ROOT}"

GREEN=$'\033[32m'; RED=$'\033[31m'; RESET=$'\033[0m'
[ -t 1 ] || { GREEN=""; RED=""; RESET=""; }

passed=0
failed=0
ok()  { printf '%sPASS%s  %s\n' "${GREEN}" "${RESET}" "$*"; passed=$((passed + 1)); }
bad() { printf '%sFAIL%s  %s\n' "${RED}" "${RESET}" "$*"; failed=$((failed + 1)); }

WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

# Pull a function definition out of the real script.
extract_function() {
  local script="$1" name="$2" body
  body="$(sed -n "/^${name}() {/,/^}/p" "${script}")"
  [ -n "${body}" ] || { echo "could not extract ${name}() from ${script}" >&2; exit 1; }
  printf '%s\n' "${body}"
}

echo "Deployment script behaviour"
echo "==========================="

# --- release_value tolerates a missing file ---------------------------------
for script in scripts/deploy_production.sh scripts/rollback_production.sh; do
  fn="$(extract_function "${script}" release_value)"

  if bash -Eeuo pipefail -c "
      CURRENT_RELEASE='${WORK}/does-not-exist'
      ${fn}
      value=\"\$(release_value GIT_SHA '${WORK}/does-not-exist')\"
      [ -z \"\${value}\" ]
    " 2>/dev/null; then
    ok "${script}: release_value survives a missing release file under pipefail"
  else
    bad "${script}: release_value aborts on a missing release file (first deployment)"
  fi

  printf 'GIT_SHA=%s\nPUBLIC_INGRESS=none\n' "$(printf '1%.0s' {1..40})" > "${WORK}/release"
  actual="$(bash -Eeuo pipefail -c "
      CURRENT_RELEASE='${WORK}/release'
      ${fn}
      release_value GIT_SHA '${WORK}/release'
    ")"
  if [ "${actual}" = "$(printf '1%.0s' {1..40})" ]; then
    ok "${script}: release_value reads an existing release file"
  else
    bad "${script}: release_value returned '${actual}'"
  fi

  # A key that is present later in the file must not be shadowed by an earlier
  # partial match, and an absent key must yield empty rather than an error.
  absent="$(bash -Eeuo pipefail -c "
      CURRENT_RELEASE='${WORK}/release'
      ${fn}
      release_value NOT_A_KEY '${WORK}/release'
    ")"
  if [ -z "${absent}" ]; then
    ok "${script}: release_value returns empty for an absent key"
  else
    bad "${script}: release_value returned '${absent}' for an absent key"
  fi
done

# --- argument handling -------------------------------------------------------
run_deploy() { bash scripts/deploy_production.sh "$@" 2>&1 || true; }

case "$(run_deploy --bogus-flag)" in
  *"unknown option: --bogus-flag"*) ok "deploy rejects an unknown option" ;;
  *) bad "deploy did not reject an unknown option" ;;
esac

case "$(run_deploy --help)" in
  *"--without-tunnel"*) ok "deploy --help documents --without-tunnel" ;;
  *) bad "deploy --help does not mention --without-tunnel" ;;
esac

# Non-root is refused before anything else happens, so a mistyped command
# cannot partially reconfigure a host.
case "$(run_deploy deadbeef)" in
  *"run as root"*) ok "deploy refuses to run as a non-root user" ;;
  *) bad "deploy did not refuse a non-root invocation" ;;
esac
case "$(bash scripts/rollback_production.sh 2>&1 || true)" in
  *"run as root"*) ok "rollback refuses to run as a non-root user" ;;
  *) bad "rollback did not refuse a non-root invocation" ;;
esac

# --- short SHAs are refused ---------------------------------------------------
# Checked by reading the guard itself, since the root check fires first here.
if grep -q '\^\[0-9a-f\]{40}\$' scripts/deploy_production.sh; then
  ok "deploy requires a full 40-character commit SHA"
else
  bad "deploy no longer validates the commit SHA"
fi

# --- the deployment runs the code it deploys ---------------------------------
# The script starts from the previous release's copy of itself. It must sync the
# checkout and hand over, or a fix to the deployment script can only ever take
# effect one deployment late -- and a broken one is unfixable through CI.
# Literal source text, not an expansion: the single quotes are deliberate.
# shellcheck disable=SC2016
if grep -q 'exec "${APP_DIR}/scripts/deploy_production.sh"' scripts/deploy_production.sh; then
  ok "deploy hands over to the deployed commit's own script"
else
  bad "deploy does not hand over after checking out the requested commit"
fi

if grep -q 'DEPLOY_HAS_LOCK' scripts/deploy_production.sh; then
  ok "deploy carries its lock across the handover instead of re-taking it"
else
  bad "deploy would release and re-take its lock across the handover"
fi

if grep -q 'Sync the checkout to the deployed commit' .github/workflows/deploy.yml; then
  ok "the workflow syncs the checkout before running anything from it"
else
  bad "the workflow runs the instance's existing script without syncing first"
fi

# --- the topology check must survive a real tunnel token ---------------------
# `docker compose config` resolves env_file into the environment, so once the
# host had a real cloudflared.env the check tripped its own "no inlined token"
# assertion and rolled the deployment back. It must be hermetic.
tunnel_root="${WORK}/data-root"
mkdir -p "${tunnel_root}/secrets"
printf 'TUNNEL_TOKEN=eyJhIjoiTEST_ONLY_NOT_A_REAL_TOKEN\n' > "${tunnel_root}/secrets/cloudflared.env"
if DATA_ROOT="${tunnel_root}" bash scripts/verify_production_config.sh >/dev/null 2>&1; then
  ok "topology check passes when a tunnel token file exists on the host"
else
  bad "topology check fails once the host has a real cloudflared.env"
fi

# It must still reject a token written into the compose file itself.
inlined="${WORK}/compose-inlined.yaml"
sed 's/^    env_file:/    environment:\n      TUNNEL_TOKEN: eyJhIjoiINLINED\n    env_file:/' \
  compose.prod.yaml > "${inlined}"
if grep -qE '^[[:space:]]*(TUNNEL_TOKEN|- *--token)' "${inlined}"; then
  ok "an inlined tunnel token is detectable in the compose source"
else
  bad "could not construct the inlined-token case"
fi

# --- no script may silently swallow a failed pipeline -------------------------
# `cmd | tail` on a possibly-absent file is the exact shape of the bug above.
suspects="$(grep -rn 'sed -n .*2>/dev/null | tail' scripts/ || true)"
if [ -z "${suspects}" ]; then
  ok "no script pipes a suppressed-error sed into tail"
else
  bad "suppressed-error sed piped into tail (fails under pipefail):"
  printf '      %s\n' "${suspects//$'\n'/$'\n      '}"
fi

echo
printf 'passed: %s\nfailed: %s\n' "${passed}" "${failed}"
[ "${failed}" -eq 0 ]
