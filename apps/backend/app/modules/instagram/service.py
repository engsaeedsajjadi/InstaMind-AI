"""Instagram connection service (OAuth, token lifecycle, disconnect).

Supported official paths
------------------------
``INSTAGRAM_LOGIN``  Instagram API with Instagram Login (Business Login).
                     No Facebook Page required. Long-lived tokens are
                     *refreshed* every ≤60 days via ``refresh_access_token``.
``FACEBOOK_LOGIN``   Instagram API with Facebook Login. Requires the account to
                     be linked to a Facebook Page; tokens are exchanged to
                     long-lived and then refreshed. Superset of capabilities
                     (Business Discovery, hashtag search, product tagging).

Everything else (password login, session cookies, browser automation) is out of
scope by design and is not implemented anywhere in this repository.
"""

from __future__ import annotations

import hashlib
import secrets
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
    ExternalServiceError,
    MetaAPIError,
    NotFoundError,
    ValidationError,
)
from app.core.logging import logger
from app.db.base import is_past
from app.modules.audit.service import AuditService
from app.modules.instagram.models import MetaAPIUsage, OAuthState, OAuthToken, SocialAccount
from app.modules.tenants.scope import TenantScope

# Documented permission sets per official path.
SCOPES_INSTAGRAM_LOGIN = [
    "instagram_business_basic",
    "instagram_business_content_publish",
    "instagram_business_manage_comments",
    "instagram_business_manage_messages",
    "instagram_business_manage_insights",
]
SCOPES_FACEBOOK_LOGIN = [
    "instagram_basic",
    "instagram_content_publish",
    "instagram_manage_comments",
    "instagram_manage_messages",
    "instagram_manage_insights",
    "pages_show_list",
    "pages_read_engagement",
]

STATE_TTL_MINUTES = 10
LONG_LIVED_TOKEN_DAYS = 60
# Refresh this many days before expiry so a gap never occurs.
REFRESH_SAFETY_MARGIN_DAYS = 7


@dataclass(frozen=True)
class ConnectInitiation:
    authorize_url: str
    state: str
    api_path: str


def _fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:16]


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)[:128]
    challenge = hashlib.sha256(verifier.encode()).digest()
    import base64

    return verifier, base64.urlsafe_b64encode(challenge).decode().rstrip("=")


