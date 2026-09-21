"""Meta webhook ingestion.

Meta requires a webhook endpoint to answer within seconds, so this handler does
the *minimum* synchronously:

1. verify the ``X-Hub-Signature-256`` HMAC (constant-time),
2. reject payloads older than the replay window,
3. persist the event with a unique ``(platform, event_key)`` so a duplicate
   delivery is a no-op,
4. hand the heavy work to the queue and return ``200`` immediately.

Anything that touches Instagram's API happens in the worker, never here.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import AuthenticationError, ValidationError
from app.core.logging import logger
from app.modules.instagram.models import MetaWebhookEvent, SocialAccount
from app.providers.meta.client import MetaGraphClient

# Meta timestamps are epoch seconds. Events older than this are refused: a
# replayed old event must not trigger a fresh action.
REPLAY_WINDOW_SECONDS = 600
# Subscribed objects. Only these are accepted; anything else is ignored.
SUPPORTED_OBJECTS = {"instagram", "page", "user"}
# Change fields we actually process.
SUPPORTED_FIELDS = {
    # ``entry[].changes[].field`` values
    "comments", "mentions", "messages", "messaging_postbacks",
    "message_reactions", "message_deliveries", "message_reads",
    "message_echoes", "story_insights", "live_comments",
    # ``entry[].messaging[]`` sub-object keys, which is what
    # ``_change_type`` reports for the messaging shape
    "message", "postback", "reaction", "delivery", "read",
}


@dataclass(frozen=True)
class IngestResult:
    accepted: bool
    duplicate: bool
    event_id: str | None = None
    reason: str | None = None


def event_key_for(entry: dict, item_index: int) -> str:
    """Stable idempotency key for one webhook change item."""
    object_id = entry.get("id") or entry.get("time") or ""
    change = (entry.get("changes") or [{}])[item_index] if entry.get("changes") else {}
    messaging = (entry.get("messaging") or [{}])[item_index] if entry.get("messaging") else {}
    inner_id = (
        change.get("field")
        or messaging.get("message", {}).get("mid")
        or messaging.get("postback", {}).get("payload")
        or ""
    )
    raw_id = (
        (change.get("value") or {}).get("id")
        or (change.get("value") or {}).get("media_id")
        or messaging.get("message", {}).get("mid")
        or ""
    )
    return f"{object_id}:{inner_id}:{raw_id}:{item_index}"


class WebhookService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ----------------------------------------------------------- verification
    @staticmethod
    def verify_subscription(
        *, mode: str | None, verify_token: str | None, challenge: str | None
    ) -> str:
        expected = settings.META_WEBHOOK_VERIFY_TOKEN.get_secret_value()
        if not expected:
            raise ValidationError("Webhook verification is not configured on this server.")
        result = MetaGraphClient.verify_challenge(
            expected, mode or "", verify_token or "", challenge or ""
        )
        if result is None:
            logger.warning("webhook_verification_failed", mode=mode)
            raise AuthenticationError("Webhook verification failed.")
        return result

    @staticmethod
    def verify_signature(payload: bytes, signature_header: str | None) -> bool:
        app_secret = settings.META_APP_SECRET.get_secret_value()
        if not app_secret:
            # Without an app secret we cannot verify signatures; fail closed in
            # production, allow in dev so local testing is possible.
            return not settings.is_prod
        if not signature_header:
            return False
        return MetaGraphClient.verify_webhook_signature(payload, signature_header, app_secret)

    # --------------------------------------------------------------- ingest
    async def ingest(
        self, *, payload_bytes: bytes, signature_header: str | None = None
    ) -> list[IngestResult]:
        if not self.verify_signature(payload_bytes, signature_header):
            logger.warning("webhook_signature_invalid")
            raise AuthenticationError("Invalid webhook signature.")

        try:
            payload = json.loads(payload_bytes.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValidationError("Webhook payload is not valid JSON.") from exc
        if not isinstance(payload, dict):
            raise ValidationError("Webhook payload must be a JSON object.")

        object_type = str(payload.get("object") or "")
        if object_type not in SUPPORTED_OBJECTS:
            logger.info("webhook_object_ignored", object=object_type)
            return [IngestResult(accepted=False, duplicate=False, reason="unsupported_object")]

        body_hash = hashlib.sha256(payload_bytes).hexdigest()
        now = datetime.now(UTC)
        results: list[IngestResult] = []

        for entry in payload.get("entry") or []:
            account = await self._resolve_account(entry)
            workspace_id = account.workspace_id if account else None
            entry_time = entry.get("time")
            event_time = (
                datetime.fromtimestamp(int(entry_time), UTC) if entry_time else now
            )
            is_replay = (now - event_time).total_seconds() > REPLAY_WINDOW_SECONDS

            for index in range(max(len(entry.get("changes") or []), len(entry.get("messaging") or []), 1)):
                key = event_key_for(entry, index)
                change_type = self._change_type(entry, index)
                result = await self._persist(
                    payload=payload,
                    entry=entry,
                    index=index,
                    key=key,
                    object_type=object_type,
                    change_type=change_type,
                    body_hash=body_hash,
                    event_time=event_time,
                    signature=signature_header,
                    workspace_id=workspace_id,
                    account=account,
                    is_replay=is_replay,
                )
                results.append(result)
        return results

    @staticmethod
    def _change_type(entry: dict, index: int) -> str | None:
        changes = entry.get("changes") or []
        if index < len(changes):
            return str(changes[index].get("field") or "")
        messaging = entry.get("messaging") or []
        if index < len(messaging):
            item = messaging[index]
            for candidate in ("message", "postback", "reaction", "delivery", "read"):
                if candidate in item:
                    return candidate
        return None

    async def _resolve_account(self, entry: dict) -> SocialAccount | None:
        """Map the webhook entry to a stored account so the event is scoped to
        the right tenant (and cannot be used to inject data elsewhere)."""
        from sqlalchemy import select

        candidates = [
            str(entry.get("id") or ""),
            str((entry.get("messaging") or [{}])[0].get("recipient", {}).get("id") or ""),
            str((entry.get("changes") or [{}])[0].get("value", {}).get("from", {}).get("id") or ""),
        ]
        for candidate in {c for c in candidates if c and c != "None"}:
            account = (
                await self.session.execute(
                    select(SocialAccount).where(SocialAccount.external_account_id == candidate)
                )
            ).scalar_one_or_none()
            if account is not None:
                return account
        return None

    async def _persist(
        self,
        *,
        payload: dict,
        entry: dict,
        index: int,
        key: str,
        object_type: str,
        change_type: str | None,
        body_hash: str,
        event_time: datetime,
        signature: str | None,
        workspace_id,
        account: SocialAccount | None,
        is_replay: bool,
    ) -> IngestResult:
        if workspace_id is None:
            logger.info("webhook_unknown_account", key=key)
            return IngestResult(accepted=False, duplicate=False, reason="unknown_account")
        if is_replay:
            logger.warning("webhook_replay_suspected", key=key, event_time=event_time.isoformat())
            return IngestResult(accepted=False, duplicate=False, reason="outside_replay_window")
        if change_type and change_type not in SUPPORTED_FIELDS:
            return IngestResult(accepted=False, duplicate=False, reason="unsupported_field")

        unique_key = f"{key}:{index}"[:255]
        item_payload = {
            "entry_id": entry.get("id"),
            "index": index,
            "object": object_type,
            "change_type": change_type,
        }
        if entry.get("changes") and index < len(entry["changes"]):
            item_payload["change"] = entry["changes"][index]
        if entry.get("messaging") and index < len(entry["messaging"]):
            item_payload["messaging"] = entry["messaging"][index]

        # Idempotency: the unique (platform, event_key) constraint is the source
        # of truth. We check first for a cheap answer, and still rely on the
        # constraint to win a race — an IntegrityError means "already seen".
        from sqlalchemy import select as _select

        existing = (
            await self.session.execute(
                _select(MetaWebhookEvent.id).where(
                    MetaWebhookEvent.platform == "META",
                    MetaWebhookEvent.event_key == unique_key,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            logger.info("webhook_duplicate_ignored", key=unique_key)
            return IngestResult(accepted=False, duplicate=True, event_id=str(existing), reason="duplicate")

        row = MetaWebhookEvent(
            workspace_id=workspace_id,
            platform="META",
            event_key=unique_key,
            object_type=object_type,
            change_type=change_type,
            entry_id=str(entry.get("id") or ""),
            social_account_id=account.id if account else None,
            payload=item_payload,
            signature=(signature or "")[:128] or None,
            body_hash=body_hash,
            received_at=datetime.now(UTC),
            event_time=event_time,
            processing_status="PENDING",
            is_replay_suspected=False,
        )
        self.session.add(row)
        try:
            await self.session.flush()
        except IntegrityError:
            # A concurrent delivery won the race. The DB constraint is the
            # authority; drop the pending row and report a duplicate. The
            # request-level session is rolled back by the API layer.
            logger.info("webhook_duplicate_race", key=unique_key)
            return IngestResult(accepted=False, duplicate=True, event_id=None, reason="duplicate")
        return IngestResult(accepted=True, duplicate=False, event_id=str(row.id))

    async def pending_count(self, workspace_id) -> int:
        from sqlalchemy import func, select

        total = (
            await self.session.execute(
                select(func.count())
                .select_from(MetaWebhookEvent)
                .where(
                    MetaWebhookEvent.workspace_id == workspace_id,
                    MetaWebhookEvent.processing_status.in_(("PENDING", "PROCESSING")),
                )
            )
        ).scalar_one()
        return int(total)


def replay_cutoff() -> datetime:
    return datetime.now(UTC) - timedelta(seconds=REPLAY_WINDOW_SECONDS)
