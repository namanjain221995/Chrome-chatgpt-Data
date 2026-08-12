"""Audit trail writer.

Every administrative read, export, approval, deletion and authentication
decision produces one append-only row. Details are limited to identifiers and
counts — never message content.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import pseudonymize
from app.core.logging import correlation_id_var
from app.models.enums import AuditAction
from app.models.events import AuditEvent

_FORBIDDEN_DETAIL_KEYS = frozenset(
    {"text", "plain_text", "sanitized_html", "content", "payload", "body", "note", "title"}
)


def _safe_details(details: dict[str, Any] | None) -> dict[str, Any]:
    if not details:
        return {}
    return {k: v for k, v in details.items() if k.lower() not in _FORBIDDEN_DETAIL_KEYS}


async def record_audit(
    session: AsyncSession,
    *,
    action: AuditAction,
    organization_id: uuid.UUID | None = None,
    actor_user_id: uuid.UUID | None = None,
    actor_email: str | None = None,
    actor_roles: list[str] | None = None,
    device_id: uuid.UUID | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    outcome: str = "success",
    client_ip: str | None = None,
    user_agent: str | None = None,
    details: dict[str, Any] | None = None,
) -> AuditEvent:
    event = AuditEvent(
        id=uuid.uuid4(),
        organization_id=organization_id,
        actor_user_id=actor_user_id,
        actor_email=actor_email,
        actor_roles=actor_roles or [],
        device_id=device_id,
        action=action,
        resource_type=resource_type,
        resource_id=str(resource_id) if resource_id is not None else None,
        outcome=outcome,
        ip_hash=pseudonymize(client_ip) if client_ip else None,
        source_ip=client_ip,
        user_agent_hash=pseudonymize(user_agent) if user_agent else None,
        correlation_id=correlation_id_var.get(),
        details=_safe_details(details),
    )
    session.add(event)
    return event
