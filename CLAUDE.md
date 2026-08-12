# CLAUDE.md — permanent project instructions

Read this before changing anything in this repository.

## What this system is

A company-managed archive of approved ChatGPT conversations for ~250 employees:
a Manifest V3 Chrome extension, a FastAPI backend, PostgreSQL 16 in Docker on
one EC2 instance, and S3 for files, immutable raw JSON, exports and backups.

## Invariants — never violate these

1. **Never capture unsent drafts, keystrokes, passwords, cookies or ChatGPT
   session tokens.** The DOM adapter refuses to read `textarea`, `input`,
   `contenteditable` and `form` elements. There is no keyboard listener. The
   manifest requests no `cookies` permission, and the manifest validator fails
   the build if one appears.
2. **Never capture personal-workspace conversations.** Rejected at three layers:
   the extension verifier, the API schema, and the ingestion service.
   `PERSONAL_WORKSPACE_CAPTURE_ENABLED` is never honoured, even if set true.
3. **Fail closed.** No configuration, no signals, no match, or any doubt means
   capture nothing.
4. **Server decides.** `BROWSER_CONTENT_CAPTURE_ENABLED` and
   `OPENAI_WRITTEN_AUTHORIZATION_CONFIRMED` are server-side and cannot be
   overridden by any local extension setting. Both default to false.
5. **Never overclaim coverage.** No UI or document may say all history is
   archived. Only the compliance feed may set `compliance_verified`.
6. **Never invent an OpenAI endpoint path.** Base URL, paths and field mapping
   are configuration, taken from the authorized documentation.
7. **Never commit a secret.** `make secret-scan` runs in CI. Terraform manages
   non-secret SSM parameters only, because state stores values in plaintext.
8. **Never expose PostgreSQL, pgAdmin or MinIO publicly.** Caddy is the only
   public service.
9. **Never use** DynamoDB, RDS, RDS Proxy, Lambda, SQS, ECS, Fargate,
   ElastiCache or API Gateway. `make verify-no-prohibited-aws-services` proves it.
10. **Never automate a live ChatGPT account in tests.** Sanitized DOM fixtures
    only.

## Where things live

| Concern | Location |
| --- | --- |
| Every ChatGPT selector and parsing heuristic | `apps/chrome-extension/src/modules/dom-adapter.ts` |
| Capture policy decisions | `services/backend/app/services/policy.py` |
| Wire contract (source of truth) | `services/backend/app/schemas/` |
| Generated shared schemas | `packages/schemas/` (do not hand-edit) |
| Job handlers | `services/backend/app/workers/handlers.py` |
| Compliance endpoint specifics | `services/backend/app/adapters/openai_compliance.py` |
| Production guardrails | `Settings._production_guardrails` in `app/core/config.py` |

## Rules for changes

### Adding an endpoint
1. Define request and response models in `app/schemas/` with `extra="forbid"`.
2. Enforce policy through `build_context`, which checks the gates and the
   workspace before any write.
3. Audit anything administrative with `record_audit`.
4. `make schemas` to regenerate the shared JSON Schemas, and commit them.
5. Add tests, including a rejection case.

### Changing the database
1. Edit the model, then `alembic revision --autogenerate`.
2. Review the generated migration by hand. Autogenerate misses partitioning and
   gets partial indexes wrong.
3. `make migration-check` — it asserts no drift and a working downgrade/upgrade
   round trip.
4. Enums must use `_enum()` from `app/models/identity.py`. Plain `sa.Enum`
   persists the member **name**, which silently breaks every partial index and
   CHECK constraint written against the lowercase value. This has already
   happened once; see ADR-004.

### Changing the extension
1. Selectors go in `dom-adapter.ts` and nowhere else.
2. Add a sanitized fixture and a failing test before changing a selector.
3. Bump `ADAPTER_VERSION` so archived messages record which build parsed them.
4. The service worker and content script are built **separately** and must stay
   self-contained: a content script cannot resolve an `import` at runtime. The
   manifest validator fails the build if one survives; see ADR-007.
5. Never assign `innerHTML`, never touch page `localStorage`, never read
   `document.cookie`. ESLint enforces all three.

### Changing capture behaviour
Update [docs/CAPTURE_LIMITATIONS.md](docs/CAPTURE_LIMITATIONS.md) in the same
change. That document is a promise to employees, not marketing.

## Verification

```bash
make verify     # lint, typecheck, tests, migrations, integration, schemas,
                # extension package, compose config, terraform, security, docs
```

`make verify` must pass before any merge. If a check is slow, make it faster —
do not remove it.

## Style

- Python: ruff (line length 100), full type annotations, docstrings that explain
  **why**, not what.
- TypeScript: strict mode, no `any`, explicit return types.
- Comments earn their place by explaining a decision, a trade-off or a
  non-obvious constraint. Do not narrate the code.
- Error messages are for a human being at 3 a.m.: say what went wrong and what
  to do about it.

## Honesty rules

This system archives employee conversations. That imposes obligations:

- State limitations plainly, in the product and in the documentation.
- Never let a UI imply more coverage than exists.
- When something cannot be captured, record that fact (`metadata_only`,
  `partial_scroll_limit`) rather than silently omitting it.
- When a test is skipped or a feature is gated off, say so in the final report.
