SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

ROOT      := $(shell pwd)
BACKEND   := $(ROOT)/services/backend
EXTENSION := $(ROOT)/apps/chrome-extension
IMAGE_NAME := techsara-chat-archive-backend
VENV      := $(BACKEND)/.venv
PY        := $(VENV)/bin/python
PIP       := $(VENV)/bin/pip
PYTEST    := $(VENV)/bin/pytest
RUFF      := $(VENV)/bin/ruff
MYPY      := $(VENV)/bin/mypy
ALEMBIC   := $(VENV)/bin/alembic
PIP_AUDIT := $(VENV)/bin/pip-audit
SHELLCHECK_IMAGE := koalaman/shellcheck-alpine:v0.10.0@sha256:5921d946dac740cbeec2fb1c898747b6105e585130cc7f0602eec9a10f7ddb63
ACTIONLINT_IMAGE := rhysd/actionlint:1.7.7@sha256:887a259a5a534f3c4f36cb02dca341673c6089431057242cdc931e9f133147e9

TEST_PG_CONTAINER ?= techsara-test-pg
TEST_PG_PORT      ?= 55433
TEST_DATABASE_URL ?= postgresql+asyncpg://techsara_app:devonly_change_me@127.0.0.1:$(TEST_PG_PORT)/techsara_chat_archive

BACKEND_ENV := ENVIRONMENT=test \
	DEV_AUTH_ENABLED=true \
	BROWSER_CONTENT_CAPTURE_ENABLED=true \
	OPENAI_WRITTEN_AUTHORIZATION_CONFIRMED=true \
	MANAGED_WORKSPACE_LABEL="TechSara's Workspace" \
	ALLOWED_EMAIL_DOMAINS=example.com \
	OIDC_CLIENT_ID=test-client-id \
	OIDC_REQUIRED_HD=example.com \
	AWS_REGION=us-east-1 \
	S3_BUCKET=techsara-chatgpt \
	S3_ENDPOINT_URL= \
	S3_USE_PATH_STYLE=false \
	LOG_LEVEL=WARNING

.PHONY: help
help: ## Show available targets
	@awk 'BEGIN {FS = ":.*?## "}; /^[a-zA-Z0-9_-]+:.*?## / {printf "  %-32s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

.PHONY: setup setup-backend setup-extension
setup: setup-backend setup-extension ## Install backend and extension dependencies
	@echo "setup complete"

setup-backend:
	@test -d $(VENV) || python3 -m venv $(VENV)
	@$(PIP) install --quiet --upgrade pip setuptools wheel
	@cd $(BACKEND) && $(PIP) install --quiet -e ".[dev]"

setup-extension:
	@cd $(EXTENSION) && npm ci --no-audit --no-fund --silent

.PHONY: lint lint-backend lint-extension lint-shell lint-workflows
lint: lint-backend lint-extension lint-shell lint-workflows ## Run all linters

lint-backend:
	@cd $(BACKEND) && $(RUFF) check app tests
	@cd $(BACKEND) && $(RUFF) format --check app tests

lint-extension:
	@cd $(EXTENSION) && npm run --silent lint

lint-shell:
	@# A script committed without its executable bit fails only on the server,
	@# with "Permission denied" or "command not found" from sudo.
	@non_exec="$$(git ls-files -s scripts tests | awk '$$1 == "100644" && $$4 ~ /\.sh$$/ {print $$4}')"; \
		if [ -n "$$non_exec" ]; then \
			echo "shell scripts are committed without the executable bit:" >&2; \
			printf '  %s\n' $$non_exec >&2; \
			echo "fix with: git update-index --chmod=+x <file>" >&2; \
			exit 1; \
		fi
	@find scripts tests/integration -type f -name '*.sh' -print0 \
		| xargs -0 -n1 bash -n
	@docker run --rm -v "$(ROOT):/mnt:ro" $(SHELLCHECK_IMAGE) sh -c \
		'find /mnt/scripts /mnt/tests/integration /mnt/tests/load -type f -name "*.sh" -exec shellcheck -x {} +'

lint-workflows:
	@docker run --rm -v "$(ROOT):/repo:ro" -w /repo $(ACTIONLINT_IMAGE)

