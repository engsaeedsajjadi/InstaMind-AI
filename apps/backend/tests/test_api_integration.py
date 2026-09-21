"""End-to-end API tests over the real ASGI app (in-process, SQLite)."""

from __future__ import annotations

import json
import uuid

import pytest

PASSWORD = "Correct-Horse-Battery-9"


async def register_and_login(client, email: str = "owner@example.com") -> dict:
    response = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": PASSWORD, "full_name": "Owner"},
    )
    assert response.status_code == 201, response.text
    login = await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert login.status_code == 200, login.text
    return login.json()


async def create_workspace(client, headers: dict, name: str = "Acme") -> dict:
    response = await client.post(
        "/api/v1/workspaces", json={"name": name}, headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.asyncio
async def test_health_endpoints(client):
    live = await client.get("/health/live")
    assert live.status_code == 200
    assert live.json()["status"] == "ok"
    ready = await client.get("/health/ready")
    assert ready.status_code == 200
    assert ready.json()["checks"]["database"] == "up"


@pytest.mark.asyncio
async def test_security_headers_are_present(client):
    response = await client.get("/health/live")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    # Plain HTTP must not advertise HSTS.
    assert "Strict-Transport-Security" not in response.headers


@pytest.mark.asyncio
async def test_trace_id_is_returned(client):
    response = await client.get("/health/live")
    assert response.headers.get("X-Request-Id")


@pytest.mark.asyncio
async def test_registration_rejects_a_weak_password(client):
    response = await client.post(
        "/api/v1/auth/register", json={"email": "x@example.com", "password": "short"}
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_registration_rejects_duplicate_email(client):
    await register_and_login(client, "dup@example.com")
    response = await client.post(
        "/api/v1/auth/register", json={"email": "dup@example.com", "password": PASSWORD}
    )
    assert response.status_code == 409
    assert response.json()["code"] == "conflict"


@pytest.mark.asyncio
async def test_login_with_wrong_password_returns_401(client):
    await register_and_login(client, "login@example.com")
    response = await client.post(
        "/api/v1/auth/login", json={"email": "login@example.com", "password": "Wrong-Password-123"}
    )
    assert response.status_code == 401
    body = response.json()
    assert body["code"] == "unauthenticated"
    assert body["type"].startswith("https://errors.instamind.ai/")
    assert "trace_id" in body


@pytest.mark.asyncio
async def test_protected_route_without_a_token(client):
    response = await client.get("/api/v1/auth/me")
    assert response.status_code == 401
    assert response.headers.get("WWW-Authenticate") == "Bearer"


@pytest.mark.asyncio
async def test_me_returns_the_current_user(client):
    tokens = await register_and_login(client, "me@example.com")
    response = await client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {tokens['access_token']}"}
    )
    assert response.status_code == 200
    assert response.json()["email"] == "me@example.com"
    assert "password_hash" not in response.text


@pytest.mark.asyncio
async def test_refresh_rotates_the_token(client, monkeypatch):
    from app.core.config import settings

    # No grace window: a replay must be rejected outright.
    monkeypatch.setattr(settings, "REFRESH_ROTATION_REUSE_GRACE_SECONDS", 0)
    tokens = await register_and_login(client, "rotate@example.com")
    response = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert response.status_code == 200
    new_tokens = response.json()
    assert new_tokens["refresh_token"] != tokens["refresh_token"]

    replay = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert replay.status_code == 401, "reusing a rotated refresh token must fail"

    # The whole family is gone, so the new token no longer works either.
    follow_up = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": new_tokens["refresh_token"]}
    )
    assert follow_up.status_code == 401


@pytest.mark.asyncio
async def test_password_reset_flow(client):
    await register_and_login(client, "reset@example.com")
    requested = await client.post(
        "/api/v1/auth/password/reset-request", json={"email": "reset@example.com"}
    )
    assert requested.status_code == 202
    token = requested.json()["dev_token"]

    confirmed = await client.post(
        "/api/v1/auth/password/reset-confirm",
        json={"token": token, "new_password": "A-Completely-New-Password-1"},
    )
    assert confirmed.status_code == 204

    login = await client.post(
        "/api/v1/auth/login",
        json={"email": "reset@example.com", "password": "A-Completely-New-Password-1"},
    )
    assert login.status_code == 200


@pytest.mark.asyncio
async def test_password_reset_for_unknown_email_is_indistinguishable(client):
    response = await client.post(
        "/api/v1/auth/password/reset-request", json={"email": "ghost@example.com"}
    )
    assert response.status_code == 202
    assert "dev_token" not in response.json()


