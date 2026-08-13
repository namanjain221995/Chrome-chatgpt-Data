"""Direct Cloudflare-to-FastAPI origin trust-boundary tests."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from starlette.requests import Request

from app.api import deps
from app.core.config import Settings
from app.main import create_app


def _request(*, peer: str, headers: dict[str, str] | None = None) -> Request:
    raw_headers = [
        (name.lower().encode("ascii"), value.encode("ascii"))
        for name, value in (headers or {}).items()
    ]
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "https",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "headers": raw_headers,
            "client": (peer, 12345),
            "server": ("archive.example.com", 443),
        }
    )


def test_production_uses_valid_cloudflare_client_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(deps, "get_settings", lambda: SimpleNamespace(is_production=True))
    request = _request(
        peer="198.51.100.10",
        headers={"CF-Connecting-IP": "2001:db8::1", "X-Forwarded-For": "192.0.2.1"},
    )
    assert deps.client_ip(request) == "2001:db8::1"


def test_production_rejects_malformed_cloudflare_client_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(deps, "get_settings", lambda: SimpleNamespace(is_production=True))
    request = _request(peer="198.51.100.10", headers={"CF-Connecting-IP": "not-an-ip"})
    assert deps.client_ip(request) is None


def test_nonproduction_ignores_forwarded_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(deps, "get_settings", lambda: SimpleNamespace(is_production=False))
    request = _request(
        peer="203.0.113.9",
        headers={"CF-Connecting-IP": "192.0.2.1", "X-Forwarded-For": "192.0.2.2"},
    )
    assert deps.client_ip(request) == "203.0.113.9"


@pytest.mark.asyncio
async def test_production_rejects_an_unexpected_host() -> None:
    settings = Settings(
        environment="production",
        dev_auth_enabled=False,
        jwt_secret="j" * 48,
        config_signing_key="c" * 48,
        database_url="postgresql+asyncpg://user:strong@postgres:5432/archive",
        public_base_url="https://archive.example.com",
        archive_hostname="archive.example.com",
        allowed_email_domains="example.com",
        oidc_client_id="real-client-id",
        aws_region="us-east-1",
        s3_bucket="techsara-chatgpt",
    )
    transport = httpx.ASGITransport(app=create_app(settings))
    async with httpx.AsyncClient(
        transport=transport, base_url="https://archive.example.com"
    ) as client:
        rejected = await client.get("/", headers={"Host": "unexpected.example.com"})
        accepted = await client.get("/", headers={"Host": "archive.example.com"})
    assert rejected.status_code == 400
    assert accepted.status_code == 200


class TestApiDocsExposure:
    """`/docs` publishes the admin and ingest surface, so it is opt-in."""

    @staticmethod
    def _production(**overrides: object) -> Settings:
        base: dict[str, object] = {
            "environment": "production",
            "dev_auth_enabled": False,
            "jwt_secret": "x" * 48,
            "config_signing_key": "y" * 48,
            "database_url": "postgresql+asyncpg://u:strongpassword@postgres:5432/db",
            "public_base_url": "https://archive.example.com",
            "archive_hostname": "archive.example.com",
            "allowed_email_domains": "example.com",
            "oidc_client_id": "real-client-id",
        }
        base.update(overrides)
        return Settings(**base)  # type: ignore[arg-type]

    def test_production_hides_docs_by_default(self) -> None:
        assert self._production().api_docs_available is False

    def test_production_serves_docs_when_explicitly_enabled(self) -> None:
        assert self._production(api_docs_enabled=True).api_docs_available is True

    def test_development_serves_docs_without_any_flag(self) -> None:
        assert Settings(environment="development").api_docs_available is True

    def test_no_openapi_schema_in_production_by_default(self) -> None:
        assert create_app(self._production()).openapi_url is None

    def test_openapi_schema_served_when_enabled(self) -> None:
        assert create_app(self._production(api_docs_enabled=True)).openapi_url == "/openapi.json"

    def test_fastapi_cdn_page_is_never_used(self) -> None:
        """The built-in page loads Swagger UI from a CDN; ours does not."""
        for settings in (self._production(), self._production(api_docs_enabled=True)):
            assert create_app(settings).docs_url is None

    @staticmethod
    def _with_assets(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
        from app.api import docs as api_docs

        (tmp_path / "swagger-ui-bundle.js").write_text("/* bundle */", encoding="utf-8")
        (tmp_path / "swagger-ui.css").write_text("/* css */", encoding="utf-8")
        monkeypatch.setattr(api_docs, "SWAGGER_UI_DIR", tmp_path)

    @staticmethod
    async def _get(app, path: str):  # type: ignore[no-untyped-def]
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="https://archive.example.com"
        ) as client:
            return await client.get(path)

    async def test_docs_are_404_until_enabled(self, tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        self._with_assets(tmp_path, monkeypatch)
        app = create_app(self._production())
        assert (await self._get(app, "/docs")).status_code == 404
        assert (await self._get(app, "/openapi.json")).status_code == 404

    async def test_docs_are_served_when_enabled(self, tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        self._with_assets(tmp_path, monkeypatch)
        app = create_app(self._production(api_docs_enabled=True))

        page = await self._get(app, "/docs")
        assert page.status_code == 200
        assert (await self._get(app, "/openapi.json")).status_code == 200
        assert (await self._get(app, "/docs/initialiser.js")).status_code == 200
        assert (await self._get(app, "/static/swagger/swagger-ui.css")).status_code == 200

    async def test_the_page_loads_nothing_third_party_and_nothing_inline(
        self, tmp_path, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        """The reason for self-hosting: no CDN, and no 'unsafe-inline' script."""
        self._with_assets(tmp_path, monkeypatch)
        app = create_app(self._production(api_docs_enabled=True))
        page = await self._get(app, "/docs")
        body = page.text

        assert "cdn.jsdelivr.net" not in body
        assert "unpkg.com" not in body
        assert "<script>" not in body  # every script is a src= reference

        csp = page.headers["content-security-policy"]
        assert "script-src 'self'" in csp
        assert "'unsafe-inline'" not in csp.split("style-src")[0]

    async def test_the_api_itself_keeps_the_strict_policy(self, tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        """Relaxing CSP for /docs must not relax it for the API."""
        self._with_assets(tmp_path, monkeypatch)
        app = create_app(self._production(api_docs_enabled=True))
        health = await self._get(app, "/health/live")
        assert health.headers["content-security-policy"].startswith("default-src 'none'")
        assert "script-src" not in health.headers["content-security-policy"]
