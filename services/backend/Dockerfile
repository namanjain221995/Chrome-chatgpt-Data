# syntax=docker/dockerfile:1.7
#
# One image, three roles: API, worker and compliance poller all run this image
# with a different command, so there is exactly one artifact to build, scan,
# sign and roll back.

# ---------------------------------------------------------------------------
# Builder: resolve dependencies into a self-contained virtualenv
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY pyproject.toml README.md* ./
COPY app ./app

RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip setuptools wheel \
 && /opt/venv/bin/pip install .

# ---------------------------------------------------------------------------
# Runtime: no compiler, no build tools, non-root
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

ARG GIT_SHA=unknown
ARG BUILD_DATE=unknown

LABEL org.opencontainers.image.title="techsara-chat-archive-backend" \
      org.opencontainers.image.description="TechSara Managed ChatGPT Session Archive backend" \
      org.opencontainers.image.revision="${GIT_SHA}" \
      org.opencontainers.image.created="${BUILD_DATE}"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:${PATH}" \
    GIT_SHA="${GIT_SHA}"

# The backup role must use the same pg_dump major version as PostgreSQL 16.
# Debian 12 ships client 15, so use the official PostgreSQL Apt repository for
# client 16. curl is also used by the container health check.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && install -d /usr/share/postgresql-common/pgdg \
 && curl --fail --show-error --silent \
      -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
      https://www.postgresql.org/media/keys/ACCC4CF8.asc \
 && printf '%s\n' \
      'deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main' \
      > /etc/apt/sources.list.d/pgdg.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends postgresql-client-16 tini awscli \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --gid 10001 techsara \
 && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin techsara

# ---------------------------------------------------------------------------
# Swagger UI assets, self-hosted.
#
# FastAPI's default docs page pulls these from a public CDN, which this API's
# Content-Security-Policy blocks -- correctly, since /docs describes the admin
# and ingest surface and an administrator may paste a bearer token into it.
# Serving them from the origin keeps the policy at 'self' and removes a
# third-party script from that page entirely. Pinned by version and verified
# by digest, so a compromised CDN cannot change what ships.
# ---------------------------------------------------------------------------
ARG SWAGGER_UI_VERSION=5.17.14
ARG SWAGGER_UI_JS_SHA256=c2e4a9ef08144839ff47c14202063ecfe4e59e70a4e7154a26bd50d880c88ba1
ARG SWAGGER_UI_CSS_SHA256=40170f0ee859d17f92131ba707329a88a070e4f66874d11365e9a77d232f6117

RUN install -d /opt/swagger-ui \
 && curl --fail --show-error --silent --location \
      -o /opt/swagger-ui/swagger-ui-bundle.js \
      "https://cdn.jsdelivr.net/npm/swagger-ui-dist@${SWAGGER_UI_VERSION}/swagger-ui-bundle.js" \
 && curl --fail --show-error --silent --location \
      -o /opt/swagger-ui/swagger-ui.css \
      "https://cdn.jsdelivr.net/npm/swagger-ui-dist@${SWAGGER_UI_VERSION}/swagger-ui.css" \
 && echo "${SWAGGER_UI_JS_SHA256}  /opt/swagger-ui/swagger-ui-bundle.js" | sha256sum -c - \
 && echo "${SWAGGER_UI_CSS_SHA256}  /opt/swagger-ui/swagger-ui.css" | sha256sum -c - \
 && chmod 0444 /opt/swagger-ui/*

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=root:root alembic.ini ./alembic.ini
COPY --chown=root:root alembic ./alembic
COPY --chown=root:root app ./app

# The application never writes into its own image; /tmp is a tmpfs mount.
USER 10001:10001

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/health/live || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]

# Gunicorn with Uvicorn workers: a supervisor that reaps and restarts a hung
# worker, which a bare `uvicorn` process would not.
CMD ["gunicorn", "app.main:app", \
     "--worker-class", "uvicorn.workers.UvicornWorker", \
     "--workers", "3", \
     "--bind", "0.0.0.0:8000", \
     "--timeout", "60", \
     "--graceful-timeout", "30", \
     "--keep-alive", "5", \
     "--max-requests", "2000", \
     "--max-requests-jitter", "200", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "--log-level", "info"]
