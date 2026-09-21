"""Instagram account connection endpoints.

The OAuth flow is split into two calls because Meta redirects the browser:

``POST   /social-accounts/connect``             → returns ``authorize_url``
``GET    /social-accounts/oauth/{path}/callback`` → Meta redirects here

Only the callback exchanges the code; the browser never sees a token.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Query, Request, Response, status
from fastapi.responses import RedirectResponse

from app.api.deps import DBSession, TenantScopeDep, client_ip, require
from app.api.schemas import (
    ConnectStartRequest,
    ConnectStartResponse,
    DisconnectRequest,
    SocialAccountRead,
)
from app.core.config import settings
from app.core.logging import logger
from app.core.permissions import Permission
from app.modules.instagram.models import SocialAccount
from app.modules.instagram.service import InstagramConnectService
from app.modules.tenants.scope import TenantScope

router = APIRouter(prefix="/social-accounts", tags=["instagram"])

OAUTH_RESULT_PAGE = settings.PUBLIC_MEDIA_BASE_URL.rsplit("/media", 1)[0]


def _service(session, scope: TenantScope) -> InstagramConnectService:
    return InstagramConnectService(session, scope)


@router.get("", response_model=list[SocialAccountRead], dependencies=[Depends(require(Permission.SOCIAL_ACCOUNT_READ))])
async def list_accounts(scope: TenantScopeDep) -> list[SocialAccountRead]:
    rows = await scope.list(SocialAccount, order_by=SocialAccount.created_at.desc())
    return [SocialAccountRead.model_validate(row) for row in rows]


@router.post("/connect", response_model=ConnectStartResponse)
async def start_connect(
    payload: ConnectStartRequest,
    request: Request,
    session: DBSession,
    scope: TenantScopeDep,
    context: Annotated[object, Depends(require(Permission.SOCIAL_ACCOUNT_CONNECT))],
) -> ConnectStartResponse:
    service = _service(session, scope)
    initiation = await service.begin_connect(
        api_path=payload.api_path,
        ip_address=client_ip(request),
        requested_scopes=payload.requested_scopes,
    )
    return ConnectStartResponse(
        authorize_url=initiation.authorize_url,
        state=initiation.state,
        api_path=initiation.api_path,
    )


@router.get("/oauth/{api_path}/callback")
async def oauth_callback(
    api_path: str,
    session: DBSession,
    scope: TenantScopeDep,
    state: str = Query(...),
    code: str | None = Query(default=None),
    error: str | None = Query(default=None),
    error_reason: str | None = Query(default=None),
) -> Response:
    """Meta's redirect target. Deliberately returns an HTML page rather than
    JSON, because the browser is what lands here."""
    service = _service(session, scope)
    try:
        account = await service.complete_connect(
            state=state, code=code or "", error=error, error_reason=error_reason
        )
    except Exception as exc:  # noqa: BLE001 - surfaced to the user's browser
        logger.warning("instagram_oauth_failed", error=str(exc))
        return RedirectResponse(
            url=f"{OAUTH_RESULT_PAGE}/settings/instagram?status=failed&reason={_slug(str(exc))}",
            status_code=status.HTTP_302_FOUND,
        )
    return RedirectResponse(
        url=(
            f"{OAUTH_RESULT_PAGE}/settings/instagram?status=connected"
            f"&username={account.username}"
        ),
        status_code=status.HTTP_302_FOUND,
    )


@router.delete(
    "/{account_id}",
    response_model=SocialAccountRead,
    dependencies=[Depends(require(Permission.SOCIAL_ACCOUNT_DISCONNECT))],
)
async def disconnect(
    account_id: UUID,
    session: DBSession,
    scope: TenantScopeDep,
    payload: DisconnectRequest = Body(default=DisconnectRequest()),
) -> SocialAccountRead:
    service = _service(session, scope)
    account = await service.disconnect(
        account_id, purge_platform_data=(payload.purge_platform_data if payload else True)
    )
    return SocialAccountRead.model_validate(account)


@router.post(
    "/{account_id}/refresh-token",
    response_model=SocialAccountRead,
    dependencies=[Depends(require(Permission.SOCIAL_ACCOUNT_CONNECT))],
)
async def refresh_token(account_id: UUID, session: DBSession, scope: TenantScopeDep) -> SocialAccountRead:
    account = await scope.get_one(SocialAccount, account_id)
    service = _service(session, scope)
    await service.refresh_if_needed(account)
    return SocialAccountRead.model_validate(account)


def _slug(value: str) -> str:
    import re

    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:80]
