# Monitoring

## What to watch, in priority order

| Priority | Signal | Where | Threshold |
| --- | --- | --- | --- |
| 1 | Instance status check | CloudWatch `StatusCheckFailed` | any failure |
| 1 | Backup missing | CloudWatch `BackupSucceeded` | none in 26 h |
| 1 | API readiness | `/health/ready` | non-200 for 2 minutes |
| 2 | Data volume space | CWAgent `disk_used_percent` | > 85% |
| 2 | Queue depth | admin summary `queue.pending` | > 10,000 |
| 2 | Unarchived events | admin summary `storage.unarchived_events` | > 1,000 |
| 3 | CPU | CloudWatch `CPUUtilization` | > 80% for 15 min |
| 3 | CPU credits (t-class) | `CPUCreditBalance` | < 30 |
| 3 | Memory | CWAgent `mem_used_percent` | > 90% |
| 3 | Compliance lag | admin summary `compliance.lag_seconds` | > 3600 |

## The health endpoints

```bash
curl -s https://archive.example.com/health/live     # process is alive
curl -s https://archive.example.com/health/ready    # can serve traffic
```

`/health/live` deliberately does not touch the database: a database blip must not
cause Docker to kill an otherwise healthy container.

## The admin summary

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  https://archive.example.com/api/v1/admin/health-summary | jq
```

```json
{
  "database_ok": true,
  "storage": { "bucket": "…", "reachable": true, "unarchived_events": 0,
               "stale_snapshots": 3, "pending_attachments": 1 },
  "queue":   { "pending": 12, "running": 2, "failed": 0, "dead": 0,
               "oldest_pending_age_seconds": 4.1, "stale_locks": 0,
               "backpressure": false },
  "compliance": { "enabled": false, "configured": false, "cursor_healthy": false },
  "policy": { "capture_active": true, "kill_switch": false },
  "counts": { "conversations": 1842, "messages": 51203, "users": 214, "devices": 231 },
  "warnings": []
}
```

The `warnings` array is the fastest read: it is empty when nothing needs
attention.

## Installing the CloudWatch agent

Disk and memory alarms need the agent. Without it those alarms sit in
`INSUFFICIENT_DATA`, which the configuration treats as noteworthy rather than
silent.

```bash
aws ssm start-session --target <instance-id>
sudo wget https://amazoncloudwatch-agent.s3.amazonaws.com/ubuntu/amd64/latest/amazon-cloudwatch-agent.deb
sudo dpkg -i amazon-cloudwatch-agent.deb

sudo tee /opt/aws/amazon-cloudwatch-agent/etc/config.json >/dev/null <<'JSON'
{
  "agent": { "metrics_collection_interval": 60 },
  "metrics": {
    "namespace": "CWAgent",
    "append_dimensions": { "InstanceId": "${aws:InstanceId}" },
    "metrics_collected": {
      "disk": { "resources": ["/srv/techsara-chat-archive"],
                "measurement": ["used_percent"], "metrics_collection_interval": 300 },
      "mem":  { "measurement": ["mem_used_percent"], "metrics_collection_interval": 60 }
    }
  },
  "logs": {
    "logs_collected": {
      "files": { "collect_list": [
        { "file_path": "/var/log/syslog",
          "log_group_name": "/aws/ec2/techsara-chat-archive",
          "log_stream_name": "{instance_id}/syslog" }
      ]}
    }
  }
}
JSON

sudo /opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl \
  -a fetch-config -m ec2 -s -c file:/opt/aws/amazon-cloudwatch-agent/etc/config.json
```

## Reporting the backup metric

The `backup-missing` alarm reads a custom metric. Publish it at the end of each
successful backup:

```bash
aws cloudwatch put-metric-data \
  --namespace TechSara/ChatArchive \
  --metric-name BackupSucceeded --value 1 \
  --region "$AWS_REGION"
```

Add this to `scripts/backup_postgres.sh` once the instance role has
`cloudwatch:PutMetricData` (it does, scoped to this namespace).

## Logs

Every service logs structured JSON to stdout, with rotation at 10 MB × 5 files.

```bash
sudo docker compose -f compose.prod.yaml logs -f api
sudo docker compose ... logs --since 1h worker | jq 'select(.level=="error")'
sudo docker compose ... logs api | jq 'select(.correlation_id=="abc123")'
```

Message content never appears in logs: content keys are suppressed unless
`LOG_MESSAGE_CONTENT=true`, which the production settings guard refuses.

## Useful database queries

```sql
-- Ingest rate over the last hour
SELECT date_trunc('minute', created_at) AS minute, count(*)
  FROM capture_events WHERE created_at > now() - interval '1 hour'
 GROUP BY 1 ORDER BY 1 DESC LIMIT 20;

-- Queue health
SELECT status, kind, count(*), min(run_after) FROM jobs GROUP BY 1, 2;

-- Jobs that keep failing
SELECT kind, error_summary, count(*) FROM jobs
 WHERE status IN ('failed','dead') GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 10;

-- Slowest statements (needs pg_stat_statements, enabled in compose.prod.yaml)
SELECT round(mean_exec_time::numeric, 1) AS avg_ms, calls, left(query, 90)
  FROM pg_stat_statements ORDER BY mean_exec_time DESC LIMIT 10;

-- Active devices in the last day
SELECT count(*) FROM devices
 WHERE last_seen_at > now() - interval '1 day' AND revoked_at IS NULL;
```

## Alert routing

CloudWatch alarms publish to the `techsara-chat-archive-alarms` SNS topic. Set
Create the alarm destination manually and confirm the subscription. For a pager, subscribe
your on-call integration to the same topic.

## What is deliberately not alerted

- Individual rejected messages — a checksum mismatch is a client bug, visible in
  the batch response, not an operational page.
- Backpressure responses — designed behaviour under load. Alert only if it
  persists for more than an hour.
- Compliance poller idle while unconfigured — expected until it is enabled.

Alert noise that nobody acts on is worse than no alert.