class InstagramConnectService:
    def __init__(
        self,
        session: AsyncSession,
        scope: TenantScope,
        *,
        audit: AuditService | None = None,
        client_factory=None,
    ) -> None:
        self.session = session
        self.scope = scope
        self.audit = audit or AuditService(session)
        self._make_client = client_factory

    # ------------------------------------------------------------- step 1
    async def begin_connect(
        self,
        *,
        api_path: str,
        ip_address: str | None = None,
        requested_scopes: list[str] | None = None,
    ) -> ConnectInitiation:
        from app.providers.meta.client import build_authorize_url

        api_path = api_path.upper()
        if api_path not in {"INSTAGRAM_LOGIN", "FACEBOOK_LOGIN"}:
            raise ValidationError("Unsupported Meta authorization path.")

        state = secrets.token_urlsafe(32)
        code_verifier: str | None = None
        code_challenge: str | None = None
        if api_path == "INSTAGRAM_LOGIN":
            code_verifier, code_challenge = _pkce_pair()

        scopes = requested_scopes or (
            SCOPES_INSTAGRAM_LOGIN if api_path == "INSTAGRAM_LOGIN" else SCOPES_FACEBOOK_LOGIN
        )

        row = OAuthState(
            state=state,
            provider="META",
            api_path=api_path,
            code_verifier=code_verifier,
            requested_scopes=scopes,
            initiated_by=self.scope.context.user_id,
            ip_address=ip_address,
            expires_at=datetime.now(UTC) + timedelta(minutes=STATE_TTL_MINUTES),
            workspace_id=self.scope.workspace_id,
        )
        self.session.add(row)
        await self.session.flush()

        client_id = (
            settings.META_INSTAGRAM_CLIENT_ID
            if api_path == "INSTAGRAM_LOGIN"
            else settings.META_FACEBOOK_CLIENT_ID
        )
        redirect_uri = (
            settings.META_REDIRECT_URI_INSTAGRAM
            if api_path == "INSTAGRAM_LOGIN"
            else settings.META_REDIRECT_URI_FACEBOOK
        )
        if not client_id:
            raise ValidationError(
                "Instagram connection is not configured on this server "
                f"(missing META_{api_path.replace('_LOGIN', '')}_CLIENT_ID)."
            )
        url = build_authorize_url(
            api_path=api_path,
            client_id=client_id,
            redirect_uri=redirect_uri,
            state=state,
            scopes=scopes,
            code_challenge=code_challenge,
        )
        logger.info(
            "instagram_oauth_started", api_path=api_path, workspace_id=str(self.scope.workspace_id)
        )
        return ConnectInitiation(authorize_url=url, state=state, api_path=api_path)

    async def _consume_state(self, state: str) -> OAuthState:
        row = (
            await self.session.execute(select(OAuthState).where(OAuthState.state == state))
        ).scalar_one_or_none()
        if row is None or row.consumed_at is not None or is_past(row.expires_at):
            raise AuthenticationError("This connection attempt has expired. Please start again.")
        if row.workspace_id != self.scope.workspace_id:
            raise AuthenticationError("This connection attempt belongs to another workspace.")
        row.consumed_at = datetime.now(UTC)
        return row

    # ------------------------------------------------------------- step 2
    async def complete_connect(
        self,
        *,
        state: str,
        code: str,
        error: str | None = None,
        error_reason: str | None = None,
    ) -> SocialAccount:
        state_row = await self._consume_state(state)
        if error:
            state_row.result = "FAILED"
            state_row.error = (error_reason or error)[:255]
            await self.session.flush()
            raise AuthenticationError(f"Instagram authorization failed: {error}")

        client_id, client_secret, redirect_uri = self._oauth_config(state_row.api_path)
        client = self._client_factory(access_token="oauth-exchange")

        token_payload = await client.exchange_code_for_token(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            code=code,
            base="ig" if state_row.api_path == "INSTAGRAM_LOGIN" else "graph",
            code_verifier=state_row.code_verifier,
        )
        short_lived_token = token_payload.get("access_token")
        if not short_lived_token:
            raise ExternalServiceError("Meta did not return an access token.")
        ig_user_id = str(token_payload.get("user_id") or "")

        # Promote to a long-lived token so the connection survives >1 hour.
        long_lived = await client.exchange_for_long_lived_token(
            client_id=client_id,
            client_secret=client_secret,
            short_lived_token=short_lived_token,
            base="ig" if state_row.api_path == "INSTAGRAM_LOGIN" else "graph",
        )
        access_token = long_lived.get("access_token") or short_lived_token
        expires_in = int(long_lived.get("expires_in") or 0)
        expires_at = (
            datetime.now(UTC) + timedelta(seconds=expires_in)
            if expires_in
            else datetime.now(UTC) + timedelta(days=LONG_LIVED_TOKEN_DAYS)
        )

        profile_client = self._client_factory(access_token=access_token)
        account_payload = await self._resolve_profile(
            profile_client, api_path=state_row.api_path, ig_user_id=ig_user_id
        )
        state_row.result = "SUCCESS"

        account = await self._upsert_account(
            payload=account_payload,
            api_path=state_row.api_path,
            scopes=state_row.requested_scopes,
            access_token=access_token,
            expires_at=expires_at,
        )
        await self.audit.record(
            action="social_account.connected",
            workspace_id=self.scope.workspace_id,
            actor_id=self.scope.context.user_id,
            resource_type="social_account",
            resource_id=str(account.id),
            after={
                "username": account.username,
                "api_path": account.api_path,
                "account_type": account.account_type,
            },
        )
        await self.session.flush()
        return account

    def _oauth_config(self, api_path: str) -> tuple[str, str, str]:
        if api_path == "INSTAGRAM_LOGIN":
            client_id = settings.META_INSTAGRAM_CLIENT_ID
            client_secret = settings.META_INSTAGRAM_CLIENT_SECRET.get_secret_value()
            redirect_uri = settings.META_REDIRECT_URI_INSTAGRAM
        else:
            client_id = settings.META_FACEBOOK_CLIENT_ID
            client_secret = settings.META_FACEBOOK_CLIENT_SECRET.get_secret_value()
            redirect_uri = settings.META_REDIRECT_URI_FACEBOOK
        if not client_id or not client_secret:
            raise ValidationError("Instagram connection is not configured on this server.")
        return client_id, client_secret, redirect_uri

    async def _resolve_profile(
        self, client, *, api_path: str, ig_user_id: str
    ) -> dict:
        if api_path == "INSTAGRAM_LOGIN" and ig_user_id:
            try:
                return await client.get_ig_user(ig_user_id)
            except MetaAPIError as exc:
                raise ExternalServiceError(f"Could not read the Instagram profile: {exc.detail}") from exc

        # Facebook Login: find the Page that owns the IG business account.
        pages = await client.get_pages()
        if not pages:
            raise ValidationError(
                "No Facebook Page was found for this login. Link your Instagram "
                "professional account to a Facebook Page, or connect using "
                "Instagram Login instead."
            )
        for page in pages:
            business = page.get("instagram_business_account") or {}
            ig_id = business.get("id")
            if not ig_id:
                continue
            detail = await client.get_ig_user(str(ig_id))
            detail["_facebook_page_id"] = page.get("id")
            detail["_facebook_page_name"] = page.get("name")
            return detail
        raise ValidationError(
            "None of your Facebook Pages is linked to an Instagram professional "
            "account. Switch the account to Business/Creator in the Instagram app "
            "and link it to a Page first."
        )

    async def _upsert_account(
        self,
        *,
        payload: dict,
        api_path: str,
        scopes: list[str],
        access_token: str,
        expires_at: datetime,
    ) -> SocialAccount:
        external_id = str(payload.get("id") or "")
        if not external_id:
            raise ExternalServiceError("Meta did not return an Instagram user id.")

        account_type = str(payload.get("account_type") or payload.get("type") or "").upper() or None
        capabilities = derive_capabilities(account_type=account_type, scopes=scopes)

        existing = (
            await self.session.execute(
                select(SocialAccount).where(SocialAccount.external_account_id == external_id)
            )
        ).scalar_one_or_none()

        if existing is not None and existing.workspace_id != self.scope.workspace_id:
            raise ConflictError(
                "This Instagram account is already connected to another workspace. "
                "Disconnect it there first."
            )

        if existing is None:
            existing = SocialAccount(
                workspace_id=self.scope.workspace_id,
                platform="INSTAGRAM",
                api_path=api_path,
                external_account_id=external_id,
                username=payload.get("username") or "unknown",
                connected_by=self.scope.context.user_id,
                created_by=self.scope.context.user_id,
            )
            self.session.add(existing)
            await self.session.flush()

        existing.username = payload.get("username") or existing.username
        existing.display_name = payload.get("name") or existing.display_name
        existing.profile_picture_url = payload.get("profile_picture_url")
        existing.account_type = account_type or existing.account_type
        existing.media_count = payload.get("media_count")
        existing.followers_count = payload.get("followers_count")
        existing.follows_count = payload.get("follows_count")
        existing.capabilities = capabilities
        existing.granted_scopes = scopes
        existing.facebook_page_id = payload.get("_facebook_page_id") or existing.facebook_page_id
        existing.facebook_page_name = payload.get("_facebook_page_name") or existing.facebook_page_name
        existing.status = "CONNECTED"
        existing.status_reason = None
        existing.connected_at = datetime.now(UTC)
        existing.last_synced_at = datetime.now(UTC)
        existing.disconnected_at = None
        existing.deleted_at = None
        existing.updated_by = self.scope.context.user_id
        await self.session.flush()

        await self._store_token(
            account=existing,
            access_token=access_token,
            scopes=scopes,
            expires_at=expires_at,
        )
        return existing

    async def _store_token(
        self,
        *,
        account: SocialAccount,
        access_token: str,
        scopes: list[str],
        expires_at: datetime | None,
    ) -> OAuthToken:
        cipher = security.get_cipher()
        # Invalidate previous tokens for this account (one active credential).
        await self.session.execute(
            update(OAuthToken)
            .where(
                OAuthToken.social_account_id == account.id,
                OAuthToken.is_revoked.is_(False),
            )
            .values(is_revoked=True, revoked_at=datetime.now(UTC))
        )
        row = OAuthToken(
            workspace_id=self.scope.workspace_id,
            social_account_id=account.id,
            provider="META",
            token_type="USER",
            access_token_encrypted="pending",
            access_token_fingerprint=_fingerprint(access_token),
            scopes=scopes,
            token_kind="LONG_LIVED",
            obtained_at=datetime.now(UTC),
            expires_at=expires_at,
            last_refreshed_at=datetime.now(UTC),
            last_validated_at=datetime.now(UTC),
        )
        self.session.add(row)
        await self.session.flush()
        # AAD binds the ciphertext to this exact row: copying it elsewhere fails.
        row.access_token_encrypted = cipher.encrypt(
            access_token, aad=f"oauth_token:{row.id}"
        )
        await self.session.flush()
        return row

    # ------------------------------------------------------- token retrieval
    async def get_active_token(self, account: SocialAccount) -> str:
        row = (
            await self.session.execute(
                select(OAuthToken)
                .where(
                    OAuthToken.social_account_id == account.id,
                    OAuthToken.is_revoked.is_(False),
                )
                .order_by(OAuthToken.created_at.desc())
            )
        ).scalars().first()
        if row is None:
            raise NotFoundError("No active token for this account. Reconnect it.")
        if is_past(row.expires_at):
            raise AuthenticationError(
                "The Instagram connection has expired. Please reconnect the account."
            )
        return security.get_cipher().decrypt(
            row.access_token_encrypted, aad=f"oauth_token:{row.id}"
        )

    async def refresh_if_needed(self, account: SocialAccount) -> bool:
        """Refresh long-lived tokens before they lapse.

        Instagram Login tokens live 60 days and must be refreshed; Facebook Login
        long-lived user tokens also lapse at ~60 days. We refresh inside the
        safety margin and record the new expiry.
        """
        row = (
            await self.session.execute(
                select(OAuthToken)
                .where(
                    OAuthToken.social_account_id == account.id,
                    OAuthToken.is_revoked.is_(False),
                )
                .order_by(OAuthToken.created_at.desc())
            )
        ).scalars().first()
        if row is None or row.expires_at is None:
            return False
        threshold = datetime.now(UTC) + timedelta(days=REFRESH_SAFETY_MARGIN_DAYS)
        from app.db.base import as_utc

        if as_utc(row.expires_at) > threshold:
            return False

        client_id, client_secret, _ = self._oauth_config(account.api_path)
        current = security.get_cipher().decrypt(
            row.access_token_encrypted, aad=f"oauth_token:{row.id}"
        )
        client = self._client_factory(access_token=current)
        try:
            if account.api_path == "INSTAGRAM_LOGIN":
                payload = await client.refresh_long_lived_token(
                    client_id=client_id, client_secret=client_secret, token=current
                )
            else:
                payload = await client.exchange_for_long_lived_token(
                    client_id=client_id,
                    client_secret=client_secret,
                    short_lived_token=current,
                    base="graph",
                )
        except MetaAPIError as exc:
            account.status = "TOKEN_EXPIRED"
            account.status_reason = exc.detail[:255]
            account.last_error_at = datetime.now(UTC)
            await self.audit.record(
                action="social_account.token_refresh_failed",
                workspace_id=self.scope.workspace_id,
                actor_type="SYSTEM",
                resource_type="social_account",
                resource_id=str(account.id),
                outcome="FAILED",
                metadata={"meta_code": exc.meta_code},
            )
            await self.session.flush()
            return False

        new_token = payload.get("access_token")
        if not new_token:
            return False
        expires_in = int(payload.get("expires_in") or 0)
        row.access_token_encrypted = security.get_cipher().encrypt(
            new_token, aad=f"oauth_token:{row.id}"
        )
        row.access_token_fingerprint = _fingerprint(new_token)
        row.last_refreshed_at = datetime.now(UTC)
        row.expires_at = (
            datetime.now(UTC) + timedelta(seconds=expires_in)
            if expires_in
            else datetime.now(UTC) + timedelta(days=LONG_LIVED_TOKEN_DAYS)
        )
        if account.status == "TOKEN_EXPIRED":
            account.status = "CONNECTED"
            account.status_reason = None
        await self.audit.record(
            action="social_account.token_refreshed",
            workspace_id=self.scope.workspace_id,
            actor_type="SYSTEM",
            resource_type="social_account",
            resource_id=str(account.id),
            metadata={"expires_at": row.expires_at.isoformat()},
        )
        await self.session.flush()
        return True

    async def mark_token_error(self, account: SocialAccount, exc: MetaAPIError) -> None:
        account.status = "TOKEN_EXPIRED" if exc.meta_code == 190 else "ERROR"
        account.status_reason = exc.detail[:255]
        account.last_error_at = datetime.now(UTC)
        await self.session.flush()

    # ------------------------------------------------------------- disconnect
    async def disconnect(
        self, account_id: uuid.UUID, *, purge_platform_data: bool = True
    ) -> SocialAccount:
        """Disconnect and, per the data-retention policy, drop cached platform
        data (messages, comments, insights) belonging to that connection."""
        account = await self.scope.get_one(SocialAccount, account_id)
        now = datetime.now(UTC)
        await self.session.execute(
            update(OAuthToken)
            .where(OAuthToken.social_account_id == account.id, OAuthToken.is_revoked.is_(False))
            .values(is_revoked=True, revoked_at=now)
        )
        account.status = "DISCONNECTED"
        account.disconnected_at = now
        account.purge_requested_at = now if purge_platform_data else None
        account.updated_by = self.scope.context.user_id

        if purge_platform_data:
            from app.modules.inbox.models import (
                AnalyticsSnapshot,
                Comment,
                Conversation,
                Message,
            )

            for model in (Message, Comment, AnalyticsSnapshot):
                await self.session.execute(
                    update(model)
                    .where(model.workspace_id == self.scope.workspace_id)  # type: ignore[attr-defined]
                    .values(deleted_at=now)
                )
            await self.session.execute(
                update(Conversation)
                .where(Conversation.workspace_id == self.scope.workspace_id)
                .values(deleted_at=now)
            )
            account.purge_completed_at = now

        await self.audit.record(
            action="social_account.disconnected",
            workspace_id=self.scope.workspace_id,
            actor_id=self.scope.context.user_id,
            resource_type="social_account",
            resource_id=str(account.id),
            metadata={"purged": purge_platform_data, "username": account.username},
        )
        await self.session.flush()
        return account

    async def record_usage(
        self,
        *,
        account: SocialAccount | None,
        metric: str,
        amount: int = 1,
        meta_quota: dict | None = None,
    ) -> None:
        now = datetime.now(UTC)
        window_start = now.replace(minute=0, second=0, microsecond=0)
        if metric == "publish":
            window_start = now - timedelta(hours=24)
        row = MetaAPIUsage(
            workspace_id=self.scope.workspace_id,
            social_account_id=account.id if account else None,
            metric=metric,
            window_start=window_start,
            count=amount,
            meta_reported_quota_used=(meta_quota or {}).get("quota_usage"),
            meta_reported_quota_total=(meta_quota or {}).get("config", {}).get("quota_total"),
        )
        self.session.add(row)
        await self.session.flush()

    # ------------------------------------------------------------------ misc
    def _client_factory(self, access_token: str):
        if self._make_client is None:  # pragma: no cover - production default
            from app.providers.meta.client import MetaGraphClient

            return MetaGraphClient(access_token=access_token)
        return self._make_client(access_token=access_token)


