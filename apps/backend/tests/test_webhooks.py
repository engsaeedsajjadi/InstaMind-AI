"""Meta webhook: signature, replay protection, idempotency."""

from __future__ import annotations

import json
import time

import pytest
from sqlalchemy import select

from app.core.exceptions import AuthenticationError, ValidationError
from app.modules.instagram.models import MetaWebhookEvent
from app.modules.webhooks.service import WebhookService
from tests.conftest import signed_webhook

APP_SECRET = "test-app-secret"


def comment_payload(account_id: str = "17840000001", comment_id: str = "c-1") -> dict:
    return {
        "object": "instagram",
        "entry": [
            {
                "id": account_id,
                "time": int(time.time()),
                "changes": [
                    {
                        "field": "comments",
                        "value": {
                            "id": comment_id,
                            "media": {"id": "media-1"},
                            "text": "How much is this?",
                            "from": {"id": "user-9", "username": "customer"},
                        },
                    }
                ],
            }
        ],
    }


@pytest.mark.asyncio
async def test_verification_challenge_is_echoed_for_a_matching_token():
    challenge = WebhookService.verify_subscription(
        mode="subscribe", verify_token="test-verify-token", challenge="12345"
    )
    assert challenge == "12345"


@pytest.mark.asyncio
async def test_verification_fails_for_a_wrong_token():
    with pytest.raises(AuthenticationError):
        WebhookService.verify_subscription(
            mode="subscribe", verify_token="wrong-token", challenge="12345"
        )


@pytest.mark.asyncio
async def test_verification_fails_for_a_wrong_mode():
    with pytest.raises(AuthenticationError):
        WebhookService.verify_subscription(
            mode="unsubscribe", verify_token="test-verify-token", challenge="12345"
        )


@pytest.mark.asyncio
async def test_bad_signature_is_rejected(session):
    service = WebhookService(session)
    body = json.dumps(comment_payload()).encode()
    with pytest.raises(AuthenticationError):
        await service.ingest(payload_bytes=body, signature_header="sha256=deadbeef")


@pytest.mark.asyncio
async def test_missing_signature_is_rejected(session):
    service = WebhookService(session)
    body = json.dumps(comment_payload()).encode()
    with pytest.raises(AuthenticationError):
        await service.ingest(payload_bytes=body, signature_header=None)


@pytest.mark.asyncio
async def test_valid_signature_is_accepted(session, make_user, make_workspace, make_account):
    user = await make_user()
    ws = await make_workspace(user.id)
    account = await make_account(ws.id, external_id="17840000001")
    body = json.dumps(comment_payload(account.external_account_id)).encode()

    service = WebhookService(session)
    results = await service.ingest(payload_bytes=body, signature_header=signed_webhook(body))
    assert len(results) == 1
    assert results[0].accepted is True
    assert results[0].duplicate is False


@pytest.mark.asyncio
async def test_duplicate_delivery_is_ignored(session, make_user, make_workspace, make_account):
    user = await make_user()
    ws = await make_workspace(user.id)
    account = await make_account(ws.id, external_id="17840000001")
    body = json.dumps(comment_payload(account.external_account_id)).encode()
    signature = signed_webhook(body)

    service = WebhookService(session)
    first = await service.ingest(payload_bytes=body, signature_header=signature)
    second = await service.ingest(payload_bytes=body, signature_header=signature)
    assert first[0].accepted is True
    assert second[0].duplicate is True
    rows = (await session.execute(select(MetaWebhookEvent))).scalars().all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_old_event_is_treated_as_a_replay(session, make_user, make_workspace, make_account):
    user = await make_user()
    ws = await make_workspace(user.id)
    account = await make_account(ws.id, external_id="17840000001")
    payload = comment_payload(account.external_account_id)
    payload["entry"][0]["time"] = int(time.time()) - 3600  # one hour old
    body = json.dumps(payload).encode()

    results = await WebhookService(session).ingest(
        payload_bytes=body, signature_header=signed_webhook(body)
    )
    assert results[0].accepted is False
    assert results[0].reason == "outside_replay_window"


@pytest.mark.asyncio
async def test_event_for_an_unknown_account_is_not_stored(session):
    body = json.dumps(comment_payload("9999999999")).encode()
    results = await WebhookService(session).ingest(
        payload_bytes=body, signature_header=signed_webhook(body)
    )
    assert results[0].accepted is False
    assert results[0].reason == "unknown_account"


@pytest.mark.asyncio
async def test_unsupported_object_is_ignored(session):
    payload = {"object": "whatsapp_business_account", "entry": [{"id": "1", "time": int(time.time())}]}
    body = json.dumps(payload).encode()
    results = await WebhookService(session).ingest(
        payload_bytes=body, signature_header=signed_webhook(body)
    )
    assert results[0].reason == "unsupported_object"


@pytest.mark.asyncio
async def test_malformed_json_is_a_validation_error(session):
    with pytest.raises(ValidationError):
        await WebhookService(session).ingest(
            payload_bytes=b"{not json", signature_header=signed_webhook(b"{not json")
        )


@pytest.mark.asyncio
async def test_messaging_event_is_stored_with_its_window_metadata(
    session, make_user, make_workspace, make_account
):
    user = await make_user()
    ws = await make_workspace(user.id)
    account = await make_account(ws.id, external_id="17840000001")
    payload = {
        "object": "instagram",
        "entry": [
            {
                "id": account.external_account_id,
                "time": int(time.time()),
                "messaging": [
                    {
                        "sender": {"id": "user-42"},
                        "recipient": {"id": account.external_account_id},
                        "timestamp": int(time.time() * 1000),
                        "message": {"mid": "m-99", "text": "Is it in stock?"},
                    }
                ],
            }
        ],
    }
    body = json.dumps(payload).encode()
    results = await WebhookService(session).ingest(
        payload_bytes=body, signature_header=signed_webhook(body)
    )
    assert results[0].accepted is True
    row = (await session.execute(select(MetaWebhookEvent))).scalar_one()
    assert row.change_type == "message"
    assert row.social_account_id == account.id
    assert row.processing_status == "PENDING"
    assert row.body_hash


@pytest.mark.asyncio
async def test_unsupported_change_field_is_skipped(session, make_user, make_workspace, make_account):
    user = await make_user()
    ws = await make_workspace(user.id)
    account = await make_account(ws.id, external_id="17840000001")
    payload = comment_payload(account.external_account_id)
    payload["entry"][0]["changes"][0]["field"] = "some_future_field"
    body = json.dumps(payload).encode()
    results = await WebhookService(session).ingest(
        payload_bytes=body, signature_header=signed_webhook(body)
    )
    assert results[0].accepted is False
    assert results[0].reason == "unsupported_field"


@pytest.mark.asyncio
async def test_stored_payload_contains_no_access_token(session, make_user, make_workspace, make_account):
    user = await make_user()
    ws = await make_workspace(user.id)
    account = await make_account(ws.id, external_id="17840000001", access_token="IGQVsupersecret")
    body = json.dumps(comment_payload(account.external_account_id)).encode()
    await WebhookService(session).ingest(payload_bytes=body, signature_header=signed_webhook(body))
    row = (await session.execute(select(MetaWebhookEvent))).scalar_one()
    assert "IGQVsupersecret" not in json.dumps(row.payload)
