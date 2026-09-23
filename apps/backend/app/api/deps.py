"""Request dependencies: authentication, workspace resolution, authorization.

The chain is always the same:

1. extract + verify the bearer token,
2. load the user and confirm the session is still valid,
3. resolve the workspace from the ``X-Workspace-Id`` header (never from the
   body — a body value could be swapped by a confused client),
4. build a :class:`TenantContext` from the membership,
5. enforce the required permission.

Every router gets ``TenantScope`` from here, which is what makes tenant
isolation structural rather than a convention.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Depends, Header, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import AuthenticationError, PermissionDenied
from app.core.jwt import (
    AccessTokenClaims,
    TokenError,
    claims_from_payload,
    decode_token,
)
from app.core.logging import set_request_context
from app.core.permissions import Permission
from app.db.session import get_db_session
from app.modules.identity.models import User, UserSession
from app.modules.identity.service import IdentityService
from app.modules.tenants.scope import TenantContext, TenantScope
from app.modules.tenants.service import TenantService

bearer_scheme = HTTPBearer(auto_error=False, bearerFormat="JWT")

DBSession = Annotated[AsyncSession, Depends(get_db_session)]


def client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


async def get_current_claims(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> AccessTokenClaims:
    # Prefer an explicit Bearer token for API/CLI clients. Browser sessions use
    # an HttpOnly cookie so JWT material is never exposed to JavaScript.
    token = credentials.credentials if credentials and credentials.credentials else request.cookies.get(
        "__Host-instamind_access" if settings.is_prod else "instamind_access"
    )
    if not token:
        raise AuthenticationError()
    try:
        payload = decode_token(token, expected_type="access")
    except TokenError as exc:
        raise AuthenticationError(f"Authentication failed: {exc}") from exc
    return claims_from_payload(payload)


ClaimsDep = Annotated[AccessTokenClaims, Depends(get_current_claims)]


async def get_current_user(
    claims: ClaimsDep,
    session: DBSession,
) -> User:
    identity = IdentityService(session)
    user = await identity.get_user(claims.user_id)
    if user is None or not user.is_active:
        raise AuthenticationError("Account is not active.")
    if user.is_superuser:
        return user
    if claims.session_id is not None:
        row = await session.get(UserSession, claims.session_id)
        if row is None or not row.is_active or row.revoked_at is not None:
            raise AuthenticationError("Session has been revoked.")
    if user.mfa_enabled and user.mfa_required and not claims.mfa_verified:
        raise AuthenticationError("Two-factor verification is required.")
    set_request_context(user_id=str(user.id))
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


async def get_current_context(
    user: CurrentUser,
    session: DBSession,
    x_workspace_id: Annotated[str | None, Header(alias="X-Workspace-Id")] = None,
) -> TenantContext:
    if x_workspace_id:
        try:
            workspace_id = uuid.UUID(x_workspace_id)
        except ValueError as exc:
            raise AuthenticationError("X-Workspace-Id is not a valid UUID.") from exc
    else:
        tenants = TenantService(session)
        workspaces = await tenants.list_workspaces_for_user(user.id)
        if not workspaces:
            raise PermissionDenied("You are not a member of any workspace.")
        workspace_id = workspaces[0].id

    tenants = TenantService(session)
    context = await tenants.build_context(
        workspace_id=workspace_id, user_id=user.id, is_superuser=user.is_superuser
    )
    set_request_context(workspace_id=str(context.workspace_id))
    return context


CurrentContext = Annotated[TenantContext, Depends(get_current_context)]


async def get_tenant_scope(
    session: DBSession,
    context: CurrentContext,
) -> TenantScope:
    return TenantScope(session, context)


TenantScopeDep = Annotated[TenantScope, Depends(get_tenant_scope)]


def require(permission: Permission):
    """Dependency factory enforcing one permission."""

    async def _checker(context: CurrentContext) -> TenantContext:
        if not context.can(permission):
            raise PermissionDenied(
                f"Missing permission: {permission.value}",
                context={"required_permission": permission.value},
            )
        return context

    return _checker


def require_any(*permissions: Permission):
    async def _checker(context: CurrentContext) -> TenantContext:
        if not any(context.can(permission) for permission in permissions):
            raise PermissionDenied(
                "Missing permission: " + " or ".join(p.value for p in permissions),
                context={"required_permissions": [p.value for p in permissions]},
            )
        return context

    return _checker


def require_superuser(user: CurrentUser) -> User:
    if not user.is_superuser:
        raise PermissionDenied("Administrator access required.")
    return user


SuperUser = Annotated[User, Depends(require_superuser)]


def rate_limit_dependency(scope: str, limit: int, window: int = 60):
    from app.core.rate_limit import enforce_rate_limit

    async def _limiter(request: Request, user: CurrentUser) -> None:
        await enforce_rate_limit(scope, str(user.id), limit=limit, window_seconds=window)

    return _limiter


__all__ = [
    "CurrentUser",
    "CurrentContext",
    "DBSession",
    "SuperUser",
    "TenantScopeDep",
    "bearer_scheme",
    "client_ip",
    "require",
    "require_any",
    "require_superuser",
    "settings",
]
