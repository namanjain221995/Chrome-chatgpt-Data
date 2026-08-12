#!/usr/bin/env bash
# =============================================================================
# Install / verify Docker Engine and the Compose plugin on Amazon Linux 2023.
#
# The production instance already has both, so this script is normally a
# verification step. It is idempotent and safe to re-run.
# =============================================================================
set -Eeuo pipefail

# `env_file: required:` in compose.prod.yaml needs Compose v2.24 or newer.
MIN_COMPOSE_MAJOR=2
MIN_COMPOSE_MINOR=24

log() { printf '[docker] %s\n' "$*"; }
die() { printf '[docker] ERROR: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root (sudo $0)"

# shellcheck disable=SC1091
. /etc/os-release
case "${ID:-}:${VERSION_ID:-}" in
  amzn:2023*) log "Amazon Linux 2023 detected" ;;
  amzn:*) log "Amazon Linux ${VERSION_ID} detected; proceeding with dnf" ;;
  *) die "this installer supports Amazon Linux 2023 (found ${ID:-unknown} ${VERSION_ID:-})" ;;
esac

if ! command -v docker >/dev/null 2>&1; then
  log "installing Docker Engine"
  dnf install -y docker
else
  log "Docker Engine already installed"
fi

systemctl enable --now docker
docker version --format 'server {{.Server.Version}}'

if ! docker compose version >/dev/null 2>&1; then
  # Amazon Linux 2023 ships the engine but not always the Compose CLI plugin.
  log "installing the Docker Compose CLI plugin"
  plugin_dir=/usr/libexec/docker/cli-plugins
  install -d -m 0755 "${plugin_dir}"
  arch="$(uname -m)"
  case "${arch}" in
    x86_64|aarch64) ;;
    *) die "unsupported architecture for the Compose plugin: ${arch}" ;;
  esac
  curl -fsSL \
    "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-${arch}" \
    -o "${plugin_dir}/docker-compose"
  chmod 0755 "${plugin_dir}/docker-compose"
fi

compose_version="$(docker compose version --short)"
log "Docker Compose ${compose_version}"

major="${compose_version%%.*}"
rest="${compose_version#*.}"
minor="${rest%%.*}"
if [ "${major}" -lt "${MIN_COMPOSE_MAJOR}" ] ||
   { [ "${major}" -eq "${MIN_COMPOSE_MAJOR}" ] && [ "${minor}" -lt "${MIN_COMPOSE_MINOR}" ]; }; then
  die "Compose ${compose_version} is too old; compose.prod.yaml needs >= ${MIN_COMPOSE_MAJOR}.${MIN_COMPOSE_MINOR}"
fi

# The deployment user runs docker through sudo, so no group membership is
# granted here: adding a user to the `docker` group is equivalent to root.
log "Docker and Compose are ready"
