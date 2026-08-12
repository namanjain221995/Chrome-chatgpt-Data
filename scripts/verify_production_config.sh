#!/usr/bin/env bash
# =============================================================================
# Assert the production Compose topology, network exposure and S3 contract.
#
# This is the check that keeps the architecture honest: it fails if FastAPI or
# PostgreSQL ever gain a public port, if pgAdmin stops being loopback-only, if
# the Cloudflare Tunnel image loses its digest pin, or if a static AWS
# credential appears in a service environment.
# =============================================================================
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

export IMAGE_NAME="${IMAGE_NAME:-techsara-chat-archive-backend}"
export IMAGE_TAG="${IMAGE_TAG:-0000000000000000000000000000000000000000}"
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

EXPECTED = {
    "postgres", "migrate", "api", "worker",
    "cloudflared", "backup", "compliance-poller", "pgadmin",
}
assert set(services) == EXPECTED, f"unexpected service set: {sorted(services)}"

# --- Port exposure ---------------------------------------------------------
# Only two host bindings are allowed, and both are loopback-only management
# endpoints. Application traffic never touches a host port.
for name, service in services.items():
    ports = service.get("ports", [])
    if name == "cloudflared":
        assert len(ports) == 1, f"cloudflared should publish exactly one port, got {ports}"
        assert ports[0]["host_ip"] == "127.0.0.1", "the tunnel metrics port must be loopback-only"
        assert int(ports[0]["target"]) == 2000
    elif name == "pgadmin":
        assert service.get("profiles") == ["admin"], "pgAdmin must stay behind the admin profile"
        assert len(ports) == 1
        assert ports[0]["host_ip"] == "127.0.0.1", "pgAdmin must be loopback-only"
        assert int(ports[0]["published"]) == 5050
        assert int(ports[0]["target"]) == 80
    else:
        assert ports == [], f"{name} unexpectedly publishes {ports}"

# --- FastAPI ---------------------------------------------------------------
api = services["api"]
assert api.get("expose") == ["8000"], f"api must expose 8000 internally, got {api.get('expose')}"
command = " ".join(str(part) for part in api["command"])
assert "0.0.0.0:8000" in command, "api must bind 0.0.0.0:8000 inside the container"
assert "--certfile" not in command and "--keyfile" not in command, (
    "the API must not terminate TLS: Cloudflare does, and cloudflared is the only ingress"
)
api_volume_targets = {v["target"] for v in api.get("volumes", [])}
assert not any(t.startswith("/run/tls") for t in api_volume_targets), (
    "origin TLS material must not be mounted into the API any more"
)

# --- PostgreSQL ------------------------------------------------------------
postgres = services["postgres"]
assert postgres["networks"] == {"backend": None} or set(postgres["networks"]) == {"backend"}, (
    "PostgreSQL must be reachable only on the internal network"
)
pg_command = " ".join(str(part) for part in postgres["command"])
assert "max_connections=" in pg_command

# --- Cloudflare Tunnel -----------------------------------------------------
tunnel = services["cloudflared"]
assert "@sha256:" in tunnel["image"], "the cloudflared image must be digest-pinned"
tunnel_command = " ".join(str(part) for part in tunnel["command"])
assert "--no-autoupdate" in tunnel_command, "cloudflared must run with --no-autoupdate"
assert "trycloudflare" not in tunnel_command, "quick tunnels are not allowed in production"
assert "--token" not in tunnel_command, (
    "the tunnel token must come from the env file, not the command line"
)
assert set(tunnel["networks"]) == {"egress"}, (
    "the tunnel must reach the API and the internet, but never PostgreSQL"
)
assert "TUNNEL_TOKEN" not in (tunnel.get("environment") or {}), (
    "the tunnel token must not be inlined into the compose environment"
)

# --- Networks --------------------------------------------------------------
assert config["networks"]["backend"]["internal"] is True
assert not config["networks"]["egress"].get("internal")
assert not config["networks"]["admin"].get("internal")

# Docker accepts a port binding on a container attached only to an internal
# network and then never creates it, so the port is silently unreachable.
# Anything that publishes a host port must sit on a non-internal network.
internal_networks = {
    name for name, net in config["networks"].items() if net.get("internal")
}
for name, service in services.items():
    if not service.get("ports"):
        continue
    attached = set(service.get("networks") or {})
    assert attached - internal_networks, (
        f"{name} publishes a host port but is only on internal network(s) {attached}; "
        "the binding would never be created"
    )

# --- Application image -----------------------------------------------------
# Built on the instance from the deployed commit: no registry, no floating tag.
for name in ("api", "worker", "migrate", "backup", "compliance-poller"):
    image = services[name]["image"]
    assert not image.endswith(":latest"), f"{name} must not run a floating :latest tag"
    assert "/" not in image.split(":")[0], (
        f"{name} should use a locally built image name, got {image}"
    )
    assert services[name].get("build"), f"{name} must be built from source on the host"

# --- AWS and capture gates -------------------------------------------------
for name in ("api", "worker", "migrate", "compliance-poller", "backup"):
    env = services[name]["environment"]
    assert env["AWS_REGION"] == "us-east-1"
    assert env["S3_BUCKET"] == "techsara-chatgpt"
    assert env["S3_ENDPOINT_URL"] == ""
    assert env["S3_USE_PATH_STYLE"] == "false"
    assert "AWS_ACCESS_KEY_ID" not in env, f"{name} must use the EC2 instance role"
    assert "AWS_SECRET_ACCESS_KEY" not in env, f"{name} must use the EC2 instance role"

api_env = services["api"]["environment"]
assert api_env["ARCHIVE_HOSTNAME"] == "archive.example.com"
assert api_env["BROWSER_CONTENT_CAPTURE_ENABLED"] == "false"
assert api_env["OPENAI_WRITTEN_AUTHORIZATION_CONFIRMED"] == "false"
assert api_env["TRAINING_EXPORT_ENABLED"] == "false"
assert api_env["DEV_AUTH_ENABLED"] == "false"
assert api_env["LOG_MESSAGE_CONTENT"] == "false"

# --- Connection budget -----------------------------------------------------
# The same arithmetic the application enforces at startup, checked statically
# against the max_connections this compose file configures.
pool = int(api_env["DATABASE_POOL_SIZE"]) + int(api_env["DATABASE_MAX_OVERFLOW"])
processes = int(api_env["API_WORKERS"]) + 1 + 1  # api workers + worker + poller
max_connections = int(api_env["POSTGRES_MAX_CONNECTIONS"])
assert pool * processes + 15 <= max_connections, (
    f"pool budget {pool * processes} + reserve exceeds max_connections {max_connections}"
)

print("production Compose topology, tunnel ingress and S3 configuration passed")
PY
