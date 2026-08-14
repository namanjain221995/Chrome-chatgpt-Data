"""Application settings.

Every value is read from the environment. Secrets are preferentially read from
`*_FILE` paths (root-owned files rendered at deploy time from SSM Parameter
Store) so that secret material never lands in an image layer, a compose file or
a process listing.
"""

from __future__ import annotations

import functools
import os
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import quote, urlsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["development", "test", "staging", "production"]

#: Connections kept aside for things that do not use the application pool:
#: PostgreSQL's own superuser reserve, the nightly `pg_dump`, a `psql` session
#: during an incident, and the optional pgAdmin profile.
DATABASE_CONNECTION_RESERVE = 15


def _read_secret_file(path: str | None) -> str | None:
    """Return the stripped contents of a secret file, or None when unusable."""
    if not path:
        return None
    p = Path(path)
    try:
        if p.is_file():
            value = p.read_text(encoding="utf-8").strip()
            return value or None
    except OSError:
        return None
    return None


def _csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=None,
        extra="ignore",
        case_sensitive=False,
    )

    # ---- Application ------------------------------------------------------
    environment: Environment = "development"
    app_name: str = "techsara-chat-archive"
    app_version: str = "1.0.0"
    public_base_url: str = "https://archive.example.com"
    archive_hostname: str = "archive.example.com"
    api_base_path: str = "/api/v1"
    git_sha: str = "unknown"

    # ---- Database ---------------------------------------------------------
    database_url: str = (
        "postgresql+asyncpg://techsara_app:devonly_change_me@postgres:5432/techsara_chat_archive"
    )
    postgres_password_file: str | None = None
    # Bounded pooling: every API worker, the job worker and the optional poller
    # hold their own pool, and the total must stay under PostgreSQL
    # max_connections. See `max_expected_database_connections` and
    # docs/CAPACITY.md.
    database_pool_size: Annotated[int, Field(ge=1, le=100)] = 12
    database_max_overflow: Annotated[int, Field(ge=0, le=100)] = 4
    database_pool_timeout_seconds: Annotated[float, Field(gt=0, le=300)] = 30.0
    database_pool_recycle_seconds: Annotated[int, Field(ge=60, le=86_400)] = 1800
    database_pool_pre_ping: bool = True
    database_statement_timeout_ms: int = 30_000
    postgres_max_connections: Annotated[int, Field(ge=10, le=10_000)] = 120

    # ---- AWS / S3 ---------------------------------------------------------
    aws_region: str = "us-east-1"
    s3_bucket: str = "techsara-chatgpt"
    s3_endpoint_url: str | None = None
    s3_use_path_style: bool = False
    s3_encryption_mode: Literal["SSE-S3", "SSE-KMS"] = "SSE-S3"
    s3_kms_key_id: str | None = None
    presigned_upload_ttl_seconds: Annotated[int, Field(ge=30, le=3600)] = 300
    presigned_download_ttl_seconds: Annotated[int, Field(ge=30, le=3600)] = 300
    max_attachment_bytes: int = 20 * 1024 * 1024
    # Readiness probes must not issue a HeadBucket per request; the result is
    # reused for this long. 0 disables caching (tests).
    s3_health_cache_seconds: Annotated[int, Field(ge=0, le=3600)] = 60
    s3_health_timeout_seconds: Annotated[float, Field(gt=0, le=30)] = 5.0

    # ---- Authentication ---------------------------------------------------
    oidc_issuer: str = "https://accounts.google.com"
    oidc_client_id: str = "replace-me"
    oidc_client_secret_file: str | None = None
    oidc_client_secret: str | None = None
    oidc_jwks_url: str | None = None
    oidc_required_hd: str | None = None
    allowed_email_domains: str = "example.com"
    extension_ids: str = ""
    admin_origins: str = ""
    jwt_secret_file: str | None = None
    jwt_secret: str = "devonly_insecure_signing_key_change_me"  # noqa: S105 - dev placeholder
    access_token_ttl_seconds: int = 1800
    refresh_token_ttl_seconds: int = 1_209_600
    dev_auth_enabled: bool = True

    # ---- Capture policy gates --------------------------------------------
    managed_workspace_label: str = "TechSara's Workspace"
    managed_workspace_ids: str = ""
    browser_content_capture_enabled: bool = False
    openai_written_authorization_confirmed: bool = False
    auto_archive_current_open_chat: bool = True
    attachment_capture_enabled: bool = True
    personal_workspace_capture_enabled: bool = False
    capture_unsent_drafts: bool = False
    kill_switch_enabled: bool = False
    config_signing_key_file: str | None = None
    config_signing_key: str = "devonly_insecure_config_key_change_me"

    # ---- Compliance poller ------------------------------------------------
    compliance_poll_enabled: bool = False
    openai_workspace_id: str | None = None
    openai_compliance_base_url: str | None = None
    openai_compliance_log_path: str | None = None
    openai_compliance_files_path: str | None = None
    openai_compliance_api_key_file: str | None = None
    openai_compliance_api_key: str | None = None
    compliance_poll_interval_seconds: int = 300
    compliance_overlap_seconds: int = 600
    compliance_page_size: int = 100
    compliance_max_pages_per_cycle: int = 50

    # ---- Retention / export ----------------------------------------------
    training_export_enabled: bool = False
    raw_retention_days: int = 365
    backup_retention_days: int = 90
    offline_queue_max_items: int = 10_000
    offline_queue_max_bytes: int = 52_428_800
    offline_queue_max_age_days: int = 7

    # ---- Runtime ----------------------------------------------------------
    # Interactive API documentation. Off in production unless switched on
    # deliberately: an unauthenticated Swagger UI publishes the whole admin and
    # ingest surface to anyone who finds the hostname. See docs/SECURITY.md for
    # the Cloudflare Access policy that must accompany it.
    api_docs_enabled: bool = False
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"
    log_message_content: bool = False
    rate_limit_requests_per_minute: int = 300
    rate_limit_burst: int = 60
    max_request_bytes: int = 2_621_440  # 2.5 MiB, covers a 2 MiB batch + framing
    max_batch_items: int = 100
    worker_concurrency: int = 2
    worker_poll_interval_seconds: float = 2.0
    worker_stale_lock_seconds: int = 300
    job_queue_backpressure_threshold: int = 50_000
    api_workers: int = 3

    # ---- Derived / validated ---------------------------------------------
    @field_validator("api_base_path")
    @classmethod
    def _normalise_base_path(cls, v: str) -> str:
        if not v.startswith("/"):
            v = "/" + v
        return v.rstrip("/")

    @model_validator(mode="after")
    def _load_secret_files(self) -> Settings:
        """Prefer file-based secrets; fall back to inline values for dev."""
        jwt_from_file = _read_secret_file(self.jwt_secret_file)
        if jwt_from_file:
            object.__setattr__(self, "jwt_secret", jwt_from_file)

        cfg_from_file = _read_secret_file(self.config_signing_key_file)
        if cfg_from_file:
            object.__setattr__(self, "config_signing_key", cfg_from_file)

        oidc_from_file = _read_secret_file(self.oidc_client_secret_file)
        if oidc_from_file:
            object.__setattr__(self, "oidc_client_secret", oidc_from_file)

        compliance_from_file = _read_secret_file(self.openai_compliance_api_key_file)
        if compliance_from_file:
            object.__setattr__(self, "openai_compliance_api_key", compliance_from_file)

        pg_from_file = _read_secret_file(self.postgres_password_file)
        if pg_from_file and "REPLACE" in self.database_url:
            object.__setattr__(
                self,
                "database_url",
                self.database_url.replace("REPLACE", quote(pg_from_file, safe="")),
            )
        return self

    @model_validator(mode="after")
    def _production_guardrails(self) -> Settings:
        """Fail closed on unsafe production configuration."""
        if self.environment != "production":
            return self

        problems: list[str] = []
        if self.dev_auth_enabled:
            problems.append("DEV_AUTH_ENABLED must be false in production")
        if "devonly" in self.jwt_secret or len(self.jwt_secret) < 32:
            problems.append("JWT_SECRET must be a strong non-default secret in production")
        if "devonly" in self.config_signing_key or len(self.config_signing_key) < 32:
            problems.append("CONFIG_SIGNING_KEY must be a strong non-default secret in production")
        if "devonly" in self.database_url or "REPLACE" in self.database_url:
            problems.append("DATABASE_URL still contains a placeholder password")
        if self.aws_region != "us-east-1":
            problems.append("AWS_REGION must be us-east-1 in production")
        if self.s3_bucket != "techsara-chatgpt":
            problems.append("S3_BUCKET must be techsara-chatgpt in production")
        if self.s3_endpoint_url:
            problems.append("S3_ENDPOINT_URL must be empty in production")
        if self.s3_use_path_style:
            problems.append("S3_USE_PATH_STYLE must be false in production")
        if not self.public_base_url.startswith("https://"):
            problems.append("PUBLIC_BASE_URL must be https in production")
        if self.public_hostname != self.archive_hostname:
            problems.append("ARCHIVE_HOSTNAME must match PUBLIC_BASE_URL")
        if self.log_message_content:
            problems.append("LOG_MESSAGE_CONTENT must be false in production")
        if not self.allowed_domains:
            problems.append("ALLOWED_EMAIL_DOMAINS must be set in production")
        if self.oidc_client_id in ("", "replace-me"):
            problems.append("OIDC_CLIENT_ID must be configured in production")
        budget = self.postgres_max_connections - DATABASE_CONNECTION_RESERVE
        if self.max_expected_database_connections > budget:
            problems.append(
                "Connection pools may exceed PostgreSQL max_connections: "
                f"{self.max_expected_database_connections} possible vs {budget} usable "
                f"(max_connections={self.postgres_max_connections}, "
                f"reserve={DATABASE_CONNECTION_RESERVE}). "
                "Lower DATABASE_POOL_SIZE/DATABASE_MAX_OVERFLOW/API_WORKERS "
                "or raise POSTGRES_MAX_CONNECTIONS"
            )
        if problems:
            raise ValueError("Unsafe production configuration: " + "; ".join(problems))
        return self

    # ---- Convenience accessors -------------------------------------------
    @property
    def allowed_domains(self) -> list[str]:
        return [d.lower() for d in _csv(self.allowed_email_domains)]

    @property
    def extension_id_list(self) -> list[str]:
        return [e for e in _csv(self.extension_ids) if e and e != "replace-after-build"]

    @property
    def admin_origin_list(self) -> list[str]:
        return _csv(self.admin_origins)

    @property
    def managed_workspace_id_list(self) -> list[str]:
        return _csv(self.managed_workspace_ids)

    @property
    def allowed_origins(self) -> list[str]:
        origins = [f"chrome-extension://{eid}" for eid in self.extension_id_list]
        origins.extend(self.admin_origin_list)
        return origins

    # There is deliberately no `jwks_url` fallback here. Guessing
    # `<issuer>/.well-known/jwks.json` is wrong for Google -- its jwks_uri is
    # on another host, so the guess 404s and every sign-in fails. The key
    # location is read from the provider's discovery document by
    # `app.core.security.discover_jwks_uri`; `OIDC_JWKS_URL` overrides it.

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def api_docs_available(self) -> bool:
        """Swagger UI and the OpenAPI schema are served only when this is true.

        Always available outside production, where the API surface is not
        reachable by anyone but the developer running it.
        """
        return self.api_docs_enabled or not self.is_production

    @property
    def public_hostname(self) -> str | None:
        return urlsplit(self.public_base_url).hostname

    @property
    def dev_auth_allowed(self) -> bool:
        """Dev identity provider is hard-blocked in production."""
        return self.dev_auth_enabled and not self.is_production

    @property
    def browser_capture_active(self) -> bool:
        """Browser content extraction requires BOTH gates plus no kill switch."""
        return (
            self.browser_content_capture_enabled
            and self.openai_written_authorization_confirmed
            and not self.kill_switch_enabled
        )

    @property
    def max_expected_database_connections(self) -> int:
        """Worst-case backend count if every pool is simultaneously saturated.

        Each Gunicorn worker, the job worker and the optional compliance poller
        run their own SQLAlchemy engine, so pools multiply by process count and
        must be compared against PostgreSQL ``max_connections``.
        """
        per_process = self.database_pool_size + self.database_max_overflow
        processes = self.api_workers + 1  # API workers + the job worker
        if self.compliance_poll_enabled:
            processes += 1
        return per_process * processes

    @property
    def libpq_database_url(self) -> str:
        """Plain libpq DSN (psql / pg_dump), derived from the SQLAlchemy URL."""
        return self.database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Test helper: drop the cached settings singleton."""
    get_settings.cache_clear()


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}
