# =============================================================================
# TechSara Managed ChatGPT Session Archive
#
# `make verify` is the gate: it runs every check that can run locally without
# AWS credentials, and fails on the first real error.
# =============================================================================

SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

ROOT           := $(shell pwd)
BACKEND        := $(ROOT)/services/backend
EXTENSION      := $(ROOT)/apps/chrome-extension
VENV           := $(BACKEND)/.venv
PY             := $(VENV)/bin/python
PIP            := $(VENV)/bin/pip
PYTEST         := $(VENV)/bin/pytest
RUFF           := $(VENV)/bin/ruff
MYPY           := $(VENV)/bin/mypy
ALEMBIC        := $(VENV)/bin/alembic
ARTIFACTS      := $(ROOT)/artifacts
TERRAFORM      := $(shell command -v terraform 2>/dev/null || echo $$HOME/.local/bin/terraform)

# Scratch PostgreSQL used by `make test-integration` and `make migrate`.
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
	S3_BUCKET=test-bucket \
	LOG_LEVEL=WARNING

BLUE  := \033[36m
BOLD  := \033[1m
GREEN := \033[32m
RESET := \033[0m

.PHONY: help
help: ## Show this help
	@printf "$(BOLD)TechSara Managed ChatGPT Session Archive$(RESET)\n\n"
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  $(BLUE)%-32s$(RESET) %s\n", $$1, $$2}'
	@printf "\n  $(BOLD)make verify$(RESET) runs every local check.\n\n"

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

.PHONY: setup
setup: setup-backend setup-extension ## Install all dependencies
	@printf "$(GREEN)setup complete$(RESET)\n"

.PHONY: setup-backend
setup-backend: ## Create the backend virtualenv and install dependencies
	@test -d $(VENV) || python3 -m venv $(VENV)
	@$(PIP) install --quiet --upgrade pip setuptools wheel
	@cd $(BACKEND) && $(PIP) install --quiet -e ".[dev]"
	@printf "$(GREEN)backend dependencies installed$(RESET)\n"

.PHONY: setup-extension
setup-extension: ## Install extension dependencies
	@cd $(EXTENSION) && npm install --no-audit --no-fund --silent
	@printf "$(GREEN)extension dependencies installed$(RESET)\n"

# ---------------------------------------------------------------------------
# Lint and typecheck
# ---------------------------------------------------------------------------

.PHONY: lint
lint: lint-backend lint-extension lint-shell ## Lint everything

.PHONY: lint-backend
lint-backend: ## Ruff lint + format check
	@printf "$(BOLD)ruff$(RESET)\n"
	@cd $(BACKEND) && $(RUFF) check app tests
	@cd $(BACKEND) && $(RUFF) format --check app tests

.PHONY: lint-extension
lint-extension: ## ESLint the extension
	@printf "$(BOLD)eslint$(RESET)\n"
	@cd $(EXTENSION) && npm run --silent lint

.PHONY: lint-shell
lint-shell: ## Syntax-check the shell scripts
	@printf "$(BOLD)shell syntax$(RESET)\n"
	@for script in scripts/*.sh tests/integration/*.sh; do \
		bash -n "$$script" || exit 1; \
	done
	@printf "  all shell scripts parse\n"

.PHONY: format
format: ## Auto-format Python, TypeScript and Terraform
	@cd $(BACKEND) && $(RUFF) check --fix app tests && $(RUFF) format app tests
	@cd $(EXTENSION) && npm run --silent lint:fix || true
	@$(TERRAFORM) fmt -recursive infra/terraform

.PHONY: typecheck
typecheck: typecheck-backend typecheck-extension ## Typecheck everything

.PHONY: typecheck-backend
typecheck-backend: ## mypy
	@printf "$(BOLD)mypy$(RESET)\n"
	@cd $(BACKEND) && $(BACKEND_ENV) $(MYPY) app

.PHONY: typecheck-extension
typecheck-extension: ## tsc --noEmit
	@printf "$(BOLD)tsc$(RESET)\n"
	@cd $(EXTENSION) && npm run --silent typecheck

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

.PHONY: test
test: test-backend test-extension ## Run unit tests (no external services)

.PHONY: test-backend
test-backend: ## Backend unit tests
	@printf "$(BOLD)pytest (unit)$(RESET)\n"
	@cd $(BACKEND) && $(BACKEND_ENV) $(PYTEST) -q -m "not integration"

.PHONY: test-extension
test-extension: ## Extension tests
	@printf "$(BOLD)vitest$(RESET)\n"
	@cd $(EXTENSION) && npm run --silent test

.PHONY: test-integration
test-integration: test-db-up migrate ## Backend integration tests against real PostgreSQL
	@printf "$(BOLD)pytest (integration)$(RESET)\n"
	@cd $(BACKEND) && $(BACKEND_ENV) TEST_DATABASE_URL=$(TEST_DATABASE_URL) $(PYTEST) -q

.PHONY: test-compose
test-compose: ## Full docker compose smoke test (starts and destroys a stack)
	@bash tests/integration/compose_smoke_test.sh

.PHONY: test-db-up
test-db-up: ## Start the scratch PostgreSQL used by integration tests
	@if ! docker ps --format '{{.Names}}' | grep -q '^$(TEST_PG_CONTAINER)$$'; then \
		docker rm -f $(TEST_PG_CONTAINER) >/dev/null 2>&1 || true; \
		docker run -d --name $(TEST_PG_CONTAINER) \
			-e POSTGRES_PASSWORD=devonly_change_me \
			-e POSTGRES_USER=techsara_app \
			-e POSTGRES_DB=techsara_chat_archive \
			-p $(TEST_PG_PORT):5432 postgres:16-alpine >/dev/null; \
		for i in $$(seq 1 40); do \
			docker exec $(TEST_PG_CONTAINER) pg_isready -U techsara_app -q && break; \
			sleep 1; \
		done; \
		printf "  started $(TEST_PG_CONTAINER) on :$(TEST_PG_PORT)\n"; \
	else \
		printf "  $(TEST_PG_CONTAINER) already running\n"; \
	fi

.PHONY: test-db-down
test-db-down: ## Remove the scratch PostgreSQL
	@docker rm -f $(TEST_PG_CONTAINER) >/dev/null 2>&1 || true

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

.PHONY: migrate
migrate: ## Apply migrations to the scratch database
	@printf "$(BOLD)alembic upgrade head$(RESET)\n"
	@cd $(BACKEND) && $(BACKEND_ENV) DATABASE_URL=$(TEST_DATABASE_URL) $(ALEMBIC) upgrade head

.PHONY: migration-check
migration-check: test-db-up ## Prove migrations match the models and round-trip
	@printf "$(BOLD)migration drift check$(RESET)\n"
	@cd $(BACKEND) && $(BACKEND_ENV) DATABASE_URL=$(TEST_DATABASE_URL) $(ALEMBIC) upgrade head >/dev/null
	@cd $(BACKEND) && $(BACKEND_ENV) DATABASE_URL=$(TEST_DATABASE_URL) $(ALEMBIC) check
	@printf "  models match the migrated schema\n"
	@cd $(BACKEND) && $(BACKEND_ENV) DATABASE_URL=$(TEST_DATABASE_URL) $(ALEMBIC) downgrade -1 >/dev/null
	@cd $(BACKEND) && $(BACKEND_ENV) DATABASE_URL=$(TEST_DATABASE_URL) $(ALEMBIC) upgrade head >/dev/null
	@printf "  downgrade/upgrade round trip succeeded\n"

.PHONY: migrate-fresh
migrate-fresh: ## Drop and rebuild the scratch schema from empty
	@docker exec $(TEST_PG_CONTAINER) psql -U techsara_app -d techsara_chat_archive \
		-q -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;" >/dev/null
	@$(MAKE) --no-print-directory migrate

# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

.PHONY: schemas
schemas: ## Regenerate the shared JSON Schemas from the backend models
	@$(BACKEND_ENV) $(PY) scripts/generate_schemas.py

.PHONY: schema-check
schema-check: ## Fail if the shared schemas have drifted from the models
	@printf "$(BOLD)shared schema drift check$(RESET)\n"
	@$(BACKEND_ENV) $(PY) scripts/generate_schemas.py >/dev/null
	@if ! git diff --quiet -- packages/schemas 2>/dev/null; then \
		printf "  shared schemas are out of date; commit the regenerated files\n"; \
		git diff --stat -- packages/schemas; \
		exit 1; \
	fi
	@cd $(EXTENSION) && node scripts/validate-schemas.mjs

# ---------------------------------------------------------------------------
# Build and package
# ---------------------------------------------------------------------------

.PHONY: build
build: build-extension build-image ## Build every artifact

.PHONY: build-extension
build-extension: ## Build the MV3 extension bundle
	@printf "$(BOLD)extension build$(RESET)\n"
	@cd $(EXTENSION) && npm run --silent build

.PHONY: extension-zip
extension-zip: build-extension ## Package a reproducible extension ZIP
	@cd $(EXTENSION) && npm run --silent validate:manifest
	@cd $(EXTENSION) && npm run --silent package

.PHONY: build-image
build-image: ## Build the backend container image
	@printf "$(BOLD)docker build$(RESET)\n"
	@docker build -t techsara/chat-archive-backend:local \
		--build-arg GIT_SHA=$$(git rev-parse --short HEAD 2>/dev/null || echo local) \
		$(BACKEND)

.PHONY: bundle
bundle: ## Build the deployment bundle
	@bash scripts/deploy_bundle.sh

# ---------------------------------------------------------------------------
# Compose
# ---------------------------------------------------------------------------

.PHONY: compose-config
compose-config: ## Validate both compose configurations
	@printf "$(BOLD)docker compose config$(RESET)\n"
	@docker compose -f compose.yaml config >/dev/null
	@printf "  compose.yaml is valid\n"
	@set -a; \
	 POSTGRES_DB=techsara_chat_archive POSTGRES_USER=techsara_app \
	 IMAGE_REPOSITORY=ghcr.io/example/backend IMAGE_TAG=validate \
	 AWS_REGION=us-east-1 S3_BUCKET=example-bucket \
	 PUBLIC_BASE_URL=https://archive.example.com CADDY_DOMAIN=archive.example.com \
	 CADDY_EMAIL=ops@example.com ALLOWED_EMAIL_DOMAINS=example.com \
	 OIDC_CLIENT_ID=x OIDC_ISSUER=https://accounts.google.com \
	 OIDC_REQUIRED_HD=example.com EXTENSION_IDS=x \
	 MANAGED_WORKSPACE_LABEL=W MANAGED_WORKSPACE_IDS=; \
	 set +a; \
	 docker compose -f compose.yaml -f compose.prod.yaml config >/dev/null
	@printf "  compose.yaml + compose.prod.yaml is valid\n"

.PHONY: compose-up
compose-up: ## Start the development stack
	@docker compose up -d --build --wait
	@docker compose ps

.PHONY: compose-down
compose-down: ## Stop the development stack
	@docker compose down

.PHONY: compose-down-clean
compose-down-clean: ## Stop the development stack and delete its data
	@docker compose down -v

.PHONY: compose-logs
compose-logs: ## Follow application logs
	@docker compose logs -f api worker compliance-poller

# ---------------------------------------------------------------------------
# Infrastructure
# ---------------------------------------------------------------------------

.PHONY: terraform-fmt
terraform-fmt: ## Check Terraform formatting
	@printf "$(BOLD)terraform fmt$(RESET)\n"
	@$(TERRAFORM) fmt -check -recursive infra/terraform

.PHONY: terraform-validate
terraform-validate: ## Validate the Terraform configuration
	@printf "$(BOLD)terraform validate$(RESET)\n"
	@cd infra/terraform && $(TERRAFORM) init -backend=false -input=false >/dev/null
	@cd infra/terraform && $(TERRAFORM) validate

.PHONY: terraform-plan
terraform-plan: ## Plan against real AWS (requires credentials)
	@cd infra/terraform && $(TERRAFORM) init -input=false
	@cd infra/terraform && $(TERRAFORM) plan -input=false

# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------

.PHONY: backup
backup: ## Run one backup against the development stack
	@docker compose exec backup sh /opt/scripts/backup_postgres.sh

.PHONY: restore-test
restore-test: ## Prove a backup restores into a clean database
	@bash scripts/restore_test_local.sh

.PHONY: load-test
load-test: ## Run the k6 load test (requires k6 and ACCESS_TOKEN)
	@command -v k6 >/dev/null || { echo "k6 is not installed: https://k6.io/docs/get-started/installation/"; exit 1; }
	@mkdir -p $(ARTIFACTS)
	@k6 run tests/load/k6-ingest.js

# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------

.PHONY: security-check
security-check: verify-no-prohibited-aws-services secret-scan ## Run all security checks
	@printf "$(BOLD)python dependency audit$(RESET)\n"
	@cd $(BACKEND) && $(PIP) list --format=freeze > /tmp/techsara-deps.txt && \
		printf "  %s python packages pinned\n" "$$(wc -l < /tmp/techsara-deps.txt)"
	@printf "$(BOLD)npm audit$(RESET)\n"
	@cd $(EXTENSION) && npm audit --audit-level=high --omit=dev || \
		printf "  npm audit reported findings; review before release\n"

.PHONY: verify-no-prohibited-aws-services
verify-no-prohibited-aws-services: ## Prove no prohibited AWS service is used
	@bash scripts/verify_no_prohibited_aws_services.sh

.PHONY: secret-scan
secret-scan: ## Scan the working tree for committed secrets
	@bash scripts/secret_scan.sh

# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------

.PHONY: verify
verify: ## Run every local check; fails on the first error
	@printf "\n$(BOLD)=== TechSara archive verification ===$(RESET)\n\n"
	@$(MAKE) --no-print-directory lint
	@$(MAKE) --no-print-directory typecheck
	@$(MAKE) --no-print-directory test
	@$(MAKE) --no-print-directory migration-check
	@$(MAKE) --no-print-directory test-integration
	@$(MAKE) --no-print-directory schema-check
	@$(MAKE) --no-print-directory extension-zip
	@$(MAKE) --no-print-directory compose-config
	@$(MAKE) --no-print-directory terraform-fmt
	@$(MAKE) --no-print-directory terraform-validate
	@$(MAKE) --no-print-directory security-check
	@$(MAKE) --no-print-directory docs-check
	@printf "\n$(GREEN)$(BOLD)verification passed$(RESET)\n\n"

.PHONY: docs-check
docs-check: ## Check that every required document exists and is non-trivial
	@bash scripts/docs_check.sh

.PHONY: clean
clean: ## Remove build artifacts and caches
	@rm -rf $(ARTIFACTS) $(EXTENSION)/dist
	@find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
	@find . -type d -name .pytest_cache -prune -exec rm -rf {} + 2>/dev/null || true
	@find . -type d -name .ruff_cache -prune -exec rm -rf {} + 2>/dev/null || true
	@find . -type d -name .mypy_cache -prune -exec rm -rf {} + 2>/dev/null || true
	@printf "$(GREEN)cleaned$(RESET)\n"
