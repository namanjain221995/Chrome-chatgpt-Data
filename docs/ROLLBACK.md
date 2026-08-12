# Rollback

Rollback restores the **application** to a previously deployed commit. It never
reverses a database migration.

## Release records

Two files on the instance, both written by `scripts/deploy_production.sh` and
`scripts/rollback_production.sh`:

```text
/opt/techsara-chat-archive/deploy/current-release
/opt/techsara-chat-archive/deploy/previous-release
```

Each is a plain `KEY=VALUE` file:

```text
GIT_SHA=1f4c9a2e0b7d5c3a8e6f2d1b4c7a9e0f3d2b5c81
IMAGE=techsara-chat-archive-backend:1f4c9a2e0b7d5c3a8e6f2d1b4c7a9e0f3d2b5c81
IMAGE_ID=sha256:4cb6557a826fdacadf16ed32a55a4ff6ed22f928db63ca3c6ea2f25576814411
DEPLOYED_AT=2026-08-13T02:41:07Z
DEPLOYED_BY=github-actions
PUBLIC_BASE_URL=https://archive.example.com
PUBLIC_HEALTH_VERIFIED=true
ROLLBACK_TARGET=9c1e7b3f0a2d4e6c8b5a1f3d7e9c0b2a4d6f8e13
```

`IMAGE_ID` is the local Docker image ID — the content digest of the image built
on this host. It is not a registry digest, because nothing is pushed to a
registry (see [SIMPLE_CICD.md](SIMPLE_CICD.md)).

Both files are untracked and gitignored, so `git reset --hard` during a
deployment cannot destroy them.

## Automatic rollback

`deploy_production.sh` installs an `ERR` trap before it changes anything. If any
step after the checkout fails — migration, container recreation, API readiness,
worker, backup service, tunnel readiness, S3 access, or the public health check
— it calls `rollback_production.sh` with the SHA recorded in
`deploy/current-release` at the start of the run.

The deployment then fails loudly. It never reports success after a rollback.

## Manual rollback

```bash
ssh ec2-user@<host>
cd /opt/techsara-chat-archive

# Roll back to whatever deploy/previous-release records:
sudo ./scripts/rollback_production.sh

# Or to a specific commit that was previously deployed:
sudo ./scripts/rollback_production.sh 9c1e7b3f0a2d4e6c8b5a1f3d7e9c0b2a4d6f8e13
```

The target must be a full 40-character SHA that exists in the local repository.

## What it does, in order

1. Takes the deployment lock (skipped when called by a deployment that already
   holds it).
2. Verifies the target commit exists locally.
3. `git reset --hard <target>` — restores the Compose file, scripts and
   application source for that release.
4. Re-renders `.env.production` from SSM with `IMAGE_TAG=<target>`.
5. Reuses the local `techsara-chat-archive-backend:<target>` image. Deployments
   prune only dangling layers, so it is normally still present; if it has been
   removed, it is rebuilt from the checked-out source.
6. Recreates `api`, `worker`, `backup` and `cloudflared`.
7. Waits for API readiness and a connected tunnel.
8. Rewrites `deploy/current-release` — including `API_HEALTHY` and
   `TUNNEL_HEALTHY` — whether or not the checks passed, so the recorded state
   always matches reality.
9. Exits non-zero if the restored release is not healthy.

## What it deliberately does not do

* **It does not downgrade the schema.** `alembic downgrade` is never run
  automatically. A downgrade can drop a column that already holds data written
  by the newer release, and there is no way to recover it. Rollback leaves the
  schema at head and relies on migrations being backward compatible for one
  release.
* **It does not touch the PostgreSQL volume.** No `down -v`, no volume prune,
  no `docker system prune -a`.
* **It does not start pgAdmin.**
* **It does not start the compliance poller** unless
  `COMPLIANCE_POLL_ENABLED=true`.

## Backward-compatible migration strategy

Because rollback runs the previous application against the newer schema, every
migration must be safe for the immediately preceding release to run against.
The rules:

* **Add, do not rename.** A rename is a drop plus an add. Add the new column,
  backfill it, teach the application to read both, and only drop the old one in
  a *later* release.
* **New columns are nullable or have a default.** The previous release's
  `INSERT` statements do not mention them.
* **New tables are additive.** The previous release ignores them.
* **Widen before narrowing.** Relax a constraint in one release; tighten it a
  release later, once no running code can violate it.
* **Never drop or narrow a column in the same release that stops writing it.**
  Split it across two deployments.
* **Index creation on a large table should be concurrent** so it does not hold
  a write lock through the deployment.

This makes a two-release window in which either version can run against the
current schema, which is exactly what automatic rollback needs.

`make migration-check` enforces the mechanical half of this in CI: an empty
upgrade to head, a model-drift check, a downgrade of one revision, and a
re-upgrade. It cannot prove semantic compatibility — that is the reviewer's
job, using the rules above.

## When a migration really must be reverted

Do it deliberately, never as part of an automated rollback:

1. Stop the application, leaving PostgreSQL up:
   ```bash
   docker compose --env-file .env.production -f compose.prod.yaml stop api worker
   ```
2. Take a fresh backup and confirm it uploaded:
   ```bash
   docker compose --env-file .env.production -f compose.prod.yaml \
     run --rm --no-deps --entrypoint /bin/sh backup /opt/scripts/backup_postgres.sh
   ```
3. Review the specific revision's `downgrade()` and satisfy yourself it does
   not destroy data you need.
4. Run it:
   ```bash
   docker compose --env-file .env.production -f compose.prod.yaml \
     run --rm --entrypoint alembic migrate downgrade -1
   ```
5. Roll the application back, then verify.

If the downgrade would lose data, restore from backup into a new database
instead and switch over deliberately. See
[BACKUP_RESTORE.md](BACKUP_RESTORE.md).

## Verifying a rollback

```bash
sudo /opt/techsara-chat-archive/scripts/verify_production.sh
cat /opt/techsara-chat-archive/deploy/current-release
curl -fsS https://archive.<company-domain>/health/ready
```

`verify_production.sh` fails if the checkout and the recorded release disagree,
which catches a half-finished rollback.

## Reporting

The deploy workflow's job summary always includes the contents of
`deploy/current-release` read back from the instance, so a rolled-back run is
visible in the GitHub Actions UI without logging in to the host.
