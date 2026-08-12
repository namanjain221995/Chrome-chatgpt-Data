#!/usr/bin/env bash
# =============================================================================
# Build the deployment bundle that gets copied to the EC2 instance.
#
# The bundle is everything the instance needs to run the stack and nothing else:
# compose files, the Caddyfile, the systemd units and the operational scripts.
# No source code, no secrets, no .env.
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${ROOT}/artifacts"
STAGE="$(mktemp -d)"
trap 'rm -rf "${STAGE}"' EXIT

VERSION="${IMAGE_TAG:-$(git -C "${ROOT}" rev-parse --short HEAD 2>/dev/null || echo local)}"
NAME="techsara-chat-archive-deploy-${VERSION}"
BUNDLE="${STAGE}/${NAME}"

mkdir -p "${BUNDLE}"/{deploy,scripts}

cp "${ROOT}/compose.yaml" "${ROOT}/compose.prod.yaml" "${BUNDLE}/"
cp "${ROOT}/.env.example" "${BUNDLE}/"
cp -r "${ROOT}/deploy/." "${BUNDLE}/deploy/"
cp "${ROOT}/scripts/deploy_ec2.sh" \
   "${ROOT}/scripts/backup_postgres.sh" \
   "${ROOT}/scripts/backup_loop.sh" \
   "${ROOT}/scripts/restore_postgres.sh" \
   "${ROOT}/scripts/verify_backup.sh" \
   "${BUNDLE}/scripts/"
chmod +x "${BUNDLE}/scripts/"*.sh

cat > "${BUNDLE}/INSTALL.md" <<EOF
# TechSara ChatGPT Archive - deployment bundle ${VERSION}

Copy this directory to /opt/techsara-chat-archive on the EC2 instance, then:

    sudo cp deploy/systemd/techsara-chat-archive.service /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo IMAGE_TAG=${VERSION} ./scripts/deploy_ec2.sh
    sudo systemctl enable techsara-chat-archive

Secrets are read from SSM Parameter Store by deploy_ec2.sh; none are in this
bundle. Run scripts/put_secrets.sh from an administrator workstation first.
EOF

mkdir -p "${OUT_DIR}"
tar --sort=name --owner=0 --group=0 --numeric-owner \
    --mtime='UTC 2020-01-01' \
    -czf "${OUT_DIR}/${NAME}.tar.gz" -C "${STAGE}" "${NAME}"

sha256sum "${OUT_DIR}/${NAME}.tar.gz" | awk '{print $1}' > "${OUT_DIR}/${NAME}.tar.gz.sha256"

echo "bundle   ${OUT_DIR}/${NAME}.tar.gz"
echo "sha256   $(cat "${OUT_DIR}/${NAME}.tar.gz.sha256")"
echo "contents $(tar -tzf "${OUT_DIR}/${NAME}.tar.gz" | wc -l) files"
