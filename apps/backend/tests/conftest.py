"""Shared test fixtures.

Everything here runs against an isolated SQLite database per test — no network,
no Docker, no shared state. Production behaviour (Postgres, real Meta API,
OpenAI) is exercised through the same interfaces with fakes at the boundary.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

# Settings must be in the environment *before* app.core.config is imported,
# because the module builds a cached Settings instance at import time.
os.environ.setdefault("INSTAMIND_APP_ENV", "test")
os.environ.setdefault("INSTAMIND_SECRET_KEY", "test-secret-key-for-unit-tests-0123456789")
os.environ.setdefault("INSTAMIND_TOKEN_ENCRYPTION_KEY", "test-token-encryption-key-abcdef")
os.environ.setdefault("INSTAMIND_DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("INSTAMIND_LOG_JSON", "false")
os.environ.setdefault("INSTAMIND_AI_PROVIDER", "null")
os.environ.setdefault("INSTAMIND_STORAGE_BACKEND", "local")
os.environ.setdefault("INSTAMIND_LOCAL_STORAGE_ROOT", "/tmp/instamind-tests")
os.environ.setdefault("INSTAMIND_PUBLIC_MEDIA_BASE_URL", "http://localhost:8000/media")
os.environ.setdefault("INSTAMIND_META_APP_SECRET", "test-app-secret")
os.environ.setdefault("INSTAMIND_META_WEBHOOK_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("INSTAMIND_META_INSTAGRAM_CLIENT_ID", "test-ig-client")
os.environ.setdefault("INSTAMIND_META_INSTAGRAM_CLIENT_SECRET", "test-ig-secret")
os.environ.setdefault("INSTAMIND_META_FACEBOOK_CLIENT_ID", "test-fb-client")
os.environ.setdefault("INSTAMIND_META_FACEBOOK_CLIENT_SECRET", "test-fb-secret")

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core import security  # noqa: E402
from app.db import models as _all_models  # noqa: E402,F401  (populates metadata)
from app.db.base import Base  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_singletons():
    """Each test gets a fresh cipher and rate limiter."""
    security.reset_cipher()
    from app.core.rate_limit import reset_limiter
    from app.providers.ai.openai_provider import reset_ai_provider

    reset_limiter()
    reset_ai_provider()
    yield
    security.reset_cipher()
    reset_limiter()
    reset_ai_provider()


@pytest_asyncio.fixture
async def engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def session(engine) -> AsyncIterator[AsyncSession]:
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db_session:
        yield db_session


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #
@pytest.fixture
def make_user(session):
    from app.modules.identity.models import User
    from app.modules.identity.service import IdentityService

    async def _make(
        email: str | None = None,
        *,
        password: str = "Correct-Horse-Battery-9",
        is_superuser: bool = False,
        full_name: str = "Test User",
    ) -> User:
        service = IdentityService(session)
        user = await service.register(
            email=email or f"user-{uuid.uuid4().hex[:8]}@example.com",
            password=password,
            full_name=full_name,
        )
        if is_superuser:
            user.is_superuser = True
        user.email_verified_at = datetime.now(UTC)
        await session.flush()
        return user

    return _make


@pytest.fixture
def make_workspace(session):
    from app.modules.tenants.service import TenantService

    async def _make(owner_id: uuid.UUID, name: str = "Acme") -> object:
        service = TenantService(session)
        return await service.create_workspace(owner_id=owner_id, name=name)

    return _make


@pytest.fixture
def make_membership(session):
    from app.modules.tenants.models import WorkspaceMembership

    async def _make(workspace_id: uuid.UUID, user_id: uuid.UUID, role: str = "EDITOR"):
        membership = WorkspaceMembership(
            workspace_id=workspace_id,
            user_id=user_id,
            role_code=role,
            status="ACTIVE",
            joined_at=datetime.now(UTC),
        )
        session.add(membership)
        await session.flush()
        return membership

    return _make


@pytest.fixture
def make_account(session):
    from app.modules.instagram.models import OAuthToken, SocialAccount
    from app.modules.instagram.service import derive_capabilities

    async def _make(
        workspace_id: uuid.UUID,
        *,
        username: str = "acme_store",
        external_id: str | None = None,
        account_type: str = "BUSINESS",
        api_path: str = "INSTAGRAM_LOGIN",
        access_token: str = "IGQV-test-token",
        capabilities: dict | None = None,
        status: str = "CONNECTED",
    ) -> SocialAccount:
        external_id = external_id or f"1784{uuid.uuid4().int % 10_000_000}"
        scopes = [
            "instagram_business_basic",
            "instagram_business_content_publish",
            "instagram_business_manage_comments",
            "instagram_business_manage_messages",
            "instagram_business_manage_insights",
        ]
        account = SocialAccount(
            workspace_id=workspace_id,
            platform="INSTAGRAM",
            api_path=api_path,
            external_account_id=external_id,
            username=username,
            display_name=username.title(),
            account_type=account_type,
            capabilities=capabilities or derive_capabilities(account_type=account_type, scopes=scopes),
            granted_scopes=scopes,
            status=status,
            media_count=120,
            followers_count=5400,
        )
        session.add(account)
        await session.flush()

        cipher = security.get_cipher()
        token = OAuthToken(
            workspace_id=workspace_id,
            social_account_id=account.id,
            provider="META",
            token_type="USER",
            access_token_encrypted="pending",
            access_token_fingerprint="fp" + external_id[:6],
            scopes=scopes,
            token_kind="LONG_LIVED",
            expires_at=datetime.now(UTC) + timedelta(days=55),
        )
        session.add(token)
        await session.flush()
        token.access_token_encrypted = cipher.encrypt(access_token, aad=f"oauth_token:{token.id}")
        await session.flush()
        return account

    return _make


@pytest.fixture
def make_media(session):
    """A tiny, valid JPEG (magic bytes) so upload validation passes."""
    from app.modules.content.models import MediaAsset

    async def _make(
        workspace_id: uuid.UUID,
        *,
        kind: str = "IMAGE",
        mime: str = "image/jpeg",
        public_url: str | None = "https://cdn.example.com/a.jpg",
        size_bytes: int = 1024,
        width: int = 1080,
        height: int = 1350,
        duration_ms: int | None = None,
    ) -> MediaAsset:
        asset = MediaAsset(
            workspace_id=workspace_id,
            storage_backend="local",
            storage_key=f"image/{uuid.uuid4().hex}.jpg",
            filename="photo.jpg",
            content_type=mime,
            detected_mime=mime,
            size_bytes=size_bytes,
            checksum_sha256=uuid.uuid4().hex * 2,
            kind=kind,
            width=width,
            height=height,
            duration_ms=duration_ms,
            status="UPLOADED",
            public_url=public_url,
        )
        session.add(asset)
        await session.flush()
        return asset

    return _make


@pytest.fixture
def make_content(session):
    from app.modules.content.models import Content

    async def _make(
        workspace_id: uuid.UUID,
        *,
        media_ids: list[uuid.UUID] | None = None,
        content_type: str = "IMAGE",
        caption: str = "Hello world",
        status: str = "DRAFT",
        scheduled_at: datetime | None = None,
    ) -> Content:
        content = Content(
            workspace_id=workspace_id,
            title="Post",
            caption=caption,
            content_type=content_type,
            media_asset_ids=[str(mid) for mid in (media_ids or [])],
            media_order=[str(mid) for mid in (media_ids or [])],
            status=status,
            scheduled_at=scheduled_at,
            share_to_feed=True,
        )
        session.add(content)
        await session.flush()
        return content

    return _make


# --------------------------------------------------------------------------- #
# HTTP client
# --------------------------------------------------------------------------- #
@pytest_asyncio.fixture
async def client(engine, session) -> AsyncIterator[AsyncClient]:
    """An AsyncClient wired to the real ASGI app with the DB dependency
    overridden to the test session."""
    from app.api.deps import get_current_context, get_current_user, get_db_session
    from app.core.jwt import claims_from_payload, create_access_token
    from app.main import create_app

    app = create_app()

    async def _override_db() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_db_session] = _override_db

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http_client:
        # Expose helpers on the client so tests stay terse.
        http_client.app = app  # type: ignore[attr-defined]
        http_client.session = session  # type: ignore[attr-defined]
        http_client.create_access_token = create_access_token  # type: ignore[attr-defined]
        http_client.claims_from_payload = claims_from_payload  # type: ignore[attr-defined]
        http_client.override_user = lambda user: app.dependency_overrides.__setitem__(  # type: ignore[attr-defined]
            get_current_user, lambda: user
        )
        http_client.override_context = lambda ctx: app.dependency_overrides.__setitem__(  # type: ignore[attr-defined]
            get_current_context, lambda: ctx
        )
        yield http_client
    app.dependency_overrides.clear()


@pytest.fixture
def auth_headers():
    def _headers(user, workspace_id: uuid.UUID | None = None, *, roles: list[str] | None = None):
        from app.core.jwt import create_access_token
        from app.core.permissions import permissions_for_roles

        token, _jti, _exp = create_access_token(
            user_id=user.id,
            session_id=uuid.uuid4(),
            workspace_id=workspace_id,
            roles=roles or ["OWNER"],
            scopes=[],
            is_superuser=user.is_superuser,
        )
        headers = {"Authorization": f"Bearer {token}"}
        if workspace_id:
            headers["X-Workspace-Id"] = str(workspace_id)
        return headers, sorted(permission.value for permission in permissions_for_roles(roles or ["OWNER"]))

    return _headers


@pytest.fixture
def tenant_context():
    from app.core.permissions import permissions_for_roles
    from app.modules.tenants.scope import TenantContext

    def _make(user, workspace, role: str = "OWNER"):
        return TenantContext(
            user_id=user.id,
            workspace_id=workspace.id,
            roles=(role,),
            permissions=permissions_for_roles([role]),
        )

    return _make


@pytest.fixture
def fake_transport():
    from app.providers.meta.client import FakeTransport

    return FakeTransport()


@pytest.fixture
def meta_client_factory(fake_transport):
    """Builds MetaGraphClient instances that share one scripted transport."""
    from app.providers.meta.client import MetaGraphClient

    def _factory(access_token: str) -> MetaGraphClient:
        return MetaGraphClient(
            fake_transport,
            access_token=access_token,
            app_secret="test-app-secret",
            api_version="v23.0",
        )

    return _factory


def signed_webhook(payload_bytes: bytes, app_secret: str = "test-app-secret") -> str:
    import hashlib
    import hmac

    digest = hmac.new(app_secret.encode(), payload_bytes, hashlib.sha256).hexdigest()
    return f"sha256={digest}"
