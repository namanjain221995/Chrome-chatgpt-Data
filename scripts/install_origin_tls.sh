#!/usr/bin/env bash
# Validate and install a Cloudflare Origin CA certificate for the API container.
set -euo pipefail

DATA_ROOT="${DATA_ROOT:-/srv/techsara-chat-archive}"
CERT_SOURCE=""
KEY_SOURCE=""

while [ $# -gt 0 ]; do
  case "$1" in
    --cert-file) CERT_SOURCE="${2:?certificate file required}"; shift 2 ;;
    --key-file) KEY_SOURCE="${2:?private-key file required}"; shift 2 ;;
    --data-root) DATA_ROOT="${2:?data root required}"; shift 2 ;;
    --help|-h) sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)" >&2; exit 1; }
[ -f "${CERT_SOURCE}" ] || { echo "--cert-file must name a PEM certificate" >&2; exit 1; }
[ -f "${KEY_SOURCE}" ] || { echo "--key-file must name its PEM private key" >&2; exit 1; }
command -v openssl >/dev/null || { echo "openssl is required" >&2; exit 1; }

openssl x509 -in "${CERT_SOURCE}" -noout >/dev/null
openssl pkey -in "${KEY_SOURCE}" -noout -check >/dev/null
openssl x509 -in "${CERT_SOURCE}" -checkend 604800 -noout >/dev/null || {
  echo "origin certificate expires in less than seven days" >&2
  exit 1
}

cert_key_hash="$(openssl x509 -in "${CERT_SOURCE}" -pubkey -noout \
  | openssl pkey -pubin -outform der 2>/dev/null | sha256sum | cut -d' ' -f1)"
private_key_hash="$(openssl pkey -in "${KEY_SOURCE}" -pubout -outform der 2>/dev/null \
  | sha256sum | cut -d' ' -f1)"
[ "${cert_key_hash}" = "${private_key_hash}" ] || {
  echo "certificate and private key do not match" >&2
  exit 1
}

install -d -o root -g 10001 -m 0750 "${DATA_ROOT}/tls"
install -o root -g 10001 -m 0440 "${CERT_SOURCE}" "${DATA_ROOT}/tls/origin.pem"
install -o root -g 10001 -m 0440 "${KEY_SOURCE}" "${DATA_ROOT}/tls/origin.key"

openssl x509 -in "${DATA_ROOT}/tls/origin.pem" -noout -subject -issuer -dates
echo "origin TLS material installed for the API container"
