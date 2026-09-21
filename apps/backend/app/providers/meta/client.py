"""Meta Graph API client (official endpoints only).

Design notes
------------
* **Transport is injectable.** Production uses ``httpx.AsyncClient``; tests use
  ``FakeTransport``. No test ever reaches the network, and no production code
  path depends on the fake.
* **No unofficial endpoints.** Every path here exists in Meta's documented
  Instagram Platform API. If Meta does not support an operation, it is not here.
* **appsecret_proof** is attached to every Graph call, as Meta requires for
  server-side calls made with a user access token.
* **Errors are classified** into transient (retry with backoff), rate-limited
  (honour ``Retry-After``/quota headers) and permanent (surface to the user).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlencode, urljoin

import httpx

from app.core.config import settings
from app.core.exceptions import MetaAPIError
from app.core.logging import logger
from app.core.security import constant_time_equals, redact_secrets

# Meta error codes we branch on. Documented at
# developers.facebook.com/docs/graph-api/guides/error-handling
CODE_TOKEN_EXPIRED = 190
CODE_PERMISSION_DENIED = {10, 200, 201, 202, 203, 204, 205}
CODE_RATE_LIMIT = {4, 17, 32, 613, 80004}
CODE_DUPLICATE_POST = 2207046
CODE_TRANSIENT = {1, 2, 368, 803, 1205, 1609}

RETRYABLE_SUBCODES = {459, 460, 463, 464, 467}  # token invalidation subcodes


@dataclass
class GraphResponse:
    status_code: int
    json: dict[str, Any]
    headers: dict[str, str] = field(default_factory=dict)
    elapsed_ms: int = 0

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    def header_int(self, name: str) -> int | None:
        raw = self.headers.get(name) or self.headers.get(name.lower())
        try:
            return int(raw) if raw is not None else None
        except ValueError:
            return None


class Transport(Protocol):
    async def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> GraphResponse: ...

    async def aclose(self) -> None: ...


class HttpxTransport:
    def __init__(self, timeout: float = 30.0, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(timeout=timeout, follow_redirects=False)

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> GraphResponse:
        started = time.perf_counter()
        response = await self._client.request(method, url, params=params, json=json_body)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        try:
            payload = response.json()
            if not isinstance(payload, dict):
                payload = {"data": payload}
        except (json.JSONDecodeError, ValueError):
            payload = {"raw": response.text[:2000]}
        return GraphResponse(
            status_code=response.status_code,
            json=payload,
            headers=dict(response.headers),
            elapsed_ms=elapsed_ms,
        )

    async def aclose(self) -> None:
        await self._client.aclose()


class FakeTransport:
    """Deterministic transport for tests. Records every call."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._responses: list[GraphResponse] = []
        self._default = GraphResponse(status_code=200, json={})

    def enqueue(self, response: GraphResponse | dict[str, Any], *, status_code: int = 200) -> None:
        if isinstance(response, GraphResponse):
            self._responses.append(response)
        else:
            self._responses.append(GraphResponse(status_code=status_code, json=response))

    def enqueue_error(
        self,
        message: str,
        *,
        code: int,
        subcode: int | None = None,
        error_type: str = "OAuthException",
        status_code: int = 400,
    ) -> None:
        self._responses.append(
            GraphResponse(
                status_code=status_code,
                json={
                    "error": {
                        "message": message,
                        "type": error_type,
                        "code": code,
                        "error_subcode": subcode,
                    }
                },
            )
        )

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> GraphResponse:
        self.calls.append({"method": method, "url": url, "params": params, "json": json_body})
        if self._responses:
            return self._responses.pop(0)
        return self._default

    async def aclose(self) -> None:  # pragma: no cover - no-op
        return None

    @property
    def last_call(self) -> dict[str, Any]:
        return self.calls[-1]


def appsecret_proof(access_token: str, app_secret: str) -> str:
    """HMAC-SHA256(app_secret, access_token) — required by Meta for user tokens."""
    return hmac.new(app_secret.encode(), access_token.encode(), hashlib.sha256).hexdigest()


