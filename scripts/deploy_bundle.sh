#!/usr/bin/env bash
# =============================================================================
# Build the deployment bundle that gets copied to the EC2 instance.
#
# The bundle is everything the instance needs to run the stack and nothing else:
# production Compose, systemd units and operational scripts.
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

cp "${ROOT}/compose.prod.yaml" "${BUNDLE}/"
cp "${ROOT}/.env.example" "${BUNDLE}/"
cp -r "${ROOT}/deploy/." "${BUNDLE}/deploy/"
cp "${ROOT}/scripts/deploy_ec2.sh" \
   "${ROOT}/scripts/fetch_ssm_secrets.sh" \
   "${ROOT}/scripts/install_origin_tls.sh" \
   "${ROOT}/scripts/verify_deployment.sh" \
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

Secrets are fetched from SSM Parameter Store; none are in this bundle. Install
the Cloudflare Origin CA certificate and key with install_origin_tls.sh before
deployment. The API terminates origin TLS directly on port 443.
EOF

mkdir -p "${OUT_DIR}"
tar --sort=name --owner=0 --group=0 --numeric-owner \
    --mtime='UTC 2020-01-01' \
    -czf "${OUT_DIR}/${NAME}.tar.gz" -C "${STAGE}" "${NAME}"

sha256sum "${OUT_DIR}/${NAME}.tar.gz" | awk '{print $1}' > "${OUT_DIR}/${NAME}.tar.gz.sha256"

echo "bundle   ${OUT_DIR}/${NAME}.tar.gz"
echo "sha256   $(cat "${OUT_DIR}/${NAME}.tar.gz.sha256")"
echo "contents $(tar -tzf "${OUT_DIR}/${NAME}.tar.gz" | wc -l) files"