@pytest.mark.asyncio
async def test_sessions_can_be_listed_and_revoked(client):
    tokens = await register_and_login(client, "sessions@example.com")
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    listing = await client.get("/api/v1/auth/sessions", headers=headers)
    assert listing.status_code == 200
    rows = listing.json()
    assert len(rows) == 1

    revoked = await client.delete(f"/api/v1/auth/sessions/{rows[0]['id']}", headers=headers)
    assert revoked.status_code == 204

    # The access token of the revoked session stops working.
    me = await client.get("/api/v1/auth/me", headers=headers)
    assert me.status_code == 401


@pytest.mark.asyncio
async def test_workspace_creation_and_listing(client):
    tokens = await register_and_login(client, "ws@example.com")
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    workspace = await create_workspace(client, headers, name="Acme Iran")
    assert workspace["name"] == "Acme Iran"
    assert workspace["calendar_system"] == "jalali"

    listing = await client.get("/api/v1/workspaces", headers=headers)
    assert [row["id"] for row in listing.json()] == [workspace["id"]]


@pytest.mark.asyncio
async def test_context_exposes_effective_permissions(client):
    tokens = await register_and_login(client, "ctx@example.com")
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    workspace = await create_workspace(client, headers)
    context = await client.get(
        "/api/v1/workspaces/context", headers={**headers, "X-Workspace-Id": workspace["id"]}
    )
    assert context.status_code == 200
    body = context.json()
    assert body["roles"] == ["OWNER"]
    assert "content:publish" in body["permissions"]


@pytest.mark.asyncio
async def test_another_users_workspace_is_forbidden(client):
    alice = await register_and_login(client, "alice-api@example.com")
    bob = await register_and_login(client, "bob-api@example.com")
    alice_headers = {"Authorization": f"Bearer {alice['access_token']}"}
    workspace = await create_workspace(client, alice_headers)

    bob_headers = {
        "Authorization": f"Bearer {bob['access_token']}",
        "X-Workspace-Id": workspace["id"],
    }
    response = await client.get("/api/v1/workspaces/context", headers=bob_headers)
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_viewer_cannot_create_content(client, session, make_user, make_workspace, make_membership):
    tokens = await register_and_login(client, "owner-rbac@example.com")
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    workspace = await create_workspace(client, headers)

    viewer = await make_user(email="viewer-rbac@example.com")
    await make_membership(uuid.UUID(workspace["id"]), viewer.id, role="VIEWER")

    viewer_login = await client.post(
        "/api/v1/auth/login", json={"email": viewer.email, "password": PASSWORD}
    )
    viewer_headers = {
        "Authorization": f"Bearer {viewer_login.json()['access_token']}",
        "X-Workspace-Id": workspace["id"],
    }
    readable = await client.get("/api/v1/contents", headers=viewer_headers)
    assert readable.status_code == 200

    denied = await client.post(
        "/api/v1/contents", json={"caption": "nope", "content_type": "IMAGE"}, headers=viewer_headers
    )
    assert denied.status_code == 403
    assert denied.json()["code"] == "permission_denied"


@pytest.mark.asyncio
async def test_member_invitation_flow(client, make_user):
    tokens = await register_and_login(client, "inviter@example.com")
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    workspace = await create_workspace(client, headers)

    invited = await client.post(
        f"/api/v1/workspaces/{workspace['id']}/invitations",
        json={"email": "newbie@example.com", "role_code": "EDITOR"},
        headers=headers,
    )
    assert invited.status_code == 201
    invite_token = invited.json()["token"]

    newbie = await make_user(email="newbie@example.com")
    newbie_login = await client.post(
        "/api/v1/auth/login", json={"email": newbie.email, "password": PASSWORD}
    )
    newbie_headers = {"Authorization": f"Bearer {newbie_login.json()['access_token']}"}
    accepted = await client.post(
        f"/api/v1/workspaces/invitations/accept?token={invite_token}", headers=newbie_headers
    )
    assert accepted.status_code == 200
    assert accepted.json()["role_code"] == "EDITOR"

    members = await client.get(
        f"/api/v1/workspaces/{workspace['id']}/members", headers=headers
    )
    assert len(members.json()) == 2


@pytest.mark.asyncio
async def test_media_upload_rejects_a_non_image(client, make_user, make_workspace, tenant_context):
    user = await make_user()
    workspace = await make_workspace(user.id)
    ctx = tenant_context(user, workspace)
    client.override_context(ctx)
    client.override_user(user)

    response = await client.post(
        "/api/v1/media?kind=IMAGE",
        files={"file": ("evil.svg", b"<svg onload=alert(1)/>", "image/svg+xml")},
    )
    assert response.status_code == 415
    assert response.json()["code"] == "unsupported_media_type"


# A minimal but structurally valid JPEG so magic-byte sniffing succeeds.
JPEG_BYTES = (
    b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    b"\xff\xdb\x00C\x00" + bytes(range(64)) + b"\xff\xd9"
)


