#!/usr/bin/env bash
# =============================================================================
# Install a Chrome managed policy on a *developer* machine, so an unpacked
# extension can find the backend.
#
#   sudo ./scripts/install_dev_policy.sh --extension-id <id> \
#        --api-base-url https://archive.example.com
#
# The extension reads `apiBaseUrl` from `chrome.storage.managed` and from
# nowhere else: there is deliberately no options-page field and no compiled-in
# default, so a developer build cannot be pointed at an arbitrary server by
# whoever is using the browser. That is the right production property, and it
# means local testing needs a policy file too.
#
# This writes the same structure Chrome Enterprise delivers in production, so
# what you test is what employees will run.
#
# Run this on the workstation running the browser -- not on the EC2 host and
# not in CloudShell, neither of which has a browser to configure.
#
# Not for production machines: there the policy comes from Google Admin console
# or your MDM. See docs/CHROME_ENTERPRISE_DEPLOYMENT.md.
# =============================================================================
set -Eeuo pipefail

EXTENSION_ID=""
API_BASE_URL=""
ORG_SLUG="techsara"
BROWSER=""
REMOVE=0

usage() {
  sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
  cat <<'EOF'

Options:
  --extension-id <id>     Extension id from chrome://extensions. Required.
  --api-base-url <url>    HTTPS backend base URL. Required.
  --organization <slug>   Organization slug (default: techsara).
  --browser <name>        chrome | chromium. Autodetected when omitted.
  --remove                Delete the policy file instead of writing it.
EOF
  exit "${1:-2}"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --extension-id) EXTENSION_ID="${2:?}"; shift 2 ;;
    --api-base-url) API_BASE_URL="${2:?}"; shift 2 ;;
    --organization) ORG_SLUG="${2:?}"; shift 2 ;;
    --browser) BROWSER="${2:?}"; shift 2 ;;
    --remove) REMOVE=1; shift ;;
    --help|-h) usage 0 ;;
    *) echo "unknown argument: $1" >&2; usage ;;
  esac
done

die() { printf '[dev-policy] ERROR: %s\n' "$*" >&2; exit 1; }
log() { printf '[dev-policy] %s\n' "$*"; }

[ "$(id -u)" -eq 0 ] || die "run as root (sudo $0 ...)"

# Chrome, Chromium and Ubuntu's snap build each read from a different place,
# and a machine may have several installed.
CHROME_DIRS=(/etc/opt/chrome/policies/managed)
CHROMIUM_DIRS=(
  /etc/chromium/policies/managed
  /etc/chromium-browser/policies/managed
  /var/snap/chromium/current/policies/managed
)

declare -a POLICY_DIRS=()
case "${BROWSER}" in
  chrome)   POLICY_DIRS=("${CHROME_DIRS[@]}") ;;
  chromium) POLICY_DIRS=("${CHROMIUM_DIRS[@]}") ;;
  "")
    # Write wherever a browser is actually installed. Writing to all of them is
    # harmless and avoids "the policy is not applying" caused by guessing wrong.
    for candidate in "${CHROME_DIRS[@]}" "${CHROMIUM_DIRS[@]}"; do
      parent="$(dirname "$(dirname "${candidate}")")"
      [ -d "${parent}" ] && POLICY_DIRS+=("${candidate}")
    done
    if [ "${#POLICY_DIRS[@]}" -eq 0 ]; then
      die "no Chrome or Chromium installation found. Run this on the machine
       running the browser, not on a server or in CloudShell. Force a location
       with --browser chrome|chromium if you know better."
    fi
    ;;
  *) die "--browser must be chrome or chromium" ;;
esac

POLICY_FILE="techsara-chat-archive.json"

if [ "${REMOVE}" -eq 1 ]; then
  for dir in "${POLICY_DIRS[@]}"; do
    if [ -f "${dir}/${POLICY_FILE}" ]; then
      rm -f "${dir}/${POLICY_FILE}"
      log "removed ${dir}/${POLICY_FILE}"
    fi
  done
  log "restart the browser for the change to take effect"
  exit 0
fi

[ -n "${EXTENSION_ID}" ] || { echo "--extension-id is required" >&2; usage; }
[ -n "${API_BASE_URL}" ] || { echo "--api-base-url is required" >&2; usage; }

# The extension refuses a non-HTTPS backend unless it is loopback, so catch a
# typo here rather than in a browser console.
case "${API_BASE_URL}" in
  https://*|http://localhost*|http://127.0.0.1*) ;;
  *) die "--api-base-url must be https:// (or a loopback address for local work)" ;;
esac
[[ "${EXTENSION_ID}" =~ ^[a-p]{32}$ ]] \
  || die "--extension-id must be 32 characters, a-p (copy it from chrome://extensions)"

for dir in "${POLICY_DIRS[@]}"; do
  install -d -m 0755 "${dir}"
  cat > "${dir}/${POLICY_FILE}" <<EOF
{
  "3rdparty": {
    "extensions": {
      "${EXTENSION_ID}": {
        "apiBaseUrl": "${API_BASE_URL%/}",
        "organizationSlug": "${ORG_SLUG}",
        "enabled": true
      }
    }
  }
}
EOF
  chmod 0644 "${dir}/${POLICY_FILE}"
  log "wrote ${dir}/${POLICY_FILE}"
done

cat <<EOF

[dev-policy] extension  ${EXTENSION_ID}
[dev-policy] backend    ${API_BASE_URL%/}

Next:
  1. Fully quit the browser (closing the window is not enough). For the snap
     build of Chromium: pkill chromium, then reopen.
  2. Reopen it and visit chrome://policy -- "Show policies with no value" off.
     The 3rdparty entry should be listed. Press "Reload policies" if not.
  3. Open the extension's service worker console from chrome://extensions and
     confirm it fetches the runtime configuration.

Setting "enabled": true here does NOT enable capture. The server decides that,
and it currently answers capture_active=false. This policy only tells the
extension which backend to ask.
EOF
