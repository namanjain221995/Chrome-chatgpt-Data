#!/usr/bin/env bash
# =============================================================================
# One-time preparation of the Amazon Linux 2023 EC2 host.
#
#   sudo ./scripts/bootstrap_ec2_host.sh                       # storage only
#   sudo ./scripts/bootstrap_ec2_host.sh --data-device /dev/nvme1n1
#
# Idempotent. With --data-device it formats (only when unformatted) and mounts
# a dedicated EBS volume at /srv/techsara-chat-archive so that the database and
# backups survive an instance replacement. Without it, the layout is created on
# the root volume.
#
# It never formats a device that already carries a filesystem it did not make.
# =============================================================================
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT_NAME="${PROJECT_NAME:-techsara-chat-archive}"
MOUNT_POINT="${DATA_ROOT:-/srv/${PROJECT_NAME}}"
DATA_DEVICE=""

log() { printf '[bootstrap] %s\n' "$*"; }
die() { printf '[bootstrap] ERROR: %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --data-device) DATA_DEVICE="${2:?device required}"; shift 2 ;;
    --mount-point) MOUNT_POINT="${2:?mount point required}"; shift 2 ;;
    --help|-h) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

[ "$(id -u)" -eq 0 ] || die "run as root (sudo $0)"

# --- Optional dedicated data volume ----------------------------------------
if [ -n "${DATA_DEVICE}" ]; then
  [ -b "${DATA_DEVICE}" ] || die "--data-device must name an attached block device"

  fs_type="$(blkid -o value -s TYPE "${DATA_DEVICE}" 2>/dev/null || true)"
  if [ -z "${fs_type}" ]; then
    log "formatting ${DATA_DEVICE} as ext4"
    mkfs.ext4 -L techsara-data "${DATA_DEVICE}"
  elif [ "${fs_type}" != "ext4" ]; then
    die "refusing ${DATA_DEVICE}: expected ext4 or unformatted, found ${fs_type}"
  else
    log "${DATA_DEVICE} already carries an ext4 filesystem; leaving it alone"
  fi

  uuid="$(blkid -o value -s UUID "${DATA_DEVICE}")"
  mkdir -p "${MOUNT_POINT}"
  if ! grep -q "UUID=${uuid}" /etc/fstab; then
    log "adding ${MOUNT_POINT} to /etc/fstab"
    printf 'UUID=%s %s ext4 defaults,nofail,x-systemd.device-timeout=30 0 2\n' \
      "${uuid}" "${MOUNT_POINT}" >> /etc/fstab
  fi
  mount "${MOUNT_POINT}" 2>/dev/null || mount -a
  mountpoint -q "${MOUNT_POINT}" || die "${MOUNT_POINT} is not mounted"
  log "${DATA_DEVICE} mounted at ${MOUNT_POINT}"
fi

# --- Packages ---------------------------------------------------------------
log "installing host packages"
dnf install -y --setopt=install_weak_deps=False \
  git jq openssl unzip tar util-linux awscli-2 >/dev/null

command -v aws >/dev/null || die "AWS CLI is not on PATH after installation"
aws --version

# --- Docker -----------------------------------------------------------------
DATA_ROOT="${MOUNT_POINT}" "${ROOT}/scripts/install_docker.sh"

# --- Directory layout -------------------------------------------------------
DATA_ROOT="${MOUNT_POINT}" "${ROOT}/scripts/prepare_server_storage.sh"

log "host bootstrap complete"
log "next: create the SSM parameters, then run scripts/deploy_production.sh <sha>"
