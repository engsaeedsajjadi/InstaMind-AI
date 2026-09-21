"""Workspace / membership service."""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import security
from app.core.exceptions import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from app.core.logging import logger
from app.core.permissions import Permission, Role, permissions_for_roles
from app.db.base import is_past
from app.modules.tenants.models import (
    Role as RoleRow,
)
from app.modules.tenants.models import (
    Workspace,
    WorkspaceInvitation,
    WorkspaceMembership,
)
from app.modules.tenants.scope import TenantContext

SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])$")


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def slugify(value: str, fallback: str = "workspace") -> str:
    base = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return base[:80] or fallback


@dataclass(frozen=True)
class MembershipView:
    membership: WorkspaceMembership
    workspace: Workspace
    role: Role | str
    permissions: frozenset[Permission]


class TenantService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # -------------------------------------------------------------- workspace
    async def create_workspace(
        self,
        *,
        owner_id: uuid.UUID,
        name: str,
        slug: str | None = None,
        locale: str = "fa-IR",
        timezone: str = "Asia/Tehran",
        calendar_system: str = "jalali",
    ) -> Workspace:
        name = name.strip()
        if not name:
            raise ValidationError("Workspace name is required.")
        candidate = slugify(slug or name)
        if not SLUG_RE.match(candidate):
            candidate = f"{candidate}-{uuid.uuid4().hex[:6]}"
        if await self._slug_exists(candidate):
            candidate = f"{candidate}-{uuid.uuid4().hex[:6]}"

        workspace = Workspace(
            name=name,
            slug=candidate,
            owner_id=owner_id,
            default_locale=locale,
            default_timezone=timezone,
            calendar_system=calendar_system,
        )
        self.session.add(workspace)
        await self.session.flush()

        self.session.add(
            WorkspaceMembership(
                workspace_id=workspace.id,
                user_id=owner_id,
                role_code=Role.OWNER.value,
                status="ACTIVE",
                joined_at=datetime.now(UTC),
            )
        )
        await self.session.flush()
        logger.info("workspace_created", workspace_id=str(workspace.id), owner=str(owner_id))
        return workspace

    async def _slug_exists(self, slug: str) -> bool:
        stmt = select(func.count()).select_from(Workspace).where(Workspace.slug == slug)
        return bool((await self.session.execute(stmt)).scalar_one())

    async def get_workspace(self, workspace_id: uuid.UUID) -> Workspace:
        workspace = await self.session.get(Workspace, workspace_id)
        if workspace is None or workspace.deleted_at is not None or not workspace.is_active:
            raise NotFoundError("Workspace not found.")
        return workspace

    async def list_workspaces_for_user(self, user_id: uuid.UUID) -> list[Workspace]:
        stmt = (
            select(Workspace)
            .join(WorkspaceMembership, WorkspaceMembership.workspace_id == Workspace.id)
            .where(
                WorkspaceMembership.user_id == user_id,
                WorkspaceMembership.status == "ACTIVE",
                Workspace.deleted_at.is_(None),
            )
            .order_by(Workspace.created_at.desc())
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def transfer_ownership(
        self,
        *,
        workspace_id: uuid.UUID,
        actor_id: uuid.UUID,
        new_owner_id: uuid.UUID,
    ) -> Workspace:
        workspace = await self.get_workspace(workspace_id)
        if workspace.owner_id != actor_id:
            raise PermissionDenied("Only the workspace owner can transfer ownership.")
        if new_owner_id == actor_id:
            raise ValidationError("New owner must be a different user.")
        membership = await self.get_membership(workspace_id, new_owner_id)
        if membership is None or membership.status != "ACTIVE":
            raise ValidationError("New owner must be an active member of the workspace.")

        previous_owner = workspace.owner_id
        workspace.owner_id = new_owner_id
        membership.role_code = Role.OWNER.value

        # The former owner keeps access but drops to ADMIN.
        old_membership = await self.get_membership(workspace_id, actor_id)
        if old_membership is not None:
            old_membership.role_code = Role.ADMIN.value
        await self.session.flush()
        logger.info(
            "workspace_ownership_transferred",
            workspace_id=str(workspace_id),
            from_user=str(previous_owner),
            to_user=str(new_owner_id),
        )
        return workspace

    # ------------------------------------------------------------ memberships
    async def get_membership(
        self, workspace_id: uuid.UUID, user_id: uuid.UUID
    ) -> WorkspaceMembership | None:
        stmt = select(WorkspaceMembership).where(
            WorkspaceMembership.workspace_id == workspace_id,
            WorkspaceMembership.user_id == user_id,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def require_membership(
        self, workspace_id: uuid.UUID, user_id: uuid.UUID
    ) -> WorkspaceMembership:
        membership = await self.get_membership(workspace_id, user_id)
        if membership is None or membership.status != "ACTIVE":
            raise AuthorizationError("You are not a member of this workspace.")
        return membership

    async def resolve_permissions(
        self, membership: WorkspaceMembership
    ) -> frozenset[Permission]:
        """Built-in role permissions, plus any custom role attached."""
        permissions = set(permissions_for_roles([membership.role_code]))
        if membership.custom_role_id is not None:
            custom = await self.session.get(RoleRow, membership.custom_role_id)
            if custom is not None:
                for code in custom.permissions:
                    try:
                        permissions.add(Permission(code))
                    except ValueError:
                        continue
        return frozenset(permissions)

    async def build_context(
        self, *, workspace_id: uuid.UUID, user_id: uuid.UUID, is_superuser: bool = False
    ) -> TenantContext:
        workspace = await self.get_workspace(workspace_id)
        if is_superuser:
            # Platform admin: full rights, but the tenant id is still explicit.
            return TenantContext(
                user_id=user_id,
                workspace_id=workspace.id,
                roles=("SUPERUSER",),
                permissions=frozenset(Permission),
                is_superuser=True,
            )
        membership = await self.require_membership(workspace.id, user_id)
        permissions = await self.resolve_permissions(membership)
        return TenantContext(
            user_id=user_id,
            workspace_id=workspace.id,
            roles=(membership.role_code,),
            permissions=permissions,
            is_superuser=False,
            membership_id=membership.id,
        )

    async def list_members(self, workspace_id: uuid.UUID) -> list[WorkspaceMembership]:
        stmt = (
            select(WorkspaceMembership)
            .where(WorkspaceMembership.workspace_id == workspace_id)
            .order_by(WorkspaceMembership.created_at.asc())
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def update_member_role(
        self,
        *,
        workspace_id: uuid.UUID,
        actor_id: uuid.UUID,
        target_user_id: uuid.UUID,
        role_code: str,
    ) -> WorkspaceMembership:
        try:
            Role(role_code)
        except ValueError as exc:
            raise ValidationError(f"Unknown role: {role_code}") from exc

        workspace = await self.get_workspace(workspace_id)
        membership = await self.get_membership(workspace_id, target_user_id)
        if membership is None:
            raise NotFoundError("Member not found.")
        if workspace.owner_id == target_user_id and role_code != Role.OWNER.value:
            raise ValidationError("Transfer ownership before changing the owner's role.")
        membership.role_code = role_code
        membership.custom_role_id = None
        await self.session.flush()
        logger.info(
            "member_role_changed",
            workspace_id=str(workspace_id),
            actor=str(actor_id),
            target=str(target_user_id),
            role=role_code,
        )
        return membership

    async def remove_member(
        self, *, workspace_id: uuid.UUID, actor_id: uuid.UUID, target_user_id: uuid.UUID
    ) -> None:
        workspace = await self.get_workspace(workspace_id)
        if workspace.owner_id == target_user_id:
            raise ValidationError("The workspace owner cannot be removed. Transfer ownership first.")
        membership = await self.get_membership(workspace_id, target_user_id)
        if membership is None:
            raise NotFoundError("Member not found.")
        membership.status = "REMOVED"
        await self.session.flush()

    # ------------------------------------------------------------ invitations
    async def invite_member(
        self,
        *,
        workspace_id: uuid.UUID,
        invited_by: uuid.UUID,
        email: str,
        role_code: str = Role.VIEWER.value,
        ttl_hours: int = 72,
    ) -> tuple[WorkspaceInvitation, str]:
        try:
            Role(role_code)
        except ValueError as exc:
            raise ValidationError(f"Unknown role: {role_code}") from exc
        normalized = email.strip().lower()
        if "@" not in normalized:
            raise ValidationError("Invalid email address.")
        existing = (
            await self.session.execute(
                select(WorkspaceInvitation).where(
                    WorkspaceInvitation.workspace_id == workspace_id,
                    WorkspaceInvitation.email_normalized == normalized,
                    WorkspaceInvitation.accepted_at.is_(None),
                    WorkspaceInvitation.revoked_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if existing is not None and existing.expires_at > datetime.now(UTC):
            raise ConflictError("An invitation for this email is already pending.")

        raw_token = security.generate_token_id()
        invitation = WorkspaceInvitation(
            workspace_id=workspace_id,
            email=email.strip(),
            email_normalized=normalized,
            role_code=role_code,
            token_hash=_hash_token(raw_token),
            invited_by=invited_by,
            expires_at=datetime.now(UTC) + timedelta(hours=ttl_hours),
        )
        self.session.add(invitation)
        await self.session.flush()
        return invitation, raw_token

    async def accept_invitation(self, *, token: str, user_id: uuid.UUID) -> WorkspaceMembership:
        invitation = (
            await self.session.execute(
                select(WorkspaceInvitation).where(
                    WorkspaceInvitation.token_hash == _hash_token(token)
                )
            )
        ).scalar_one_or_none()
        if invitation is None:
            raise NotFoundError("Invitation is invalid or has expired.")
        existing = await self.get_membership(invitation.workspace_id, user_id)
        if invitation.accepted_at is not None:
            # Idempotent: re-accepting an invitation you already used returns
            # the resulting membership instead of erroring.
            if existing is not None and existing.status == "ACTIVE":
                return existing
            raise NotFoundError("Invitation has already been used.")
        if invitation.revoked_at is not None or is_past(invitation.expires_at):
            raise NotFoundError("Invitation is invalid or has expired.")
        if existing is not None and existing.status == "ACTIVE":
            invitation.accepted_at = datetime.now(UTC)
            await self.session.flush()
            return existing
        if existing is not None:
            existing.status = "ACTIVE"
            existing.role_code = invitation.role_code
            existing.joined_at = datetime.now(UTC)
            membership = existing
        else:
            membership = WorkspaceMembership(
                workspace_id=invitation.workspace_id,
                user_id=user_id,
                role_code=invitation.role_code,
                status="ACTIVE",
                invited_by=invitation.invited_by,
                joined_at=datetime.now(UTC),
            )
            self.session.add(membership)
        invitation.accepted_at = datetime.now(UTC)
        await self.session.flush()
        return membership

    # ----------------------------------------------------------- custom roles
    async def create_custom_role(
        self,
        *,
        workspace_id: uuid.UUID,
        code: str,
        name: str,
        permissions: list[str],
        description: str = "",
    ) -> RoleRow:
        code = code.strip().upper()
        if not re.match(r"^[A-Z][A-Z0-9_]{1,62}$", code):
            raise ValidationError("Role code must match ^[A-Z][A-Z0-9_]{1,62}$.")
        resolved: list[str] = []
        for item in permissions:
            try:
                resolved.append(Permission(item).value)
            except ValueError as exc:
                raise ValidationError(f"Unknown permission: {item}") from exc
        if not resolved:
            raise ValidationError("A role must grant at least one permission.")
        role = RoleRow(
            workspace_id=workspace_id,
            code=code,
            name=name.strip() or code,
            description=description,
            permissions=resolved,
            is_builtin=False,
        )
        self.session.add(role)
        await self.session.flush()
        return role
