"""Development-only local identity provider.

Purpose: let developers and the automated test-suite exercise the *real* OIDC
verification path (RS256 signature, issuer, audience, nonce, hosted domain)
without contacting Google and without weakening production code.

Hard guarantees:
  * every entry point calls :func:`assert_dev_auth_allowed`;
  * `ENVIRONMENT=production` makes that assertion raise unconditionally, even
    if `DEV_AUTH_ENABLED=true` was somehow set;
  * the signing key is generated in memory at process start and is never
    written to disk or to a repository file.
"""

from __future__ import annotations

import time
import uuid
from functools import lru_cache
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.core.config import Settings, get_settings
from app.core.errors import AuthenticationError, AuthorizationError
from app.core.security import VerifiedIdentity, identity_from_claims

DEV_KEY_ID = "techsara-dev-local-key"


def assert_dev_auth_allowed(settings: Settings | None = None) -> Settings:
    settings = settings or get_settings()
    if settings.is_production:
        raise AuthorizationError(
            "The development identity provider is permanently disabled in production"
        )
    if not settings.dev_auth_enabled:
        raise AuthorizationError("DEV_AUTH_ENABLED is false")
    return settings


class LocalIdentityProvider:
    """In-memory RSA issuer used only for development and tests."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = assert_dev_auth_allowed(settings)
        self._private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    @property
    def public_pem(self) -> bytes:
        return self._private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )

    def jwks(self) -> dict[str, Any]:
        assert_dev_auth_allowed(self._settings)
        numbers = self._private_key.public_key().public_numbers()

        def b64(value: int) -> str:
            raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
            import base64

            return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

        return {
            "keys": [
                {
                    "kty": "RSA",
                    "use": "sig",
                    "alg": "RS256",
                    "kid": DEV_KEY_ID,
                    "n": b64(numbers.n),
                    "e": b64(numbers.e),
                }
            ]
        }

    def issue_id_token(
        self,
        *,
        email: str,
        subject: str | None = None,
        nonce: str | None = None,
        hosted_domain: str | None = None,
        email_verified: bool = True,
        expires_in: int = 900,
        audience: str | None = None,
        issuer: str | None = None,
        extra_claims: dict[str, Any] | None = None,
    ) -> str:
        settings = assert_dev_auth_allowed(self._settings)
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": issuer or settings.oidc_issuer,
            "aud": audience or settings.oidc_client_id,
            "sub": subject or str(uuid.uuid5(uuid.NAMESPACE_URL, f"dev-idp:{email}")),
            "email": email,
            "email_verified": email_verified,
            "iat": now,
            "nbf": now,
            "exp": now + expires_in,
            "name": email.split("@")[0].replace(".", " ").title(),
        }
        hd = hosted_domain if hosted_domain is not None else settings.oidc_required_hd
        if hd:
            claims["hd"] = hd
        if nonce:
            claims["nonce"] = nonce
        if extra_claims:
            claims.update(extra_claims)
        return jwt.encode(
            claims,
            self._private_key,
            algorithm="RS256",
            headers={"kid": DEV_KEY_ID},
        )

    def verify(self, id_token: str, *, expected_nonce: str | None = None) -> VerifiedIdentity:
        """Verify a locally issued token through the production claim checks."""
        settings = assert_dev_auth_allowed(self._settings)
        try:
            claims = jwt.decode(
                id_token,
                self._private_key.public_key(),
                algorithms=["RS256"],
                audience=settings.oidc_client_id,
                issuer=settings.oidc_issuer,
                leeway=30,
                options={"require": ["exp", "iat", "sub", "aud", "iss"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise AuthenticationError("ID token expired") from exc
        except Exception as exc:
            # Mirror the production verifier: every failure is one error type.
            raise AuthenticationError("ID token verification failed") from exc
        return identity_from_claims(claims, settings, expected_nonce)


@lru_cache(maxsize=1)
def get_local_identity_provider() -> LocalIdentityProvider:
    return LocalIdentityProvider()


def reset_local_identity_provider() -> None:
    get_local_identity_provider.cache_clear()
