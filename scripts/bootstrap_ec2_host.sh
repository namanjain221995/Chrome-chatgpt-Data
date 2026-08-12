#!/usr/bin/env bash
# Idempotently prepare the Ubuntu EC2 host and encrypted data volume.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DEVICE=""
MOUNT_POINT="/srv/techsara-chat-archive"

while [ $# -gt 0 ]; do
  case "$1" in
    --data-device) DATA_DEVICE="${2:?device required}"; shift 2 ;;
    --mount-point) MOUNT_POINT="${2:?mount point required}"; shift 2 ;;
    --help|-h) sed -n '2,18p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)" >&2; exit 1; }
[ -b "${DATA_DEVICE}" ] || { echo "--data-device must name the attached EBS block device" >&2; exit 1; }

fs_type="$(blkid -o value -s TYPE "${DATA_DEVICE}" 2>/dev/null || true)"
if [ -z "${fs_type}" ]; then
  mkfs.ext4 -L techsara-data "${DATA_DEVICE}"
elif [ "${fs_type}" != "ext4" ]; then
  echo "refusing device ${DATA_DEVICE}: expected ext4 or unformatted, found ${fs_type}" >&2
  exit 1
fi

uuid="$(blkid -o value -s UUID "${DATA_DEVICE}")"
mkdir -p "${MOUNT_POINT}"
if ! grep -q "UUID=${uuid}" /etc/fstab; then
  printf 'UUID=%s %s ext4 defaults,nofail,x-systemd.device-timeout=30 0 2\n' \
    "${uuid}" "${MOUNT_POINT}" >> /etc/fstab
fi
mount "${MOUNT_POINT}" 2>/dev/null || mount -a
mountpoint -q "${MOUNT_POINT}"

install -d -o root -g root -m 0750 \
  "${MOUNT_POINT}/postgres" "${MOUNT_POINT}/backups" \
  "${MOUNT_POINT}/secrets" "${MOUNT_POINT}/tls" "${MOUNT_POINT}/pgadmin"
chown 70:70 "${MOUNT_POINT}/postgres"
chown 10001:10001 "${MOUNT_POINT}/backups"
chown 5050:5050 "${MOUNT_POINT}/pgadmin"
chown root:10001 "${MOUNT_POINT}/secrets"
chown root:10001 "${MOUNT_POINT}/tls"
chmod 0700 "${MOUNT_POINT}/postgres" "${MOUNT_POINT}/pgadmin"
chmod 0750 "${MOUNT_POINT}/secrets" "${MOUNT_POINT}/tls"

"${ROOT}/scripts/install_docker.sh"
apt-get install -y --no-install-recommends jq openssl unzip
cli_dir="$(mktemp -d)"
trap 'rm -rf "${cli_dir}"' EXIT
machine="$(uname -m)"
case "${machine}" in
  x86_64|aarch64) ;;
  *) echo "unsupported architecture for AWS CLI v2: ${machine}" >&2; exit 1 ;;
esac
curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-${machine}.zip" \
  -o "${cli_dir}/awscliv2.zip"
unzip -q "${cli_dir}/awscliv2.zip" -d "${cli_dir}"
"${cli_dir}/aws/install" --update
aws --version
echo "host bootstrap complete; reboot once before production deployment"
