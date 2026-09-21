"""AI assistant endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import DBSession, TenantScopeDep, require
from app.api.schemas import (
    CaptionGenerateRequest,
    CaptionGenerateResponse,
    ContentPlanRequest,
    ContentPlanResponse,
)
from app.core.permissions import Permission
from app.modules.ai.service import AIContentService
from app.providers.ai.openai_provider import get_ai_provider

router = APIRouter(prefix="/ai", tags=["ai"])


@router.post(
    "/generate-caption",
    response_model=CaptionGenerateResponse,
    dependencies=[Depends(require(Permission.AI_GENERATE))],
)
async def generate_caption(
    payload: CaptionGenerateRequest, session: DBSession, scope: TenantScopeDep
) -> CaptionGenerateResponse:
    service = AIContentService(session, scope, provider=get_ai_provider())
    result = await service.generate_caption(
        topic=payload.topic,
        brand_profile_id=payload.brand_profile_id,
        language=payload.language,
        tone=payload.tone,
        max_hashtags=payload.max_hashtags,
        include_cta=payload.include_cta,
        product_names=payload.product_names,
        content_id=payload.content_id,
    )
    return CaptionGenerateResponse(
        generation_id=result.generation_id,
        caption=result.caption,
        hashtags=result.hashtags,
        cta=result.cta,
        model=result.model,
        cost_cents=result.cost_cents,
        warnings=result.warnings,
    )


@router.post(
    "/generate-content-plan",
    response_model=ContentPlanResponse,
    dependencies=[Depends(require(Permission.AI_GENERATE))],
)
async def generate_plan(
    payload: ContentPlanRequest, session: DBSession, scope: TenantScopeDep
) -> ContentPlanResponse:
    service = AIContentService(session, scope, provider=get_ai_provider())
    result = await service.generate_content_plan(
        horizon_days=payload.horizon_days,
        posts_per_week=payload.posts_per_week,
        language=payload.language,
        goals=payload.goals,
        brand_profile_id=payload.brand_profile_id,
    )
    return ContentPlanResponse(
        generation_id=result.generation_id,
        items=[item.__dict__ for item in result.items],
        model=result.model,
        cost_cents=result.cost_cents,
        status=result.status,
        warnings=result.warnings,
    )


@router.get("/usage", dependencies=[Depends(require(Permission.AI_GENERATE))])
async def usage(session: DBSession, scope: TenantScopeDep) -> dict[str, int]:
    service = AIContentService(session, scope, provider=get_ai_provider())
    spent = await service.spent_this_month()
    return {"ai_cost_cents_month": spent, "budget_cents": service.monthly_budget_cents or 0}
