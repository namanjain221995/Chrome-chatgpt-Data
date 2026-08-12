# EC2 production deployment

One Amazon Linux 2023 instance running the whole stack under Docker Compose.
Public traffic arrives only through a Cloudflare Tunnel; the instance needs no
inbound application port.

| Item | Value |
| --- | --- |
| OS | Amazon Linux 2023 |
| Login user | `ec2-user` |
| Application source | `/opt/techsara-chat-archive` (git checkout) |
| PostgreSQL data | `/srv/techsara-chat-archive/postgres` |
| Backup staging | `/srv/techsara-chat-archive/backups` |
| Secret files | `/srv/techsara-chat-archive/secrets` |
| Runtime config | `/opt/techsara-chat-archive/.env.production` (mode 0600) |
| Region | `us-east-1` |
| S3 bucket | `techsara-chatgpt` |
| Image | `techsara-chat-archive-backend:<full-git-sha>`, built on the host |

## Services

| Service | Started normally | Host port | Notes |
| --- | --- | --- | --- |
| `postgres` | yes | none | Internal network only. |
| `migrate` | yes, one-shot | none | Runs to completion before `api`/`worker`. |
| `api` | yes | none — `expose: 8000` | Plain HTTP; TLS is Cloudflare's. |
| `worker` | yes | none | PostgreSQL-backed jobs. |
| `cloudflared` | yes | `127.0.0.1:2000` (metrics only) | The only public ingress. |
| `backup` | yes | none | Nightly `pg_dump` → gzip → SHA-256 → S3. |
| `compliance-poller` | only if `COMPLIANCE_POLL_ENABLED=true` | none | Profile `compliance`. |
| `pgadmin` | **never** by a deployment | `127.0.0.1:5050` | Profile `admin`. See [PGADMIN_ACCESS.md](PGADMIN_ACCESS.md). |

## First deployment

Everything below is a one-time sequence. Steps 3–6 cannot be scripted: they
depend on your AWS account, your Cloudflare zone and your company domain.

### 1. The repository is already cloned

```bash
ssh ec2-user@<host>
ls -d /opt/techsara-chat-archive/.git
```

If it is missing:

```bash
sudo mkdir -p /opt/techsara-chat-archive
sudo chown ec2-user:ec2-user /opt/techsara-chat-archive
git clone https://github.com/namanjain221995/Chrome-chatgpt-Data.git \
  /opt/techsara-chat-archive
```

### 2. Prepare storage and verify Docker

```bash
cd /opt/techsara-chat-archive
sudo ./scripts/prepare_server_storage.sh
sudo ./scripts/install_docker.sh      # verifies versions; installs if missing
```

`prepare_server_storage.sh` is idempotent and never touches the contents of an
existing PostgreSQL data directory. If you attached a dedicated EBS volume, run
`sudo ./scripts/bootstrap_ec2_host.sh --data-device /dev/nvme1n1` instead, which
formats (only if unformatted), mounts it at `/srv/techsara-chat-archive`, adds
an `/etc/fstab` entry and then calls the storage script.

### 3. Create the SSM parameters

From an administrator workstation:

```bash
./scripts/put_secrets.sh --generate     # machine secrets, then prompts
```

The exact parameter list is in [GITHUB_SECRETS.md](GITHUB_SECRETS.md). The
deployment fails with a named parameter if any required one is missing — it
never guesses a default for a secret.

### 4. Create the Cloudflare tunnel

Follow [CLOUDFLARE_TUNNEL_SETUP.md](CLOUDFLARE_TUNNEL_SETUP.md):

1. Zero Trust → Networks → Tunnels → Create tunnel → Cloudflared.
2. Name it `techsara-chatgpt-production`.
3. Copy the tunnel token into
   `/techsara-chat-archive/cloudflare_tunnel_token` as a SecureString.
4. Add the public hostname `archive.<company-domain>` → service
   `http://api:8000`.
5. Put `https://archive.<company-domain>` into
   `/techsara-chat-archive/public_base_url`.

### 5. Validate the instance role and S3

```bash
aws sts get-caller-identity --region us-east-1
# Arn must be arn:aws:sts::<account>:assumed-role/TechSaraChatArchiveEC2Role/<instance-id>

aws s3api head-bucket --bucket techsara-chatgpt --region us-east-1
aws ssm get-parameter --name /techsara-chat-archive/postgres_user \
  --region us-east-1 --query 'Parameter.Name' --output text
```

All three must succeed from the instance, with no `~/.aws/credentials` file
present. If any fails, fix the instance profile before continuing —
[AWS_MANUAL_SETUP.md](AWS_MANUAL_SETUP.md) has the policy.

### 6. Run the first deployment

