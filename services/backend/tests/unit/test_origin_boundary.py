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

    def test_routes_follow_the_setting(self) -> None:
        from app.main import create_app

        hidden = create_app(self._production())
        assert hidden.docs_url is None
        assert hidden.openapi_url is None

        shown = create_app(self._production(api_docs_enabled=True))
        assert shown.docs_url == "/docs"
        assert shown.openapi_url == "/openapi.json"
