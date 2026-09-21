"""Workspace / team management endpoints."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Response, status

from app.api.deps import (
    CurrentContext,
    CurrentUser,
    DBSession,
    require,
)
from app.api.schemas import (
    ContextRead,
    InviteRequest,
    MembershipRead,
    RoleCreateRequest,
    RoleRead,
    RoleUpdateRequest,
    WorkspaceCreate,
    WorkspaceRead,
)
from app.core.permissions import Permission
from app.modules.audit.service import AuditService
from app.modules.tenants.models import Role as RoleRow
from app.modules.tenants.service import TenantService

router = APIRouter(prefix="/workspaces", tags=["workspaces"])


@router.get("", response_model=list[WorkspaceRead])
async def list_workspaces(session: DBSession, user: CurrentUser) -> list[WorkspaceRead]:
    service = TenantService(session)
    rows = await service.list_workspaces_for_user(user.id)
    return [WorkspaceRead.model_validate(row) for row in rows]


@router.post("", response_model=WorkspaceRead, status_code=status.HTTP_201_CREATED)
async def create_workspace(
    payload: WorkspaceCreate, session: DBSession, user: CurrentUser
) -> WorkspaceRead:
    service = TenantService(session)
    workspace = await service.create_workspace(
        owner_id=user.id,
        name=payload.name,
        slug=payload.slug,
        locale=payload.default_locale,
        timezone=payload.default_timezone,
        calendar_system=payload.calendar_system,
    )
    audit = AuditService(session)
    await audit.record(
        action="workspace.created",
        workspace_id=workspace.id,
        actor_id=user.id,
        actor_email=user.email,
        resource_type="workspace",
        resource_id=str(workspace.id),
        after={"name": workspace.name, "slug": workspace.slug},
    )
    return WorkspaceRead.model_validate(workspace)


@router.get("/context", response_model=ContextRead)
async def current_context(context: CurrentContext) -> ContextRead:
    return ContextRead(
        workspace_id=context.workspace_id,
        user_id=context.user_id,
        roles=list(context.roles),
        permissions=[permission.value for permission in context.permissions],
        is_superuser=context.is_superuser,
    )


@router.get("/{workspace_id}", response_model=WorkspaceRead)
async def get_workspace(
    workspace_id: UUID,
    session: DBSession,
    context: Annotated[object, Depends(require(Permission.WORKSPACE_READ))],
) -> WorkspaceRead:
    service = TenantService(session)
    workspace = await service.get_workspace(workspace_id)
    return WorkspaceRead.model_validate(workspace)


@router.post("/{workspace_id}/transfer", response_model=WorkspaceRead)
async def transfer_ownership(
    workspace_id: UUID,
    new_owner_id: UUID,
    session: DBSession,
    user: CurrentUser,
    context: Annotated[object, Depends(require(Permission.WORKSPACE_TRANSFER))],
) -> WorkspaceRead:
    service = TenantService(session)
    workspace = await service.transfer_ownership(
        workspace_id=workspace_id, actor_id=user.id, new_owner_id=new_owner_id
    )
    await AuditService(session).record(
        action="workspace.ownership_transferred",
        workspace_id=workspace_id,
        actor_id=user.id,
        resource_type="workspace",
        resource_id=str(workspace_id),
        after={"owner_id": str(new_owner_id)},
    )
    return WorkspaceRead.model_validate(workspace)


# --------------------------------------------------------------------- members
@router.get(
    "/{workspace_id}/members",
    response_model=list[MembershipRead],
    dependencies=[Depends(require(Permission.WORKSPACE_READ))],
)
async def list_members(workspace_id: UUID, session: DBSession) -> list[MembershipRead]:
    service = TenantService(session)
    rows = await service.list_members(workspace_id)
    return [MembershipRead.model_validate(row) for row in rows]


@router.post(
    "/{workspace_id}/invitations",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require(Permission.MEMBER_INVITE))],
)
async def invite(
    workspace_id: UUID,
    payload: InviteRequest,
    session: DBSession,
    user: CurrentUser,
) -> dict[str, str]:
    service = TenantService(session)
    await service.get_workspace(workspace_id)
    _invitation, raw_token = await service.invite_member(
        workspace_id=workspace_id,
        invited_by=user.id,
        email=payload.email,
        role_code=payload.role_code,
    )
    await AuditService(session).record(
        action="member.invited",
        workspace_id=workspace_id,
        actor_id=user.id,
        resource_type="invitation",
        resource_id=payload.email,
        after={"role": payload.role_code},
    )
    # The raw token is returned once; production delivers it by email.
    return {"status": "invited", "email": payload.email, "token": raw_token}


@router.patch(
    "/{workspace_id}/members/{user_id}",
    response_model=MembershipRead,
    dependencies=[Depends(require(Permission.MEMBER_ROLE_UPDATE))],
)
async def update_member_role(
    workspace_id: UUID,
    user_id: UUID,
    payload: RoleUpdateRequest,
    session: DBSession,
    user: CurrentUser,
) -> MembershipRead:
    service = TenantService(session)
    membership = await service.update_member_role(
        workspace_id=workspace_id,
        actor_id=user.id,
        target_user_id=user_id,
        role_code=payload.role_code,
    )
    return MembershipRead.model_validate(membership)


@router.delete(
    "/{workspace_id}/members/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require(Permission.MEMBER_REMOVE))],
)
async def remove_member(
    workspace_id: UUID, user_id: UUID, session: DBSession, user: CurrentUser
) -> Response:
    service = TenantService(session)
    await service.remove_member(workspace_id=workspace_id, actor_id=user.id, target_user_id=user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/invitations/accept", status_code=status.HTTP_200_OK)
async def accept_invitation(
    token: str, session: DBSession, user: CurrentUser
) -> MembershipRead:
    service = TenantService(session)
    membership = await service.accept_invitation(token=token, user_id=user.id)
    return MembershipRead.model_validate(membership)


# ----------------------------------------------------------------------- roles
@router.get(
    "/{workspace_id}/roles",
    response_model=list[RoleRead],
    dependencies=[Depends(require(Permission.WORKSPACE_READ))],
)
async def list_roles(workspace_id: UUID, session: DBSession) -> list[RoleRead]:
    from sqlalchemy import select

    from app.core.permissions import BUILTIN_ROLE_PERMISSIONS

    builtin = [
        RoleRead(
            id=UUID(int=index + 1, version=4),
            workspace_id=None,
            code=role.value,
            name=role.value.title().replace("_", " "),
            description="Built-in role",
            permissions=[permission.value for permission in sorted(permissions)],
            is_builtin=True,
        )
        for index, (role, permissions) in enumerate(BUILTIN_ROLE_PERMISSIONS.items())
    ]
    rows = (
        await session.execute(
            select(RoleRow).where(RoleRow.workspace_id == workspace_id, RoleRow.is_builtin.is_(False))
        )
    ).scalars().all()
    return builtin + [RoleRead.model_validate(row) for row in rows]


@router.post(
    "/{workspace_id}/roles",
    response_model=RoleRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require(Permission.ROLE_MANAGE))],
)
async def create_role(
    workspace_id: UUID, payload: RoleCreateRequest, session: DBSession
) -> RoleRead:
    service = TenantService(session)
    role = await service.create_custom_role(
        workspace_id=workspace_id,
        code=payload.code,
        name=payload.name,
        permissions=payload.permissions,
        description=payload.description,
    )
    return RoleRead.model_validate(role)