def derive_capabilities(*, account_type: str | None, scopes: list[str]) -> dict[str, bool]:
    """Map account type + granted scopes to concrete UI/publishing capabilities.

    Stories are the important one: Meta allows Story publishing only for
    **Business** accounts, not Creator accounts. Publishing a Story for a
    Creator account fails at Meta, so we disable it up front.
    """
    scopes_set = {scope.lower() for scope in scopes}
    can_publish = bool(
        {"instagram_business_content_publish", "instagram_content_publish"} & scopes_set
    )
    is_business = (account_type or "").upper() == "BUSINESS"
    return {
        "content_publish": can_publish,
        "stories_publish": can_publish and is_business,
        "comments_manage": bool(
            {"instagram_business_manage_comments", "instagram_manage_comments"} & scopes_set
        ),
        "messages_manage": bool(
            {"instagram_business_manage_messages", "instagram_manage_messages"} & scopes_set
        ),
        "insights_read": bool(
            {"instagram_business_manage_insights", "instagram_manage_insights"} & scopes_set
        ),
        # Facebook Login only: Business Discovery / hashtag search / product tags
        "business_discovery": bool(
            {"instagram_basic", "pages_read_engagement"} & scopes_set
        ),
        "is_business_account": is_business,
        "is_creator_account": (account_type or "").upper() == "CREATOR",
    }
