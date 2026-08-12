# pgAdmin access

pgAdmin is a database administration UI, not the database. PostgreSQL itself
runs in the `postgres` container; pgAdmin connects to it over the private Docker
network.

**It is never exposed to the internet.** It binds to `127.0.0.1:5050` on the
instance, runs only under the `admin` compose profile, and is expected to be
stopped when nobody is using it.

## Reaching it

### 1. Start it

```bash
aws ssm start-session --target <instance-id>
cd /opt/techsara-chat-archive
sudo docker compose -f compose.prod.yaml --profile admin up -d pgadmin
```

### 2. Forward the port from your workstation

```bash
aws ssm start-session \
  --target <instance-id> \
  --document-name AWS-StartPortForwardingSession \
  --parameters '{"portNumber":["5050"],"localPortNumber":["5050"]}' \
  --region us-east-1
```

The instance ID comes from the EC2 Console or `aws ec2 describe-instances`.

### 3. Open it

`http://localhost:5050` — credentials are `PGADMIN_DEFAULT_EMAIL` and the
password stored at `/techsara-chat-archive/pgadmin_password` in SSM:

```bash
aws ssm get-parameter --name /techsara-chat-archive/pgadmin_password \
  --with-decryption --query 'Parameter.Value' --output text
```

### 4. Register the server, once

| Field | Value |
| --- | --- |
| Name | TechSara Archive |
| Host | `postgres` |
| Port | `5432` |
| Database | `techsara_chat_archive` |
| Username | `techsara_app` |
| Password | the value at `/techsara-chat-archive/postgres_password` |
| SSL mode | `prefer` (traffic never leaves the Docker network) |

### 5. Stop it when you are done

```bash
sudo docker compose -f compose.prod.yaml --profile admin stop pgadmin
```

Leaving it running consumes ~300 MiB and widens the attack surface for no
benefit.

## Rules

1. **Never** publish port 5050 on `0.0.0.0` or route public traffic to pgAdmin.
2. Prefer read-only queries. Data changes belong in a migration or a script that
   can be reviewed and repeated.
3. Anything you do here is outside the application's audit trail. For anything
   that touches employee content, use the audited admin API instead.
4. Never export query results containing message content to a personal machine.
5. Rotate the pgAdmin password when an administrator leaves.

## Useful queries

```sql
-- Archive size by table
SELECT relname, pg_size_pretty(pg_total_relation_size(relid)) AS size
  FROM pg_catalog.pg_statio_user_tables
 ORDER BY pg_total_relation_size(relid) DESC LIMIT 15;

-- Job queue health
SELECT status, kind, count(*), min(run_after) AS oldest
  FROM jobs GROUP BY status, kind ORDER BY status, count DESC;

-- Stale locks (a worker died mid-job)
SELECT id, kind, locked_by, locked_at, attempts
  FROM jobs
 WHERE status = 'running' AND locked_at < now() - interval '5 minutes';

-- Capture events not yet archived to S3
SELECT count(*), min(created_at) FROM capture_events WHERE archived_at IS NULL;

-- Conversations by completeness (how much do we actually have?)
SELECT capture_completeness, count(*) FROM conversations
 GROUP BY 1 ORDER BY 2 DESC;

-- Attachments stuck in quarantine
SELECT state, count(*) FROM attachments GROUP BY 1;

-- Partition sizes
SELECT c.relname, pg_size_pretty(pg_total_relation_size(c.oid))
  FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid
  JOIN pg_class p ON p.oid = i.inhparent
 WHERE p.relname = 'capture_events' ORDER BY c.relname;

-- Recent administrative activity
SELECT created_at, actor_email, action, resource_type, outcome
  FROM audit_events ORDER BY created_at DESC LIMIT 50;
```

## Without pgAdmin

`psql` inside the container is often faster and leaves less running:

```bash
sudo docker compose -f compose.prod.yaml exec postgres \
  psql -U techsara_app -d techsara_chat_archive
```
