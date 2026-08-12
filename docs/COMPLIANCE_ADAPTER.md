# Compliance adapter

The optional path to company-wide, cross-device coverage. Disabled by default
and idle until an authorized configuration is supplied.

## Why it exists

The browser extension archives what a browser renders. That leaves four gaps:
conversations never opened in this browser, conversations on other devices,
conversations deleted before capture, and historical attachment bytes.

The OpenAI Enterprise Compliance Platform is the authorized source that closes
them. It is the **preferred** source for company-wide records.

## No invented endpoints

This system does not guess at API paths. `app/adapters/openai_compliance.py`
takes the base URL, the log path, the files path and the response field mapping
entirely from configuration, supplied from the documentation you receive with
your Enterprise agreement.

Until they are set, `is_configured` is false, the poller logs that it is
unconfigured, and no request is ever made.

## Configuration

```env
COMPLIANCE_POLL_ENABLED=true
OPENAI_COMPLIANCE_BASE_URL=https://<documented-host>
OPENAI_COMPLIANCE_LOG_PATH=/<documented-log-path>
OPENAI_COMPLIANCE_FILES_PATH=/<documented-files-path>   # supports {file_id}
OPENAI_COMPLIANCE_API_KEY_FILE=/run/secrets/openai_compliance_api_key
COMPLIANCE_POLL_INTERVAL_SECONDS=300
COMPLIANCE_OVERLAP_SECONDS=600
COMPLIANCE_PAGE_SIZE=100
COMPLIANCE_MAX_PAGES_PER_CYCLE=50
```

Field names in the response are mapped with a JSON environment variable:

```env
OPENAI_COMPLIANCE_FIELD_MAP={"items":"data","event_id":"id","event_time":"created_at","conversation_id":"conversation_id","message_id":"message_id","workspace_id":"workspace_id","actor_email":"user.email","next_cursor":"next_cursor","has_more":"has_more","deleted_flag":"deleted"}
```

Dotted paths (`user.email`) are supported. Unknown keys are ignored rather than
crashing the poller.

## How a cycle works

```
read checkpoint
  → window = [last_window_start - OVERLAP, now]
  → for each page (bounded by MAX_PAGES_PER_CYCLE):
        for each event:
            claim the event id in source_event_keys   (global dedupe)
            write the raw JSON to S3                  (durable first)
            insert the source_events row
  → advance the checkpoint                            (only now)
  → enqueue a compliance_sync job to fold events into conversations
```

Three properties make this safe:

1. **Raw before checkpoint.** Every event is in S3 and PostgreSQL before the
   cursor moves. A crash causes reprocessing, never loss.
2. **Overlapping windows.** Each poll re-reads the previous 10 minutes, so an
   event that arrives slightly late is still collected.
3. **Global dedupe.** `source_event_keys` is keyed on
   (organization, source, source_event_id), so reprocessing is a no-op.

Failures back off exponentially with jitter, and the checkpoint records only the
**exception type** — never the upstream message, which could echo request
details.

## What compliance data may assert

Only this feed can set `capture_completeness = compliance_verified`. Browser
capture that claims it is downgraded on ingest, and there is a test for that. A
browser cannot vouch for company-wide completeness, so it is not allowed to.

## Deletion events are tombstones

When upstream reports a deletion, the archive records:

```json
{
  "upstream_deleted": true,
  "upstream_deleted_at": "2026-03-15T10:30:00Z",
  "upstream_deletion_event_id": "evt-123"
}
```

The archived content is **not** removed. An archive that silently drops records
when the source deletes them is not an archive. Removal is a separate, audited
retention decision.

## Health and monitoring

`GET /api/v1/admin/health-summary` reports:

| Field | Meaning |
| --- | --- |
| `enabled` | The flag is on |
| `configured` | Base URL, log path and API key are all present |
| `last_success_at` | Last cycle that completed cleanly |
| `last_attempt_at` | Last cycle attempted |
| `last_event_time` | Newest upstream event time seen |
| `lag_seconds` | How far behind the newest event we are |
| `consecutive_errors` | Reset to zero on success |
| `cursor_healthy` | False once errors reach five in a row |
| `total_events` | Lifetime count |

Alert on: `lag_seconds` above one hour, `consecutive_errors` ≥ 3, or
`last_success_at` older than two poll intervals.

## Enabling it

1. Obtain written authorization and the API documentation.
2. `./scripts/put_secrets.sh` to store the key.
3. Set the base URL, paths and field map in SSM.
4. Set `COMPLIANCE_POLL_ENABLED=true`.
5. Redeploy and watch the first cycles:
   ```bash
   sudo docker compose -f compose.yaml -f compose.prod.yaml logs -f compliance-poller
   ```
6. Confirm objects land under `s3://<bucket>/raw/compliance/`.
7. Check the admin health summary shows `configured: true` and a recent
   `last_success_at`.

## Adapting to a documented change

Everything endpoint-specific is inside `ComplianceAdapter`. A documented change
means editing `fetch_log_page` (query parameter names) or the field map
(response shape) — not the poller, not the importer, not the schema. Unit tests
in `tests/unit/test_services_offline.py` cover parsing, classification,
timestamp formats and the guarantee that `describe()` never leaks the API key.
