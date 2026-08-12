# End-to-end tests

The end-to-end suite is `tests/integration/compose_smoke_test.sh`, which runs
against a real `docker compose up`: PostgreSQL 16, MinIO, the API, the worker
and Caddy. It covers 23 checks — clean start, migrations on an empty database,
partition creation, TLS through Caddy, the HTTP→HTTPS redirect, a real HTTP
batch ingest, replay deduplication, the worker archiving to object storage,
snapshot creation, a full restart with no data loss, and a backup restored into
a clean database.

```bash
make test-compose
```

Browser end-to-end testing is deliberately absent. Driving a live ChatGPT
account is prohibited by the project brief, so the extension is verified against
sanitized DOM fixtures (`apps/chrome-extension/tests/fixtures/transcripts.ts`),
manifest validation, and a reproducible package build. See docs/TESTING.md.
