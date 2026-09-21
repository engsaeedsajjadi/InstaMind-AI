"""Identity service: registration, authentication, sessions, token rotation.

Security properties implemented here (verified by tests):

* Argon2id password hashing; transparent rehash when parameters change.
* Account lockout after repeated failures (credential-stuffing resistance).
* Refresh-token rotation with **reuse detection**: presenting a token that was
  already rotated revokes its entire family and logs a security event.
* Session enumeration so a user can revoke a single device or all of them.
* Every login/lockout/revocation writes a ``SecurityEvent`` row.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import security
from app.core.config import settings
from app.core.exceptions import (
    AuthenticationError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from app.core.jwt import TokenPayload, create_access_token, create_refresh_token, decode_token
from app.core.logging import logger
from app.db.base import as_utc, is_past
from app.modules.identity.models import (
    PasswordResetToken,
    RefreshToken,
    SecurityEvent,
    User,
    UserSession,
)

MAX_FAILED_LOGINS = 5
LOCKOUT_MINUTES = 15


def _hash_token(token_id: str) -> str:
    return hashlib.sha256(token_id.encode()).hexdigest()


def normalize_email(email: str) -> str:
    return email.strip().lower()


@dataclass(frozen=True)
class AuthTokens:
    access_token: str
    refresh_token: str
    token_type: str = "Bearer"
    expires_in: int = 0
    session_id: uuid.UUID | None = None


@dataclass(frozen=True)
class AuthResult:
    user: User
    session: UserSession
    tokens: AuthTokens
    mfa_required: bool = False


class IdentityService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------------ users
    async def get_user_by_email(self, email: str) -> User | None:
        stmt = select(User).where(User.email_normalized == normalize_email(email))
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_user(self, user_id: uuid.UUID) -> User | None:
        stmt = select(User).where(User.id == user_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def register(
        self,
        *,
        email: str,
        password: str,
        full_name: str = "",
        locale: str = "fa-IR",
        timezone: str = "Asia/Tehran",
    ) -> User:
        normalized = normalize_email(email)
        if not normalized or "@" not in normalized:
            raise ValidationError("Invalid email address.", errors=[{"field": "email", "code": "invalid_email", "message": "Invalid email address."}])
        issues = security.password_strength_issues(password)
        if issues:
            raise ValidationError(
                "Password does not meet the security policy.",
                errors=[{"field": "password", "code": issue, "message": issue} for issue in issues],
            )
        if await self.get_user_by_email(normalized):
            raise ConflictError("An account with this email already exists.", context={"code": "email_taken"})

        user = User(
            email=email.strip(),
            email_normalized=normalized,
            password_hash=security.hash_password(password),
            full_name=full_name.strip(),
            locale=locale,
            timezone=timezone,
            password_changed_at=datetime.now(UTC),
        )
        self.session.add(user)
        await self.session.flush()
        await self.record_security_event(
            "user.registered", user_id=user.id, detail={"locale": locale}
        )
        return user

    async def verify_password_for_user(self, user: User, password: str) -> bool:
        if not user.password_hash:
            # Passwordless account (OAuth-only): reject password auth.
            return False
        ok = security.verify_password(password, user.password_hash)
        if ok and security.password_needs_rehash(user.password_hash):
            user.password_hash = security.hash_password(password)
            user.password_changed_at = datetime.now(UTC)
            await self.session.flush()
        return ok

    # --------------------------------------------------------------- sessions
    async def create_session(
        self,
        user: User,
        *,
        ip_address: str | None = None,
        user_agent: str | None = None,
        device_label: str | None = None,
        mfa_verified: bool = False,
    ) -> UserSession:
        now = datetime.now(UTC)
        session = UserSession(
            user_id=user.id,
            ip_address=ip_address,
            user_agent=(user_agent or "")[:512] or None,
            device_label=device_label,
            mfa_verified=mfa_verified,
            last_seen_at=now,
            expires_at=now + timedelta(days=settings.SESSION_IDLE_TTL_DAYS),
        )
        self.session.add(session)
        await self.session.flush()
        return session

    async def issue_tokens(
        self,
        user: User,
        session: UserSession,
        *,
        roles: list[str] | None = None,
        is_superuser: bool | None = None,
    ) -> AuthTokens:
        family_id = security.generate_nonce(16)
        return await self._issue_new_chain(
            user=user, session=session, family_id=family_id, roles=roles or [],
            is_superuser=is_superuser if is_superuser is not None else user.is_superuser,
        )

    async def _issue_new_chain(
        self,
        *,
        user: User,
        session: UserSession,
        family_id: str,
        roles: list[str],
        is_superuser: bool,
    ) -> AuthTokens:
        now = datetime.now(UTC)
        token_id = security.generate_token_id()
        token_expires = now + timedelta(days=settings.REFRESH_TOKEN_TTL_DAYS)
        chain_expires = now + timedelta(days=settings.REFRESH_TOKEN_MAX_AGE_DAYS)

        refresh_jwt = create_refresh_token(
            user_id=user.id,
            session_id=session.id,
            family_id=family_id,
            token_id=token_id,
            expires_at=min(token_expires, chain_expires),
        )
        row = RefreshToken(
            session_id=session.id,
            user_id=user.id,
            token_hash=_hash_token(token_id),
            family_id=family_id,
            expires_at=min(token_expires, chain_expires),
            chain_expires_at=chain_expires,
        )
        self.session.add(row)

        access_jwt, _jti, access_expires = create_access_token(
            user_id=user.id,
            session_id=session.id,
            roles=roles,
            is_superuser=is_superuser,
            mfa_verified=session.mfa_verified,
        )
        await self.session.flush()
        return AuthTokens(
            access_token=access_jwt,
            refresh_token=refresh_jwt,
            expires_in=int((access_expires - now).total_seconds()),
            session_id=session.id,
        )

    async def authenticate(
        self,
        *,
        email: str,
        password: str,
        ip_address: str | None = None,
        user_agent: str | None = None,
        device_label: str | None = None,
    ) -> AuthResult:
        """Validate credentials and start a session.

        Timing is kept roughly uniform for unknown emails (a dummy hash is
        verified) so the endpoint cannot be used to enumerate accounts.
        """
        user = await self.get_user_by_email(email)
        if user is None:
            security.verify_password(password, DUMMY_HASH)
            await self.record_security_event(
                "auth.login_failed", detail={"reason": "unknown_email", "email": normalize_email(email)},
                ip_address=ip_address,
            )
            raise AuthenticationError("Invalid email or password.")

        if not user.is_active:
            raise AuthenticationError("This account is disabled.")
        if user.is_locked:
            await self.record_security_event(
                "auth.login_blocked", user_id=user.id,
                detail={"reason": "locked", "locked_until": user.locked_until.isoformat()},
                ip_address=ip_address,
            )
            raise AuthenticationError("Too many failed attempts. Try again later.")

        if not await self.verify_password_for_user(user, password):
            await self._register_failed_login(user, ip_address=ip_address)
            raise AuthenticationError("Invalid email or password.")

        user.failed_login_count = 0
        user.locked_until = None
        user.last_login_at = datetime.now(UTC)
        session = await self.create_session(
            user, ip_address=ip_address, user_agent=user_agent, device_label=device_label
        )
        tokens = await self.issue_tokens(user, session)
        await self.record_security_event(
            "auth.login_succeeded", user_id=user.id, ip_address=ip_address,
            detail={"session_id": str(session.id)},
        )
        await self.session.flush()
        return AuthResult(
            user=user,
            session=session,
            tokens=tokens,
            mfa_required=user.mfa_enabled and user.mfa_required and not session.mfa_verified,
        )

    async def _register_failed_login(self, user: User, *, ip_address: str | None) -> None:
        user.failed_login_count = (user.failed_login_count or 0) + 1
        detail: dict[str, object] = {"attempt": user.failed_login_count}
        if user.failed_login_count >= MAX_FAILED_LOGINS:
            user.locked_until = datetime.now(UTC) + timedelta(minutes=LOCKOUT_MINUTES)
            user.failed_login_count = 0
            detail["locked_until"] = user.locked_until.isoformat()
        await self.record_security_event(
            "auth.login_failed", user_id=user.id, ip_address=ip_address, detail=detail
        )
        await self.session.flush()

    # ------------------------------------------------------- token rotation
    async def rotate_refresh_token(self, refresh_jwt: str) -> AuthTokens:
        """Rotate a refresh token. Reuse of an already-rotated token revokes
        the whole family (RFC 6819 / OAuth BCP)."""
        payload: TokenPayload = decode_token(refresh_jwt, expected_type="refresh")
        token_hash = _hash_token(payload.jti)

        stmt = select(RefreshToken).where(RefreshToken.token_hash == token_hash)
        row = (await self.session.execute(stmt)).scalar_one_or_none()
        if row is None:
            await self.record_security_event(
                "auth.refresh_unknown_token", user_id=uuid.UUID(payload.sub),
                detail={"family": payload.family},
            )
            raise AuthenticationError("Invalid refresh token.")

        user = await self.get_user(row.user_id)
        if user is None or not user.is_active:
            raise AuthenticationError("Account is no longer active.")

        session_row = await self.session.get(UserSession, row.session_id)
        if session_row is None or not session_row.is_active or session_row.revoked_at:
            raise AuthenticationError("Session has been revoked.")
        if is_past(session_row.expires_at):
            raise AuthenticationError("Session has expired.")

        if row.is_revoked:
            # A rotated token arriving again is either (a) a client retry of a
            # request whose response was lost, or (b) a stolen token being
            # replayed. Inside the grace window we hand back the same
            # replacement (idempotent, no new credential); outside it we treat
            # it as theft and kill the whole family.
            if row.used_at is not None and row.replaced_by_id is not None:
                used_at = as_utc(row.used_at)
                grace = timedelta(seconds=settings.REFRESH_ROTATION_REUSE_GRACE_SECONDS)
                if used_at is not None and datetime.now(UTC) - used_at <= grace:
                    replacement = await self.session.get(RefreshToken, row.replaced_by_id)
                    if replacement is not None and not is_past(replacement.expires_at):
                        return self._reissue_access_only(user=user, session=session_row, row=replacement)
            await self.revoke_token_family(
                row.family_id, reason="reuse_detected", user_id=user.id
            )
            await self.record_security_event(
                "auth.refresh_reuse_detected",
                user_id=user.id,
                detail={"family": row.family_id, "session_id": str(session_row.id)},
            )
            logger.warning("refresh_token_reuse", family=row.family_id, user_id=str(user.id))
            raise AuthenticationError("Refresh token reuse detected. All sessions were signed out.")

        now = datetime.now(UTC)
        if is_past(row.expires_at) or is_past(row.chain_expires_at):
            raise AuthenticationError("Refresh token expired.")

        # Rotate: revoke the presented token and link it to its replacement.
        # Setting ``is_revoked`` here is what makes replay detectable — without
        # it an old token would simply be accepted again.
        row.used_at = now
        row.is_revoked = True
        row.revoked_at = now
        row.revoked_reason = "rotated"
        new_tokens = await self._issue_new_chain(
            user=user,
            session=session_row,
            family_id=row.family_id,
            roles=[],
            is_superuser=user.is_superuser,
        )
        new_hash = _hash_token(decode_token(new_tokens.refresh_token, expected_type="refresh").jti)
        new_row = (
            await self.session.execute(
                select(RefreshToken).where(RefreshToken.token_hash == new_hash)
            )
        ).scalar_one()
        new_row.chain_expires_at = row.chain_expires_at
        new_row.expires_at = min(new_row.expires_at, row.chain_expires_at)
        row.replaced_by_id = new_row.id

        session_row.last_seen_at = now
        await self.session.flush()
        return new_tokens

    def _reissue_access_only(self, *, user: User, session: UserSession, row: RefreshToken) -> AuthTokens:
        """Return a fresh access token without minting a new refresh token.

        Used only for a retry that lands inside the rotation grace window: the
        client already holds the replacement refresh token from the first call,
        so we must not create another credential (that is how a replay would
        silently fork the session).
        """
        access_jwt, _jti, access_expires = create_access_token(
            user_id=user.id,
            session_id=session.id,
            roles=[],
            is_superuser=user.is_superuser,
            mfa_verified=session.mfa_verified,
        )
        return AuthTokens(
            access_token=access_jwt,
            refresh_token="",
            expires_in=int((access_expires - datetime.now(UTC)).total_seconds()),
            session_id=session.id,
        )

    async def revoke_token_family(self, family_id: str, *, reason: str, user_id: uuid.UUID | None = None) -> int:
        now = datetime.now(UTC)
        stmt = (
            update(RefreshToken)
            .where(RefreshToken.family_id == family_id, RefreshToken.is_revoked.is_(False))
            .values(is_revoked=True, revoked_at=now, revoked_reason=reason)
        )
        result = await self.session.execute(stmt)
        return result.rowcount or 0

    async def revoke_session(self, session_id: uuid.UUID, *, reason: str = "user_logout") -> None:
        now = datetime.now(UTC)
        session_row = await self.session.get(UserSession, session_id)
        if session_row is None:
            raise NotFoundError("Session not found.")
        session_row.is_active = False
        session_row.revoked_at = now
        session_row.revoked_reason = reason
        await self.session.execute(
            update(RefreshToken)
            .where(RefreshToken.session_id == session_id, RefreshToken.is_revoked.is_(False))
            .values(is_revoked=True, revoked_at=now, revoked_reason=reason)
        )
        await self.record_security_event(
            "auth.session_revoked", user_id=session_row.user_id, detail={"reason": reason}
        )
        await self.session.flush()

    async def revoke_all_sessions(self, user_id: uuid.UUID, *, reason: str = "logout_all") -> int:
        now = datetime.now(UTC)
        sessions = (
            await self.session.execute(
                select(UserSession).where(UserSession.user_id == user_id, UserSession.is_active.is_(True))
            )
        ).scalars().all()
        for row in sessions:
            row.is_active = False
            row.revoked_at = now
            row.revoked_reason = reason
        await self.session.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == user_id, RefreshToken.is_revoked.is_(False))
            .values(is_revoked=True, revoked_at=now, revoked_reason=reason)
        )
        await self.record_security_event(
            "auth.all_sessions_revoked", user_id=user_id, detail={"count": len(sessions)}
        )
        await self.session.flush()
        return len(sessions)

    async def list_sessions(self, user_id: uuid.UUID) -> list[UserSession]:
        stmt = (
            select(UserSession)
            .where(UserSession.user_id == user_id)
            .order_by(UserSession.created_at.desc())
            .limit(50)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    # ------------------------------------------------------------- passwords
    async def change_password(self, user: User, *, current_password: str, new_password: str) -> None:
        if not await self.verify_password_for_user(user, current_password):
            raise AuthenticationError("Current password is incorrect.")
        issues = security.password_strength_issues(new_password)
        if issues:
            raise ValidationError(
                "Password does not meet the security policy.",
                errors=[{"field": "password", "code": issue, "message": issue} for issue in issues],
            )
        user.password_hash = security.hash_password(new_password)
        user.password_changed_at = datetime.now(UTC)
        # Rotating credentials invalidates every existing session.
        await self.revoke_all_sessions(user.id, reason="password_changed")
        await self.record_security_event("auth.password_changed", user_id=user.id)
        await self.session.flush()

    async def create_password_reset_token(self, email: str, *, ip_address: str | None = None) -> str | None:
        """Always returns None to the caller path when the email is unknown —
        the endpoint must not reveal account existence."""
        user = await self.get_user_by_email(email)
        if user is None:
            await self.record_security_event(
                "auth.password_reset_unknown_email", detail={"email": normalize_email(email)},
                ip_address=ip_address,
            )
            return None
        raw = security.generate_token_id()
        row = PasswordResetToken(
            user_id=user.id,
            token_hash=_hash_token(raw),
            expires_at=datetime.now(UTC) + timedelta(minutes=settings.PASSWORD_RESET_TTL_MINUTES),
            request_ip=ip_address,
        )
        self.session.add(row)
        await self.record_security_event(
            "auth.password_reset_requested", user_id=user.id, ip_address=ip_address
        )
        await self.session.flush()
        return raw

    async def consume_password_reset_token(self, raw_token: str, new_password: str) -> User:
        row = (
            await self.session.execute(
                select(PasswordResetToken).where(PasswordResetToken.token_hash == _hash_token(raw_token))
            )
        ).scalar_one_or_none()
        if row is None or row.consumed_at is not None or is_past(row.expires_at):
            raise AuthenticationError("Reset link is invalid or has expired.")
        user = await self.get_user(row.user_id)
        if user is None:
            raise AuthenticationError("Reset link is invalid or has expired.")
        issues = security.password_strength_issues(new_password)
        if issues:
            raise ValidationError(
                "Password does not meet the security policy.",
                errors=[{"field": "password", "code": issue, "message": issue} for issue in issues],
            )
        row.consumed_at = datetime.now(UTC)
        user.password_hash = security.hash_password(new_password)
        user.password_changed_at = datetime.now(UTC)
        await self.revoke_all_sessions(user.id, reason="password_reset")
        await self.record_security_event("auth.password_reset_completed", user_id=user.id)
        await self.session.flush()
        return user

    # ------------------------------------------------------------- telemetry
    async def record_security_event(
        self,
        event_type: str,
        *,
        user_id: uuid.UUID | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
        detail: dict | None = None,
    ) -> SecurityEvent:
        event = SecurityEvent(
            user_id=user_id,
            event_type=event_type,
            ip_address=ip_address,
            user_agent=(user_agent or "")[:512] or None,
            detail=security.redact_secrets(detail or {}),
        )
        self.session.add(event)
        return event


# A fixed Argon2id hash used only to equalise timing for unknown emails.
DUMMY_HASH = security.hash_password(security.generate_password(32))
