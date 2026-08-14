"""Authentication and authorization primitives.

* OIDC ID tokens are verified against the provider JWKS (signature, issuer,
  audience, expiry, issued-at, nonce, hosted domain).
* The backend then mints its own short-lived HS256 access token plus an opaque
  rotating refresh token bound to a registered device.
* An e-mail address supplied by the extension is never trusted on its own.
"""

from __future__ import annotations

import enum
import secrets
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
import jwt
from jwt import PyJWKClient

from app.core.config import Settings, get_settings
from app.core.crypto import sha256_hex
from app.core.errors import AuthenticationError, AuthorizationError
from app.core.logging import get_logger

logger = get_logger(__name__)

ACCESS_TOKEN_TYPE = "access"  # noqa: S105 - claim value, not a secret
REFRESH_TOKEN_BYTES = 48
JWKS_CACHE_TTL_SECONDS = 3600
MAX_ID_TOKEN_BYTES = 8192


class Role(str, enum.Enum):
    EMPLOYEE = "employee"
    SUPPORT = "support"
    COMPLIANCE_ADMIN = "compliance_admin"
    SECURITY_REVIEWER = "security_reviewer"
    DATA_CURATOR = "data_curator"

    @classmethod
    def parse(cls, value: str) -> Role:
        try:
            return cls(value.strip().lower())
        except ValueError as exc:  # pragma: no cover - defensive
            raise AuthorizationError(f"Unknown role: {value}") from exc


#: Roles allowed to read administrative surfaces.
ADMIN_ROLES: frozenset[Role] = frozenset(
    {Role.COMPLIANCE_ADMIN, Role.SECURITY_REVIEWER, Role.SUPPORT, Role.DATA_CURATOR}
)
#: Roles allowed to read archived message content.
CONTENT_READ_ROLES: frozenset[Role] = frozenset({Role.COMPLIANCE_ADMIN, Role.SECURITY_REVIEWER})
#: Roles allowed to create curated exports.
EXPORT_ROLES: frozenset[Role] = frozenset({Role.DATA_CURATOR, Role.COMPLIANCE_ADMIN})
#: Roles allowed to approve records for curated export.
APPROVAL_ROLES: frozenset[Role] = frozenset({Role.DATA_CURATOR, Role.COMPLIANCE_ADMIN})
#: Roles allowed to run deletion / retention actions.
DELETION_ROLES: frozenset[Role] = frozenset({Role.COMPLIANCE_ADMIN})


@dataclass(frozen=True)
class VerifiedIdentity:
    """Result of validating an external OIDC ID token."""

    subject: str
    issuer: str
    email: str
    email_verified: bool
    hosted_domain: str | None
    name: str | None = None
    raw_claims: dict[str, Any] = field(default_factory=dict)

    @property
    def domain(self) -> str:
        return self.email.rsplit("@", 1)[-1].lower()


@dataclass(frozen=True)
class AccessTokenClaims:
    user_id: uuid.UUID
    organization_id: uuid.UUID
    device_id: uuid.UUID | None
    email: str
    roles: frozenset[Role]
    session_id: uuid.UUID
    expires_at: int

    def require_any(self, allowed: frozenset[Role]) -> None:
        if not (self.roles & allowed):
            raise AuthorizationError(
                "Insufficient role for this operation",
                details={"required_any": sorted(r.value for r in allowed)},
            )

    def has_any(self, allowed: frozenset[Role]) -> bool:
        return bool(self.roles & allowed)


# ---------------------------------------------------------------------------
# Backend session tokens
# ---------------------------------------------------------------------------


def create_access_token(
    *,
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
    device_id: uuid.UUID | None,
    email: str,
    roles: list[Role] | frozenset[Role],
    session_id: uuid.UUID,
    settings: Settings | None = None,
    ttl_seconds: int | None = None,
) -> tuple[str, int]:
    settings = settings or get_settings()
    now = int(time.time())
    ttl = ttl_seconds if ttl_seconds is not None else settings.access_token_ttl_seconds
    exp = now + ttl
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "org": str(organization_id),
        "did": str(device_id) if device_id else None,
        "email": email,
        "roles": sorted({r.value for r in roles}),
        "sid": str(session_id),
        "typ": ACCESS_TOKEN_TYPE,
        "iat": now,
        "nbf": now,
        "exp": exp,
        "iss": settings.app_name,
        "aud": settings.app_name + ":extension",
        "jti": secrets.token_urlsafe(16),
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm="HS256")
    return token, ttl