class MetaGraphClient:
    """Thin, typed wrapper over the Graph API."""

    def __init__(
        self,
        transport: Transport | None = None,
        *,
        access_token: str,
        app_secret: str | None = None,
        api_version: str | None = None,
        graph_base: str | None = None,
        ig_base: str | None = None,
    ) -> None:
        if not access_token:
            raise ValueError("access_token is required")
        self._transport = transport or HttpxTransport()
        self._access_token = access_token
        self._app_secret = app_secret or settings.META_APP_SECRET.get_secret_value()
        self.api_version = api_version or settings.META_API_VERSION
        self.graph_base = (graph_base or settings.META_GRAPH_BASE_URL).rstrip("/")
        self.ig_base = (ig_base or settings.META_IG_BASE_URL).rstrip("/")

    # ------------------------------------------------------------ primitives
    def _base_for(self, path: str) -> str:
        # Instagram Login tokens must call graph.instagram.com; Facebook Login
        # tokens call graph.facebook.com.
        if path.startswith("ig:"):
            return self.ig_base
        return self.graph_base

    async def _call(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        clean_path = path[3:] if path.startswith("ig:") else path
        base = self._base_for(path)
        url = f"{base}/{self.api_version}/{clean_path.lstrip('/')}"

        query: dict[str, Any] = dict(params or {})
        if self._app_secret:
            query["appsecret_proof"] = appsecret_proof(self._access_token, self._app_secret)

        response = await self._transport.request(
            method, url, params=query, json_body=json_body
        )
        logger.debug(
            "meta_api_call",
            method=method,
            path=clean_path,
            status=response.status_code,
            elapsed_ms=response.elapsed_ms,
        )
        if not response.ok or "error" in response.json:
            raise self._build_error(response)
        # Surface quota headers for the caller's accounting.
        if isinstance(response.json, dict):
            response.json.setdefault("_meta_headers", {
                "x-app-usage": response.header_int("x-app-usage"),
                "x-business-use-case-usage": response.headers.get("x-business-use-case-usage"),
                "x-ad-account-usage": response.header_int("x-ad-account-usage"),
                "retry-after": response.header_int("retry-after"),
            })
        return response.json

    def _build_error(self, response: GraphResponse) -> MetaAPIError:
        error = response.json.get("error") if isinstance(response.json, dict) else None
        if not isinstance(error, dict):
            return MetaAPIError(
                f"Unexpected response from Meta (HTTP {response.status_code}).",
                http_status=response.status_code,
            )
        code = error.get("code")
        subcode = error.get("error_subcode")
        message = error.get("message", "Unknown Meta API error")
        error_type = error.get("type")
        transient = bool(
            (code in CODE_RATE_LIMIT or code in CODE_TRANSIENT)
            or (code == CODE_TOKEN_EXPIRED and subcode in RETRYABLE_SUBCODES)
        )
        if code in CODE_RATE_LIMIT:
            logger.warning("meta_rate_limited", code=code, message=message)
        return MetaAPIError(
            message,
            meta_code=code,
            meta_subcode=subcode,
            error_type=error_type,
            http_status=response.status_code,
            is_transient=transient,
            context={"retryable": transient},
        )

    async def aclose(self) -> None:
        await self._transport.aclose()

    # --------------------------------------------------------------- profiles
    async def get_ig_user(self, ig_user_id: str, *, fields: str | None = None) -> dict[str, Any]:
        default_fields = (
            "id,username,name,profile_picture_url,media_count,"
            "followers_count,follows_count,is_published_user,account_type"
        )
        return await self._call(
            "GET",
            ig_user_id,
            params={"fields": fields or default_fields, "access_token": self._access_token},
        )

    async def get_me(self, *, fields: str = "id,name,email") -> dict[str, Any]:
        return await self._call("GET", "me", params={"fields": fields, "access_token": self._access_token})

    async def get_pages(self) -> list[dict[str, Any]]:
        data = await self._call(
            "GET",
            "me/accounts",
            params={"fields": "id,name,access_token,instagram_business_account,category"},
        )
        return data.get("data", [])

    async def get_page_instagram_account(self, page_id: str) -> dict[str, Any]:
        return await self._call(
            "GET",
            f"{page_id}",
            params={"fields": "instagram_business_account{id,username,profile_picture_url,media_count,account_type}"},
        )

    async def debug_token(self, input_token: str, *, app_token: str) -> dict[str, Any]:
        return await self._call(
            "GET",
            "debug_token",
            params={"input_token": input_token, "access_token": app_token},
        )

    # ----------------------------------------------------------- oauth helpers
    async def exchange_code_for_token(
        self,
        *,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        code: str,
        base: str = "graph",
        code_verifier: str | None = None,
    ) -> dict[str, Any]:
        """Step 2 of the official OAuth code flow."""
        host = self.ig_base if base == "ig" else self.graph_base
        url = f"{host}/{self.api_version}/oauth/access_token"
        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code": code,
        }
        body = {"client_secret": client_secret}
        if code_verifier:
            body["code_verifier"] = code_verifier
        response = await self._transport.request("POST", url, params=params, json_body=body)
        if not response.ok or "error" in response.json:
            raise self._build_error(response)
        return response.json

    async def exchange_for_long_lived_token(
        self,
        *,
        client_id: str,
        client_secret: str,
        short_lived_token: str,
        base: str = "graph",
    ) -> dict[str, Any]:
        host = self.ig_base if base == "ig" else self.graph_base
        url = f"{host}/{self.api_version}/oauth/access_token"
        response = await self._transport.request(
            "GET",
            url,
            params={
                "grant_type": "fb_exchange_token",
                "client_id": client_id,
                "client_secret": client_secret,
                "fb_exchange_token": short_lived_token,
            },
        )
        if not response.ok or "error" in response.json:
            raise self._build_error(response)
        return response.json

    async def refresh_long_lived_token(self, *, client_id: str, client_secret: str, token: str) -> dict[str, Any]:
        """Instagram Login long-lived tokens are refreshed (not re-exchanged)
        and must be refreshed at least once every 60 days."""
        url = f"{self.ig_base}/{self.api_version}/refresh_access_token"
        response = await self._transport.request(
            "GET",
            url,
            params={"grant_type": "ig_refresh_token", "access_token": token},
        )
        if not response.ok or "error" in response.json:
            raise self._build_error(response)
        return response.json

    # ------------------------------------------------------------- publishing
    async def create_media_container(
        self, ig_user_id: str, *, params: dict[str, Any]
    ) -> dict[str, Any]:
        """POST /{ig-user-id}/media — creates a container (step 1 of 2)."""
        payload = {**params, "access_token": self._access_token}
        return await self._call("POST", f"ig:{ig_user_id}/media", json_body=payload)

    async def publish_container(self, ig_user_id: str, *, creation_id: str) -> dict[str, Any]:
        """POST /{ig-user-id}/media_publish — step 2 of 2."""
        payload = {"creation_id": creation_id, "access_token": self._access_token}
        return await self._call("POST", f"ig:{ig_user_id}/media_publish", json_body=payload)

    async def get_container_status(self, container_id: str) -> dict[str, Any]:
        return await self._call(
            "GET",
            f"ig:{container_id}",
            params={"fields": "status_code,status", "access_token": self._access_token},
        )

    async def get_media(self, media_id: str, *, fields: str | None = None) -> dict[str, Any]:
        default_fields = "id,permalink,media_type,media_url,caption,timestamp,shortcode,comments_count,like_count"
        return await self._call(
            "GET",
            f"ig:{media_id}",
            params={"fields": fields or default_fields, "access_token": self._access_token},
        )

    async def get_content_publishing_limit(self, ig_user_id: str) -> dict[str, Any]:
        """Meta's own quota endpoint. We use it to double-check our counter."""
        return await self._call(
            "GET",
            f"ig:{ig_user_id}/content_publishing_limit",
            params={
                "fields": "quota_usage,config",
                "access_token": self._access_token,
            },
        )

    # -------------------------------------------------------------- comments
    async def list_media_comments(self, media_id: str, *, after: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {
            "fields": "id,text,timestamp,username,like_count,from{id,username}",
            "access_token": self._access_token,
        }
        if after:
            params["after"] = after
        return await self._call("GET", f"ig:{media_id}/comments", params=params)

    async def reply_to_comment(self, comment_id: str, *, message: str) -> dict[str, Any]:
        return await self._call(
            "POST",
            f"ig:{comment_id}/replies",
            json_body={"message": message, "access_token": self._access_token},
        )

    async def hide_comment(self, comment_id: str, *, hide: bool = True) -> dict[str, Any]:
        return await self._call(
            "POST",
            f"ig:{comment_id}",
            json_body={"hide": hide, "access_token": self._access_token},
        )

    # ------------------------------------------------------------- messaging
    async def list_conversations(self, ig_user_id: str, *, after: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {
            "fields": (
                "participants,updated_time,message_count,"
                "messages{message,id,from,created_time,text,attachments,is_read,is_deleted}"
            ),
            "access_token": self._access_token,
            "platform": "instagram",
        }
        if after:
            params["after"] = after
        return await self._call("GET", f"ig:{ig_user_id}/conversations", params=params)

    async def send_message(
        self,
        recipient_ig_id: str,
        *,
        message: dict[str, Any],
        tag: str | None = None,
    ) -> dict[str, Any]:
        """Send API. ``tag`` is only ever ``HUMAN_AGENT`` in this codebase, and
        only for human-authored replies (Meta forbids bot use)."""
        body: dict[str, Any] = {
            "recipient": {"id": recipient_ig_id},
            "message": message,
            "access_token": self._access_token,
        }
        if tag:
            body["tag"] = tag
        return await self._call("POST", "ig:me/messages", json_body=body)

    async def send_private_reply(self, comment_id: str, *, message: str) -> dict[str, Any]:
        return await self._call(
            "POST",
            f"ig:{comment_id}/private_replies",
            json_body={"message": message, "access_token": self._access_token},
        )

    # --------------------------------------------------------------- insights
    async def get_insights(
        self,
        node_id: str,
        *,
        metric: list[str],
        period: str | None = None,
        since: int | None = None,
        until: int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "metric": ",".join(metric),
            "access_token": self._access_token,
        }
        if period:
            params["period"] = period
        if since:
            params["since"] = since
        if until:
            params["until"] = until
        return await self._call("GET", f"ig:{node_id}/insights", params=params)

    # -------------------------------------------------------------- webhooks
    @staticmethod
    def verify_webhook_signature(payload: bytes, signature_header: str, app_secret: str) -> bool:
        """Validate ``X-Hub-Signature-256: sha256=<hex>``."""
        if not signature_header.startswith("sha256="):
            return False
        provided = signature_header.split("=", 1)[1]
        expected = hmac.new(app_secret.encode(), payload, hashlib.sha256).hexdigest()
        return constant_time_equals(expected, provided)

    @staticmethod
    def verify_challenge(verify_token: str, mode: str, token: str, challenge: str) -> str | None:
        """GET verification handshake. Returns the challenge or ``None``."""
        if mode != "subscribe":
            return None
        if not constant_time_equals(verify_token, token):
            return None
        return challenge


def build_authorize_url(
    *,
    api_path: str,
    client_id: str,
    redirect_uri: str,
    state: str,
    scopes: list[str],
    code_challenge: str | None = None,
) -> str:
    """Step 1 of the OAuth flow for either official path."""
    if api_path == "INSTAGRAM_LOGIN":
        url = "https://api.instagram.com/oauth/authorize"
        params: dict[str, Any] = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": ",".join(scopes),
            "response_type": "code",
            "state": state,
        }
        if code_challenge:
            params["code_challenge"] = code_challenge
            params["code_challenge_method"] = "S256"
    else:
        url = "https://www.facebook.com/v23.0/dialog/oauth"
        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": ",".join(scopes),
            "response_type": "code",
            "state": state,
            "auth_type": "rerequest",
        }
    return f"{url}?{urlencode(params)}"


def redact_call(call: dict[str, Any]) -> dict[str, Any]:
    """Safe representation of a recorded transport call for logs/tests."""
    return redact_secrets(
        {
            "method": call.get("method"),
            "url": call.get("url"),
            "params": {
                key: ("[redacted]" if "token" in key or "proof" in key else value)
                for key, value in (call.get("params") or {}).items()
            },
            "json": {
                key: ("[redacted]" if "token" in key else value)
                for key, value in (call.get("json") or {}).items()
            },
        }
    )


__all__ = [
    "CODE_DUPLICATE_POST",
    "CODE_RATE_LIMIT",
    "CODE_TOKEN_EXPIRED",
    "FakeTransport",
    "GraphResponse",
    "HttpxTransport",
    "MetaGraphClient",
    "appsecret_proof",
    "build_authorize_url",
    "redact_call",
    "urljoin",
]