.PHONY: format
format: ## Format Python and autofix extension lint
	@cd $(BACKEND) && $(RUFF) check --fix app tests
	@cd $(BACKEND) && $(RUFF) format app tests
	@cd $(EXTENSION) && npm run --silent lint:fix

.PHONY: typecheck typecheck-backend typecheck-extension
typecheck: typecheck-backend typecheck-extension ## Typecheck backend and extension

typecheck-backend:
	@cd $(BACKEND) && $(BACKEND_ENV) $(MYPY) app

typecheck-extension:
	@cd $(EXTENSION) && npm run --silent typecheck

.PHONY: test test-backend test-extension
test: test-backend test-extension ## Run unit tests without external services

test-backend:
	@cd $(BACKEND) && $(BACKEND_ENV) $(PYTEST) -q -m "not integration and not real_s3"

test-extension:
	@cd $(EXTENSION) && npm run --silent test

.PHONY: test-db-up test-db-down migrate test-integration migration-check migrate-fresh
test-db-up:
	@if ! docker ps --format '{{.Names}}' | grep -q '^$(TEST_PG_CONTAINER)$$'; then \
		docker rm -f $(TEST_PG_CONTAINER) >/dev/null 2>&1 || true; \
		docker run -d --name $(TEST_PG_CONTAINER) \
			-e POSTGRES_PASSWORD=devonly_change_me \
			-e POSTGRES_USER=techsara_app \
			-e POSTGRES_DB=techsara_chat_archive \
			-p 127.0.0.1:$(TEST_PG_PORT):5432 postgres:16.14-alpine >/dev/null; \
		for _ in $$(seq 1 45); do \
			docker exec $(TEST_PG_CONTAINER) pg_isready -U techsara_app -q && break; \
			sleep 1; \
		done; \
	fi
	@docker exec $(TEST_PG_CONTAINER) pg_isready -U techsara_app -q

test-db-down:
	@docker rm -f $(TEST_PG_CONTAINER) >/dev/null 2>&1 || true

migrate: test-db-up
	@cd $(BACKEND) && $(BACKEND_ENV) DATABASE_URL=$(TEST_DATABASE_URL) $(ALEMBIC) upgrade head

test-integration: migrate ## Run all PostgreSQL integration tests
	@cd $(BACKEND) && $(BACKEND_ENV) TEST_DATABASE_URL=$(TEST_DATABASE_URL) \
		$(PYTEST) -q -m integration

migration-check: test-db-up ## Verify empty upgrade, drift, downgrade and re-upgrade
	@cd $(BACKEND) && $(BACKEND_ENV) DATABASE_URL=$(TEST_DATABASE_URL) $(ALEMBIC) upgrade head
	@cd $(BACKEND) && $(BACKEND_ENV) DATABASE_URL=$(TEST_DATABASE_URL) $(ALEMBIC) check
	@cd $(BACKEND) && $(BACKEND_ENV) DATABASE_URL=$(TEST_DATABASE_URL) $(ALEMBIC) downgrade -1
	@cd $(BACKEND) && $(BACKEND_ENV) DATABASE_URL=$(TEST_DATABASE_URL) $(ALEMBIC) upgrade head
	@cd $(BACKEND) && $(BACKEND_ENV) DATABASE_URL=$(TEST_DATABASE_URL) $(ALEMBIC) check

migrate-fresh: test-db-up
	@docker exec $(TEST_PG_CONTAINER) psql -U techsara_app -d techsara_chat_archive \
		-q -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
	@$(MAKE) --no-print-directory migrate

.PHONY: schemas schema-check
schemas:
	@$(BACKEND_ENV) $(PY) scripts/generate_schemas.py

schema-check:
	@$(BACKEND_ENV) $(PY) scripts/generate_schemas.py >/dev/null
	@git diff --exit-code -- packages/schemas
	@cd $(EXTENSION) && node scripts/validate-schemas.mjs

.PHONY: extension-build extension-zip extension-verify build-image
extension-build: ## Build the Manifest V3 extension
	@cd $(EXTENSION) && npm run --silent build

