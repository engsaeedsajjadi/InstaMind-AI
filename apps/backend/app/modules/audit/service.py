"""Audit logging.

Every sensitive operation writes one row. The writer never raises for a
logging failure — an audit problem must not take down the business operation —
but it does emit a structured error log so monitoring can alert on it.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_trace_id, logger
from app.core.security import redact_secrets
from app.modules.audit.models import AuditLog


class AuditService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def record(
        self,
        *,
        action: str,
        workspace_id: uuid.UUID | None = None,
        actor_id: uuid.UUID | None = None,
        actor_email: str | None = None,
        actor_type: str = "USER",
        resource_type: str | None = None,
        resource_id: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
        outcome: str = "SUCCESS",
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AuditLog:
        entry = AuditLog(
            workspace_id=workspace_id,
            actor_id=actor_id,
            actor_email=actor_email,
            actor_type=actor_type,
            action=action,
            resource_type=resource_type,
            resource_id=str(resource_id) if resource_id else None,
            ip_address=ip_address,
            user_agent=(user_agent or "")[:512] or None,
            outcome=outcome,
            before=redact_secrets(before or {}),
            after=redact_secrets(after or {}),
            detail=redact_secrets(metadata or {}),
            trace_id=get_trace_id(),
        )
        self.session.add(entry)
        try:
            await self.session.flush()
        except Exception:  # pragma: no cover - defensive
            logger.error("audit_write_failed", action=action, exc_info=True)
        return entry

    async def query(
        self,
        *,
        workspace_id: uuid.UUID | None,
        action: str | None = None,
        actor_id: uuid.UUID | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[AuditLog]:
        stmt = select(AuditLog).order_by(AuditLog.created_at.desc())
        if workspace_id is not None:
            stmt = stmt.where(AuditLog.workspace_id == workspace_id)
        else:
            stmt = stmt.where(AuditLog.workspace_id.is_(None))
        if action:
            stmt = stmt.where(AuditLog.action == action)
        if actor_id:
            stmt = stmt.where(AuditLog.actor_id == actor_id)
        stmt = stmt.offset(offset).limit(min(limit, 500))
        return list((await self.session.execute(stmt)).scalars().all())
