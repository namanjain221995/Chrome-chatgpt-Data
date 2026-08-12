# Incident runbook

## Severity

| Level | Meaning | Response |
| --- | --- | --- |
| SEV1 | Data loss, or content exposed to someone unauthorized | Immediate; wake people |
| SEV2 | Archiving stopped for everyone | Within the hour |
| SEV3 | Degraded: slow, partial, or one employee affected | Same business day |
| SEV4 | Cosmetic or single-user annoyance | Next working day |

## First five minutes, any incident

```bash
curl -s https://archive.example.com/health/ready | jq
ssh ec2-user@<host>
cd /opt/techsara-chat-archive
sudo docker compose --env-file .env.production -f compose.prod.yaml ps
sudo docker compose ... logs --tail 100 api worker
df -h /srv/techsara-chat-archive
```

Write down what you find before you change anything. During an incident,
memory is unreliable and the timeline matters afterwards.

---

## SEV1: content may have been exposed

**Stop the bleeding first.**

```bash
# 1. Halt capture immediately
sudo sed -i 's/^KILL_SWITCH_ENABLED=.*/KILL_SWITCH_ENABLED=true/' .env
sudo docker compose --env-file .env.production -f compose.prod.yaml up -d api

# 2. Revoke the affected sessions
sudo docker compose ... exec postgres psql -U techsara_app -d techsara_chat_archive -c \
  "UPDATE devices SET revoked_at = now(), revoked_reason = 'incident <id>', refresh_token_hash = NULL WHERE user_id = '<uuid>';"

# 3. Preserve the evidence
sudo docker compose ... exec postgres psql -U techsara_app -d techsara_chat_archive -c \
  "COPY (SELECT * FROM audit_events WHERE created_at > now() - interval '7 days') TO STDOUT CSV HEADER;" \
  > /tmp/incident-audit.csv
```

Then: who accessed what, when, and under which role? The `audit_events` table
answers all three. Involve your privacy and legal teams before notifying anyone
externally — notification timelines are jurisdiction-specific.

## SEV1: data loss suspected

```bash
# Do NOT let the next backup overwrite good history
sudo docker compose --env-file .env.production -f compose.prod.yaml stop backup

# Establish what is actually missing
sudo docker compose ... exec postgres psql -U techsara_app -d techsara_chat_archive -c \
  "SELECT date_trunc('day', created_at) AS day, count(*) FROM messages
    WHERE created_at > now() - interval '14 days' GROUP BY 1 ORDER BY 1;"
```

A sudden drop to zero is an ingestion outage, not necessarily deletion. Check
whether S3 still holds the raw events for that window: if it does, the archive
can be rebuilt from `raw/` even if the database lost rows.

Then follow [DISASTER_RECOVERY.md](DISASTER_RECOVERY.md) scenario 5.

## SEV2: nothing is being archived

Work down this list; each step tells you which layer is at fault.

```bash
# 1. Is the API up?
curl -s https://archive.example.com/health/ready

# 2. Are the gates on?
curl -s https://archive.example.com/api/v1/config | jq '.config.policy'

# 3. Is the kill switch engaged?
grep KILL_SWITCH /opt/techsara-chat-archive/.env

# 4. Are requests arriving at all?
sudo docker compose ... logs --since 15m api | grep -c http_request

# 5. Are they being refused?
sudo docker compose ... logs --since 15m api | jq -r 'select(.event=="app_error") | .code' | sort | uniq -c
```

| Finding | Cause | Fix |
| --- | --- | --- |
| `/health/ready` fails | Database down | Restart `postgres`; check disk |
| `capture_active: false` | Gates off | Expected if deliberate; otherwise fix SSM and redeploy |
| Kill switch true | Someone engaged it | Confirm why before clearing it |
| No requests at all | Clients cannot reach the backend | DNS, certificate, extension policy |
| Many `policy_blocked` | Workspace verification failing | Compare `MANAGED_WORKSPACE_LABEL` with the current ChatGPT UI |
| Many `workspace_unverified` | The ChatGPT UI changed | The DOM adapter needs updating — see below |

### The DOM adapter stopped matching

This is the most likely cause of a sudden, total capture failure, and it is not
an outage of your system: ChatGPT's markup changed.

1. Have one person capture a **sanitized** structural fixture from the current UI.
2. Add it to `tests/fixtures/transcripts.ts` with a failing test.
3. Update the selectors in `dom-adapter.ts` (most specific first).
4. Bump `ADAPTER_VERSION`.
5. Ship the extension update; managed browsers pick it up within hours.

Employees' conversations are not lost in the meantime — they simply are not
archived until the fix lands, and the popup says so.

## SEV2: the queue is not draining

```bash
sudo docker compose ... exec postgres psql -U techsara_app -d techsara_chat_archive -c \
  "SELECT status, kind, count(*), min(run_after) FROM jobs GROUP BY 1,2 ORDER BY 3 DESC;"
sudo docker compose ... logs --tail 100 worker
```

| Finding | Fix |
| --- | --- |
| Worker container not running | `docker compose up -d worker` |
| Many `running` with old `locked_at` | Stale locks; recovery runs automatically each cycle — confirm the worker is alive |
| Many `failed` with an S3 error | Check the instance role and the bucket policy |
| Pending grows faster than it drains | Raise `WORKER_CONCURRENCY`; consider a larger instance |

## SEV3: slow ingestion

```bash
docker stats --no-stream
sudo docker compose ... exec postgres psql -U techsara_app -d techsara_chat_archive -c \
  "SELECT count(*), state FROM pg_stat_activity GROUP BY state;"
sudo docker compose ... exec postgres psql -U techsara_app -d techsara_chat_archive -c \
  "SELECT round(mean_exec_time::numeric,1) ms, calls, left(query,80) FROM pg_stat_statements ORDER BY mean_exec_time DESC LIMIT 5;"
iostat -x 5 3
```

Match against the thresholds in [CAPACITY.md](CAPACITY.md).

## SEV3: disk filling

```bash
df -h /srv/techsara-chat-archive
sudo du -sh /srv/techsara-chat-archive/* | sort -h
```

Quick wins: prune succeeded jobs older than 14 days, prune Docker images, lower
local backup retention. Real fix: grow the volume (online) and consider dropping
old partitions once you have confirmed no legal hold covers the period.

## After any incident

- [ ] Timeline written while it is fresh
- [ ] Root cause identified, not just the trigger
- [ ] A test added that would have caught it
- [ ] Alarm added or tuned if detection was slow
- [ ] Documentation updated
- [ ] Blameless review held

The tests in this repository that exist because of a real bug are noted in
[DECISIONS.md](DECISIONS.md). Add to that list rather than fixing quietly.

## Contacts

| Role | Contact |
| --- | --- |
| Platform on-call | `<fill in>` |
| Security | `security@<company-domain>` |
| Privacy / legal | `<fill in>` |
| AWS support | Support Center, if a plan is active |
