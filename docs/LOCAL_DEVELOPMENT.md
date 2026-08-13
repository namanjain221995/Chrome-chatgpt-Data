# Local development

Local work needs Python 3.12, Node 20, Docker Engine with Compose v2, Git, and
OpenSSL. PostgreSQL runs in Docker. Storage unit tests use an in-memory double
and botocore Stubber, so no AWS credential or object-storage server is required.

## Setup

```bash
make setup
cp .env.example .env
make lint
make typecheck
make test
```

The example environment is fail-closed. Browser capture, written authorization,
and training export are false. Do not enable them merely to develop unrelated
features.

## PostgreSQL integration tests

```bash
make test-integration
make migration-check
make restore-test
make test-db-down
```

The scratch PostgreSQL port binds to `127.0.0.1:55433`. Tests run migrations on
an empty database, use transaction rollback, exercise concurrent job claiming,
and prove a logical backup restores.

## Local Compose stack

```bash
make compose-up
curl -fsS http://127.0.0.1:8000/health/live | jq .
curl -fsS http://127.0.0.1:8000/health/ready | jq .
make compose-logs
make compose-down
```

FastAPI binds only loopback. PostgreSQL has no published port. The worker can
run safely while capture gates are false; jobs that require AWS should be tested
with the unit double or in the explicitly authorized real-prefix workflow.

Optional pgAdmin:

```bash
docker compose --profile admin up -d pgadmin
```

It binds `127.0.0.1:5050:80`. It is a database administration UI, not the
database, and should not be part of routine development.

## Extension

```bash
cd apps/chrome-extension
npm run test
npm run build
npm run validate:manifest
npm run package
```

Tests use sanitized DOM fixtures and never drive a live ChatGPT account. The
content script and service worker are built separately; manifest validation
fails if runtime imports survive in the classic content script.

## Shared schemas

Pydantic models are the source of truth. After a wire-model change:

```bash
make schemas
make schema-check
```

Commit the generated JSON Schemas with the model change.

## Production-shaped Compose checks

`make test-production-compose` starts `compose.prod.yaml` with disposable bind
mounts and throwaway secret files. It proves the real production contract:
FastAPI serves plain HTTP on the private network and publishes no host port,
PostgreSQL publishes no host port, pgAdmin's loopback binding actually exists,
the backup role can reach PostgreSQL through its `.pgpass`, and `cloudflared`
stays unstarted because no tunnel token exists in CI.

There is no TLS in this stack. Cloudflare terminates TLS at the edge and the
tunnel carries traffic to `http://api:8000`, so there is no origin certificate
to generate or validate.

## Real S3 test

Neither CI nor `make verify` ever touches AWS: unit tests use an in-memory
double and botocore Stubber. `tests/integration/test_real_s3.py` exists for an
administrator who wants a genuine round trip, and runs only when
`RUN_REAL_S3_TESTS=true` and `TEST_S3_PREFIX` are both set:

```bash
cd services/backend
RUN_REAL_S3_TESTS=true AWS_REGION=us-east-1 S3_BUCKET=techsara-chatgpt \
  TEST_S3_PREFIX=integration-tests/$(date -u +%Y%m%dT%H%M%SZ)/ \
  pytest -q tests/integration/test_real_s3.py
```

Run it with short-lived credentials from your own session and a unique prefix
beginning `integration-tests/`. It asserts the exact bucket and region, creates
one generated key, verifies it and deletes only that key. Never point it at a
broad production prefix.

## Testing the extension against a real backend

The extension reads its backend URL from `chrome.storage.managed` and from
nowhere else -- there is no options-page field and no compiled-in default, so
nobody using the browser can repoint a build at another server. That is the
right production property, and it means a developer build needs a policy file
too.

1. Build and load it:

   ```bash
   make extension-build
   ```

   `chrome://extensions` -> Developer mode -> **Load unpacked** ->
   `apps/chrome-extension/dist`. Copy the **ID** Chrome shows.

2. Install the policy (same shape Chrome Enterprise delivers in production):

   ```bash
   sudo ./scripts/install_dev_policy.sh \
     --extension-id <id-from-chrome> \
     --api-base-url https://archive.<company-domain>
   ```

3. **Quit the browser completely** and reopen it. Closing the window is not
   enough — policy is read at start-up, and the snap build keeps processes
   alive. Match only your own processes:

   ```bash
   pkill -u "$(id -u)" -f 'snap/chromium|chromium-browser|google-chrome'
   pgrep -u "$(id -u)" -f 'snap/chromium|chromium-browser|google-chrome' | wc -l   # expect 0
   ```

   A bare `pkill -f chromium` matches other users' processes, fails with
   `Permission denied` for each, and leaves the browser running — so the policy
   appears not to work when in fact it was never re-read.

   Confirm at `chrome://policy`. If the extension is listed by name but shows
   *No policies set*, the browser read none of the files: check that the
   extension id still matches, and that the browser really did restart.

4. Open the service worker console from `chrome://extensions` and watch it
   fetch the runtime configuration.

The unpacked id is derived from the directory path, so it changes if the
extension is loaded from elsewhere. Re-run the script with the new id, and add
each id you test with as a redirect URI on the OAuth client
(`https://<id>.chromiumapp.org/oidc`) -- see
[GOOGLE_OAUTH_SETUP.md](GOOGLE_OAUTH_SETUP.md).

Remove the policy with `sudo ./scripts/install_dev_policy.sh --remove`.

### What you should expect to see

With the capture gates off -- which is their default and their current
production value -- the extension will authenticate, fetch the signed
configuration, report `capture_active: false` in the popup, and **capture
nothing**. That is success, not a failure. Message capture begins only when the
server answers `capture_active: true`, which requires both gates and written
authorization.

## Troubleshooting

| Symptom | Safe action |
| --- | --- |
| Port 8000 busy | `API_PORT=18080 docker compose up -d api` |
| Scratch database stale | `make test-db-down && make test-integration` |
| Migration drift | Generate/review a revision; do not alter the assertion |
| Extension fixture fails | Add a structural sanitized fixture before changing selectors |
| Worker reports AWS auth | Keep gates false locally or use the approved explicit AWS test |
| Production smoke fails | Run `make build-image` first; the smoke reuses `techsara-chat-archive-backend:local` |

Before submitting, run `make verify` and ensure the working tree contains only
the intended changes.
