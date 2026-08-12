#!/usr/bin/env bash
# Assert the production Compose topology and network exposure contract.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

export IMAGE_REPOSITORY="${IMAGE_REPOSITORY:-ghcr.io/example/backend}"
export IMAGE_TAG="${IMAGE_TAG:-validation-sha}"
export PUBLIC_BASE_URL="${PUBLIC_BASE_URL:-https://archive.example.com}"
export ARCHIVE_HOSTNAME="${ARCHIVE_HOSTNAME:-archive.example.com}"
export POSTGRES_DB="${POSTGRES_DB:-techsara_chat_archive}"
export POSTGRES_USER="${POSTGRES_USER:-techsara_app}"
export OIDC_ISSUER="${OIDC_ISSUER:-https://accounts.google.com}"
export OIDC_CLIENT_ID="${OIDC_CLIENT_ID:-validation-client}"
export OIDC_REQUIRED_HD="${OIDC_REQUIRED_HD:-example.com}"
export ALLOWED_EMAIL_DOMAINS="${ALLOWED_EMAIL_DOMAINS:-example.com}"
export MANAGED_WORKSPACE_LABEL="${MANAGED_WORKSPACE_LABEL:-Managed Workspace}"
export PGADMIN_DEFAULT_EMAIL="${PGADMIN_DEFAULT_EMAIL:-dba@example.com}"

config_file="$(mktemp)"
trap 'rm -f "${config_file}"' EXIT
docker compose -f compose.prod.yaml --profile '*' config --format json > "${config_file}"

python3 - "${config_file}" <<'PY'
import json
import sys

config = json.load(open(sys.argv[1], encoding="utf-8"))
services = config["services"]
expected = {"postgres", "migrate", "api", "worker", "compliance-poller", "backup", "pgadmin"}
assert set(services) == expected, sorted(services)

for name, service in services.items():
    ports = service.get("ports", [])
    if name == "api":
        assert len(ports) == 1
        assert ports[0]["host_ip"] == "0.0.0.0"
        assert int(ports[0]["published"]) == 443
        assert int(ports[0]["target"]) == 8443
        command = services[name]["command"]
        assert "--certfile" in command and "--keyfile" in command
    elif name == "pgadmin":
        assert service.get("profiles") == ["admin"]
        assert len(ports) == 1
        assert ports[0]["host_ip"] == "127.0.0.1"
        assert int(ports[0]["published"]) == 5050
        assert int(ports[0]["target"]) == 80
    else:
        assert ports == [], f"{name} unexpectedly publishes {ports}"

assert config["networks"]["backend"]["internal"] is True
for name in ("api", "worker", "migrate", "compliance-poller", "backup"):
    env = services[name]["environment"]
    assert env["AWS_REGION"] == "us-east-1"
    assert env["S3_BUCKET"] == "techsara-chatgpt"
    assert env["S3_ENDPOINT_URL"] == ""
    assert env["S3_USE_PATH_STYLE"] == "false"
    assert "AWS_ACCESS_KEY_ID" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env

api_env = services["api"]["environment"]
assert api_env["ARCHIVE_HOSTNAME"] == "archive.example.com"
assert api_env["BROWSER_CONTENT_CAPTURE_ENABLED"] == "false"
assert api_env["OPENAI_WRITTEN_AUTHORIZATION_CONFIRMED"] == "false"
assert api_env["TRAINING_EXPORT_ENABLED"] == "false"
api_volumes = services["api"]["volumes"]
targets = {volume["target"] for volume in api_volumes}
assert "/run/tls/origin.pem" in targets
assert "/run/tls/origin.key" in targets
print("production Compose exposure and S3 configuration passed")
PY
