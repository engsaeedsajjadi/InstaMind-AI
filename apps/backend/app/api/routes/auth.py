"""Authentication endpoints.

Rate limiting is applied per IP on every credential-handling route; combined
with account lockout and refresh-token rotation this is the credential-stuffing
defence described in SECURITY.md.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status

from app.api.deps import DBSession, client_ip, get_current_claims, get_current_user
from app.api.schemas import (
    LoginRequest,
    PasswordChangeRequest,
    PasswordResetConfirm,
    PasswordResetRequest,
    RefreshRequest,
    RegisterRequest,
    SessionRead,
    TokenResponse,
    UserRead,
)
from app.core.config import settings
from app.core.exceptions import NotFoundError
from app.core.rate_limit import enforce_rate_limit
from app.modules.identity.models import User
from app.modules.identity.service import IdentityService

router = APIRouter(prefix="/auth", tags=["auth"])

AuthedUser = Annotated[User, Depends(get_current_user)]
AccessTokenClaims = Annotated[object, Depends(get_current_claims)]


async def _limit_auth(request: Request, bucket: str) -> None:
    await enforce_rate_limit(
        f"auth:{bucket}",
        client_ip(request) or "unknown",
        limit=settings.RATE_LIMIT_AUTH_PER_MINUTE,
        window_seconds=60,
        error_detail="Too many authentication attempts. Try again in a minute.",
    )


@router.post("/register", response_model=UserRead, status_code=status.HTTP_201_CREATED)
async def register(payload: RegisterRequest, request: Request, session: DBSession) -> UserRead:
    await _limit_auth(request, "register")
    identity = IdentityService(session)
    user = await identity.register(
        email=payload.email,
        password=payload.password,
        full_name=payload.full_name,
        locale=payload.locale,
        timezone=payload.timezone,
    )
    return UserRead.model_validate(user)


@router.post("/login", response_model=TokenResponse)
async def login(payload: LoginRequest, request: Request, session: DBSession) -> TokenResponse:
    await _limit_auth(request, "login")
    identity = IdentityService(session)
    result = await identity.authenticate(
        email=payload.email,
        password=payload.password,
        ip_address=client_ip(request),
        user_agent=request.headers.get("user-agent"),
        device_label=payload.device_label,
    )
    return TokenResponse(
        access_token=result.tokens.access_token,
        refresh_token=result.tokens.refresh_token,
        expires_in=result.tokens.expires_in,
        session_id=result.tokens.session_id,
    )


@router.post("/refresh", response_model=TokenResponse)
async def refresh(payload: RefreshRequest, request: Request, session: DBSession) -> TokenResponse:
    await _limit_auth(request, "refresh")
    identity = IdentityService(session)
    tokens = await identity.rotate_refresh_token(payload.refresh_token)
    return TokenResponse(
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        expires_in=tokens.expires_in,
        session_id=tokens.session_id,
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(session: DBSession, user: AuthedUser, claims: AccessTokenClaims) -> Response:
    if getattr(claims, "session_id", None) is None:  # pragma: no cover - defensive
        from app.core.exceptions import AuthenticationError

        raise AuthenticationError("Token has no session binding.")
    identity = IdentityService(session)
    await identity.revoke_session(claims.session_id, reason="user_logout")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/logout-all", status_code=status.HTTP_204_NO_CONTENT)
async def logout_all(session: DBSession, user: AuthedUser) -> Response:
    identity = IdentityService(session)
    await identity.revoke_all_sessions(user.id, reason="user_logout_all")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me", response_model=UserRead)
async def me(user: AuthedUser) -> UserRead:
    return UserRead.model_validate(user)


@router.post("/password/change", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    payload: PasswordChangeRequest, request: Request, session: DBSession, user: AuthedUser
) -> Response:
    await _limit_auth(request, "password-change")
    identity = IdentityService(session)
    await identity.change_password(
        user, current_password=payload.current_password, new_password=payload.new_password
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/password/reset-request", status_code=status.HTTP_202_ACCEPTED)
async def password_reset_request(
    payload: PasswordResetRequest, request: Request, session: DBSession
) -> dict[str, str]:
    """Always 202: the response must not reveal whether the email exists.

    Outside production, with the console mail backend, the raw token is echoed
    back so QA can complete the flow without an SMTP server.
    """
    await _limit_auth(request, "password-reset")
    identity = IdentityService(session)
    token = await identity.create_password_reset_token(
        payload.email, ip_address=client_ip(request)
    )
    if token and not settings.is_prod and settings.EMAIL_BACKEND == "console":
        return {"status": "accepted", "dev_token": token}
    return {"status": "accepted"}


@router.post("/password/reset-confirm", status_code=status.HTTP_204_NO_CONTENT)
async def password_reset_confirm(
    payload: PasswordResetConfirm, request: Request, session: DBSession
) -> Response:
    await _limit_auth(request, "password-reset-confirm")
    identity = IdentityService(session)
    await identity.consume_password_reset_token(payload.token, payload.new_password)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/sessions", response_model=list[SessionRead])
async def list_sessions(session: DBSession, user: AuthedUser) -> list[SessionRead]:
    identity = IdentityService(session)
    rows = await identity.list_sessions(user.id)
    return [SessionRead.model_validate(row) for row in rows]


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_session(session_id: uuid.UUID, session: DBSession, user: AuthedUser) -> Response:
    identity = IdentityService(session)
    rows = await identity.list_sessions(user.id)
    if not any(row.id == session_id for row in rows):
        raise NotFoundError("Session not found.")
    await identity.revoke_session(session_id, reason="revoked_by_user")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
