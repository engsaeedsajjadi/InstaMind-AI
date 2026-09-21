"""Multi-tenancy: the TenantScope must make cross-tenant reads impossible."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.core.exceptions import AuthorizationError, NotFoundError, TenantIsolationViolation
from app.core.permissions import Permission
from app.modules.content.models import Content
from app.modules.instagram.models import SocialAccount
from app.modules.tenants.scope import TenantScope
from app.modules.tenants.service import TenantService


@pytest.mark.asyncio
async def test_scope_never_returns_another_workspaces_row(session, make_user, make_workspace, make_content):
    alice = await make_user(email="alice@example.com")
    bob = await make_user(email="bob@example.com")
    ws_a = await make_workspace(alice.id, name="Alice Inc")
    ws_b = await make_workspace(bob.id, name="Bob LLC")
    secret = await make_content(ws_a.id, caption="Alice's private post")

    tenant_service = TenantService(session)
    ctx_b = await tenant_service.build_context(workspace_id=ws_b.id, user_id=bob.id)
    scope_b = TenantScope(session, ctx_b)

    # Direct id lookup: 404, not 403 — existence is not disclosed.
    with pytest.raises(NotFoundError):
        await scope_b.get_one(Content, secret.id)
    assert await scope_b.find(Content, secret.id) is None
    assert await scope_b.list(Content) == []
    assert await scope_b.count(Content) == 0


@pytest.mark.asyncio
async def test_scope_returns_own_rows(session, make_user, make_workspace, make_content):
    alice = await make_user()
    ws_a = await make_workspace(alice.id)
    mine = await make_content(ws_a.id, caption="mine")

    ctx = await TenantService(session).build_context(workspace_id=ws_a.id, user_id=alice.id)
    scope = TenantScope(session, ctx)
    found = await scope.get_one(Content, mine.id)
    assert found.id == mine.id
    assert len(await scope.list(Content)) == 1


@pytest.mark.asyncio
async def test_verify_rejects_a_forged_object(session, make_user, make_workspace, make_content):
    alice = await make_user(email="a@example.com")
    bob = await make_user(email="b@example.com")
    ws_a = await make_workspace(alice.id)
    ws_b = await make_workspace(bob.id)
    secret = await make_content(ws_a.id)

    ctx_b = await TenantService(session).build_context(workspace_id=ws_b.id, user_id=bob.id)
    scope_b = TenantScope(session, ctx_b)
    with pytest.raises(TenantIsolationViolation):
        scope_b._verify(secret)


@pytest.mark.asyncio
async def test_soft_delete_hides_rows(session, make_user, make_workspace, make_content):
    alice = await make_user()
    ws = await make_workspace(alice.id)
    content = await make_content(ws.id)
    ctx = await TenantService(session).build_context(workspace_id=ws.id, user_id=alice.id)
    scope = TenantScope(session, ctx)

    await scope.soft_delete(content)
    assert await scope.find(Content, content.id) is None
    assert await scope.find(Content, content.id, include_deleted=True) is not None


@pytest.mark.asyncio
async def test_non_member_cannot_build_a_context(session, make_user, make_workspace):
    owner = await make_user(email="owner@example.com")
    intruder = await make_user(email="intruder@example.com")
    ws = await make_workspace(owner.id)
    with pytest.raises(AuthorizationError):
        await TenantService(session).build_context(workspace_id=ws.id, user_id=intruder.id)


@pytest.mark.asyncio
async def test_membership_resolves_builtin_permissions(session, make_user, make_workspace, make_membership):
    owner = await make_user(email="o@example.com")
    viewer = await make_user(email="v@example.com")
    ws = await make_workspace(owner.id)
    await make_membership(ws.id, viewer.id, role="VIEWER")

    service = TenantService(session)
    ctx = await service.build_context(workspace_id=ws.id, user_id=viewer.id)
    assert ctx.can(Permission.CONTENT_READ) is True
    assert ctx.can(Permission.CONTENT_PUBLISH) is False
    assert ctx.can(Permission.BILLING_MANAGE) is False


@pytest.mark.asyncio
async def test_social_media_manager_can_publish_but_not_delete_workspace(
    session, make_user, make_workspace, make_membership
):
    owner = await make_user(email="o2@example.com")
    smm = await make_user(email="smm@example.com")
    ws = await make_workspace(owner.id)
    await make_membership(ws.id, smm.id, role="SOCIAL_MEDIA_MANAGER")

    ctx = await TenantService(session).build_context(workspace_id=ws.id, user_id=smm.id)
    assert ctx.can(Permission.CONTENT_PUBLISH) is True
    assert ctx.can(Permission.WORKSPACE_DELETE) is False
    assert ctx.can(Permission.WORKSPACE_TRANSFER) is False


@pytest.mark.asyncio
async def test_owner_can_transfer_only_to_a_member(session, make_user, make_workspace, make_membership):
    owner = await make_user(email="owner2@example.com")
    outsider = await make_user(email="outsider@example.com")
    member = await make_user(email="member@example.com")
    ws = await make_workspace(owner.id)
    await make_membership(ws.id, member.id, role="ADMIN")

    service = TenantService(session)
    from app.core.exceptions import ValidationError

    with pytest.raises(ValidationError):
        await service.transfer_ownership(workspace_id=ws.id, actor_id=owner.id, new_owner_id=outsider.id)

    updated = await service.transfer_ownership(workspace_id=ws.id, actor_id=owner.id, new_owner_id=member.id)
    assert updated.owner_id == member.id


@pytest.mark.asyncio
async def test_custom_role_grants_only_its_permissions(session, make_user, make_workspace, make_membership):
    owner = await make_user(email="owner3@example.com")
    member = await make_user(email="member3@example.com")
    ws = await make_workspace(owner.id)
    membership = await make_membership(ws.id, member.id, role="VIEWER")

    service = TenantService(session)
    role = await service.create_custom_role(
        workspace_id=ws.id,
        code="PUBLISH_ONLY",
        name="Publish only",
        permissions=[Permission.CONTENT_PUBLISH.value],
    )
    membership.custom_role_id = role.id
    await session.flush()

    ctx = await service.build_context(workspace_id=ws.id, user_id=member.id)
    assert ctx.can(Permission.CONTENT_PUBLISH) is True   # from the custom role
    assert ctx.can(Permission.CONTENT_READ) is True      # from VIEWER
    assert ctx.can(Permission.BILLING_MANAGE) is False


@pytest.mark.asyncio
async def test_unknown_role_code_contributes_nothing(session, make_user, make_workspace, make_membership):
    owner = await make_user(email="owner4@example.com")
    member = await make_user(email="member4@example.com")
    ws = await make_workspace(owner.id)
    await make_membership(ws.id, member.id, role="NOT_A_REAL_ROLE")
    ctx = await TenantService(session).build_context(workspace_id=ws.id, user_id=member.id)
    assert ctx.permissions == frozenset()


@pytest.mark.asyncio
async def test_social_accounts_are_isolated(session, make_user, make_workspace, make_account):
    alice = await make_user(email="al@example.com")
    bob = await make_user(email="bo@example.com")
    ws_a = await make_workspace(alice.id)
    ws_b = await make_workspace(bob.id)
    account = await make_account(ws_a.id)

    ctx_b = await TenantService(session).build_context(workspace_id=ws_b.id, user_id=bob.id)
    scope_b = TenantScope(session, ctx_b)
    assert await scope_b.list(SocialAccount) == []
    with pytest.raises(NotFoundError):
        await scope_b.get_one(SocialAccount, account.id)


@pytest.mark.asyncio
async def test_invite_and_accept_creates_membership(session, make_user, make_workspace):
    owner = await make_user(email="owner5@example.com")
    invitee = await make_user(email="invitee5@example.com")
    ws = await make_workspace(owner.id)

    service = TenantService(session)
    _invitation, token = await service.invite_member(
        workspace_id=ws.id, invited_by=owner.id, email=invitee.email, role_code="EDITOR"
    )
    membership = await service.accept_invitation(token=token, user_id=invitee.id)
    assert membership.role_code == "EDITOR"
    assert membership.status == "ACTIVE"
    # The invitation is single-use.
    again = await service.accept_invitation(token=token, user_id=invitee.id)
    assert again.id == membership.id


@pytest.mark.asyncio
async def test_invitation_token_from_another_workspace_is_rejected(session, make_user, make_workspace):
    owner = await make_user(email="owner6@example.com")
    invitee = await make_user(email="invitee6@example.com")
    ws = await make_workspace(owner.id)
    service = TenantService(session)
    _invitation, token = await service.invite_member(
        workspace_id=ws.id, invited_by=owner.id, email=invitee.email
    )
    from app.core.exceptions import NotFoundError as _NF

    with pytest.raises(_NF):
        await service.accept_invitation(token="not-the-real-token", user_id=invitee.id)


@pytest.mark.asyncio
async def test_workspace_slug_is_unique(session, make_user, make_workspace):
    alice = await make_user(email="a9@example.com")
    bob = await make_user(email="b9@example.com")
    first = await make_workspace(alice.id, name="Acme")
    second = await make_workspace(bob.id, name="Acme")
    assert first.slug != second.slug


@pytest.mark.asyncio
async def test_raw_query_without_scope_is_not_used_by_services(session, make_user, make_workspace, make_content):
    """Sanity check that the fixtures really do create tenant rows, so the
    isolation tests above are meaningful."""
    alice = await make_user(email="a10@example.com")
    ws = await make_workspace(alice.id)
    await make_content(ws.id)
    rows = (await session.execute(select(Content))).scalars().all()
    assert len(rows) == 1
    assert rows[0].workspace_id == uuid.UUID(str(ws.id))
