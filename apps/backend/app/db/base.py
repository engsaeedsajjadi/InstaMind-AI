"""Declarative base + shared mixins.

Conventions enforced here (see docs/database-schema.md):

* Primary keys are UUIDs generated server-side (``uuid4``) — no sequential ids
  leaking tenant volume, and safe for future sharding.
* Every timestamp is timezone-aware UTC (``DateTime(timezone=True)``).
* Rows that must never disappear get ``deleted_at`` (soft delete); the default
  repository helpers filter them out.
* Anything owned by a workspace carries ``workspace_id`` via
  :class:`TenantScoped` and gets a composite ``(workspace_id, created_at)``
  index because that is the dominant query shape.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column
from sqlalchemy.types import CHAR, TypeDecorator, TypeEngine


def utcnow() -> datetime:
    return datetime.now(UTC)


def as_utc(value: datetime | None) -> datetime | None:
    """Normalise a datetime read from the database to an aware UTC value.

    PostgreSQL ``timestamptz`` round-trips as aware, but SQLite (used in tests
    and local dev) returns naive datetimes. Comparing a naive DB value with
    ``datetime.now(UTC)`` raises ``TypeError``, so every comparison goes
    through this helper.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def is_past(value: datetime | None) -> bool:
    """True when ``value`` is set and already in the past (UTC)."""
    aware = as_utc(value)
    return aware is not None and aware <= datetime.now(UTC)


class GUID(TypeDecorator):
    """Platform-agnostic UUID: native ``uuid`` on PostgreSQL, ``CHAR(36)`` elsewhere.

    Lets the same models run on Postgres in production and SQLite in tests.
    """

    impl = CHAR
    cache_ok = True

    def load_dialect_impl(self, dialect: Any) -> TypeEngine:  # noqa: ANN401
        if dialect.name == "postgresql":
            return dialect.type_descriptor(PG_UUID(as_uuid=True))
        return dialect.type_descriptor(CHAR(36))

    def process_bind_param(self, value: Any, dialect: Any) -> Any:  # noqa: ANN401
        if value is None:
            return value
        if dialect.name == "postgresql":
            return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
        return str(value)

    def process_result_value(self, value: Any, dialect: Any) -> Any:  # noqa: ANN401
        if value is None:
            return value
        if isinstance(value, uuid.UUID):
            return value
        return uuid.UUID(str(value))


class Base(DeclarativeBase):
    """Root declarative base."""

    metadata_naming_convention = {
        "ix": "ix_%(column_0_label)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        pk = getattr(self, "id", None)
        return f"<{self.__class__.__name__} id={pk}>"


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
        nullable=False,
    )


class UUIDPrimaryKeyMixin:
    id: Mapped[uuid.UUID] = mapped_column(
        GUID(), primary_key=True, default=uuid.uuid4, nullable=False
    )


class SoftDeleteMixin:
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None


class TenantScoped:
    """Marks a row as belonging to exactly one workspace.

    ``workspace_id`` is NOT NULL: an orphaned tenant row is a data-integrity bug,
    not a valid state.
    """

    @declared_attr
    def workspace_id(cls) -> Mapped[uuid.UUID]:  # noqa: N805
        return mapped_column(
            GUID(),
            ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )

    @declared_attr
    def __table_args__(cls) -> Any:  # noqa: N805
        return (Index(f"ix_{cls.__tablename__}_workspace_created", "workspace_id", "created_at"),)


class AuditMixin:
    """Who created / last modified the row (application-level attribution)."""

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class Model(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Standard base for most tables."""

    __abstract__ = True


class TenantModel(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, AuditMixin, TenantScoped, Base):
    """Standard base for workspace-owned tables."""

    __abstract__ = True
