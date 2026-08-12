"""Organization / user / device provisioning and session issuance."""

from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.crypto import pseudonymize, utcnow
from app.core.errors import AuthenticationError, AuthorizationError
from app.core.logging import get_logger
from app.core.security import (
    Role,
    VerifiedIdentity,
    create_access_token,
    generate_refresh_token,
    hash_refresh_token,
)
from app.models.identity import Device, Organization, User, UserIdentity

logger = get_logger(__name__)

DEFAULT_ORG_SLUG = "techsara"


async def get_or_create_organization(
    session: AsyncSession, *, settings: Settings | None = None
) -> Organization:
    """Single-tenant deployment: one organization derived from configuration."""
    settings = settings or get_settings()
    slug = DEFAULT_ORG_SLUG
    org = (
        await session.execute(select(Organization).where(Organization.slug == slug))
    ).scalar_one_or_none()
    if org is not None:
        if sorted(org.allowed_domains or []) != sorted(settings.allowed_domains):
            org.allowed_domains = settings.allowed_domains
        return org

    domains = settings.allowed_domains or ["example.com"]
    org = Organization(
        id=uuid.uuid4(),
        slug=slug,
        display_name=settings.managed_workspace_label or "TechSara",
        primary_domain=domains[0],
        allowed_domains=domains,
        is_active=True,
        settings={},
    )
    session.add(org)
    await session.flush()
    return org


async def get_or_create_user(
    session: AsyncSession,
    *,
    organization: Organization,
    identity: VerifiedIdentity,
    settings: Settings | None = None,
) -> User:
    settings = settings or get_settings()
    domain = identity.domain
    allowed = settings.allowed_domains or organization.allowed_domains or []
    if allowed and domain not in [d.lower() for d in allowed]:
        raise AuthorizationError("Email domain is not allowlisted", code="domain_not_allowed")

    link = (
        await session.execute(
            select(UserIdentity).where(
                UserIdentity.issuer == identity.issuer,
                UserIdentity.subject == identity.subject,
            )
        )
    ).scalar_one_or_none()

    now = utcnow()
    if link is not None:
        user = await session.get(User, link.user_id)
        if user is None:  # pragma: no cover - referential integrity guarantees this
            raise AuthenticationError("Identity is not attached to a user")
        if not user.is_active:
            raise AuthorizationError("User account is deactivated", code="user_deactivated")
        link.last_authenticated_at = now
        user.last_seen_at = now
        # The provider is authoritative for the email address.
        if user.email != identity.email:
            user.email = identity.email
            user.email_hash = pseudonymize(identity.email)
        return user

    user = (
        await session.execute(
            select(User).where(
                User.organization_id == organization.id, User.email == identity.email
            )
        )
    ).scalar_one_or_none()

    if user is None:
        user = User(
            id=uuid.uuid4(),
            organization_id=organization.id,
            email=identity.email,
            email_hash=pseudonymize(identity.email),
            display_name=identity.name,
            roles=[Role.EMPLOYEE.value],
            is_active=True,
            last_seen_at=now,
        )
        session.add(user)
        await session.flush()

    session.add(
        UserIdentity(
            id=uuid.uuid4(),
            user_id=user.id,
            provider="google" if "google" in identity.issuer else "oidc",
            issuer=identity.issuer,
            subject=identity.subject,
            hosted_domain=identity.hosted_domain,
            last_authenticated_at=now,
        )
    )
    await session.flush()
    return user


async def get_or_create_device(
    session: AsyncSession,
    *,
    user: User,
    organization: Organization,
    device_fingerprint: str,
    extension_id: str | None = None,
    extension_version: str | None = None,
    adapter_version: str | None = None,
    browser_version: str | None = None,
    platform: str | None = None,
    managed_by_policy: bool = False,
    client_ip: str | None = None,
) -> Device:
    device = (
        await session.execute(
            select(Device).where(
                Device.user_id == user.id, Device.device_fingerprint == device_fingerprint
            )
        )
    ).scalar_one_or_none()

    now = utcnow()
    if device is None:
        device = Device(
            id=uuid.uuid4(),
            user_id=user.id,
            organization_id=organization.id,
            device_fingerprint=device_fingerprint,
        )
        session.add(device)

    device.extension_id = extension_id or device.extension_id
    device.extension_version = extension_version or device.extension_version
    device.adapter_version = adapter_version or device.adapter_version
    device.browser_version = browser_version or device.browser_version
    device.platform = platform or device.platform
    device.managed_by_policy = managed_by_policy or device.managed_by_policy
    device.last_seen_at = now
    if client_ip:
        device.last_ip_hash = pseudonymize(client_ip)
    await session.flush()
    return device


async def issue_session(
    session: AsyncSession,
    *,
    user: User,
    organization: Organization,
    device: Device | None,
    settings: Settings | None = None,
) -> tuple[str, int, str, int, uuid.UUID]:
    """Mint an access token and a rotating refresh token bound to the device."""
    settings = settings or get_settings()
    if device is not None and device.revoked_at is not None:
        raise AuthorizationError("This device session has been revoked", code="device_revoked")

    session_id = uuid.uuid4()
    roles = [Role.parse(r) for r in (user.roles or [Role.EMPLOYEE.value])]
    access_token, access_ttl = create_access_token(
        user_id=user.id,
        organization_id=organization.id,
        device_id=device.id if device else None,
        email=user.email,
        roles=roles,
        session_id=session_id,
        settings=settings,
    )
    refresh_token, refresh_hash = generate_refresh_token()
    if device is not None:
        device.session_id = session_id
        device.refresh_token_hash = refresh_hash
        device.refresh_token_expires_at = utcnow() + timedelta(
            seconds=settings.refresh_token_ttl_seconds
        )
        device.refresh_rotation_counter += 1
        device.last_sync_at = device.last_sync_at
    return (
        access_token,
        access_ttl,
        refresh_token,
        settings.refresh_token_ttl_seconds,
        session_id,
    )


async def rotate_refresh_token(
    session: AsyncSession, *, refresh_token: str, settings: Settings | None = None
) -> tuple[User, Organization, Device]:
    """Validate a refresh token and invalidate it (single use)."""
    settings = settings or get_settings()
    token_hash = hash_refresh_token(refresh_token)
    device = (
        await session.execute(select(Device).where(Device.refresh_token_hash == token_hash))
    ).scalar_one_or_none()
    if device is None:
        raise AuthenticationError("Refresh token is not recognised", code="refresh_invalid")
    if device.revoked_at is not None:
        raise AuthorizationError("This device session has been revoked", code="device_revoked")
    if device.refresh_token_expires_at and device.refresh_token_expires_at < utcnow():
        raise AuthenticationError("Refresh token expired", code="refresh_expired")

    user = await session.get(User, device.user_id)
    organization = await session.get(Organization, device.organization_id)
    if user is None or organization is None or not user.is_active:
        raise AuthenticationError("Refresh token no longer maps to an active user")

    # Single use: the hash is replaced by issue_session on the next call.
    device.refresh_token_hash = None
    return user, organization, device


async def revoke_device(
    session: AsyncSession, *, device_id: uuid.UUID, reason: str | None = None
) -> Device:
    device = await session.get(Device, device_id)
    if device is None:
        raise AuthenticationError("Unknown device", code="device_not_found")
    device.revoked_at = utcnow()
    device.revoked_reason = (reason or "revoked by administrator")[:1000]
    device.refresh_token_hash = None
    device.session_id = None
    return device