def decode_access_token(token: str, settings: Settings | None = None) -> AccessTokenClaims:
    settings = settings or get_settings()
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=["HS256"],
            audience=settings.app_name + ":extension",
            issuer=settings.app_name,
            options={"require": ["exp", "iat", "sub", "iss", "aud"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthenticationError("Access token expired", code="token_expired") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthenticationError("Invalid access token") from exc

    if payload.get("typ") != ACCESS_TOKEN_TYPE:
        raise AuthenticationError("Wrong token type")

    try:
        roles = frozenset(Role.parse(r) for r in payload.get("roles", []))
        return AccessTokenClaims(
            user_id=uuid.UUID(payload["sub"]),
            organization_id=uuid.UUID(payload["org"]),
            device_id=uuid.UUID(payload["did"]) if payload.get("did") else None,
            email=str(payload.get("email", "")),
            roles=roles or frozenset({Role.EMPLOYEE}),
            session_id=uuid.UUID(payload["sid"]),
            expires_at=int(payload["exp"]),
        )
    except (KeyError, ValueError) as exc:
        raise AuthenticationError("Malformed access token claims") from exc


def generate_refresh_token() -> tuple[str, str]:
    """Return `(plaintext, sha256_hex)`; only the hash is ever stored."""
    token = secrets.token_urlsafe(REFRESH_TOKEN_BYTES)
    return token, sha256_hex(token)


def hash_refresh_token(token: str) -> str:
    return sha256_hex(token)


# ---------------------------------------------------------------------------
# OIDC verification
# ---------------------------------------------------------------------------


class OIDCVerifier:
    """Verifies provider ID tokens using cached JWKS."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._jwk_client: PyJWKClient | None = None
        self._jwk_client_created_at = 0.0

    def _client(self) -> PyJWKClient:
        now = time.monotonic()
        if self._jwk_client is None or now - self._jwk_client_created_at > JWKS_CACHE_TTL_SECONDS:
            self._jwk_client = PyJWKClient(
                discover_jwks_uri(self._settings),
                cache_keys=True,
                lifespan=JWKS_CACHE_TTL_SECONDS,
                timeout=10,
            )
            self._jwk_client_created_at = now
        return self._jwk_client

    def verify(self, id_token: str, *, expected_nonce: str | None = None) -> VerifiedIdentity:
        settings = self._settings
        if not id_token or len(id_token) > MAX_ID_TOKEN_BYTES:
            raise AuthenticationError("Malformed ID token")
        try:
            signing_key = self._client().get_signing_key_from_jwt(id_token)
        except Exception as exc:
            raise AuthenticationError("Unable to resolve ID token signing key") from exc

        try:
            claims = jwt.decode(
                id_token,
                signing_key.key,
                algorithms=["RS256", "ES256"],
                audience=settings.oidc_client_id,
                issuer=settings.oidc_issuer,
                leeway=60,
                options={"require": ["exp", "iat", "sub", "aud", "iss"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise AuthenticationError("ID token expired") from exc
        except jwt.InvalidTokenError as exc:
            # PyJWT raises a distinct subclass per failure -- InvalidSignature,
            # InvalidIssuer, InvalidAudience, MissingRequiredClaim. Collapsing
            # them into one message makes a 401 undiagnosable. The subclass
            # name and PyJWT's own text carry no token material.
            logger.warning(
                "oidc_id_token_invalid",
                error_type=type(exc).__name__,
                error=str(exc)[:200],
            )
            raise AuthenticationError("ID token verification failed") from exc

        return identity_from_claims(claims, settings, expected_nonce)


def identity_from_claims(
    claims: dict[str, Any], settings: Settings, expected_nonce: str | None
) -> VerifiedIdentity:
    if expected_nonce is not None and claims.get("nonce") != expected_nonce:
        # Distinguish "the client lost its nonce" from "the client sent a
        # different one" -- a retry that overwrote stored state looks nothing
        # like a replay attack, but both land here. Presence only: never the
        # values.
        logger.warning(
            "oidc_nonce_mismatch",
            token_nonce_present=bool(claims.get("nonce")),
            expected_nonce_present=bool(expected_nonce),
        )
        raise AuthenticationError("ID token nonce mismatch")

    email = str(claims.get("email", "")).strip().lower()
    if not email or "@" not in email:
        raise AuthenticationError("ID token has no usable email claim")

    email_verified = bool(claims.get("email_verified", False))
    if not email_verified:
        raise AuthenticationError("Company identity provider reports the email as unverified")

    hosted_domain = claims.get("hd")
    hosted_domain = str(hosted_domain).lower() if hosted_domain else None
    if settings.oidc_required_hd and hosted_domain != settings.oidc_required_hd.lower():
        raise AuthenticationError(
            "ID token hosted domain is not the configured company workspace domain"
        )

    domain = email.rsplit("@", 1)[-1]
    if settings.allowed_domains and domain not in settings.allowed_domains:
        raise AuthorizationError("Email domain is not allowlisted for this deployment")

    return VerifiedIdentity(
        subject=str(claims["sub"]),
        issuer=str(claims.get("iss", settings.oidc_issuer)),
        email=email,
        email_verified=email_verified,
        hosted_domain=hosted_domain,
        name=claims.get("name"),
        raw_claims={
            k: v for k, v in claims.items() if k in {"sub", "iss", "aud", "hd", "email_verified"}
        },
    )


async def exchange_authorization_code(
    *, code: str, code_verifier: str, redirect_uri: str, settings: Settings
) -> dict[str, Any]:
    """PKCE authorization-code exchange against the provider token endpoint."""
    token_endpoint = await _discover_token_endpoint(settings)
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": code_verifier,
        "redirect_uri": redirect_uri,
        "client_id": settings.oidc_client_id,
    }
    if settings.oidc_client_secret:
        data["client_secret"] = settings.oidc_client_secret

    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(token_endpoint, data=data)
    if response.status_code >= 400:
        # Never log or echo the provider body: it can contain the code. The
        # OAuth `error` and `error_description` fields are defined by
        # RFC 6749 5.2 and carry no credential, so logging those two -- and
        # only those two -- is safe and is the only way to diagnose a
        # rejected exchange without replaying the flow.
        try:
            payload = response.json()
            provider_error = str(payload.get("error", ""))[:64]
            provider_detail = str(payload.get("error_description", ""))[:200]
        except ValueError:
            provider_error = ""
            provider_detail = ""
        logger.warning(
            "oidc_exchange_rejected",
            status=response.status_code,
            provider_error=provider_error or "unparseable",
            provider_error_description=provider_detail,
        )
        raise AuthenticationError("Identity provider rejected the authorization code")
    return response.json()


_DISCOVERY_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def _cached_discovery(issuer: str) -> dict[str, Any] | None:
    cached = _DISCOVERY_CACHE.get(issuer)
    if cached and time.monotonic() - cached[0] < JWKS_CACHE_TTL_SECONDS:
        return cached[1]
    return None


def discover_jwks_uri(settings: Settings) -> str:
    """Where the provider says its signing keys live.

    Ask, never guess. Appending the conventional
    ``<issuer>/.well-known/jwks.json`` is wrong for Google, whose ``jwks_uri``
    is on a different host entirely -- that guess 404s and every sign-in then
    fails with "Unable to resolve ID token signing key".

    ``OIDC_JWKS_URL`` still wins when set, for an air-gapped or mirrored
    provider. This is a blocking call by design: it is reached from the
    synchronous verifier, which already performs blocking network I/O inside
    :class:`PyJWKClient`, and the result is cached for the JWKS TTL.
    """
    if settings.oidc_jwks_url:
        return settings.oidc_jwks_url

    issuer = settings.oidc_issuer.rstrip("/")
    doc = _cached_discovery(issuer)
    if doc is None:
        response = httpx.get(f"{issuer}/.well-known/openid-configuration", timeout=10)
        if response.status_code >= 400:
            raise AuthenticationError("Unable to load identity provider metadata")
        doc = response.json()
        _DISCOVERY_CACHE[issuer] = (time.monotonic(), doc)
    jwks_uri = doc.get("jwks_uri")
    if not jwks_uri:
        raise AuthenticationError("Identity provider metadata has no jwks_uri")
    return str(jwks_uri)


async def _discover_token_endpoint(settings: Settings) -> str:
    issuer = settings.oidc_issuer.rstrip("/")
    cached = _cached_discovery(issuer)
    if cached is not None:
        return str(cached["token_endpoint"])
    url = f"{issuer}/.well-known/openid-configuration"
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(url)
    if response.status_code >= 400:
        raise AuthenticationError("Unable to load identity provider metadata")
    doc = response.json()
    if "token_endpoint" not in doc:
        raise AuthenticationError("Identity provider metadata has no token endpoint")
    _DISCOVERY_CACHE[issuer] = (time.monotonic(), doc)
    return str(doc["token_endpoint"])


def clear_oidc_caches() -> None:
    """Test helper."""
    _DISCOVERY_CACHE.clear()
