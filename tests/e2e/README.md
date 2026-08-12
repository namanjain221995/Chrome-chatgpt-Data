# End-to-end tests

The end-to-end suite is `tests/integration/compose_smoke_test.sh`. It starts
PostgreSQL 16, migrations, FastAPI, and the PostgreSQL-backed worker; exercises
HTTP ingestion and replay deduplication; restarts the stack; and restores a
logical backup. Direct production TLS is covered by `make test-production-compose`.

AWS SDK requests are unit-tested with botocore Stubber. A real-bucket round trip
is available only through the explicitly dispatched, dedicated-prefix CI job.

```bash
make test-compose
```

Browser end-to-end testing is deliberately absent. Driving a live ChatGPT
account is prohibited by the project brief, so the extension is verified against
sanitized DOM fixtures (`apps/chrome-extension/tests/fixtures/transcripts.ts`),
manifest validation, and a reproducible package build. See docs/TESTING.md.
