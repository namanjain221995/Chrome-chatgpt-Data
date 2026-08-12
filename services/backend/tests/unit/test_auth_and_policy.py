"""OIDC verification with local test keys, RBAC, and fail-closed capture gates."""

from __future__ import annotations

import time
from datetime import UTC

import jwt
import pytest

from app.core.config import Settings
from app.core.devauth import LocalIdentityProvider, assert_dev_auth_allowed
from app.core.errors import AuthenticationError, AuthorizationError, PolicyError
from app.core.security import (
    ADMIN_ROLES,
    CONTENT_READ_ROLES,
    AccessTokenClaims,
    Role,
    create_access_token,
    decode_access_token,
    generate_refresh_token,
    hash_refresh_token,
)
from app.schemas.common import WorkspaceRef
from app.services.policy import (
    assert_attachment_capture_allowed,
    assert_browser_capture_allowed,
    evaluate_workspace_ref,
    workspace_hash_for,
)
from app.services.runtime_config import build_runtime_config, sign_runtime_config

BASE_ENV = {
    "environment": "test",
    "oidc_client_id": "test-client-id",
    "oidc_issuer": "https://accounts.google.com",
    "oidc_required_hd": "example.com",
    "allowed_email_domains": "example.com",
    "dev_auth_enabled": True,
}


def make_settings(**overrides: object) -> Settings:
    return Settings(**{**BASE_ENV, **overrides})  # type: ignore[arg-type]


@pytest.fixture
def idp() -> LocalIdentityProvider:
    return LocalIdentityProvider(make_settings())


class TestOIDCVerification:
    def test_valid_token_yields_identity(self, idp: LocalIdentityProvider) -> None:
        token = idp.issue_id_token(email="alice@example.com", nonce="nonce-1")
        identity = idp.verify(token, expected_nonce="nonce-1")
        assert identity.email == "alice@example.com"
        assert identity.hosted_domain == "example.com"
        assert identity.domain == "example.com"

    def test_nonce_mismatch_is_rejected(self, idp: LocalIdentityProvider) -> None:
        token = idp.issue_id_token(email="alice@example.com", nonce="nonce-1")
        with pytest.raises(AuthenticationError, match="nonce"):
            idp.verify(token, expected_nonce="different")

    def test_expired_token_is_rejected(self, idp: LocalIdentityProvider) -> None:
        token = idp.issue_id_token(email="alice@example.com", expires_in=-120)
        with pytest.raises(AuthenticationError):
            idp.verify(token)

    def test_wrong_audience_is_rejected(self, idp: LocalIdentityProvider) -> None:
        token = idp.issue_id_token(email="alice@example.com", audience="someone-else")
        with pytest.raises(AuthenticationError):
            idp.verify(token)

    def test_wrong_issuer_is_rejected(self, idp: LocalIdentityProvider) -> None:
        token = idp.issue_id_token(email="alice@example.com", issuer="https://evil.example")
        with pytest.raises(AuthenticationError):
            idp.verify(token)

    def test_unverified_email_is_rejected(self, idp: LocalIdentityProvider) -> None:
        token = idp.issue_id_token(email="alice@example.com", email_verified=False)
        with pytest.raises(AuthenticationError, match="unverified"):
            idp.verify(token)

    def test_wrong_hosted_domain_is_rejected(self, idp: LocalIdentityProvider) -> None:
        token = idp.issue_id_token(email="alice@example.com", hosted_domain="other.example")
        with pytest.raises(AuthenticationError, match="hosted domain"):
            idp.verify(token)

    def test_domain_outside_allowlist_is_rejected(self) -> None:
        settings = make_settings(oidc_required_hd=None, allowed_email_domains="corp.example")
        provider = LocalIdentityProvider(settings)
        token = provider.issue_id_token(email="alice@example.com", hosted_domain=None)
        with pytest.raises(AuthorizationError, match="allowlisted"):
            provider.verify(token)

    def test_unsigned_token_is_rejected(self, idp: LocalIdentityProvider) -> None:
        forged = jwt.encode(
            {
                "iss": "https://accounts.google.com",
                "aud": "test-client-id",
                "sub": "attacker",
                "email": "attacker@example.com",
                "email_verified": True,
                "hd": "example.com",
                "exp": int(time.time()) + 600,
                "iat": int(time.time()),
            },
            "not-the-right-key",
            algorithm="HS256",
        )
        with pytest.raises(AuthenticationError):
            idp.verify(forged)

    def test_email_alone_is_never_proof_of_identity(self, idp: LocalIdentityProvider) -> None:
        """There is no code path that accepts a bare email address."""
        with pytest.raises(AuthenticationError):
            idp.verify("alice@example.com")


