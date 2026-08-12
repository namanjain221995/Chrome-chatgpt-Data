# Disaster recovery

## Recovery targets

Configurable goals, not guarantees:

| Target | Value | Determined by |
| --- | --- | --- |
| RPO (data loss) | ≤ 24 hours | Nightly logical backup. WAL archiving would reduce this to minutes. |
| RTO (time to serve) | ≤ 4 hours | Documented single-instance rebuild by one engineer. |
| RPO for S3 content | ~0 | Objects are written continuously; versioning is enabled. |

## Scenarios

### 1. A container crashed

Compose restarts it (`restart: unless-stopped`). If it crash-loops:

```bash
sudo docker compose --env-file .env.production -f compose.prod.yaml logs --tail 100 <service>
sudo docker compose --env-file .env.production -f compose.prod.yaml up -d --force-recreate <service>
```

No data loss: the queue is in PostgreSQL and client work is in IndexedDB.

### 2. The instance was stopped or rebooted

The systemd unit brings the stack back automatically. Confirm:

```bash
sudo systemctl status techsara-chat-archive
curl -s https://archive.example.com/health/ready
```

Extensions flush their queued items on reconnect. Nothing is lost.

### 3. The instance is unrecoverable, the data volume survives

**Target: ~1 hour.**

```bash
# 1. Launch a reviewed replacement from docs/AWS_MANUAL_SETUP.md and attach the
# preserved encrypted data volume without formatting it.

# 2. Deploy onto the new instance
ssh ec2-user@<new-instance-host>
cd /opt/techsara-chat-archive
sudo ./scripts/deploy_production.sh <full-40-char-sha>
```

The volume mounts by filesystem UUID, so PostgreSQL finds its data where it left
it. Verify row counts against the last known values before announcing recovery.

### 4. Both the instance and the data volume are lost

**Target: ~4 hours.**

```bash
# 1. Rebuild the host from docs/AWS_MANUAL_SETUP.md

# 2. Secrets are already in SSM; deploy the application
ssh ec2-user@<new-instance-host>
cd /opt/techsara-chat-archive
sudo ./scripts/deploy_production.sh <full-40-char-sha>

# 3. Restore the newest backup
sudo docker compose --env-file .env.production -f compose.prod.yaml stop api worker
aws s3 ls s3://techsara-chatgpt/backups/postgres/ --recursive --region us-east-1 | tail -5
sudo docker compose --env-file .env.production -f compose.prod.yaml exec backup \
  sh /opt/scripts/restore_postgres.sh --from-s3 <key> --target-db techsara_restored

# 4. Verify, then promote
sudo docker compose ... exec postgres psql -U techsara_app -d techsara_restored \
  -c "SELECT count(*) FROM conversations; SELECT count(*) FROM messages; SELECT version_num FROM alembic_version;"
sudo docker compose ... exec postgres psql -U techsara_app -d postgres \
  -c "ALTER DATABASE techsara_chat_archive RENAME TO techsara_chat_archive_broken;" \
  -c "ALTER DATABASE techsara_restored RENAME TO techsara_chat_archive;"

# 5. Restart and confirm
sudo docker compose --env-file .env.production -f compose.prod.yaml up -d
curl -s https://archive.example.com/health/ready
```

**Expected loss:** everything captured since the last nightly backup, minus what
clients still hold in their offline queues (up to 7 days by default) and replay
on reconnect. S3 raw events and snapshots are unaffected, so an authorized
administrator can reconstruct conversations from `raw/` and `normalized/` if a
gap matters.

### 5. Data corruption or a bad migration

```bash
# Stop writers immediately
sudo docker compose --env-file .env.production -f compose.prod.yaml stop api worker

# Restore the pre-migration backup that deploy_production.sh took
aws s3 ls "s3://techsara-chatgpt/backups/postgres/$(date -u +%Y/%m/%d)/" --recursive --region us-east-1
sudo docker compose ... exec backup sh /opt/scripts/restore_postgres.sh \
  --from-s3 <pre-migration-key> --target-db techsara_rollback
```

Migrations are not automatically reversible. Restoring the pre-migration backup
is the supported path, which is exactly why `deploy_production.sh` refuses to migrate
when that backup fails.

### 6. The region is unavailable

Not covered by version 1: this is a single-region, single-instance design.
Reduce exposure by enabling S3 cross-region replication for the `backups/`
prefix, which makes a manual rebuild in another region possible.

## EBS snapshots

The snapshot schedule is an account-level manual control. Configure AWS Backup
or Data Lifecycle Manager to snapshot
volumes tagged `Backup=daily` (the data volume already carries that tag):

```bash
aws dlm create-lifecycle-policy \
  --description "TechSara archive daily EBS snapshots" \
  --state ENABLED \
  --execution-role-arn arn:aws:iam::<account>:role/AWSDataLifecycleManagerDefaultRole \
  --policy-details file://dlm-policy.json
```

Restoring a snapshot:

```bash
aws ec2 create-volume --snapshot-id snap-xxxx --availability-zone <az> \
  --volume-type gp3 --encrypted
aws ec2 attach-volume --volume-id vol-new --instance-id i-xxxx --device /dev/sdf
# On the instance:
sudo /usr/local/sbin/mount-data-volume.sh
```

Remember: a snapshot preserves whatever state the database was in, including
corruption. Prefer the logical backup unless you are recovering whole-volume
loss.

## Rehearsal checklist (quarterly)

- [ ] Provision a scratch instance from the AWS Console checklist
- [ ] Restore the newest production backup into it
- [ ] Verify row counts against production
- [ ] Verify the Alembic revision matches
- [ ] Confirm `/health/ready` returns ok
- [ ] Time the whole exercise and compare against the 4-hour RTO
- [ ] Destroy the scratch instance
- [ ] Record the result and any surprise in the operations log

An untested recovery plan is a plan to discover problems during an incident.
