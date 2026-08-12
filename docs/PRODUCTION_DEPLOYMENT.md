# Production deployment

## Deployment model

Immutable images tagged with the git SHA, pulled onto one EC2 instance, applied
by a script that backs up before it migrates and rolls back if health fails.

```
git tag → CI builds and pushes ghcr.io/<org>/…-backend:<sha>
        → SSM Run Command (or a shell session) runs deploy_ec2.sh
        → render .env + secrets from SSM
        → pull → backup → migrate → up -d → health check
        → on failure: application images roll back to the previous tag
```

## Prerequisites

- `terraform apply` completed ([AWS_STEP_BY_STEP.md](AWS_STEP_BY_STEP.md))
- Secrets written with `scripts/put_secrets.sh`
- DNS pointing at the Elastic IP
- The deployment bundle at `/opt/techsara-chat-archive`
- The systemd unit installed and enabled

## Deploying

### Through SSM Run Command (preferred)

```bash
aws ssm send-command \
  --document-name techsara-chat-archive-deploy \
  --targets Key=instanceids,Values=i-0123456789abcdef0 \
  --parameters imageTag=a1b2c3d4e5f6 \
  --comment "Release v1.2.0"
```

Nothing inbound is opened, and CloudTrail records who deployed what.

### Through GitHub Actions

Actions → **Deploy to EC2** → run workflow, supplying the image tag, the
instance id and the environment. The workflow assumes an AWS role through OIDC —
no long-lived key is stored.

### Interactively

```bash
aws ssm start-session --target i-0123456789abcdef0
cd /opt/techsara-chat-archive
sudo IMAGE_TAG=a1b2c3d4e5f6 ./scripts/deploy_ec2.sh
```

## What the script does, and why in that order

| Step | Why it is where it is |
| --- | --- |
| 1. Render `.env` and 0400 secret files from SSM | Secrets never live in the image, the compose file, or a process listing |
| 2. `docker compose pull` | Fail before touching the running system if the tag is wrong |
| 3. **Backup** | The only reliable way back from a bad migration |
| 4. `alembic upgrade head` | Schema first, so the new code never meets an old schema |
| 5. `docker compose up -d` | Start the new containers |
| 6. Wait for `/health/ready` | Up to 180 seconds |
| 7. Roll back application images on failure | Restores the previous tag and reports that migrations were **not** reverted |

If step 3 fails, the deployment stops. Migrating without a backup is refused.

## The rollback asymmetry

**Application images roll back automatically.** A failed health check restores
the previous tag.

**Database migrations do not.** Alembic downgrades exist but are not run
automatically: a downgrade that drops a column destroys data that the previous
version cannot recover. The supported path is to restore the pre-migration
backup taken in step 3.

Design migrations to be backwards compatible so a rollback is rarely needed:

1. Add a nullable column, deploy, backfill, deploy code that uses it.
2. Never rename in one step: add, dual-write, migrate readers, drop later.
3. Never drop a column in the same release that stops writing it.

## Zero-downtime expectations

This is a single instance, so a deployment restarts the API and there is a brief
gap. It is small in practice:

- Caddy keeps listening and returns 502 for a few seconds.
- Extensions queue locally and retry; nothing is lost.
- The worker finishes its current job (60-second grace) before stopping.

For a genuinely gapless deploy you need two application instances behind a load
balancer, which is step 4 of the growth path in
[SCALING_250_USERS.md](SCALING_250_USERS.md).

## Verifying a deployment

```bash
curl -s https://archive.example.com/health/ready | jq
curl -s https://archive.example.com/api/v1/config | jq '.config.config_version'
sudo docker compose -f compose.yaml -f compose.prod.yaml ps
curl -s -H "Authorization: Bearer $TOKEN" \
  https://archive.example.com/api/v1/admin/health-summary | jq '.warnings, .git_sha'
```

Then confirm the queue drains: `queue.pending` should trend down, and
`storage.unarchived_events` should reach zero.

## Configuration changes without a deployment

Most settings live in SSM. Change the parameter, then redeploy so the containers
re-read it:

```bash
aws ssm put-parameter --name /techsara-chat-archive/kill_switch_enabled \
  --value true --type String --overwrite --region us-east-1
aws ssm send-command --document-name techsara-chat-archive-deploy \
  --targets Key=instanceids,Values=<id> --parameters imageTag=<current-sha>
```

### Emergency stop

To halt capture immediately without a deployment:

```bash
aws ssm start-session --target <id>
cd /opt/techsara-chat-archive
sudo sed -i 's/^KILL_SWITCH_ENABLED=.*/KILL_SWITCH_ENABLED=true/' .env
sudo docker compose -f compose.yaml -f compose.prod.yaml up -d api
```

Every ingest request is then refused server-side, regardless of what any client
has cached.

## Secret rotation

```bash
./scripts/put_secrets.sh --project techsara-chat-archive --region us-east-1
aws ssm send-command --document-name techsara-chat-archive-deploy \
  --targets Key=instanceids,Values=<id> --parameters imageTag=<current-sha>
```

Rotating `JWT_SECRET` invalidates every access token, so employees sign in again.
Rotating `CONFIG_SIGNING_KEY` changes the config key id; clients fetch a fresh
document on their next poll. Rotating the PostgreSQL password requires an
`ALTER ROLE` before the redeploy.

## Host maintenance

```bash
# Unattended upgrades handle security patches; a kernel update needs a reboot
sudo needrestart -r l
sudo reboot        # systemd brings the stack back automatically
```

Docker image cleanup:

```bash
sudo docker image prune -a --filter "until=168h"
```

## Pre-release checklist

- [ ] `make verify` passes locally
- [ ] CI is green on the commit being deployed
- [ ] The image tag exists in the registry
- [ ] Migrations reviewed for backwards compatibility
- [ ] A recent backup exists (`verify_backup.sh`)
- [ ] A rollback tag is known and recorded
- [ ] Someone is watching for 30 minutes afterwards
