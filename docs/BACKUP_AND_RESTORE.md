# Backup and restore

A backup you have never restored is a hypothesis. This document exists to turn
it into a fact, on a schedule.

## Two layers

| Layer | What it protects against | RPO | RTO |
| --- | --- | --- | --- |
| PostgreSQL logical backup (`pg_dump`) | Data corruption, bad migration, accidental deletion, instance loss | 24 h | ~1 h |
| EBS snapshot | Whole-volume loss, instance loss | 24 h | ~2-4 h |

**These are goals, not guarantees.** They are configurable and depend on data
volume, instance size and how quickly a human responds.

An EBS snapshot is **not** a substitute for a tested logical backup: a snapshot
of a corrupted database is a corrupted database, and a snapshot cannot restore a
single table.

## What runs automatically

The `backup` service runs `scripts/backup_postgres.sh` every
`BACKUP_INTERVAL_SECONDS` (default 24 h):

1. `pg_dump --format=custom` piped through `gzip -9`
2. SHA-256 checksum and a JSON manifest
3. Upload the dump, then the manifest, to S3 with SSE
4. Verify the uploaded object's size matches what was produced
5. Prune local copies older than two days (S3 is the durable store)

```
s3://<bucket>/backups/postgres/YYYY/MM/DD/techsara-<timestamp>.dump.gz
s3://<bucket>/backups/manifests/YYYY/MM/DD/techsara-<timestamp>.sha256
```

Retention is enforced by the bucket lifecycle rule
(`BACKUP_RETENTION_DAYS`, default 90), with a transition to Standard-IA at 30
days.

## Checking backups without restoring

```bash
aws ssm start-session --target <instance-id>
cd /opt/techsara-chat-archive
sudo docker compose -f compose.yaml -f compose.prod.yaml exec backup \
  sh /opt/scripts/verify_backup.sh
```

Checks that a backup exists, is larger than a trivial size, is younger than
`MAX_AGE_HOURS` (default 30), and has a matching manifest. Exit code 0 means the
chain is healthy today.

## Proving a restore (do this monthly)

```bash
sudo docker compose -f compose.yaml -f compose.prod.yaml exec backup \
  sh /opt/scripts/verify_backup.sh --full-restore
```

Downloads the newest backup, verifies its checksum against the manifest,
restores it into a disposable database, asserts the schema has at least 20
tables, prints row counts and the Alembic revision, then drops the database.

Locally:

```bash
make restore-test
```

## Restoring for real

### A single table or a specific point

```bash
aws s3 ls s3://<bucket>/backups/postgres/ --recursive | tail -20

sudo docker compose -f compose.yaml -f compose.prod.yaml exec backup \
  sh /opt/scripts/restore_postgres.sh \
    --from-s3 backups/postgres/2026/03/15/techsara-20260315T031500Z.dump.gz \
    --target-db techsara_recovered
```

The script refuses `--drop-existing` against the live database. Restore beside
it, verify, then switch over deliberately.

### Full recovery onto a replacement instance

1. Provision the replacement (`terraform apply`) and attach the existing data
   volume if it survived; otherwise start from an empty one.
2. Deploy the application: `sudo IMAGE_TAG=<sha> ./scripts/deploy_ec2.sh`.
3. Stop the writers so nothing races the restore:
   ```bash
   sudo docker compose -f compose.yaml -f compose.prod.yaml stop api worker compliance-poller
   ```
4. Restore into a new database, verify, then promote:
   ```bash
   sudo docker compose ... exec backup sh /opt/scripts/restore_postgres.sh \
     --from-s3 <key> --target-db techsara_restored
   # verify counts and the alembic revision, then rename:
   sudo docker compose ... exec postgres psql -U techsara_app -d postgres \
     -c "ALTER DATABASE techsara_chat_archive RENAME TO techsara_chat_archive_old;" \
     -c "ALTER DATABASE techsara_restored RENAME TO techsara_chat_archive;"
   ```
5. Start the writers and confirm `/health/ready`.

Full sequence in [DISASTER_RECOVERY.md](DISASTER_RECOVERY.md).

## What is not in the database

S3 holds the raw event JSON, conversation snapshots, attachments and exports.
The bucket has versioning enabled and the instance role cannot delete objects,
so S3 content is protected independently of the database. A database restore
does not restore S3, and does not need to: the two are separately durable and
are linked by keys and checksums recorded in both.

## Verification schedule

| Frequency | Action |
| --- | --- |
| Daily | Automatic backup; alarm fires if none succeeded in 26 h |
| Weekly | `verify_backup.sh` (existence, freshness, manifest) |
| Monthly | `verify_backup.sh --full-restore` (a real restore) |
| Quarterly | Full disaster-recovery rehearsal onto a scratch instance |
| After every schema change | CI already runs backup + restore in the integration job |

## Failure modes and responses

| Symptom | Likely cause | Response |
| --- | --- | --- |
| `backup-missing` alarm | Backup container stopped, or S3 permissions changed | `docker compose logs backup`; check the instance role |
| Dump much smaller than usual | Partial dump, or unexpected data loss | Do **not** overwrite older backups; investigate before the next cycle |
| Checksum mismatch on download | Corrupted transfer or object | Restore from the previous day; check S3 object versions |
| Restore fails on a constraint | Backup predates a migration | Restore into a scratch database, then `alembic upgrade head` |
| Disk full during backup | Data volume near capacity | Free space, grow the volume, and lower local retention |
