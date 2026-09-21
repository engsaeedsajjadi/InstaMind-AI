"""Audit log + notifications."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import (
    GUID,
    AuditMixin,
    Base,
    TenantModel,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)


class AuditLog(UUIDPrimaryKeyMixin, TimestampMixin, AuditMixin, Base):
    """Append-only record of sensitive operations.

    Deliberately *not* a ``TenantModel``: rows are never soft-deleted and
    ``workspace_id`` may be NULL for platform-level (admin panel) actions.
    """

    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_ws_action", "workspace_id", "action"),
        Index("ix_audit_logs_ws_actor", "workspace_id", "actor_id"),
        Index("ix_audit_logs_ws_created", "workspace_id", "created_at"),
    )

    # NULL workspace_id => platform-level (admin) action.
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=True
    )
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    actor_email: Mapped[str | None] = mapped_column(String(320))
    actor_type: Mapped[str] = mapped_column(String(24), nullable=False, default="USER")
    # USER | AGENT | SYSTEM | WEBHOOK
    action: Mapped[str] = mapped_column(String(96), nullable=False)
    resource_type: Mapped[str | None] = mapped_column(String(64))
    resource_id: Mapped[str | None] = mapped_column(String(64))
    ip_address: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(512))
    outcome: Mapped[str] = mapped_column(String(24), nullable=False, default="SUCCESS")
    # SUCCESS | DENIED | FAILED
    before: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    after: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    # `metadata` is reserved by SQLAlchemy's Declarative API, so the Python
    # attribute is `detail` while the column keeps its documented name.
    detail: Mapped[dict] = mapped_column("metadata", JSON, nullable=False, default=dict)
    trace_id: Mapped[str | None] = mapped_column(String(64), index=True)


class Notification(TenantModel):
    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notifications_ws_user_read", "workspace_id", "user_id", "is_read"),)

    user_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    channel: Mapped[str] = mapped_column(String(24), nullable=False, default="IN_APP")
    # IN_APP | EMAIL | SMS
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="INFO")
    is_read: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivery_error: Mapped[str | None] = mapped_column(Text)
