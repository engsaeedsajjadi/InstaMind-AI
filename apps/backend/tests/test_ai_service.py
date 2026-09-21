"""AI content service: budget metering, brand grounding, history."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from app.core.exceptions import QuotaExceeded, ServiceUnavailable, ValidationError
from app.modules.ai.service import AIContentService
from app.modules.content.models import AIGeneration, BrandProfile, Content
from app.modules.tenants.scope import TenantScope
from app.modules.tenants.service import TenantService
from app.providers.ai.openai_provider import FakeAIProvider, NullProvider, estimate_cost_cents


@pytest.fixture
async def workspace_scope(session, make_user, make_workspace):
    user = await make_user()
    workspace = await make_workspace(user.id)
    ctx = await TenantService(session).build_context(workspace_id=workspace.id, user_id=user.id)
    return workspace, TenantScope(session, ctx)


@pytest.fixture
async def brand(session, workspace_scope):
    workspace, _scope = workspace_scope
    profile = BrandProfile(
        workspace_id=workspace.id,
        name="Acme Coffee",
        industry="Specialty coffee",
        target_audience="Tehran-based remote workers",
        tone_of_voice=["warm", "concise"],
        products=["Ethiopia Yirgacheffe 250g"],
        verified_claims=["Roasted weekly in Tehran", "Free delivery inside Tehran over 500,000 IRR"],
        banned_words=["ارزان‌ترین"],
    )
    session.add(profile)
    await session.flush()
    return profile


def caption_response(caption: str = "کاپوچینوی تازه", hashtags=None) -> str:
    return json.dumps(
        {"caption": caption, "hashtags": hashtags or ["قهوه", "کافه"], "cta": "سفارش بدهید"}
    )


@pytest.mark.asyncio
async def test_caption_generation_records_a_generation_and_its_cost(
    session, workspace_scope, brand
):
    _ws, scope = workspace_scope
    provider = FakeAIProvider(response=caption_response(), cost_cents=3)
    service = AIContentService(session, scope, provider=provider)

    result = await service.generate_caption(
        topic="New Ethiopia roast", brand_profile_id=brand.id, language="fa-IR"
    )
    assert result.caption == "کاپوچینوی تازه"
    assert result.hashtags == ["قهوه", "کافه"]
    assert result.cost_cents == 3
    assert result.warnings == []

    row = (
        await session.execute(select(AIGeneration).where(AIGeneration.id == result.generation_id))
    ).scalar_one()
    assert row.kind == "caption"
    assert row.status == "SUCCEEDED"
    assert row.total_tokens == 30


@pytest.mark.asyncio
async def test_brand_facts_are_passed_to_the_model(session, workspace_scope, brand):
    _ws, scope = workspace_scope
    provider = FakeAIProvider(response=caption_response())
    service = AIContentService(session, scope, provider=provider)
    await service.generate_caption(topic="Cold brew", brand_profile_id=brand.id)

    prompt = provider.calls[0]["messages"][-1]["content"]
    assert "Acme Coffee" in prompt
    assert "Roasted weekly in Tehran" in prompt, "verified claims must ground the prompt"
    assert "Never use these words" in prompt


@pytest.mark.asyncio
async def test_unverifiable_claims_are_flagged(session, workspace_scope, brand):
    _ws, scope = workspace_scope
    provider = FakeAIProvider(
        response=caption_response(caption="بهترین در ایران، درمان قطعی برای خستگی")
    )
    service = AIContentService(session, scope, provider=provider)
    result = await service.generate_caption(topic="hype", brand_profile_id=brand.id)
    assert any(flag.startswith("unverifiable_claim") for flag in result.warnings)

    row = (
        await session.execute(select(AIGeneration).where(AIGeneration.id == result.generation_id))
    ).scalar_one()
    assert row.moderation_flags == result.warnings


@pytest.mark.asyncio
async def test_banned_words_are_flagged(session, workspace_scope, brand):
    _ws, scope = workspace_scope
    provider = FakeAIProvider(response=caption_response(caption="ارزان‌ترین قهوه شهر"))
    service = AIContentService(session, scope, provider=provider)
    result = await service.generate_caption(topic="price", brand_profile_id=brand.id)
    assert "banned_word:ارزان‌ترین" in result.warnings


@pytest.mark.asyncio
async def test_caption_can_be_written_back_to_a_content_row(session, workspace_scope, brand, make_content):
    ws, scope = workspace_scope
    content = await make_content(ws.id, caption="")
    provider = FakeAIProvider(response=caption_response(caption="A new caption"))
    service = AIContentService(session, scope, provider=provider)
    result = await service.generate_caption(
        topic="launch", brand_profile_id=brand.id, content_id=content.id
    )
    refreshed = await scope.get_one(Content, content.id)
    assert refreshed.caption == "A new caption"
    assert refreshed.ai_generated is True
    assert refreshed.ai_generation_id == result.generation_id


@pytest.mark.asyncio
async def test_cost_is_metered_into_usage_records(session, workspace_scope, brand):
    _ws, scope = workspace_scope
    provider = FakeAIProvider(response=caption_response(), cost_cents=7)
    service = AIContentService(session, scope, provider=provider)
    await service.generate_caption(topic="a", brand_profile_id=brand.id)
    await service.generate_caption(topic="b", brand_profile_id=brand.id)
    assert await service.spent_this_month() == 14


@pytest.mark.asyncio
async def test_budget_exhaustion_blocks_before_calling_the_provider(session, workspace_scope, brand):
    _ws, scope = workspace_scope
    provider = FakeAIProvider(response=caption_response(), cost_cents=5)
    service = AIContentService(session, scope, provider=provider, monthly_budget_cents=5)
    await service.generate_caption(topic="first", brand_profile_id=brand.id)
    assert len(provider.calls) == 1

    with pytest.raises(QuotaExceeded):
        await service.generate_caption(topic="second", brand_profile_id=brand.id)
    assert len(provider.calls) == 1, "the provider must not be called once the budget is gone"


@pytest.mark.asyncio
async def test_missing_brand_profile_is_an_error_not_a_guess(session, workspace_scope):
    import uuid

    _ws, scope = workspace_scope
    service = AIContentService(session, scope, provider=FakeAIProvider(response=caption_response()))
    with pytest.raises(ValidationError):
        await service.generate_caption(topic="x", brand_profile_id=uuid.uuid4())


@pytest.mark.asyncio
async def test_content_plan_returns_the_requested_number_of_items(session, workspace_scope, brand):
    _ws, scope = workspace_scope
    payload = json.dumps(
        {
            "items": [
                {
                    "day_index": 1,
                    "content_type": "REELS",
                    "idea": "Brewing timelapse",
                    "caption_draft": "Behind the bar",
                    "hashtags": ["قهوه"],
                },
                {
                    "day_index": 3,
                    "content_type": "IMAGE",
                    "idea": "New bag design",
                    "caption_draft": "Meet the new bag",
                    "hashtags": ["بسته‌بندی"],
                },
            ]
        }
    )
    service = AIContentService(session, scope, provider=FakeAIProvider(response=payload))
    result = await service.generate_content_plan(
        horizon_days=7, posts_per_week=2, brand_profile_id=brand.id
    )
    assert len(result.items) == 2
    assert result.items[0].content_type == "REELS"
    assert result.items[1].hashtags == ["بسته‌بندی"]


@pytest.mark.asyncio
async def test_malformed_ai_output_is_recorded_and_surfaced(session, workspace_scope, brand):
    """A broken model response must be recorded as a failure and reported to
    the caller — never presented as an empty, successful plan."""
    _ws, scope = workspace_scope
    service = AIContentService(session, scope, provider=FakeAIProvider(response="not json at all"))
    result = await service.generate_content_plan(brand_profile_id=brand.id)
    assert result.status == "FAILED"
    assert result.items == []
    assert result.warnings, "the caller must be told why nothing came back"

    rows = (await session.execute(select(AIGeneration))).scalars().all()
    assert rows and rows[0].status == "FAILED"
    assert rows[0].error


@pytest.mark.asyncio
async def test_unconfigured_provider_refuses_instead_of_inventing(session, workspace_scope):
    _ws, scope = workspace_scope
    service = AIContentService(session, scope, provider=NullProvider())
    with pytest.raises(ServiceUnavailable):
        await service.generate_caption(topic="anything")


@pytest.mark.asyncio
async def test_generation_history_is_listable(session, workspace_scope, brand):
    _ws, scope = workspace_scope
    provider = FakeAIProvider(response=caption_response())
    service = AIContentService(session, scope, provider=provider)
    await service.generate_caption(topic="one", brand_profile_id=brand.id)
    await service.generate_caption(topic="two", brand_profile_id=brand.id)
    history = await service.list_generations(kind="caption")
    assert len(history) == 2


class TestCostEstimation:
    def test_cost_is_rounded_up_never_zero_for_real_usage(self):
        assert estimate_cost_cents("gpt-4o-mini", 1500, 500) == 1
        assert estimate_cost_cents("gpt-4o", 1500, 500) == 1

    def test_zero_tokens_cost_zero(self):
        assert estimate_cost_cents("gpt-4o-mini", 0, 0) == 0

    def test_unknown_model_uses_a_default_price(self):
        assert estimate_cost_cents("some-future-model", 1_000_000, 0) > 0

    def test_expensive_model_costs_more(self):
        cheap = estimate_cost_cents("gpt-4o-mini", 10_000, 10_000)
        pricey = estimate_cost_cents("gpt-4o", 10_000, 10_000)
        assert pricey > cheap