class TestDevAuthGuardrails:
    def test_dev_idp_refuses_to_construct_in_production(self) -> None:
        prod = Settings(
            environment="production",
            dev_auth_enabled=False,
            jwt_secret="x" * 48,
            config_signing_key="y" * 48,
            database_url="postgresql+asyncpg://u:strongpassword@postgres:5432/db",
            public_base_url="https://archive.example.com",
            allowed_email_domains="example.com",
            oidc_client_id="real-client-id",
        )
        with pytest.raises(AuthorizationError):
            assert_dev_auth_allowed(prod)

    def test_production_rejects_dev_auth_enabled(self) -> None:
        with pytest.raises(ValueError, match="DEV_AUTH_ENABLED"):
            Settings(
                environment="production",
                dev_auth_enabled=True,
                jwt_secret="x" * 48,
                config_signing_key="y" * 48,
                database_url="postgresql+asyncpg://u:strongpassword@postgres:5432/db",
                allowed_email_domains="example.com",
                oidc_client_id="real",
            )

    def test_production_rejects_default_secrets(self) -> None:
        with pytest.raises(ValueError, match="JWT_SECRET"):
            Settings(
                environment="production",
                dev_auth_enabled=False,
                database_url="postgresql+asyncpg://u:strongpassword@postgres:5432/db",
                allowed_email_domains="example.com",
                oidc_client_id="real",
            )

    def test_production_rejects_minio_endpoint(self) -> None:
        with pytest.raises(ValueError, match="S3_ENDPOINT_URL"):
            Settings(
                environment="production",
                dev_auth_enabled=False,
                jwt_secret="x" * 48,
                config_signing_key="y" * 48,
                database_url="postgresql+asyncpg://u:strongpassword@postgres:5432/db",
                allowed_email_domains="example.com",
                oidc_client_id="real",
                s3_endpoint_url="http://minio:9000",
            )

    def test_production_rejects_content_logging(self) -> None:
        with pytest.raises(ValueError, match="LOG_MESSAGE_CONTENT"):
            Settings(
                environment="production",
                dev_auth_enabled=False,
                jwt_secret="x" * 48,
                config_signing_key="y" * 48,
                database_url="postgresql+asyncpg://u:strongpassword@postgres:5432/db",
                allowed_email_domains="example.com",
                oidc_client_id="real",
                log_message_content=True,
            )


class TestBackendTokens:
    def test_access_token_round_trip(self) -> None:
        import uuid

        settings = make_settings(jwt_secret="unit-test-secret-key-that-is-long")
        user_id, org_id, device_id, session_id = (uuid.uuid4() for _ in range(4))
        token, ttl = create_access_token(
            user_id=user_id,
            organization_id=org_id,
            device_id=device_id,
            email="alice@example.com",
            roles=[Role.EMPLOYEE, Role.SUPPORT],
            session_id=session_id,
            settings=settings,
        )
        claims = decode_access_token(token, settings)
        assert ttl > 0
        assert claims.user_id == user_id
        assert claims.device_id == device_id
        assert claims.roles == frozenset({Role.EMPLOYEE, Role.SUPPORT})

    def test_token_signed_with_another_key_is_rejected(self) -> None:
        import uuid

        good = make_settings(jwt_secret="a" * 40)
        bad = make_settings(jwt_secret="b" * 40)
        token, _ = create_access_token(
            user_id=uuid.uuid4(),
            organization_id=uuid.uuid4(),
            device_id=None,
            email="a@example.com",
            roles=[Role.EMPLOYEE],
            session_id=uuid.uuid4(),
            settings=good,
        )
        with pytest.raises(AuthenticationError):
            decode_access_token(token, bad)

    def test_expired_access_token_is_rejected(self) -> None:
        import uuid

        settings = make_settings(jwt_secret="c" * 40)
        token, _ = create_access_token(
            user_id=uuid.uuid4(),
            organization_id=uuid.uuid4(),
            device_id=None,
            email="a@example.com",
            roles=[Role.EMPLOYEE],
            session_id=uuid.uuid4(),
            settings=settings,
            ttl_seconds=-10,
        )
        with pytest.raises(AuthenticationError, match="expired"):
            decode_access_token(token, settings)

    def test_refresh_tokens_are_stored_only_as_hashes(self) -> None:
        plaintext, digest = generate_refresh_token()
        assert plaintext != digest
        assert len(digest) == 64
        assert hash_refresh_token(plaintext) == digest


