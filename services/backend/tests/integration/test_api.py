"""Full-stack HTTP tests: auth, RBAC, headers, gates, audit and rate limiting."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from app.core.crypto import sha256_hex
from app.core.devauth import LocalIdentityProvider
from app.core.security import Role
from app.models.enums import AuditAction
from app.models.events import AuditEvent
from app.models.identity import Device, User
from tests.conftest import make_client_context, managed_workspace_ref, new_idempotency_key

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

DEVICE_FINGERPRINT = "f" * 32


@pytest.fixture
def idp() -> LocalIdentityProvider:
    from app.api.v1 import auth as auth_module
    from app.core.config import get_settings

    provider = LocalIdentityProvider(get_settings())
    auth_module.reset_verifier(provider)  # type: ignore[arg-type]
    yield provider
    auth_module.reset_verifier(None)


async def login(client, idp, *, email: str = "alice@example.com", fingerprint=DEVICE_FINGERPRINT):
    token = idp.issue_id_token(email=email, nonce="n1")
    response = await client.post(
        "/api/v1/auth/exchange",
        json={
            "grant_type": "id_token",
            "id_token": token,
            "nonce": "n1",
            "device_fingerprint": fingerprint,
            "extension_version": "1.0.0",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def auth_headers(tokens: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


class TestHealth:
    async def test_liveness_never_touches_the_database(self, api_client) -> None:
        response = await api_client.get("/health/live")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    async def test_readiness_checks_the_database(self, api_client) -> None:
        response = await api_client.get("/health/ready")
        assert response.status_code in (200, 503)
        assert "database" in response.json()["checks"]

    async def test_security_headers_are_present(self, api_client) -> None:
        response = await api_client.get("/health/live")
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["X-Frame-Options"] == "DENY"
        assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
        assert response.headers["Cache-Control"] == "no-store"

    async def test_correlation_id_is_echoed(self, api_client) -> None:
        response = await api_client.get("/health/live", headers={"X-Correlation-Id": "abc123"})
        assert response.headers["X-Correlation-Id"] == "abc123"

    async def test_hostile_correlation_id_is_replaced(self, api_client) -> None:
        response = await api_client.get(
            "/health/live", headers={"X-Correlation-Id": "bad\nvalue <script>"}
        )
        assert response.headers["X-Correlation-Id"] != "bad\nvalue <script>"


class TestConfigEndpoint:
    async def test_config_is_signed_and_public(self, api_client) -> None:
        response = await api_client.get("/api/v1/config")
        assert response.status_code == 200
        body = response.json()
        assert body["signature"]
        assert body["config"]["policy"]["personal_workspace_capture_enabled"] is False
        assert body["config"]["policy"]["capture_unsent_drafts"] is False

    async def test_config_contains_no_secrets(self, api_client) -> None:
        body = (await api_client.get("/api/v1/config")).text
        for needle in ("devonly", "jwt_secret", "password", "api_key"):
            assert needle not in body.lower()


class TestAuthentication:
    async def test_login_with_verified_id_token(self, api_client, idp) -> None:
        tokens = await login(api_client, idp)
        assert tokens["email"] == "alice@example.com"
        assert tokens["roles"] == ["employee"]
        assert tokens["device_id"]
        assert tokens["access_token"] and tokens["refresh_token"]

    async def test_forged_token_is_rejected(self, api_client, idp) -> None:
        response = await api_client.post(
            "/api/v1/auth/exchange",
            json={"grant_type": "id_token", "id_token": "not.a.token"},
        )
        assert response.status_code == 401

    async def test_email_outside_allowlist_is_rejected(self, api_client, idp) -> None:
        token = idp.issue_id_token(email="mallory@evil.example", hosted_domain="evil.example")
        response = await api_client.post(
            "/api/v1/auth/exchange", json={"grant_type": "id_token", "id_token": token}
        )
        assert response.status_code in (401, 403)

    async def test_missing_bearer_token_is_unauthorised(self, api_client) -> None:
        response = await api_client.get("/api/v1/sync/status")
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "unauthenticated"

    async def test_refresh_token_rotates(self, api_client, idp) -> None:
        tokens = await login(api_client, idp)
        response = await api_client.post(
            "/api/v1/auth/exchange",
            json={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]},
        )
        assert response.status_code == 200
        rotated = response.json()
        assert rotated["refresh_token"] != tokens["refresh_token"]

        # The old refresh token is single-use and must now fail.
        replay = await api_client.post(
            "/api/v1/auth/exchange",
            json={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]},
        )
        assert replay.status_code == 401

    async def test_login_is_audited(self, api_client, idp) -> None:
        await login(api_client, idp, email="audited@example.com")
        factory = api_client.app_session_factory
        async with factory() as session:
            rows = (
                (
                    await session.execute(
                        select(AuditEvent).where(
                            AuditEvent.action == AuditAction.AUTH_LOGIN.value,
                            AuditEvent.actor_email == "audited@example.com",
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert rows


class TestDeviceLifecycle:
    async def test_register_device_and_revoke_it(self, api_client, idp) -> None:
        tokens = await login(api_client, idp, email="device-user@example.com")
        response = await api_client.post(
            "/api/v1/devices/register",
            headers=auth_headers(tokens),
            json={
                "device_fingerprint": DEVICE_FINGERPRINT,
                "extension_version": "1.0.0",
                "adapter_version": "2024.1",
                "notice_acknowledged": True,
            },
        )
        assert response.status_code == 200
        assert response.json()["revoked"] is False

        # Revoking the device must immediately invalidate its access token.
        factory = api_client.app_session_factory
        async with factory() as session:
            device = await session.get(Device, uuid.UUID(tokens["device_id"]))
            assert device is not None
            from app.services.accounts import revoke_device

            await revoke_device(session, device_id=device.id, reason="test")
            await session.commit()

        blocked = await api_client.get("/api/v1/sync/status", headers=auth_headers(tokens))
        assert blocked.status_code == 403
        assert blocked.json()["error"]["code"] == "device_revoked"


class TestIngestEndpoints:
    async def test_conversation_upsert_and_message_batch(self, api_client, idp) -> None:
        tokens = await login(api_client, idp, email="ingest@example.com")
        headers = auth_headers(tokens)
        client_ctx = make_client_context().model_dump(mode="json")

        upsert = await api_client.post(
            "/api/v1/conversations/upsert",
            headers=headers,
            json={
                "idempotency_key": new_idempotency_key("conv"),
                "source_conversation_id": "http-conv-1",
                "title": "HTTP test",
                "workspace": managed_workspace_ref().model_dump(mode="json"),
                "capture_completeness": "complete_current_page",
                "client": client_ctx,
            },
        )
        assert upsert.status_code == 200, upsert.text
        assert upsert.json()["created"] is True

        text = "Hello from the extension"
        batch = await api_client.post(
            "/api/v1/messages/batch",
            headers=headers,
            json={
                "workspace": managed_workspace_ref().model_dump(mode="json"),
                "client": client_ctx,
                "messages": [
                    {
                        "idempotency_key": new_idempotency_key("msg"),
                        "source_conversation_id": "http-conv-1",
                        "role": "user",
                        "sequence_index": 0,
                        "text": text,
                        "content_sha256": sha256_hex(text),
                    }
                ],
            },
        )
        assert batch.status_code == 200, batch.text
        body = batch.json()
        assert body["accepted"] == 1
        assert body["results"][0]["status"] == "accepted"
        assert body["backpressure"] is False

    async def test_personal_workspace_batch_is_refused(self, api_client, idp) -> None:
        tokens = await login(api_client, idp, email="personal@example.com")
        text = "should never be stored"
        response = await api_client.post(
            "/api/v1/messages/batch",
            headers=auth_headers(tokens),
            json={
                "workspace": {"kind": "personal", "verified": True},
                "client": make_client_context().model_dump(mode="json"),
                "messages": [
                    {
                        "idempotency_key": new_idempotency_key("msg"),
                        "source_conversation_id": "personal-conv",
                        "role": "user",
                        "sequence_index": 0,
                        "text": text,
                        "content_sha256": sha256_hex(text),
                    }
                ],
            },
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "personal_workspace_blocked"

    async def test_unverified_workspace_batch_is_refused(self, api_client, idp) -> None:
        tokens = await login(api_client, idp, email="unverified@example.com")
        text = "unverified"
        response = await api_client.post(
            "/api/v1/messages/batch",
            headers=auth_headers(tokens),
            json={
                "workspace": {"kind": "unverified", "verified": False},
                "client": make_client_context().model_dump(mode="json"),
                "messages": [
                    {
                        "idempotency_key": new_idempotency_key("msg"),
                        "source_conversation_id": "c",
                        "role": "user",
                        "sequence_index": 0,
                        "text": text,
                        "content_sha256": sha256_hex(text),
                    }
                ],
            },
        )
        assert response.status_code == 403

    async def test_batch_over_the_limit_is_rejected(self, api_client, idp) -> None:
        tokens = await login(api_client, idp, email="toobig@example.com")
        text = "x"
        message = {
            "idempotency_key": new_idempotency_key("msg"),
            "source_conversation_id": "c",
            "role": "user",
            "sequence_index": 0,
            "text": text,
            "content_sha256": sha256_hex(text),
        }
        response = await api_client.post(
            "/api/v1/messages/batch",
            headers=auth_headers(tokens),
            json={
                "workspace": managed_workspace_ref().model_dump(mode="json"),
                "client": make_client_context().model_dump(mode="json"),
                "messages": [dict(message, idempotency_key=f"msg-{i:06d}") for i in range(101)],
            },
        )
        assert response.status_code == 422

    async def test_oversized_body_is_rejected_before_parsing(self, api_client, idp) -> None:
        tokens = await login(api_client, idp, email="huge@example.com")
        response = await api_client.post(
            "/api/v1/messages/batch",
            headers={**auth_headers(tokens), "Content-Length": "99999999"},
            content=b"{}",
        )
        assert response.status_code == 413
        assert response.json()["error"]["code"] == "payload_too_large"

    async def test_sync_status_states_coverage_honestly(self, api_client, idp) -> None:
        tokens = await login(api_client, idp, email="sync@example.com")
        response = await api_client.get("/api/v1/sync/status", headers=auth_headers(tokens))
        assert response.status_code == 200
        body = response.json()
        assert "does not archive conversations you never open" in body["coverage_statement"]
        assert body["capture_enabled"] is True
        assert body["kill_switch"] is False


class TestRoleBasedAccess:
    async def test_employee_cannot_read_the_admin_summary(self, api_client, idp) -> None:
        tokens = await login(api_client, idp, email="employee@example.com")
        response = await api_client.get(
            "/api/v1/admin/health-summary", headers=auth_headers(tokens)
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "forbidden"

    async def test_admin_can_read_the_summary(self, api_client, idp) -> None:
        tokens = await login(api_client, idp, email="admin@example.com")
        factory = api_client.app_session_factory
        async with factory() as session:
            user = await session.get(User, uuid.UUID(tokens["user_id"]))
            assert user is not None
            user.roles = [Role.EMPLOYEE.value, Role.COMPLIANCE_ADMIN.value]
            await session.commit()

        # Re-login so the new role lands in a freshly minted access token.
        tokens = await login(api_client, idp, email="admin@example.com")
        response = await api_client.get(
            "/api/v1/admin/health-summary", headers=auth_headers(tokens)
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["policy"]["capture_active"] is True
        assert "queue" in body and "storage" in body
        assert body["compliance"]["enabled"] is False

    async def test_employee_cannot_create_an_export(self, api_client, idp) -> None:
        tokens = await login(api_client, idp, email="notcurator@example.com")
        response = await api_client.post(
            "/api/v1/admin/exports",
            headers=auth_headers(tokens),
            json={"kind": "curated_training_jsonl", "reason": "unauthorised attempt"},
        )
        assert response.status_code == 403

    async def test_curated_export_is_blocked_while_the_flag_is_off(self, api_client, idp) -> None:
        tokens = await login(api_client, idp, email="curator@example.com")
        factory = api_client.app_session_factory
        async with factory() as session:
            user = await session.get(User, uuid.UUID(tokens["user_id"]))
            assert user is not None
            user.roles = [Role.DATA_CURATOR.value]
            await session.commit()

        tokens = await login(api_client, idp, email="curator@example.com")
        response = await api_client.post(
            "/api/v1/admin/exports",
            headers=auth_headers(tokens),
            json={"kind": "curated_training_jsonl", "reason": "quarterly curation batch"},
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "training_export_disabled"


class TestErrorContract:
    async def test_validation_errors_do_not_echo_content(self, api_client, idp) -> None:
        tokens = await login(api_client, idp, email="validation@example.com")
        secret = "MY SECRET PROMPT TEXT"
        response = await api_client.post(
            "/api/v1/messages/batch",
            headers=auth_headers(tokens),
            json={
                "workspace": managed_workspace_ref().model_dump(mode="json"),
                "client": make_client_context().model_dump(mode="json"),
                "messages": [
                    {
                        "idempotency_key": "bad key with spaces",
                        "source_conversation_id": "c",
                        "role": "user",
                        "sequence_index": 0,
                        "text": secret,
                        "content_sha256": "nope",
                    }
                ],
            },
        )
        assert response.status_code == 422
        assert secret not in response.text
        assert response.json()["error"]["code"] == "validation_error"

    async def test_unknown_route_uses_the_error_envelope(self, api_client) -> None:
        response = await api_client.get("/api/v1/does-not-exist")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"


class TestRateLimiting:
    async def test_authenticated_requests_are_rate_limited(
        self, api_client, idp, monkeypatch
    ) -> None:
        from app.api import deps

        tokens = await login(api_client, idp, email="ratelimited@example.com")
        headers = auth_headers(tokens)

        from app.core.ratelimit import CompositeRateLimiter

        tiny = CompositeRateLimiter(per_minute=3, workers=1, burst=100)
        monkeypatch.setattr(deps, "get_rate_limiter", lambda: tiny)

        statuses = []
        for _ in range(6):
            response = await api_client.get("/api/v1/sync/status", headers=headers)
            statuses.append(response.status_code)
        assert 429 in statuses
        limited = next(s for s in statuses if s == 429)
        assert limited == 429

    async def test_rate_limited_response_has_retry_after(
        self, api_client, idp, monkeypatch
    ) -> None:
        from app.api import deps
        from app.core.ratelimit import CompositeRateLimiter

        tokens = await login(api_client, idp, email="retryafter@example.com")
        tiny = CompositeRateLimiter(per_minute=1, workers=1, burst=100)
        monkeypatch.setattr(deps, "get_rate_limiter", lambda: tiny)

        await api_client.get("/api/v1/sync/status", headers=auth_headers(tokens))
        response = await api_client.get("/api/v1/sync/status", headers=auth_headers(tokens))
        assert response.status_code == 429
        assert int(response.headers["Retry-After"]) >= 1


class TestAuditTrail:
    async def test_admin_read_is_audited(self, api_client, idp) -> None:
        tokens = await login(api_client, idp, email="auditor@example.com")
        factory = api_client.app_session_factory
        async with factory() as session:
            user = await session.get(User, uuid.UUID(tokens["user_id"]))
            assert user is not None
            user.roles = [Role.SECURITY_REVIEWER.value]
            await session.commit()

        tokens = await login(api_client, idp, email="auditor@example.com")
        await api_client.get("/api/v1/admin/health-summary", headers=auth_headers(tokens))

        async with factory() as session:
            count = (
                await session.execute(
                    select(func.count())
                    .select_from(AuditEvent)
                    .where(
                        AuditEvent.action == AuditAction.ADMIN_READ.value,
                        AuditEvent.actor_email == "auditor@example.com",
                    )
                )
            ).scalar_one()
            assert count >= 1
