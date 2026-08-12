# Operations

## Daily

Nothing, if the alarms are quiet. When you do look:

```bash
curl -s https://archive.example.com/health/ready | jq
curl -s -H "Authorization: Bearer $TOKEN" \
  https://archive.example.com/api/v1/admin/health-summary | jq '.warnings, .queue, .storage'
```

An empty `warnings` array means nothing needs attention.

## Weekly

```bash
# Backups exist, are fresh, and have manifests
sudo docker compose --env-file .env.production -f compose.prod.yaml exec backup \
  sh /opt/scripts/verify_backup.sh

# Disk
df -h /srv/techsara-chat-archive

# Anything stuck
sudo docker compose ... exec postgres psql -U techsara_app -d techsara_chat_archive -c \
  "SELECT status, count(*) FROM jobs GROUP BY status;"
```

## Monthly

```bash
# Prove a restore actually works
sudo docker compose ... exec backup sh /opt/scripts/verify_backup.sh --full-restore

# Review who has privileged roles
sudo docker compose ... exec postgres psql -U techsara_app -d techsara_chat_archive -c \
  "SELECT email, roles, last_seen_at FROM users WHERE roles::text != '[\"employee\"]';"

# Review administrative activity
sudo docker compose ... exec postgres psql -U techsara_app -d techsara_chat_archive -c \
  "SELECT action, count(*) FROM audit_events WHERE created_at > now() - interval '30 days' GROUP BY 1;"

# Host patches
sudo unattended-upgrade --dry-run
sudo needrestart -r l
```

## Quarterly

- Full disaster-recovery rehearsal ([DISASTER_RECOVERY.md](DISASTER_RECOVERY.md))
- Rotate `JWT_SECRET` and `CONFIG_SIGNING_KEY`
- Review retention policies against current legal advice
- Re-run the load test and compare against the last report
- Review the extension's DOM adapter against the current ChatGPT UI

## Common tasks

### Grant an administrative role

```sql
UPDATE users SET roles = '["employee","compliance_admin"]'
 WHERE email = 'admin@example.com';
```

The user must sign in again for the new role to appear in their token.

### Revoke a device

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"device_id":"<uuid>","reason":"laptop returned"}' \
  https://archive.example.com/api/v1/admin/devices/revoke
```

Takes effect immediately: the access token's session id no longer matches.

### Apply a legal hold

```sql
-- Through psql for now; the admin API surface for holds is future work
UPDATE conversations SET legal_hold = true
 WHERE id IN ('<uuid>', '<uuid>');
UPDATE messages SET legal_hold = true
 WHERE conversation_id IN ('<uuid>', '<uuid>');
```

Held records are excluded from soft delete, hard delete and curated export, and
a CHECK constraint makes a held-and-deleted row impossible.

### Run a curated export

Requires `TRAINING_EXPORT_ENABLED=true` and an approval row per conversation.

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"kind":"curated_training_jsonl","reason":"Q1 curation batch","split_ratios":{"train":0.8,"validation":0.1,"test":0.1}}' \
  https://archive.example.com/api/v1/admin/exports

curl -s -H "Authorization: Bearer $TOKEN" \
  https://archive.example.com/api/v1/admin/exports/<export-id> | jq
```

Download URLs are short-lived presigned GETs, and issuing them is audited.

### Retry dead jobs

```sql
UPDATE jobs SET status = 'pending', attempts = 0, run_after = now(), error_summary = NULL
 WHERE status = 'dead' AND kind = 'archive_raw_event';
```

Investigate `error_summary` first: a job that is dead for a good reason will
simply die again.

### Free disk space

```bash
sudo docker image prune -a --filter "until=168h"
sudo docker compose ... exec postgres psql -U techsara_app -d techsara_chat_archive \
  -c "DELETE FROM jobs WHERE status='succeeded' AND finished_at < now() - interval '14 days';"
# Growing the volume is online:
aws ec2 modify-volume --volume-id vol-xxx --size 200
sudo xfs_growfs /srv/techsara-chat-archive
```

## Runbooks by symptom

| Symptom | First check | Then |
| --- | --- | --- |
| Employees report "not archived" | Popup status | Workspace verified? Gates on? Queue draining? |
| Queue depth growing | `queue.oldest_pending_age_seconds` | Worker logs; raise `WORKER_CONCURRENCY` |
| `unarchived_events` growing | S3 reachability | Instance role; bucket policy; `worker` logs |
| Attachments stuck in quarantine | `attachments.state` counts | `finalize_attachment` job errors |
| 429s reported | Rate limit configuration | Raise `RATE_LIMIT_REQUESTS_PER_MINUTE` if legitimate |
| 503 `backpressure` | Queue depth vs threshold | Expected under load; investigate if sustained |
| Certificate errors | Cloudflare events and API logs | Proxied DNS? Origin SAN valid? Full (strict)? |
| Sign-in failures | Which error | Redirect URI, hosted domain, allowed domains |

Detailed incident procedures are in [INCIDENT_RUNBOOK.md](INCIDENT_RUNBOOK.md).

## Access model

| Task | Who | How |
| --- | --- | --- |
| Instance shell | Platform engineer | SSH as `ec2-user` (SSM Session Manager as break-glass) |
| Database queries | DBA / platform | psql in the container, or pgAdmin over a forwarded port |
| Reading archived content | compliance_admin, security_reviewer | Audited admin API |
| Exports | data_curator, compliance_admin | Audited admin API |
| Deployments | Platform, or CI | SSM Run Command |
| Secrets | Platform | `scripts/put_secrets.sh` |

Never share the pgAdmin password, never read employee content outside the
audited API, and never export content to a personal machine.
