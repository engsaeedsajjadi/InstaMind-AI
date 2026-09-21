"""Meta webhook endpoints.

``GET  /webhooks/meta`` verifies the subscription (returns the challenge).
``POST /webhooks/meta`` receives events. Both are unauthenticated by design —
Meta does not carry our JWT — so security comes from the verify token and the
HMAC signature, and the handler answers immediately (Meta times out at ~20 s
and retries, which would otherwise duplicate work).
"""

from __future__ import annotations

from fastapi import APIRouter, Header, Query, Request, Response

from app.api.deps import DBSession
from app.api.schemas import WebhookIngestResponse
from app.core.logging import logger
from app.modules.webhooks.service import WebhookService

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.get("/meta")
async def verify_subscription(
    mode: str = Query(...),
    verify_token: str = Query(...),
    challenge: str = Query(...),
) -> Response:
    result = WebhookService.verify_subscription(
        mode=mode, verify_token=verify_token, challenge=challenge
    )
    return Response(content=result, media_type="text/plain")


@router.post("/meta", response_model=WebhookIngestResponse)
async def receive_event(
    request: Request,
    session: DBSession,
    x_hub_signature_256: str | None = Header(default=None, alias="X-Hub-Signature-256"),
) -> WebhookIngestResponse:
    body = await request.body()
    service = WebhookService(session)
    results = await service.ingest(payload_bytes=body, signature_header=x_hub_signature_256)

    accepted = sum(1 for item in results if item.accepted)
    duplicates = sum(1 for item in results if item.duplicate)
    rejected = len(results) - accepted - duplicates
    logger.info(
        "webhook_ingested",
        accepted=accepted,
        duplicates=duplicates,
        rejected=rejected,
    )
    # Always 200 for well-formed, signed deliveries: returning an error makes
    # Meta retry the same event, which is exactly the duplicate we just
    # de-duplicated.
    return WebhookIngestResponse(accepted=accepted, duplicates=duplicates, rejected=rejected)