class TestRoleEnforcement:
    def _claims(self, *roles: Role) -> AccessTokenClaims:
        import uuid

        return AccessTokenClaims(
            user_id=uuid.uuid4(),
            organization_id=uuid.uuid4(),
            device_id=None,
            email="a@example.com",
            roles=frozenset(roles),
            session_id=uuid.uuid4(),
            expires_at=0,
        )

    def test_employee_cannot_reach_admin_surfaces(self) -> None:
        with pytest.raises(AuthorizationError):
            self._claims(Role.EMPLOYEE).require_any(ADMIN_ROLES)

    def test_support_cannot_read_content(self) -> None:
        with pytest.raises(AuthorizationError):
            self._claims(Role.SUPPORT).require_any(CONTENT_READ_ROLES)

    def test_compliance_admin_can_read_content(self) -> None:
        self._claims(Role.COMPLIANCE_ADMIN).require_any(CONTENT_READ_ROLES)

    def test_unknown_role_is_rejected(self) -> None:
        with pytest.raises(AuthorizationError):
            Role.parse("root")


class TestCaptureGates:
    def test_capture_blocked_when_content_flag_false(self) -> None:
        settings = make_settings(
            browser_content_capture_enabled=False,
            openai_written_authorization_confirmed=True,
        )
        with pytest.raises(PolicyError) as exc:
            assert_browser_capture_allowed(settings)
        assert exc.value.code == "capture_disabled"

    def test_capture_blocked_without_written_authorization(self) -> None:
        settings = make_settings(
            browser_content_capture_enabled=True,
            openai_written_authorization_confirmed=False,
        )
        with pytest.raises(PolicyError) as exc:
            assert_browser_capture_allowed(settings)
        assert exc.value.code == "authorization_not_confirmed"

    def test_kill_switch_overrides_both_gates(self) -> None:
        settings = make_settings(
            browser_content_capture_enabled=True,
            openai_written_authorization_confirmed=True,
            kill_switch_enabled=True,
        )
        with pytest.raises(PolicyError) as exc:
            assert_browser_capture_allowed(settings)
        assert exc.value.code == "kill_switch_active"

    def test_capture_allowed_when_both_gates_true(self) -> None:
        settings = make_settings(
            browser_content_capture_enabled=True,
            openai_written_authorization_confirmed=True,
        )
        assert_browser_capture_allowed(settings)
        assert settings.browser_capture_active is True

    def test_attachment_gate_is_independent(self) -> None:
        settings = make_settings(
            browser_content_capture_enabled=True,
            openai_written_authorization_confirmed=True,
            attachment_capture_enabled=False,
        )
        with pytest.raises(PolicyError) as exc:
            assert_attachment_capture_allowed(settings)
        assert exc.value.code == "attachments_disabled"


