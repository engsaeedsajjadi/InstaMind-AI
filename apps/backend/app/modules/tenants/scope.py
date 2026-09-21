"""Multi-tenancy primitives.

The single most important rule of this codebase: **every workspace-owned query
goes through :class:`TenantScope`**, which injects ``workspace_id`` into the
WHERE clause and re-verifies the ownership of whatever comes back. A bug that
forgets the filter therefore still cannot leak another tenant's row — it raises
:class:`TenantIsolationViolation` instead.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, TypeVar

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError, TenantIsolationViolation
from app.core.permissions import Permission

ModelT = TypeVar("ModelT")


@dataclass(frozen=True)
class TenantContext:
    """Immutable description of *who* is acting and *where*."""

    user_id: uuid.UUID
    workspace_id: uuid.UUID
    roles: tuple[str, ...] = ()
    permissions: frozenset[Permission] = field(default_factory=frozenset)
    is_superuser: bool = False
    membership_id: uuid.UUID | None = None

    def can(self, permission: Permission) -> bool:
        return self.is_superuser or permission in self.permissions


class TenantScope:
    """Query helper bound to one workspace.

    Usage::

        scope = TenantScope(session, context)
        content = await scope.get_one(Content, content_id)      # raises if foreign
        rows = await scope.list(Content, Content.status == "DRAFT")
        await scope.delete(content)                              # soft delete
    """

    def __init__(self, session: AsyncSession, context: TenantContext) -> None:
        self.session = session
        self.context = context

    # ------------------------------------------------------------- internals
    @property
    def workspace_id(self) -> uuid.UUID:
        return self.context.workspace_id

    def scoped(self, stmt: Select, model: type[ModelT], *, include_deleted: bool = False) -> Select:
        """Attach the tenant predicate to any statement over ``model``."""
        stmt = stmt.where(model.workspace_id == self.workspace_id)  # type: ignore[attr-defined]
        if not include_deleted and hasattr(model, "deleted_at"):
            stmt = stmt.where(model.deleted_at.is_(None))  # type: ignore[attr-defined]
        return stmt

    def _verify(self, obj: ModelT) -> ModelT:
        owner = getattr(obj, "workspace_id", None)
        if owner != self.workspace_id:
            # Never reveal that the row exists.
            raise TenantIsolationViolation()
        return obj

    # ------------------------------------------------------------------ reads
    async def get_one(
        self,
        model: type[ModelT],
        object_id: uuid.UUID | str,
        *,
        include_deleted: bool = False,
    ) -> ModelT:
        stmt = select(model).where(model.id == _as_uuid(object_id))  # type: ignore[attr-defined]
        obj = (await self.session.execute(self.scoped(stmt, model, include_deleted=include_deleted))).scalar_one_or_none()
        if obj is None:
            raise NotFoundError(f"{model.__name__} not found.")
        return self._verify(obj)

    async def find(
        self,
        model: type[ModelT],
        object_id: uuid.UUID | str,
        *,
        include_deleted: bool = False,
    ) -> ModelT | None:
        stmt = select(model).where(model.id == _as_uuid(object_id))  # type: ignore[attr-defined]
        obj = (await self.session.execute(self.scoped(stmt, model, include_deleted=include_deleted))).scalar_one_or_none()
        return self._verify(obj) if obj is not None else None

    async def list(
        self,
        model: type[ModelT],
        *conditions: Any,
        include_deleted: bool = False,
        limit: int | None = None,
        offset: int | None = None,
        order_by: Any = None,
    ) -> list[ModelT]:
        stmt = self.scoped(select(model), model, include_deleted=include_deleted)
        if conditions:
            stmt = stmt.where(*conditions)
        if order_by is not None:
            stmt = stmt.order_by(order_by)
        if offset:
            stmt = stmt.offset(offset)
        if limit:
            stmt = stmt.limit(limit)
        rows = list((await self.session.execute(stmt)).scalars().all())
        for row in rows:
            self._verify(row)
        return rows

    async def count(self, model: type[ModelT], *conditions: Any, include_deleted: bool = False) -> int:
        from sqlalchemy import func

        stmt = self.scoped(select(func.count()).select_from(model), model, include_deleted=include_deleted)
        if conditions:
            stmt = stmt.where(*conditions)
        return int((await self.session.execute(stmt)).scalar_one())

    # ----------------------------------------------------------------- writes
    def attach(self, obj: ModelT) -> ModelT:
        """Stamp the tenant (and actor) onto a new row."""
        obj.workspace_id = self.workspace_id
        if hasattr(obj, "created_by") and getattr(obj, "created_by", None) is None:
            obj.created_by = self.context.user_id
        obj.updated_by = self.context.user_id
        return obj

    def touch(self, obj: ModelT) -> ModelT:
        self._verify(obj)
        obj.updated_by = self.context.user_id
        return obj

    async def soft_delete(self, obj: ModelT) -> None:
        from datetime import UTC, datetime

        self._verify(obj)
        if not hasattr(obj, "deleted_at"):
            raise TypeError(f"{type(obj).__name__} does not support soft delete")
        obj.deleted_at = datetime.now(UTC)
        obj.updated_by = self.context.user_id
        await self.session.flush()

    async def flush(self) -> None:
        await self.session.flush()


def _as_uuid(value: uuid.UUID | str) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def assert_memberships(rows: Sequence[Any], workspace_id: uuid.UUID) -> None:
    """Bulk guard used by raw/legacy queries."""
    for row in rows:
        if getattr(row, "workspace_id", workspace_id) != workspace_id:
            raise TenantIsolationViolation()
