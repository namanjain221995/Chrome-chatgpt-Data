"""FastAPI dependencies: authentication, authorization and rate limiting."""

from __future__ import annotations

import functools
import ipaddress
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.errors import AuthenticationError, AuthorizationError, RateLimitError
from app.core.logging import actor_var
from app.core.ratelimit import CompositeRateLimiter
from app.core.security import AccessTokenClaims, Role, decode_access_token
from app.db.session import get_db_session
from app.models.identity import Device, Organization, User

SessionDep = Annotated[AsyncSession, Depends(get_db_session)]


@functools.lru_cache(maxsize=1)
def get_rate_limiter() -> CompositeRateLimiter:
    settings = get_settings()
    return CompositeRateLimiter(
        per_minute=settings.rate_limit_requests_per_minute,
        workers=max(1, settings.api_workers),
        burst=settings.rate_limit_burst,
    )


def reset_rate_limiter() -> None:
    get_rate_limiter().reset()
    get_rate_limiter.cache_clear()


@dataclass
class Principal:
    """The authenticated caller plus its loaded rows."""

    claims: AccessTokenClaims
    user: User
    organization: Organization
    device: Device | None

    @property
    def roles(self) -> frozenset[Role]:
        return self.claims.roles

    def require_any(self, allowed: frozenset[Role]) -> None:
        self.claims.require_any(allowed)


def client_ip(request: Request) -> str | None:
    """Return the client address from Cloudflare at the production origin.

    Production port 443 is restricted to Cloudflare source ranges by the EC2
    security group, and Cloudflare overwrites ``CF-Connecting-IP``. Outside
    production, use the direct peer address and never trust forwarded headers.
    """
    candidate = (
        request.headers.get("cf-connecting-ip")
        if get_settings().is_production
        else (request.client.host if request.client else None)
    )
    if not candidate:
        return None
    try:
        return str(ipaddress.ip_address(candidate.strip()))
    except ValueError:
        return None


async def get_bearer_token(
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise AuthenticationError("Missing bearer token")
    token = authorization[7:].strip()
    if not token or len(token) > 8192:
        raise AuthenticationError("Malformed bearer token")
    return token


async def get_principal(
    request: Request,
    session: SessionDep,
    token: Annotated[str, Depends(get_bearer_token)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Principal:
    claims = decode_access_token(token, settings)

    user = await session.get(User, claims.user_id)
    if user is None or not user.is_active:
        raise AuthenticationError("Token does not map to an active user")
    if user.organization_id != claims.organization_id:
        raise AuthorizationError("Token organization mismatch")

    organization = await session.get(Organization, claims.organization_id)
    if organization is None or not organization.is_active:
        raise AuthorizationError("Organization is not active")

    device: Device | None = None
    if claims.device_id is not None:
        device = await session.get(Device, claims.device_id)
        if device is None:
            raise AuthenticationError("Unknown device", code="device_not_found")
        if device.revoked_at is not None:
            raise AuthorizationError("This device session has been revoked", code="device_revoked")
        if device.user_id != user.id:
            raise AuthorizationError("Device does not belong to this user")
        # Session binding: an access token minted before a revoke-and-relogin
        # cycle must not keep working.
        if device.session_id is not None and device.session_id != claims.session_id:
            raise AuthenticationError("Session superseded", code="session_superseded")

    actor_var.set(str(user.id))

    decision = get_rate_limiter().check(
        user_key=str(user.id),
        device_key=str(device.id) if device else None,
        ip_key=client_ip(request),
    )
    if not decision.allowed:
        raise RateLimitError(
            "Rate limit exceeded",
            retry_after=decision.retry_after,
            details={"limit": decision.limit},
        )

    return Principal(claims=claims, user=user, organization=organization, device=device)


PrincipalDep = Annotated[Principal, Depends(get_principal)]


def require_roles(*roles: Role):  # type: ignore[no-untyped-def]
    """Dependency factory enforcing role membership."""
    allowed = frozenset(roles)

    async def _dependency(principal: PrincipalDep) -> Principal:
        principal.require_any(allowed)
        return principal

    return _dependency


async def rate_limit_anonymous(request: Request) -> None:
    """Limiter for unauthenticated endpoints (auth exchange, health)."""
    decision = get_rate_limiter().check(
        user_key=None, device_key=None, ip_key=client_ip(request), cost=1
    )
    if not decision.allowed:
        raise RateLimitError("Rate limit exceeded", retry_after=decision.retry_after)
