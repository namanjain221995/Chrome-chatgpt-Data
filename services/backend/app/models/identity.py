"""Organizations, workspaces, users, external identities and devices."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import WorkspaceKind


def _enum(enum_cls: type, name: str) -> Enum:
    """VARCHAR-backed enum storing the member *value*, with a CHECK constraint.

    ``values_callable`` is essential: without it SQLAlchemy persists the member
    *name* (``PENDING``), which would silently stop every partial index and
    CHECK constraint written against the lowercase value (``pending``) from
    ever matching.
    """
    return Enum(
        enum_cls,
        native_enum=False,
        name=name,
        validate_strings=True,
        create_constraint=True,
        length=48,
        values_callable=lambda obj: [member.value for member in obj],
    )


class Organization(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "organizations"

    slug: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    primary_domain: Mapped[str] = mapped_column(String(255), nullable=False)
    allowed_domains: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    settings: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    workspaces: Mapped[list[Workspace]] = relationship(back_populates="organization")

    __table_args__ = (
        CheckConstraint("length(slug) > 0", name="slug_not_empty"),
        Index("ix_organizations_primary_domain", "primary_domain"),
    )


class Workspace(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A ChatGPT workspace as observed by the extension or compliance API.

    ``kind`` is authoritative for capture policy: only ``managed_company``
    workspaces may ever store content.
    """

    __tablename__ = "workspaces"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    source_workspace_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    workspace_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[WorkspaceKind] = mapped_column(
        _enum(WorkspaceKind, "workspace_kind"), nullable=False, default=WorkspaceKind.UNVERIFIED
    )
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    capture_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    organization: Mapped[Organization] = relationship(back_populates="workspaces")

    __table_args__ = (
        UniqueConstraint("organization_id", "workspace_hash", name="uq_workspaces_org_hash"),
        Index("ix_workspaces_source_workspace_id", "source_workspace_id"),
        CheckConstraint(
            "(kind <> 'managed_company') OR (capture_enabled IS NOT NULL)",
            name="managed_requires_capture_flag",
        ),
    )


class User(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "users"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    email_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    roles: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=lambda: ["employee"])
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    notice_acknowledged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    identities: Mapped[list[UserIdentity]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    devices: Mapped[list[Device]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("organization_id", "email", name="uq_users_org_email"),
        Index("ix_users_email_hash", "email_hash"),
        CheckConstraint("position('@' in email) > 1", name="email_looks_valid"),
    )


class UserIdentity(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """External OIDC subject mapped to an internal user."""

    __tablename__ = "user_identities"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False, default="google")
    issuer: Mapped[str] = mapped_column(String(255), nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    hosted_domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_authenticated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    user: Mapped[User] = relationship(back_populates="identities")

    __table_args__ = (
        UniqueConstraint("issuer", "subject", name="uq_user_identities_issuer_subject"),
        Index("ix_user_identities_user_id", "user_id"),
    )


class Device(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A registered browser profile running the managed extension.

    The refresh token is stored only as a SHA-256 hash and is rotated on every
    use; ``revoked_at`` lets an administrator kill a session immediately.
    """

    __tablename__ = "devices"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    device_fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    extension_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    extension_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    adapter_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    browser_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    platform: Mapped[str | None] = mapped_column(String(64), nullable=True)
    managed_by_policy: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    session_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    refresh_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    refresh_token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    refresh_rotation_counter: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_ip_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    user: Mapped[User] = relationship(back_populates="devices")

    __table_args__ = (
        UniqueConstraint("user_id", "device_fingerprint", name="uq_devices_user_fingerprint"),
        Index("ix_devices_org_last_seen", "organization_id", "last_seen_at"),
        Index("ix_devices_refresh_token_hash", "refresh_token_hash"),
    )

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None
