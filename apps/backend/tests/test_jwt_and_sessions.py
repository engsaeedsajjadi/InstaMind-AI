"""JWT semantics and refresh-token rotation (incl. reuse detection)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.core.config import settings
from app.core.exceptions import AuthenticationError
from app.core.jwt import TokenError, create_access_token, create_refresh_token, decode_token
from app.modules.identity.models import RefreshToken, SecurityEvent
from app.modules.identity.service import IdentityService


@pytest.mark.asyncio
async def test_access_token_carries_roles_and_session(session, make_user, make_workspace, make_membership):
    owner = await make_user(email="owner-tok@example.com")
    member = await make_user(email="member-tok@example.com")
    workspace = await make_workspace(owner.id)
    # The workspace creator already has an OWNER membership; the second user
    # gets an EDITOR one.
    await make_membership(workspace.id, member.id, role="EDITOR")

    token, jti, expires = create_access_token(
        user_id=member.id, session_id=uuid.uuid4(), workspace_id=workspace.id, roles=["EDITOR"]
    )
    payload = decode_token(token, expected_type="access")
    assert payload.sub == str(member.id)
    assert payload.jti == jti
    assert payload.roles == ["EDITOR"]
    assert payload.workspace_id == str(workspace.id)
    assert expires > datetime.now(UTC)


@pytest.mark.asyncio
async def test_access_token_cannot_be_used_as_refresh(make_user):
    user = await make_user()
    token, _jti, _exp = create_access_token(user_id=user.id, session_id=uuid.uuid4())
    with pytest.raises(TokenError) as exc:
        decode_token(token, expected_type="refresh")
    assert "token_type_mismatch" in str(exc.value)


def test_tampered_token_is_rejected():
    token, _jti, _exp = create_access_token(user_id=uuid.uuid4(), session_id=uuid.uuid4())
    with pytest.raises(TokenError):
        decode_token(token[:-3] + "abc", expected_type="access")


def test_expired_token_is_rejected():
    token, _jti, _exp = create_access_token(
        user_id=uuid.uuid4(), session_id=uuid.uuid4(), ttl_minutes=-1
    )
    with pytest.raises(TokenError) as exc:
        decode_token(token, expected_type="access")
    assert "token_expired" in str(exc.value)


@pytest.mark.asyncio
async def test_login_issues_both_tokens_and_creates_a_session(session, make_user):
    user = await make_user()
    service = IdentityService(session)
    result = await service.authenticate(email=user.email, password="Correct-Horse-Battery-9")
    assert result.tokens.access_token and result.tokens.refresh_token
    assert result.tokens.expires_in > 0
    assert result.session.id is not None


@pytest.mark.asyncio
async def test_login_with_wrong_password_fails_and_counts(session, make_user):
    user = await make_user()
    service = IdentityService(session)
    with pytest.raises(AuthenticationError):
        await service.authenticate(email=user.email, password="Definitely-Wrong-123")
    events = (
        await session.execute(
            select(SecurityEvent).where(SecurityEvent.event_type == "auth.login_failed")
        )
    ).scalars().all()
    assert events, "a failed login must be recorded"


@pytest.mark.asyncio
async def test_unknown_email_is_rejected_with_the_same_error(session):
    service = IdentityService(session)
    with pytest.raises(AuthenticationError):
        await service.authenticate(email="nobody@example.com", password="Correct-Horse-Battery-9")


@pytest.mark.asyncio
async def test_account_locks_after_repeated_failures(session, make_user):
    user = await make_user()
    service = IdentityService(session)
    for _ in range(5):
        with pytest.raises(AuthenticationError):
            await service.authenticate(email=user.email, password="Definitely-Wrong-123")
    assert user.locked_until is not None
    # Even the correct password is refused while locked.
    with pytest.raises(AuthenticationError):
        await service.authenticate(email=user.email, password="Correct-Horse-Battery-9")


@pytest.mark.asyncio
async def test_refresh_rotation_invalidates_the_old_token(session, make_user, monkeypatch):
    monkeypatch.setattr(settings, "REFRESH_ROTATION_REUSE_GRACE_SECONDS", 0)
    user = await make_user()
    service = IdentityService(session)
    first = await service.authenticate(email=user.email, password="Correct-Horse-Battery-9")
    rotated = await service.rotate_refresh_token(first.tokens.refresh_token)
    assert rotated.refresh_token != first.tokens.refresh_token

    with pytest.raises(AuthenticationError):
        await service.rotate_refresh_token(first.tokens.refresh_token)


@pytest.mark.asyncio
async def test_refresh_reuse_revokes_the_whole_family(session, make_user, monkeypatch):
    monkeypatch.setattr(settings, "REFRESH_ROTATION_REUSE_GRACE_SECONDS", 0)
    user = await make_user()
    service = IdentityService(session)
    first = await service.authenticate(email=user.email, password="Correct-Horse-Battery-9")
    stale = first.tokens.refresh_token
    await service.rotate_refresh_token(stale)

    # Replay the stale token: theft signal.
    with pytest.raises(AuthenticationError):
        await service.rotate_refresh_token(stale)

    rows = (await session.execute(select(RefreshToken).where(RefreshToken.user_id == user.id))).scalars().all()
    assert rows and all(row.is_revoked for row in rows), "reuse must revoke the entire family"
    events = (
        await session.execute(
            select(func.count())
            .select_from(SecurityEvent)
            .where(SecurityEvent.event_type == "auth.refresh_reuse_detected")
        )
    ).scalar_one()
    assert events == 1


@pytest.mark.asyncio
async def test_retry_inside_the_grace_window_is_not_treated_as_theft(session, make_user, monkeypatch):
    """A lost HTTP response must not sign the user out."""
    monkeypatch.setattr(settings, "REFRESH_ROTATION_REUSE_GRACE_SECONDS", 60)
    user = await make_user()
    service = IdentityService(session)
    first = await service.authenticate(email=user.email, password="Correct-Horse-Battery-9")
    await service.rotate_refresh_token(first.tokens.refresh_token)

    # Same token again, inside the window: tolerated, no family revocation.
    retry = await service.rotate_refresh_token(first.tokens.refresh_token)
    assert retry.access_token
    events = (
        await session.execute(
            select(func.count())
            .select_from(SecurityEvent)
            .where(SecurityEvent.event_type == "auth.refresh_reuse_detected")
        )
    ).scalar_one()
    assert events == 0


@pytest.mark.asyncio
async def test_rotated_token_is_marked_revoked(session, make_user):
    user = await make_user()
    service = IdentityService(session)
    first = await service.authenticate(email=user.email, password="Correct-Horse-Battery-9")
    await service.rotate_refresh_token(first.tokens.refresh_token)
    rows = (
        await session.execute(
            select(RefreshToken).where(RefreshToken.user_id == user.id).order_by(RefreshToken.created_at)
        )
    ).scalars().all()
    assert rows[0].is_revoked is True, "the presented token must be revoked on rotation"
    assert rows[0].revoked_reason == "rotated"
    assert rows[0].replaced_by_id == rows[-1].id


@pytest.mark.asyncio
async def test_expired_refresh_token_is_refused(session, make_user):
    user = await make_user()
    service = IdentityService(session)
    result = await service.authenticate(email=user.email, password="Correct-Horse-Battery-9")
    # Force expiry on the stored row and re-issue a matching short-lived JWT.
    row = (
        await session.execute(select(RefreshToken).where(RefreshToken.user_id == user.id))
    ).scalar_one()
    row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await session.flush()
    with pytest.raises(AuthenticationError):
        await service.rotate_refresh_token(result.tokens.refresh_token)


@pytest.mark.asyncio
async def test_logout_all_revokes_every_session(session, make_user):
    user = await make_user()
    service = IdentityService(session)
    await service.authenticate(email=user.email, password="Correct-Horse-Battery-9")
    await service.authenticate(email=user.email, password="Correct-Horse-Battery-9")
    count = await service.revoke_all_sessions(user.id)
    assert count == 2
    sessions = await service.list_sessions(user.id)
    assert all(not row.is_active for row in sessions)


@pytest.mark.asyncio
async def test_password_change_revokes_sessions(session, make_user):
    user = await make_user()
    service = IdentityService(session)
    await service.authenticate(email=user.email, password="Correct-Horse-Battery-9")
    await service.change_password(
        user, current_password="Correct-Horse-Battery-9", new_password="A-Brand-New-Password-1"
    )
    sessions = await service.list_sessions(user.id)
    assert all(not row.is_active for row in sessions)


@pytest.mark.asyncio
async def test_password_reset_flow_and_unknown_email_leak(session, make_user):
    user = await make_user()
    service = IdentityService(session)
    token = await service.create_password_reset_token(user.email)
    assert token is not None
    # An unknown email must not be distinguishable.
    assert await service.create_password_reset_token("ghost@example.com") is None

    await service.consume_password_reset_token(token, "Another-Strong-Password-1")
    refreshed = await service.authenticate(email=user.email, password="Another-Strong-Password-1")
    assert refreshed.user.id == user.id
    # The token is single-use.
    with pytest.raises(AuthenticationError):
        await service.consume_password_reset_token(token, "Yet-Another-Password-1")


def test_refresh_token_ttl_configuration_is_sane():
    assert settings.REFRESH_TOKEN_TTL_DAYS < settings.REFRESH_TOKEN_MAX_AGE_DAYS
    assert settings.ACCESS_TOKEN_TTL_MINUTES <= 60


def test_refresh_jwt_structure():
    jwt_token = create_refresh_token(
        user_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        family_id="fam-1",
        token_id="tok-1",
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    payload = decode_token(jwt_token, expected_type="refresh")
    assert payload.family == "fam-1"
    assert payload.jti == "tok-1"
