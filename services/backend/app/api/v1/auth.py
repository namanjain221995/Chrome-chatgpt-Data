"""Authentication, device registration and signed configuration endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request

from app.api.deps import PrincipalDep, SessionDep, client_ip, rate_limit_anonymous
from app.core.config import Settings, get_settings
from app.core.crypto import utcnow
from app.core.errors import AuthenticationError, AuthorizationError, ValidationError
from app.core.logging import get_logger
from app.core.security import (
    OIDCVerifier,
    VerifiedIdentity,
    decode_access_token,
    exchange_authorization_code,
)
from app.models.enums import AuditAction
from app.models.identity import Device
from app.schemas.auth import (
    AuthExchangeIn,
    AuthTokensOut,
    DeviceRegisterIn,
    DeviceRegisterOut,
    SignedRuntimeConfig,
)
from app.services import accounts
from app.services.audit import record_audit
from app.services.runtime_config import get_signed_config

logger = get_logger(__name__)
router = APIRouter(tags=["auth"])

_verifier: OIDCVerifier | None = None


def get_verifier(settings: Settings) -> OIDCVerifier:
    global _verifier
    if _verifier is None:
        _verifier = OIDCVerifier(settings)
    return _verifier


def reset_verifier(verifier: OIDCVerifier | None = None) -> None:
    """Test seam: inject a verifier backed by local test keys."""
    global _verifier
    _verifier = verifier


@router.get(
    "/config",
    response_model=SignedRuntimeConfig,
    summary="Signed runtime configuration",
    dependencies=[Depends(rate_limit_anonymous)],
)
async def get_config(
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    settings: Annotated[Settings, Depends(get_settings)] = None,  # type: ignore[assignment]
) -> SignedRuntimeConfig:
    """Signed policy document. Contains no secrets and no employee data.

    Served unauthenticated so the extension can discover the kill switch and the
    privacy notice before anyone signs in — but the managed workspace label and
    id allowlist are withheld from anonymous callers, because knowing them makes
    a workspace spoof more convincing. A valid company session gets the full
    document.
    """
    authenticated = False
    if authorization and authorization.lower().startswith("bearer "):
        try:
            decode_access_token(authorization[7:].strip(), settings)
            authenticated = True
        except (AuthenticationError, AuthorizationError):
            # An invalid token is not an error here: the caller simply gets the
            # public document.
            authenticated = False
    return get_signed_config(settings, authenticated=authenticated)


async def _identity_from_request(payload: AuthExchangeIn, settings: Settings) -> VerifiedIdentity:
    if payload.grant_type == "authorization_code":
        if not payload.code or not payload.code_verifier or not payload.redirect_uri:
            raise ValidationError("code, code_verifier and redirect_uri are required")
        token_response = await exchange_authorization_code(
            code=payload.code,
            code_verifier=payload.code_verifier,
            redirect_uri=payload.redirect_uri,
            settings=settings,
        )
        id_token = token_response.get("id_token")
        if not id_token:
            raise AuthenticationError("Identity provider did not return an ID token")
        return get_verifier(settings).verify(id_token, expected_nonce=payload.nonce)

    if payload.grant_type == "id_token":
        if not payload.id_token:
            raise ValidationError("id_token is required for this grant type")
        return get_verifier(settings).verify(payload.id_token, expected_nonce=payload.nonce)

    raise ValidationError("Unsupported grant type")


@router.post(
    "/auth/exchange",
    response_model=AuthTokensOut,
    summary="Exchange an OIDC credential for a backend session",
    dependencies=[Depends(rate_limit_anonymous)],
)
async def auth_exchange(
    request: Request,
    payload: AuthExchangeIn,
    session: SessionDep,
    settings: Annotated[Settings, Depends(get_settings)],
) -> AuthTokensOut:
    ip = client_ip(request)

    if payload.grant_type == "refresh_token":
        if not payload.refresh_token:
            raise ValidationError("refresh_token is required")
        user, organization, device = await accounts.rotate_refresh_token(
            session, refresh_token=payload.refresh_token, settings=settings
        )
        access, access_ttl, refresh, refresh_ttl, _ = await accounts.issue_session(
            session, user=user, organization=organization, device=device, settings=settings
        )
        await record_audit(
            session,
            action=AuditAction.AUTH_REFRESH,
            organization_id=organization.id,
            actor_user_id=user.id,
            actor_email=user.email,
            device_id=device.id,
            client_ip=ip,
        )
        return AuthTokensOut(
            access_token=access,
            expires_in=access_ttl,
            refresh_token=refresh,
            refresh_expires_in=refresh_ttl,
            user_id=user.id,
            organization_id=organization.id,
            device_id=device.id,
            email=user.email,
            roles=list(user.roles or []),
            notice_acknowledged=user.notice_acknowledged_at is not None,
        )

    try:
        identity = await _identity_from_request(payload, settings)
    except (AuthenticationError, AuthorizationError):
        await record_audit(
            session,
            action=AuditAction.AUTH_DENIED,
            outcome="denied",
            client_ip=ip,
            details={"grant_type": payload.grant_type},
        )
        raise

    organization = await accounts.get_or_create_organization(session, settings=settings)
    user = await accounts.get_or_create_user(
        session, organization=organization, identity=identity, settings=settings
    )

    registered_device: Device | None = None
    if payload.device_fingerprint:
        registered_device = await accounts.get_or_create_device(
            session,
            user=user,
            organization=organization,
            device_fingerprint=payload.device_fingerprint,
            extension_version=payload.extension_version,
            client_ip=ip,
        )

    access, access_ttl, refresh, refresh_ttl, _ = await accounts.issue_session(
        session, user=user, organization=organization, device=registered_device, settings=settings
    )
    await record_audit(
        session,
        action=AuditAction.AUTH_LOGIN,
        organization_id=organization.id,
        actor_user_id=user.id,
        actor_email=user.email,
        actor_roles=list(user.roles or []),
        device_id=registered_device.id if registered_device else None,
        client_ip=ip,
        details={"issuer": identity.issuer},
    )
    return AuthTokensOut(
        access_token=access,
        expires_in=access_ttl,
        refresh_token=refresh,
        refresh_expires_in=refresh_ttl,
        user_id=user.id,
        organization_id=organization.id,
        device_id=registered_device.id if registered_device else None,
        email=user.email,
        roles=list(user.roles or []),
        notice_acknowledged=user.notice_acknowledged_at is not None,
    )


@router.post(
    "/devices/register",
    response_model=DeviceRegisterOut,
    summary="Register or refresh this browser profile",
)
async def register_device(
    request: Request,
    payload: DeviceRegisterIn,
    session: SessionDep,
    principal: PrincipalDep,
) -> DeviceRegisterOut:
    device = await accounts.get_or_create_device(
        session,
        user=principal.user,
        organization=principal.organization,
        device_fingerprint=payload.device_fingerprint,
        extension_id=payload.extension_id,
        extension_version=payload.extension_version,
        adapter_version=payload.adapter_version,
        browser_version=payload.browser_version,
        platform=payload.platform,
        managed_by_policy=payload.managed_by_policy,
        client_ip=client_ip(request),
    )
    if payload.notice_acknowledged and principal.user.notice_acknowledged_at is None:
        principal.user.notice_acknowledged_at = utcnow()

    return DeviceRegisterOut(
        device_id=device.id,
        registered_at=device.created_at,
        revoked=device.revoked_at is not None,
        server_time=utcnow(),
    )