```bash
cd /opt/techsara-chat-archive
git fetch origin
sudo ./scripts/deploy_production.sh "$(git rev-parse origin/main)"
```

The script prints each phase. On the first run there is no previous release, so
a public-URL failure is reported as a warning rather than triggering a rollback:
the Cloudflare hostname route may still be propagating. Everything inside the
host — API readiness, worker, backup service, tunnel connection, S3 — is still
required to pass.

### 7. Verify

```bash
sudo ./scripts/verify_production.sh
curl -fsS https://archive.<company-domain>/health/ready
```

### 8. Pilot before rollout

1. Load `apps/chrome-extension/dist` as an unpacked extension in a managed test
   profile, or install the packaged ZIP from a GitHub Release.
2. Confirm `GET /api/v1/config` returns `"capture_active": false` — capture is
   still gated off, which is the correct state until authorization is granted.
3. Put the extension id into `/techsara-chat-archive/extension_ids` and
   redeploy so CORS accepts it.
4. Run a five-employee pilot as described in
   [CHROME_ENTERPRISE_DEPLOYMENT.md](CHROME_ENTERPRISE_DEPLOYMENT.md).

Do **not** set `browser_content_capture_enabled` or
`openai_written_authorization_confirmed` to enable the pilot. Both stay `false`
until written authorization exists; see
[PRIVACY_AND_EMPLOYEE_NOTICE.md](PRIVACY_AND_EMPLOYEE_NOTICE.md).

## Normal deployments

Push to `main`. GitHub Actions runs the full CI suite for that commit and, only
if it is green, connects over SSH and runs:

```bash
sudo /opt/techsara-chat-archive/scripts/deploy_production.sh <github.sha>
```

See [SIMPLE_CICD.md](SIMPLE_CICD.md). Nothing needs to be done on the instance.

## Manual deployment on the instance

```bash
cd /opt/techsara-chat-archive
git fetch origin
sudo ./scripts/deploy_production.sh <full-40-char-sha>
```

The script refuses a short SHA, refuses to run as a non-root user, and takes an
exclusive `flock` on `/var/lock/techsara-chat-archive-deploy.lock` so it cannot
overlap with a workflow run.

## What a deployment does

1. Acquires the deployment lock.
2. Reads `deploy/current-release` to learn the rollback target.
3. `git fetch origin` and verifies the requested commit exists.
4. `git reset --hard <sha>`.
5. Renders `.env.production` (0600) and the secret files from SSM.
6. Validates `compose.prod.yaml` and re-runs the topology assertions.
7. Pulls the pinned `postgres` and `cloudflared` images.
8. Starts PostgreSQL and waits for `pg_isready`.
9. Builds `techsara-chat-archive-backend:<sha>`.
10. Takes a pre-migration backup when a schema already exists.
11. Runs `alembic upgrade head` in a one-shot container.
12. Recreates `api`, `worker`, `backup`, `cloudflared`.
13. Starts the compliance poller only if its flag is on. Never starts pgAdmin.
14. Health checks: internal readiness, worker, backup, tunnel `/ready`, S3
    `HeadBucket`, public URL.
15. Writes `deploy/current-release` and rotates the old one to
    `deploy/previous-release`.
16. Prunes dangling images only.

Any failure after step 4 triggers an automatic application rollback; see
[ROLLBACK.md](ROLLBACK.md).

## Operational commands

```bash
cd /opt/techsara-chat-archive
COMPOSE="docker compose --env-file .env.production -f compose.prod.yaml"

sudo $COMPOSE ps
sudo $COMPOSE logs -f --tail 100 api
sudo $COMPOSE logs -f --tail 100 worker cloudflared

# Readiness from inside the private network
sudo $COMPOSE exec -T api curl -fsS \
  -H "Host: $(sed -n 's/^ARCHIVE_HOSTNAME=//p' .env.production)" \
  http://127.0.0.1:8000/health/ready

# Tunnel readiness
curl -fsS http://127.0.0.1:2000/ready

# Ad-hoc backup
sudo $COMPOSE run --rm --no-deps --entrypoint /bin/sh backup \
  /opt/scripts/backup_postgres.sh
```

Always pass `--env-file .env.production`. Compose does not load it
automatically, and without it every `${VAR:?}` in the file fails.

## Restarting the host

Containers use `restart: unless-stopped`, so Docker brings the stack back after
a reboot. `deploy/systemd/techsara-chat-archive.service` is available if you
prefer the stack to be owned by systemd; it is optional.

## Instance sizing

The defaults in `compose.prod.yaml` target 2 vCPU / 8 GiB with the database on
the same host. See [CAPACITY.md](CAPACITY.md) for measurements and the
thresholds at which to move up.