class TestWorkspaceVerification:
    def _settings(self, **overrides: object) -> Settings:
        base: dict[str, object] = {
            "browser_content_capture_enabled": True,
            "openai_written_authorization_confirmed": True,
            "managed_workspace_label": "TechSara's Workspace",
        }
        base.update(overrides)
        return make_settings(**base)

    def test_managed_workspace_with_matching_label_is_accepted(self) -> None:
        ref = WorkspaceRef(
            kind="managed_company",
            verified=True,
            label="TechSara's Workspace",
            verification_signals=["workspace_label_match"],
        )
        assert evaluate_workspace_ref(ref, self._settings()) == "workspace_label_matched"

    def test_personal_workspace_is_always_rejected(self) -> None:
        ref = WorkspaceRef(kind="personal", verified=True, label="TechSara's Workspace")
        with pytest.raises(PolicyError) as exc:
            evaluate_workspace_ref(ref, self._settings())
        assert exc.value.code == "personal_workspace_blocked"

    def test_personal_workspace_rejected_even_if_flag_set_true(self) -> None:
        settings = self._settings(personal_workspace_capture_enabled=True)
        ref = WorkspaceRef(kind="personal", verified=True)
        with pytest.raises(PolicyError):
            evaluate_workspace_ref(ref, settings)

    def test_unverified_workspace_is_rejected(self) -> None:
        ref = WorkspaceRef(kind="unverified", verified=False)
        with pytest.raises(PolicyError) as exc:
            evaluate_workspace_ref(ref, self._settings())
        assert exc.value.code == "workspace_unverified"

    def test_client_claiming_managed_without_verified_flag_is_rejected(self) -> None:
        ref = WorkspaceRef(kind="managed_company", verified=False, label="TechSara's Workspace")
        with pytest.raises(PolicyError):
            evaluate_workspace_ref(ref, self._settings())

    def test_label_mismatch_is_rejected(self) -> None:
        ref = WorkspaceRef(
            kind="managed_company",
            verified=True,
            label="Someone Else's Workspace",
            verification_signals=["workspace_label_match"],
        )
        with pytest.raises(PolicyError) as exc:
            evaluate_workspace_ref(ref, self._settings())
        assert exc.value.code == "workspace_label_mismatch"

    def test_missing_strong_signal_is_rejected(self) -> None:
        ref = WorkspaceRef(
            kind="managed_company",
            verified=True,
            label="TechSara's Workspace",
            verification_signals=["managed_account_url_path"],
        )
        with pytest.raises(PolicyError) as exc:
            evaluate_workspace_ref(ref, self._settings())
        assert exc.value.code == "workspace_signal_missing"

    def test_unconfigured_server_refuses_everything(self) -> None:
        settings = self._settings(managed_workspace_label="", managed_workspace_ids="")
        ref = WorkspaceRef(kind="managed_company", verified=True, label="Anything")
        with pytest.raises(PolicyError) as exc:
            evaluate_workspace_ref(ref, settings)
        assert exc.value.code == "workspace_policy_unconfigured"

    def test_id_allowlist_takes_precedence_over_label(self) -> None:
        settings = self._settings(managed_workspace_ids="ws-approved")
        allowed = WorkspaceRef(
            kind="managed_company",
            verified=True,
            source_workspace_id="ws-approved",
            label="Anything At All",
            verification_signals=["workspace_id_match"],
        )
        assert evaluate_workspace_ref(allowed, settings) == "workspace_id_allowlisted"

        denied = WorkspaceRef(
            kind="managed_company",
            verified=True,
            source_workspace_id="ws-other",
            label="TechSara's Workspace",
            verification_signals=["workspace_id_match"],
        )
        with pytest.raises(PolicyError) as exc:
            evaluate_workspace_ref(denied, settings)
        assert exc.value.code == "workspace_not_allowlisted"

    def test_workspace_hash_is_stable_and_opaque(self) -> None:
        ref = WorkspaceRef(kind="managed_company", verified=True, label="TechSara's Workspace")
        first = workspace_hash_for("techsara", ref)
        assert first == workspace_hash_for("techsara", ref)
        assert "techsara" not in first.lower()
        assert len(first) == 32


class TestSignedRuntimeConfig:
    def test_config_reports_gates_and_is_signed(self) -> None:
        settings = make_settings(
            browser_content_capture_enabled=True,
            openai_written_authorization_confirmed=True,
            config_signing_key="config-signing-key-for-tests-only",
        )
        signed = sign_runtime_config(build_runtime_config(settings), settings)
        assert signed.config.policy.capture_active is True
        assert signed.config.policy.personal_workspace_capture_enabled is False
        assert signed.config.policy.capture_unsent_drafts is False
        assert signed.signature

        from app.core.crypto import verify_signature

        assert verify_signature(
            signed.config.model_dump(mode="json"),
            settings.config_signing_key,
            signed.signature,
        )

    def test_config_never_advertises_capture_when_a_gate_is_closed(self) -> None:
        settings = make_settings(
            browser_content_capture_enabled=True,
            openai_written_authorization_confirmed=False,
            auto_archive_current_open_chat=True,
            attachment_capture_enabled=True,
        )
        config = build_runtime_config(settings)
        assert config.policy.capture_active is False
        # Derived flags collapse too: the extension gets no usable capture path.
        assert config.policy.auto_archive_current_open_chat is False
        assert config.policy.attachment_capture_enabled is False

    def test_config_version_changes_when_policy_changes(self) -> None:
        open_gate = build_runtime_config(
            make_settings(
                browser_content_capture_enabled=True,
                openai_written_authorization_confirmed=True,
            )
        )
        closed_gate = build_runtime_config(make_settings(browser_content_capture_enabled=False))
        assert open_gate.config_version != closed_gate.config_version

    def test_coverage_statement_does_not_overclaim(self) -> None:
        config = build_runtime_config(make_settings())
        statement = config.coverage_statement.lower()
        assert "does not archive conversations you never open" in statement
        assert "all history" not in statement