@pytest.mark.asyncio
async def test_media_upload_accepts_a_real_jpeg(client, make_user, make_workspace, tenant_context):
    user = await make_user()
    workspace = await make_workspace(user.id)
    client.override_user(user)
    client.override_context(tenant_context(user, workspace))

    response = await client.post(
        "/api/v1/media?kind=IMAGE",
        files={"file": ("photo.jpg", JPEG_BYTES, "image/jpeg")},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["detected_mime"] == "image/jpeg"
    assert body["size_bytes"] == len(JPEG_BYTES)


@pytest.mark.asyncio
async def test_content_crud_and_review_flow(client, make_user, make_workspace, make_media, tenant_context):
    user = await make_user()
    workspace = await make_workspace(user.id)
    asset = await make_media(workspace.id)
    client.override_user(user)
    headers = {"X-Workspace-Id": str(workspace.id)}
    client.override_context(tenant_context(user, workspace))

    created = await client.post(
        "/api/v1/contents",
        json={
            "caption": "Launch week",
            "content_type": "IMAGE",
            "media_asset_ids": [str(asset.id)],
            "hashtags": ["#launch", "launch", "#launch"],
        },
        headers=headers,
    )
    assert created.status_code == 201, created.text
    content = created.json()
    assert content["status"] == "DRAFT"
    assert content["hashtags"] == ["launch"], "hashtags must be de-duplicated and normalised"

    submitted = await client.post(f"/api/v1/contents/{content['id']}/submit", headers=headers)
    assert submitted.status_code == 200
    assert submitted.json()["status"] == "IN_REVIEW"

    approved = await client.post(
        f"/api/v1/contents/{content['id']}/approve",
        json={"status": "APPROVED", "comment": "looks good"},
        headers=headers,
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "APPROVED"

    listing = await client.get("/api/v1/contents?status=APPROVED", headers=headers)
    assert len(listing.json()) == 1

    # An illegal transition is refused by the state machine (not by schema
    # validation): ARCHIVED is a legal status but not a legal next state for
    # APPROVED content.
    bad = await client.patch(
        f"/api/v1/contents/{content['id']}", json={"status": "ARCHIVED"}, headers=headers
    )
    assert bad.status_code == 422, bad.text
    assert bad.json()["code"] == "validation_error"
    assert bad.json()["errors"][0]["code"] == "illegal_state_transition"


@pytest.mark.asyncio
async def test_publishing_job_is_queued_and_visible(
    client, make_user, make_workspace, make_account, make_media, make_content, tenant_context
):
    user = await make_user()
    workspace = await make_workspace(user.id)
    account = await make_account(workspace.id)
    asset = await make_media(workspace.id)
    content = await make_content(workspace.id, media_ids=[asset.id], status="APPROVED")
    client.override_user(user)
    client.override_context(tenant_context(user, workspace))
    headers = {"X-Workspace-Id": str(workspace.id)}

    queued = await client.post(
        "/api/v1/publishing/jobs",
        json={
            "content_id": str(content.id),
            "social_account_id": str(account.id),
            "idempotency_key": "api-key-1",
        },
        headers=headers,
    )
    assert queued.status_code == 202, queued.text
    job = queued.json()
    assert job["state"] == "QUEUED"

    again = await client.post(
        "/api/v1/publishing/jobs",
        json={
            "content_id": str(content.id),
            "social_account_id": str(account.id),
            "idempotency_key": "api-key-1",
        },
        headers=headers,
    )
    assert again.json()["id"] == job["id"], "the same idempotency key must return the same job"

    cancelled = await client.post(f"/api/v1/publishing/jobs/{job['id']}/cancel", headers=headers)
    assert cancelled.status_code == 200
    assert cancelled.json()["state"] == "CANCELLED"


@pytest.mark.asyncio
async def test_webhook_endpoint_rejects_a_bad_signature(client):
    body = json.dumps({"object": "instagram", "entry": []}).encode()
    response = await client.post(
        "/api/v1/webhooks/meta",
        content=body,
        headers={"X-Hub-Signature-256": "sha256=00" * 32, "Content-Type": "application/json"},
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_webhook_verification_endpoint(client):
    response = await client.get(
        "/api/v1/webhooks/meta",
        params={"mode": "subscribe", "verify_token": "test-verify-token", "challenge": "42"},
    )
    assert response.status_code == 200
    assert response.text == "42"


@pytest.mark.asyncio
async def test_openapi_spec_is_published(client):
    response = await client.get("/openapi.json")
    assert response.status_code == 200
    spec = response.json()
    assert spec["info"]["title"] == "InstaMind AI"
    paths = spec["paths"]
    for expected in (
        "/api/v1/auth/login",
        "/api/v1/workspaces",
        "/api/v1/social-accounts/connect",
        "/api/v1/contents",
        "/api/v1/publishing/jobs",
        "/api/v1/ai/generate-caption",
        "/api/v1/webhooks/meta",
    ):
        assert expected in paths, f"{expected} must be documented"
