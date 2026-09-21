"""Instagram connection: OAuth flow, token storage, refresh, disconnect.

Meta is never called — the transport is a scripted fake. What is verified is
*our* logic: state handling, token encryption, capability derivation, and the
guards that stop a bad connection from being stored.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.core import security
from app.core.exceptions import (
    AuthenticationError,
    ConflictError,
    MetaAPIError,
    NotFoundError,
    ValidationError,
)
from app.modules.instagram.models import OAuthState, OAuthToken
from app.modules.instagram.service import (
    SCOPES_FACEBOOK_LOGIN,
    SCOPES_INSTAGRAM_LOGIN,
    InstagramConnectService,
    derive_capabilities,
)
from app.modules.tenants.scope import TenantScope
from app.modules.tenants.service import TenantService


@pytest.fixture
def scope_for(session):
    async def _make(user, workspace, role: str = "OWNER") -> TenantScope:
        ctx = await TenantService(session).build_context(
            workspace_id=workspace.id, user_id=user.id
        )
        return TenantScope(session, ctx)

    return _make


@pytest.mark.asyncio
async def test_authorize_url_is_built_for_instagram_login(session, make_user, make_workspace, scope_for):
    user = await make_user()
    ws = await make_workspace(user.id)
    scope = await scope_for(user, ws)
    service = InstagramConnectService(session, scope)

    initiation = await service.begin_connect(api_path="INSTAGRAM_LOGIN")
    assert initiation.authorize_url.startswith("https://api.instagram.com/oauth/authorize")
    assert "client_id=test-ig-client" in initiation.authorize_url
    assert "code_challenge_method=S256" in initiation.authorize_url, "PKCE must be used"
    for permission in SCOPES_INSTAGRAM_LOGIN:
        assert permission in initiation.authorize_url

    state_row = (
        await session.execute(select(OAuthState).where(OAuthState.state == initiation.state))
    ).scalar_one()
    assert state_row.consumed_at is None
    assert state_row.workspace_id == ws.id


@pytest.mark.asyncio
async def test_authorize_url_for_facebook_login_uses_facebook_dialog(
    session, make_user, make_workspace, scope_for
):
    user = await make_user()
    ws = await make_workspace(user.id)
    scope = await scope_for(user, ws)
    service = InstagramConnectService(session, scope)
    initiation = await service.begin_connect(api_path="FACEBOOK_LOGIN")
    assert initiation.authorize_url.startswith("https://www.facebook.com/")
    for permission in SCOPES_FACEBOOK_LOGIN:
        assert permission in initiation.authorize_url


@pytest.mark.asyncio
async def test_unsupported_api_path_is_rejected(session, make_user, make_workspace, scope_for):
    user = await make_user()
    ws = await make_workspace(user.id)
    scope = await scope_for(user, ws)
    with pytest.raises(ValidationError):
        await InstagramConnectService(session, scope).begin_connect(api_path="PASSWORD_LOGIN")


@pytest.mark.asyncio
async def test_state_cannot_be_reused_or_forged(session, make_user, make_workspace, scope_for):
    user = await make_user()
    ws = await make_workspace(user.id)
    scope = await scope_for(user, ws)
    service = InstagramConnectService(session, scope, client_factory=lambda access_token: None)
    initiation = await service.begin_connect(api_path="INSTAGRAM_LOGIN")
    await service._consume_state(initiation.state)
    with pytest.raises(AuthenticationError):
        await service._consume_state(initiation.state)
    with pytest.raises(AuthenticationError):
        await service._consume_state("forged-state-value")


@pytest.mark.asyncio
async def test_state_from_another_workspace_is_rejected(session, make_user, make_workspace, scope_for):
    alice = await make_user(email="a@x.com")
    bob = await make_user(email="b@x.com")
    ws_a = await make_workspace(alice.id)
    ws_b = await make_workspace(bob.id)
    scope_a = await scope_for(alice, ws_a)
    scope_b = await scope_for(bob, ws_b)

    initiation = await InstagramConnectService(session, scope_a).begin_connect(api_path="INSTAGRAM_LOGIN")
    service_b = InstagramConnectService(session, scope_b, client_factory=lambda access_token: None)
    with pytest.raises(AuthenticationError):
        await service_b._consume_state(initiation.state)


@pytest.mark.asyncio
async def test_user_denying_consent_is_not_an_internal_error(
    session, make_user, make_workspace, scope_for
):
    user = await make_user()
    ws = await make_workspace(user.id)
    scope = await scope_for(user, ws)
    service = InstagramConnectService(session, scope, client_factory=lambda access_token: None)
    initiation = await service.begin_connect(api_path="INSTAGRAM_LOGIN")
    with pytest.raises(AuthenticationError):
        await service.complete_connect(
            state=initiation.state, code="", error="access_denied", error_reason="user_denied"
        )


@pytest.mark.asyncio
async def test_full_instagram_login_connect_stores_an_encrypted_token(
    session, make_user, make_workspace, scope_for, fake_transport, meta_client_factory
):
    user = await make_user()
    ws = await make_workspace(user.id)
    scope = await scope_for(user, ws)
    service = InstagramConnectService(
        session, scope, client_factory=lambda access_token: meta_client_factory(access_token)
    )
    initiation = await service.begin_connect(api_path="INSTAGRAM_LOGIN")

    fake_transport.enqueue({"access_token": "IGQVshort", "user_id": "17840000001"})
    fake_transport.enqueue({"access_token": "IGQVlonglived", "expires_in": 5184000})
    fake_transport.enqueue(
        {
            "id": "17840000001",
            "username": "acme_store",
            "name": "Acme Store",
            "profile_picture_url": "https://example.com/p.jpg",
            "media_count": 42,
            "followers_count": 1200,
            "follows_count": 300,
            "account_type": "BUSINESS",
        }
    )

    account = await service.complete_connect(state=initiation.state, code="auth-code-123")
    assert account.username == "acme_store"
    assert account.external_account_id == "17840000001"
    assert account.status == "CONNECTED"
    assert account.capabilities["stories_publish"] is True
    assert account.capabilities["content_publish"] is True

    token_row = (
        await session.execute(select(OAuthToken).where(OAuthToken.social_account_id == account.id))
    ).scalar_one()
    # The raw token must never appear in the stored value.
    assert "IGQVlonglived" not in token_row.access_token_encrypted
    assert token_row.access_token_fingerprint == security.fingerprint("IGQVlonglived")
    decrypted = security.get_cipher().decrypt(
        token_row.access_token_encrypted, aad=f"oauth_token:{token_row.id}"
    )
    assert decrypted == "IGQVlonglived"

    # Both OAuth calls used the official endpoints.
    urls = [call["url"] for call in fake_transport.calls]
    assert any("/oauth/access_token" in url for url in urls)
    assert any("graph.instagram.com" in url for url in urls)


@pytest.mark.asyncio
async def test_facebook_login_requires_a_linked_page(
    session, make_user, make_workspace, scope_for, fake_transport, meta_client_factory
):
    user = await make_user()
    ws = await make_workspace(user.id)
    scope = await scope_for(user, ws)
    service = InstagramConnectService(
        session, scope, client_factory=lambda access_token: meta_client_factory(access_token)
    )
    initiation = await service.begin_connect(api_path="FACEBOOK_LOGIN")
    fake_transport.enqueue({"access_token": "EAAsb", "expires_in": 3600})
    fake_transport.enqueue({"access_token": "EAAsb-long", "expires_in": 5184000})
    fake_transport.enqueue({"data": [{"id": "111", "name": "Page without IG"}]})

    with pytest.raises(ValidationError) as exc:
        await service.complete_connect(state=initiation.state, code="fb-code")
    assert "Facebook Page" in str(exc.value.detail) or "Page" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_facebook_login_resolves_the_linked_ig_account(
    session, make_user, make_workspace, scope_for, fake_transport, meta_client_factory
):
    user = await make_user()
    ws = await make_workspace(user.id)
    scope = await scope_for(user, ws)
    service = InstagramConnectService(
        session, scope, client_factory=lambda access_token: meta_client_factory(access_token)
    )
    initiation = await service.begin_connect(api_path="FACEBOOK_LOGIN")
    fake_transport.enqueue({"access_token": "EAAsb", "expires_in": 3600})
    fake_transport.enqueue({"access_token": "EAAsb-long", "expires_in": 5184000})
    fake_transport.enqueue(
        {
            "data": [
                {
                    "id": "111",
                    "name": "Acme Page",
                    "instagram_business_account": {"id": "17840000099"},
                }
            ]
        }
    )
    fake_transport.enqueue(
        {
            "id": "17840000099",
            "username": "acme_ig",
            "account_type": "CREATOR",
            "followers_count": 900,
        }
    )
    account = await service.complete_connect(state=initiation.state, code="fb-code")
    assert account.api_path == "FACEBOOK_LOGIN"
    assert account.facebook_page_id == "111"
    assert account.capabilities["stories_publish"] is False, "Creator accounts cannot publish Stories"


@pytest.mark.asyncio
async def test_reconnecting_an_account_owned_by_another_workspace_is_blocked(
    session, make_user, make_workspace, scope_for, fake_transport, meta_client_factory
):
    alice = await make_user(email="al@x.com")
    bob = await make_user(email="bo@x.com")
    ws_a = await make_workspace(alice.id)
    ws_b = await make_workspace(bob.id)

    async def connect(scope):
        service = InstagramConnectService(
            session, scope, client_factory=lambda access_token: meta_client_factory(access_token)
        )
        initiation = await service.begin_connect(api_path="INSTAGRAM_LOGIN")
        fake_transport.enqueue({"access_token": "IGQVshort", "user_id": "17840000777"})
        fake_transport.enqueue({"access_token": "IGQVlong", "expires_in": 5184000})
        fake_transport.enqueue({"id": "17840000777", "username": "shared", "account_type": "BUSINESS"})
        return await service.complete_connect(state=initiation.state, code="code")

    await connect(await scope_for(alice, ws_a))
    with pytest.raises(ConflictError):
        await connect(await scope_for(bob, ws_b))


@pytest.mark.asyncio
async def test_token_is_refreshed_inside_the_safety_margin(
    session, make_user, make_workspace, make_account, scope_for, fake_transport, meta_client_factory
):
    user = await make_user()
    ws = await make_workspace(user.id)
    account = await make_account(ws.id, access_token="IGQVold")
    scope = await scope_for(user, ws)

    token_row = (
        await session.execute(select(OAuthToken).where(OAuthToken.social_account_id == account.id))
    ).scalar_one()
    token_row.expires_at = datetime.now(UTC) + timedelta(days=3)  # inside the 7-day margin
    await session.flush()

    fake_transport.enqueue({"access_token": "IGQVnew", "expires_in": 5184000})
    service = InstagramConnectService(
        session, scope, client_factory=lambda access_token: meta_client_factory(access_token)
    )
    assert await service.refresh_if_needed(account) is True
    assert token_row.access_token_fingerprint == security.fingerprint("IGQVnew")
    assert token_row.expires_at > datetime.now(UTC) + timedelta(days=50)


@pytest.mark.asyncio
async def test_token_far_from_expiry_is_left_alone(
    session, make_user, make_workspace, make_account, scope_for, fake_transport, meta_client_factory
):
    user = await make_user()
    ws = await make_workspace(user.id)
    account = await make_account(ws.id)
    scope = await scope_for(user, ws)
    service = InstagramConnectService(
        session, scope, client_factory=lambda access_token: meta_client_factory(access_token)
    )
    assert await service.refresh_if_needed(account) is False
    assert fake_transport.calls == []


@pytest.mark.asyncio
async def test_failed_refresh_marks_the_account_as_expired(
    session, make_user, make_workspace, make_account, scope_for, fake_transport, meta_client_factory
):
    user = await make_user()
    ws = await make_workspace(user.id)
    account = await make_account(ws.id)
    scope = await scope_for(user, ws)
    token_row = (
        await session.execute(select(OAuthToken).where(OAuthToken.social_account_id == account.id))
    ).scalar_one()
    token_row.expires_at = datetime.now(UTC) + timedelta(days=1)
    await session.flush()

    fake_transport.enqueue_error("Session has expired", code=190, subcode=463, status_code=400)
    service = InstagramConnectService(
        session, scope, client_factory=lambda access_token: meta_client_factory(access_token)
    )
    assert await service.refresh_if_needed(account) is False
    assert account.status == "TOKEN_EXPIRED"
    assert account.last_error_at is not None


@pytest.mark.asyncio
async def test_disconnect_revokes_tokens_and_purges_platform_data(
    session, make_user, make_workspace, make_account, scope_for
):
    user = await make_user()
    ws = await make_workspace(user.id)
    account = await make_account(ws.id)
    scope = await scope_for(user, ws)
    service = InstagramConnectService(session, scope)

    result = await service.disconnect(account.id, purge_platform_data=True)
    assert result.status == "DISCONNECTED"
    assert result.purge_completed_at is not None
    token_row = (
        await session.execute(select(OAuthToken).where(OAuthToken.social_account_id == account.id))
    ).scalar_one()
    assert token_row.is_revoked is True
    with pytest.raises(NotFoundError):
        await service.get_active_token(account)


@pytest.mark.asyncio
async def test_expired_token_cannot_be_used_for_publishing(
    session, make_user, make_workspace, make_account, scope_for
):
    user = await make_user()
    ws = await make_workspace(user.id)
    account = await make_account(ws.id)
    token_row = (
        await session.execute(select(OAuthToken).where(OAuthToken.social_account_id == account.id))
    ).scalar_one()
    token_row.expires_at = datetime.now(UTC) - timedelta(hours=1)
    await session.flush()

    scope = await scope_for(user, ws)
    with pytest.raises(AuthenticationError):
        await InstagramConnectService(session, scope).get_active_token(account)


class TestCapabilityDerivation:
    def test_business_account_gets_stories(self):
        caps = derive_capabilities(account_type="BUSINESS", scopes=SCOPES_INSTAGRAM_LOGIN)
        assert caps["stories_publish"] is True
        assert caps["is_business_account"] is True

    def test_creator_account_never_gets_stories(self):
        caps = derive_capabilities(account_type="CREATOR", scopes=SCOPES_INSTAGRAM_LOGIN)
        assert caps["stories_publish"] is False
        assert caps["content_publish"] is True

    def test_missing_publish_scope_disables_publishing(self):
        caps = derive_capabilities(account_type="BUSINESS", scopes=["instagram_business_basic"])
        assert caps["content_publish"] is False
        assert caps["stories_publish"] is False

    def test_facebook_login_enables_business_discovery(self):
        caps = derive_capabilities(account_type="BUSINESS", scopes=SCOPES_FACEBOOK_LOGIN)
        assert caps["business_discovery"] is True


@pytest.mark.asyncio
async def test_meta_error_carries_code_and_retryability(
    session, make_user, make_workspace, make_account, scope_for, fake_transport, meta_client_factory
):
    """MetaAPIError must expose enough for the engine to decide retry vs fail."""
    client = meta_client_factory("IGQVx")
    fake_transport.enqueue_error("(#4) Application request limit reached", code=4, status_code=400)
    with pytest.raises(MetaAPIError) as exc:
        await client.get_ig_user("17840000001")
    assert exc.value.meta_code == 4
    assert exc.value.is_transient is True

    fake_transport.enqueue_error("Error validating access token", code=190, status_code=400)
    with pytest.raises(MetaAPIError) as exc2:
        await client.get_ig_user("17840000001")
    assert exc2.value.meta_code == 190
    assert exc2.value.is_transient is False


@pytest.mark.asyncio
async def test_appsecret_proof_is_attached_to_every_call(fake_transport, meta_client_factory):
    client = meta_client_factory("IGQVtoken")
    fake_transport.enqueue({"id": "17840000001", "username": "acme"})
    await client.get_ig_user("17840000001")
    params = fake_transport.last_call["params"]
    assert "appsecret_proof" in params
    assert len(params["appsecret_proof"]) == 64