class TestSecurityReviewFindings:
    """Regression tests for the findings in docs/SECURITY_REVIEW.md."""

    def test_f01_source_identifiers_reject_control_characters(self) -> None:
        """F-01: identifiers reach S3 keys, so the charset is constrained."""
        from pydantic import ValidationError as PydanticValidationError

        from app.core.crypto import sha256_hex
        from app.schemas.ingest import MessageIn

        hostile = "../../backups/postgres/evil\nX-Injected: 1"
        with pytest.raises(PydanticValidationError):
            MessageIn(
                idempotency_key="k-0000000000000001",
                source_conversation_id=hostile,
                role="user",
                sequence_index=0,
                text="x",
                content_sha256=sha256_hex("x"),
            )

    def test_f01_key_builders_sanitize_every_segment(self) -> None:
        """F-01, defence in depth: even a bad value cannot shape a key."""
        from datetime import datetime

        from app.services.storage import attachment_key, export_part_key, raw_event_key

        hostile = "../../backups/x\nY"
        raw = raw_event_key(
            workspace_hash="h",
            conversation_id=hostile,
            event_id="e",
            when=datetime(2026, 3, 15, tzinfo=UTC),
        )
        assert raw.startswith("raw/events/")
        assert "\n" not in raw
        assert "/../" not in raw

        attachment = attachment_key(
            stage="quarantine",
            workspace_hash="h",
            conversation_id=hostile,
            attachment_id="a",
            filename="x.png",
        )
        assert attachment.startswith("attachments/quarantine/")
        assert "\n" not in attachment

        export = export_part_key(export_id="../evil", part_number=1, split="train")
        assert export.startswith("exports/jsonl/")
        assert "/../" not in export

    def test_f02_public_config_withholds_workspace_identifiers(self) -> None:
        """F-02: anonymous callers must not learn the workspace allowlist."""
        from app.services.runtime_config import get_signed_config

        settings = make_settings(
            managed_workspace_label="TechSara's Workspace",
            managed_workspace_ids="ws-secret-1,ws-secret-2",
            config_signing_key="config-signing-key-for-tests-only",
        )

        public = get_signed_config(settings, authenticated=False)
        assert public.config.workspace_rules.managed_workspace_label is None
        assert public.config.workspace_rules.managed_workspace_ids == []
        assert "ws-secret" not in public.model_dump_json()

        private = get_signed_config(settings, authenticated=True)
        assert private.config.workspace_rules.managed_workspace_label == "TechSara's Workspace"
        assert private.config.workspace_rules.managed_workspace_ids == [
            "ws-secret-1",
            "ws-secret-2",
        ]

    def test_f02_public_config_still_carries_the_safety_information(self) -> None:
        """Redaction must not remove what makes the client fail closed."""
        from app.services.runtime_config import get_signed_config

        settings = make_settings(kill_switch_enabled=True)
        public = get_signed_config(settings, authenticated=False)
        assert public.config.policy.kill_switch is True
        assert public.config.policy.capture_active is False
        assert public.config.privacy_notice_url
        assert public.signature

    def test_f04_allowlisted_id_still_requires_an_observed_signal(self) -> None:
        """F-04: asserting an id is not the same as having observed it."""
        settings = make_settings(
            browser_content_capture_enabled=True,
            openai_written_authorization_confirmed=True,
            managed_workspace_ids="ws-approved",
        )
        without_signal = WorkspaceRef(
            kind="managed_company",
            verified=True,
            source_workspace_id="ws-approved",
            verification_signals=[],
        )
        with pytest.raises(PolicyError) as exc:
            evaluate_workspace_ref(without_signal, settings)
        assert exc.value.code == "workspace_signal_missing"

        with_signal = without_signal.model_copy(
            update={"verification_signals": ["workspace_id_match"]}
        )
        assert evaluate_workspace_ref(with_signal, settings) == "workspace_id_allowlisted"
