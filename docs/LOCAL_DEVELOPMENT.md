# Local development

## Prerequisites

| Tool | Version | Why |
| --- | --- | --- |
| Python | 3.12+ | Backend |
| Node.js | 18.18+ (20 recommended) | Extension |
| Docker + Compose v2 | recent | PostgreSQL, MinIO, the full stack |
| Terraform | 1.6+ | Infrastructure checks only |
| k6 | optional | Load tests |

## First run

```bash
make setup          # backend virtualenv + npm install
cp .env.example .env
make compose-up     # postgres, minio, api, worker, caddy
```

The API is then on `http://localhost:8080` (through Caddy) and MinIO's console
on `http://localhost:9001` (`minioadmin` / `minioadmin`).

```bash
curl -s http://localhost:8080/health/ready | jq
curl -s http://localhost:8080/api/v1/config | jq '.config.policy'
```

## Turning capture on locally

Both gates are false by default, exactly as in production. For local work:

```bash
# .env
BROWSER_CONTENT_CAPTURE_ENABLED=true
OPENAI_WRITTEN_AUTHORIZATION_CONFIRMED=true
MANAGED_WORKSPACE_LABEL=TechSara's Workspace
ALLOWED_EMAIL_DOMAINS=example.com
DEV_AUTH_ENABLED=true
```

`DEV_AUTH_ENABLED` starts the local identity provider, which mints RS256 ID
tokens with an in-memory key so the *real* verification path runs. It is
hard-blocked when `ENVIRONMENT=production` — the code raises unconditionally,
regardless of the flag.

## Running the pieces separately

```bash
# Backend, against the scratch database
make test-db-up
make migrate
cd services/backend
ENVIRONMENT=test DATABASE_URL=postgresql+asyncpg://techsara_app:devonly_change_me@127.0.0.1:55433/techsara_chat_archive \
  .venv/bin/uvicorn app.main:app --reload

# Worker
.venv/bin/python -m app.workers.worker

# Extension, rebuilding on change
cd apps/chrome-extension && npm run dev
```

## Loading the extension in Chrome

1. `make build-extension`
2. `chrome://extensions` → enable **Developer mode** → **Load unpacked**
3. Select `apps/chrome-extension/dist`
4. Copy the extension id and set it in `.env` as `EXTENSION_IDS`, then restart
   the API so CORS allows the extension origin.

For a local backend, set managed policy so the extension knows where to look.
Create `/etc/opt/chrome/policies/managed/techsara.json` (Linux):

```json
{
  "3rdparty": {
    "extensions": {
      "<your-extension-id>": {
        "apiBaseUrl": "http://localhost:8080",
        "oidcClientId": "your-dev-oauth-client-id",
        "allowedEmailDomains": ["example.com"],
        "managedWorkspaceLabel": "TechSara's Workspace"
      }
    }
  }
}
```

`http://localhost` is the one non-HTTPS backend the extension accepts, for
development only.

## Tests

```bash
make test                # unit tests, no external services
make test-integration    # starts PostgreSQL, migrates, runs integration tests
make test-compose        # full docker compose smoke test, then destroys the stack
make verify              # everything CI runs
```

Watch mode while working:

```bash
cd apps/chrome-extension && npm run test:watch
cd services/backend && .venv/bin/pytest -q -m "not integration" -x --ff
```

## Working on the DOM adapter

Every ChatGPT selector lives in `apps/chrome-extension/src/modules/dom-adapter.ts`.
When the product's markup changes:

1. Capture a **sanitized** structural fixture — no real employee content — and
   add it to `tests/fixtures/transcripts.ts`.
2. Add a failing test in `tests/dom-adapter.test.ts`.
3. Adjust the selectors, keeping the most specific first.
4. Bump `ADAPTER_VERSION`, so archived messages record which build parsed them.

Never point tests at a live ChatGPT account.

## Regenerating shared schemas

The Pydantic models are the source of truth:

```bash
make schemas       # regenerate packages/schemas
make schema-check  # fail if committed schemas drifted, then validate extension payloads
```

## Common problems

| Symptom | Cause | Fix |
| --- | --- | --- |
| `alembic check` reports operations | Models changed without a migration | `alembic revision --autogenerate -m "..."` |
| Extension shows "Waiting for company configuration" | No managed policy, or the backend is unreachable | Set `apiBaseUrl` in managed policy; check `/health/ready` |
| Popup shows "not enabled" | Capture gates are false | Set both gates in `.env` and restart the API |
| Nothing is archived on a ChatGPT page | Workspace not verified | Check `MANAGED_WORKSPACE_LABEL` matches exactly; open the options page for the reason |
| `docker compose up` fails on port 8080 | Port in use | `CADDY_HTTP_PORT=18080 make compose-up` |
| Integration tests skip | `TEST_DATABASE_URL` unset | `make test-integration` sets it for you |
