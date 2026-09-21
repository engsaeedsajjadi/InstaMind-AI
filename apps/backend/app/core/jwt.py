"""JWT issuance and validation.

Two token types, deliberately different lifetimes and semantics:

``access``  — short lived (15 min default), presented on every API call,
              stateless verification.
``refresh`` — long lived, opaque-id based, **rotated on every use**. A reused
              (already-rotated) refresh token is treated as theft and revokes
              the whole family.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

import jwt
from pydantic import BaseModel

from app.core.config import settings
from app.core.security import constant_time_equals

TokenType = Literal["access", "refresh"]


class TokenError(Exception):
    """Any problem decoding/validating a JWT."""


class TokenPayload(BaseModel):
    sub: str
    typ: TokenType
    jti: str
    iat: int
    exp: int
    iss: str
    aud: str
    sid: str | None = None          # session id (refresh tokens only)
    family: str | None = None       # rotation family (refresh tokens only)
    workspace_id: str | None = None
    roles: list[str] = []
    scopes: list[str] = []
    is_superuser: bool = False
    mfa_verified: bool = False


ISSUER = "instamind-ai"
AUDIENCE = "instamind-api"


def _now() -> datetime:
    return datetime.now(UTC)


def create_access_token(
    *,
    user_id: UUID,
    session_id: UUID | None = None,
    workspace_id: UUID | None = None,
    roles: list[str] | None = None,
    scopes: list[str] | None = None,
    is_superuser: bool = False,
    mfa_verified: bool = False,
    jti: str | None = None,
    ttl_minutes: int | None = None,
) -> tuple[str, str, datetime]:
    """Return ``(token, jti, expires_at)``."""
    from app.core.security import generate_nonce  # local import avoids cycles

    jti = jti or generate_nonce(12)
    ttl = timedelta(minutes=ttl_minutes or settings.ACCESS_TOKEN_TTL_MINUTES)
    expires_at = _now() + ttl
    claims: dict[str, Any] = {
        "sub": str(user_id),
        "typ": "access",
        "jti": jti,
        "iat": int(_now().timestamp()),
        "exp": int(expires_at.timestamp()),
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sid": str(session_id) if session_id else None,
        "workspace_id": str(workspace_id) if workspace_id else None,
        "roles": roles or [],
        "scopes": scopes or [],
        "is_superuser": is_superuser,
        "mfa_verified": mfa_verified,
    }
    token = jwt.encode(claims, settings.SECRET_KEY.get_secret_value(), algorithm=settings.JWT_ALGORITHM)
    return token, jti, expires_at


def create_refresh_token(
    *,
    user_id: UUID,
    session_id: UUID,
    family_id: str,
    token_id: str,
    expires_at: datetime,
) -> str:
    claims = {
        "sub": str(user_id),
        "typ": "refresh",
        "jti": token_id,
        "iat": int(_now().timestamp()),
        "exp": int(expires_at.timestamp()),
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sid": str(session_id),
        "family": family_id,
        "is_superuser": False,
        "mfa_verified": False,
        "roles": [],
        "scopes": [],
        "workspace_id": None,
    }
    return jwt.encode(claims, settings.SECRET_KEY.get_secret_value(), algorithm=settings.JWT_ALGORITHM)


def decode_token(token: str, *, expected_type: TokenType | None = None) -> TokenPayload:
    """Decode and validate a JWT. Raises :class:`TokenError` on any failure."""
    try:
        claims = jwt.decode(
            token,
            settings.SECRET_KEY.get_secret_value(),
            algorithms=[settings.JWT_ALGORITHM],
            issuer=ISSUER,
            audience=AUDIENCE,
            options={"require": ["exp", "iat", "sub", "jti", "typ"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("token_expired") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenError("token_invalid") from exc

    try:
        payload = TokenPayload.model_validate(claims)
    except Exception as exc:  # pragma: no cover - defensive
        raise TokenError("token_claims_invalid") from exc

    if expected_type is not None and payload.typ != expected_type:
        raise TokenError("token_type_mismatch")
    if not constant_time_equals(payload.aud, AUDIENCE):  # pragma: no cover
        raise TokenError("token_audience_invalid")
    return payload


@dataclass(frozen=True)
class AccessTokenClaims:
    user_id: UUID
    session_id: UUID | None
    workspace_id: UUID | None
    roles: tuple[str, ...]
    scopes: tuple[str, ...]
    is_superuser: bool
    mfa_verified: bool
    jti: str
    expires_at: datetime


def claims_from_payload(payload: TokenPayload) -> AccessTokenClaims:
    return AccessTokenClaims(
        user_id=UUID(payload.sub),
        session_id=UUID(payload.sid) if payload.sid else None,
        workspace_id=UUID(payload.workspace_id) if payload.workspace_id else None,
        roles=tuple(payload.roles),
        scopes=tuple(payload.scopes),
        is_superuser=payload.is_superuser,
        mfa_verified=payload.mfa_verified,
        jti=payload.jti,
        expires_at=datetime.fromtimestamp(payload.exp, UTC),
    )


def tokens_from_refresh_ttl(days: int | None = None) -> timedelta:
    return timedelta(days=days if days is not None else settings.REFRESH_TOKEN_TTL_DAYS)


def token_ttl_remaining(expires_at: datetime) -> timedelta:
    return expires_at - _now()