extension-zip: extension-build ## Build and validate the deterministic extension ZIP
	@cd $(EXTENSION) && npm run --silent validate:manifest
	@cd $(EXTENSION) && npm run --silent package
	@first="$$(sha256sum artifacts/techsara-chatgpt-extension-*.zip | awk '{print $$1}')"; \
		cd $(EXTENSION); npm run --silent package >/dev/null; cd $(ROOT); \
		second="$$(sha256sum artifacts/techsara-chatgpt-extension-*.zip | awk '{print $$1}')"; \
		test "$$first" = "$$second"; echo "extension package reproducible: $$first"
	@$(MAKE) --no-print-directory extension-verify

extension-verify: ## Assert the packaged extension carries no secret or source map
	@bash scripts/verify_extension_package.sh

build-image: ## Build the backend image under the production image name
	@docker build -t $(IMAGE_NAME):local \
		--build-arg GIT_SHA=$$(git rev-parse --short HEAD 2>/dev/null || echo local) $(BACKEND)

.PHONY: compose-config production-config
compose-config: ## Validate development and production Compose files
	@docker compose -f compose.yaml config --quiet
	@bash scripts/verify_production_config.sh

production-config:
	@bash scripts/verify_production_config.sh

.PHONY: compose-up compose-down compose-logs test-compose test-production-compose
compose-up:
	@docker compose -f compose.yaml up -d --build --wait

compose-down:
	@docker compose -f compose.yaml down

compose-logs:
	@docker compose -f compose.yaml logs -f api worker

test-compose:
	@bash tests/integration/compose_smoke_test.sh

test-production-compose:
	@bash tests/integration/production_compose_smoke_test.sh

.PHONY: backup restore-test load-test
backup:
	@docker compose --env-file .env.production -f compose.prod.yaml exec -T backup /bin/sh /opt/scripts/backup_postgres.sh

restore-test: test-db-up
	@bash scripts/test_restore.sh

load-test:
	@bash tests/load/run_smoke.sh

.PHONY: verify-removed-technologies verify-no-caddy verify-no-terraform verify-no-minio
verify-removed-technologies:
	@bash scripts/verify_removed_technologies.sh

verify-no-caddy: ## Verify the retired proxy is absent
	@bash scripts/verify_removed_technologies.sh

verify-no-terraform: ## Verify retired infrastructure code is absent
	@bash scripts/verify_removed_technologies.sh

verify-no-minio: ## Verify the retired object-store service is absent
	@bash scripts/verify_removed_technologies.sh

.PHONY: verify-no-prohibited-aws-services secret-scan security-check dependency-check
verify-no-prohibited-aws-services: ## Check active code for prohibited AWS services
	@bash scripts/verify_no_prohibited_aws_services.sh

secret-scan:
	@bash scripts/secret_scan.sh

dependency-check:
	@cd $(BACKEND) && $(PIP_AUDIT) --local --skip-editable
	@cd $(EXTENSION) && npm audit --audit-level=high --omit=dev

security-check: verify-removed-technologies verify-no-prohibited-aws-services secret-scan dependency-check ## Run security checks

.PHONY: docs-check verify
docs-check:
	@bash scripts/docs_check.sh

verify: ## Run the complete local CI gate
	@$(MAKE) --no-print-directory lint
	@$(MAKE) --no-print-directory typecheck
	@$(MAKE) --no-print-directory test
	@$(MAKE) --no-print-directory test-integration
	@$(MAKE) --no-print-directory migration-check
	@$(MAKE) --no-print-directory schema-check
	@$(MAKE) --no-print-directory extension-build
	@$(MAKE) --no-print-directory extension-zip
	@$(MAKE) --no-print-directory compose-config
	@$(MAKE) --no-print-directory security-check
	@$(MAKE) --no-print-directory docs-check
	@$(MAKE) --no-print-directory build-image
	@$(MAKE) --no-print-directory test-production-compose
	@$(MAKE) --no-print-directory test-compose
	@$(MAKE) --no-print-directory restore-test
	@$(MAKE) --no-print-directory load-test
	@echo "verification passed"

.PHONY: clean
clean:
	@rm -rf artifacts apps/chrome-extension/dist
	@find . -type d \( -name __pycache__ -o -name .pytest_cache -o -name .ruff_cache -o -name .mypy_cache \) \
		-prune -exec rm -rf {} +
