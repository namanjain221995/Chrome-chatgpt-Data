# CI/CD

One pipeline, three workflows, no registry credential and no cloud federation.

```text
git push origin main
        |
        v
.github/workflows/deploy.yml
        |
        +--> job "validate": calls ci.yml for this exact commit
        |        backend lint/format/types/tests
        |        extension lint/types/tests/build/package
        |        PostgreSQL migrations, integration tests, restore round trip
        |        Docker build, Compose topology, stack smoke tests, load smoke
        |        schema drift, secret scan, dependency audit, documentation
        |
        +--> job "deploy" (needs validate, environment: production)
                 ssh ec2-user@EC2_HOST
                 sudo scripts/deploy_production.sh <github.sha>
                         git fetch && git reset --hard <sha>
                         render .env.production from AWS SSM
                         docker compose build api          (image tagged <sha>)
                         start postgres, pre-migration backup
                         docker compose run --rm migrate
                         recreate api, worker, backup, cloudflared
                         health: API, worker, backup, tunnel, S3, public URL
                         write deploy/current-release
                 sudo scripts/verify_production.sh
```

A pull request runs `ci.yml` alone. It never touches the production
environment, so a fork cannot obtain a deployment secret.

## Workflows

| File | Trigger | What it does |
| --- | --- | --- |
| `.github/workflows/ci.yml` | `pull_request`, `push` to `main`, `workflow_call` | Full validation. Requires no production secret. |
| `.github/workflows/deploy.yml` | `push` to `main`, `workflow_dispatch` | Calls CI, then deploys the exact SHA over SSH. |
| `.github/workflows/release.yml` | tag `v*`, `workflow_dispatch` | Calls CI, packages the extension, publishes a GitHub Release. |
| `.github/workflows/test-ec2-connection.yml` | `workflow_dispatch` | Proves SSH and host prerequisites without deploying. |

`.github/actions/ec2-ssh` is a composite action shared by the deploy and
connection-test workflows. It installs the key at mode `600`, builds a
`known_hosts` file, and writes an SSH config that keeps
`StrictHostKeyChecking yes`, `PasswordAuthentication no` and `BatchMode yes`.

## Permissions and concurrency

Every workflow declares `permissions: contents: read` at the top level.
`release.yml` raises it to `contents: write` on the publishing job only.
Nothing uses `write-all`, and no workflow requests `id-token`, because no
OIDC federation is used anywhere.

```yaml
concurrency:
  group: production-deploy
  cancel-in-progress: false
```

Deployments queue instead of cancelling. Cancelling mid-flight could interrupt
a migration or leave containers half recreated; the EC2 side additionally holds
an exclusive `flock` on `/var/lock/techsara-chat-archive-deploy.lock`, so even a
manual run on the instance cannot overlap with a workflow run.

## Why no container registry

The specification asks for the simplest thing that is reliable, and to justify
the choice rather than default to it.

**Decision: build the image on the EC2 host from the deployed commit. Do not
use a container registry.**

* The instance already holds a git checkout at `/opt/techsara-chat-archive`,
  and the deployment already does `git reset --hard <sha>` to get the Compose
  file, scripts and migrations. Once the source is present, `docker compose
  build api` is the only remaining step.
* A registry would add a credential that has to live somewhere on the instance,
  be rotated, and be excluded from logs — for one host that pulls one image.
* The build is cached: only changed layers rebuild, so a typical deployment
  spends well under a minute in `build`.
* Immutability is preserved where it matters. The image is tagged
  `techsara-chat-archive-backend:<full-git-sha>`. `latest` is never used, and
  `scripts/verify_production_config.sh` fails the build if any service gains a
  floating tag.
* Rollback stays fast: deployments prune only *dangling* layers, so the
  previous SHA-tagged image is still on the host and
  `scripts/rollback_production.sh` reuses it instead of rebuilding.

The trade-off is that the artifact deployed is reproduced from source on the
host rather than byte-identical to something CI built. CI builds the same
Dockerfile on every run, so a build failure is caught before deployment; and
the *extension* — the artifact that leaves the building and is installed on 250
machines — **is** byte-reproducible and checksummed.

If this ever grows to more than one instance, publish to GHCR from CI and
change `compose.prod.yaml` to `image: ghcr.io/...:<sha>` with `pull_policy:
always`. Nothing else in the deployment flow has to change.

## What runs where

| Concern | CI runner | EC2 host |
| --- | --- | --- |
| Lint, types, unit tests | yes | no |
| PostgreSQL integration and migration tests | yes | no |
| Docker image build | yes (validation only) | yes (the deployed image) |
| Compose topology assertions | yes | yes, again, before starting anything |
| AWS credentials | never | EC2 instance role |
| Production secrets | never | AWS SSM Parameter Store |
| Cloudflare tunnel token | never | AWS SSM Parameter Store |

## Deploying a specific commit

`workflow_dispatch` on *Deploy to production* accepts a full 40-character SHA.
The workflow refuses anything that is not an ancestor of `origin/main`, so the
manual path cannot deploy an unreviewed branch.

## Failure behaviour

| Failure | Result |
| --- | --- |
| Any CI job fails | Deploy job never starts. |
| SSH or preflight fails | Deployment aborts before touching the stack. |
| SSM parameter missing | `fetch_ssm_secrets.sh` exits before any container changes. |
| Compose validation fails | Aborts before starting anything. |
| Migration fails | Aborts; a pre-migration backup was already taken. Application rolls back, schema stays at whatever the migration committed. |
| Health check fails | Application rolls back to the previous SHA; schema untouched. |
| Public URL fails on the very first deployment | Reported as a warning, because the Cloudflare hostname route may not exist yet. On every later deployment it is a hard failure. |

See [ROLLBACK.md](ROLLBACK.md) for what rollback does and does not undo.

## Local equivalent

`make verify` runs the same gate CI runs, in the same order. Run it before
pushing; CI is not a substitute for it.
