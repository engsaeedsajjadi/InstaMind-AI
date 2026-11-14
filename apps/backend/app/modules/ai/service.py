"""AI content generation service.

Product rules encoded here (not just in the prompt):

1. **Budget enforcement.** A workspace cannot spend past its monthly AI budget;
   the check happens *before* the provider call, and the actual cost is written
   to ``usage_records`` afterwards.
2. **No invented claims.** The prompt is built from the workspace's
   ``BrandProfile`` (verified claims, products, audience). The system prompt
   explicitly forbids inventing prices, stock, certifications or reviews, and
   the output passes a guard that flags unverifiable superlatives.
3. **History.** Every call — including failures and blocked outputs — is stored
   in ``ai_generations`` so a user can inspect, copy or regenerate.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import QuotaExceeded, ServiceUnavailable, ValidationError
from app.core.logging import logger
from app.modules.audit.service import AuditService
from app.modules.content.models import AIGeneration, BrandProfile, Content
from app.modules.tenants.scope import TenantScope
from app.providers.ai.base import AIMessage, parse_json_object

# Phrases that usually signal an unverifiable claim. They are *flagged*, not
# silently rewritten: a human decides.
UNVERIFIABLE_PATTERNS = (
    "بهترین در ایران",
    "بهترین در جهان",
    "تضمین ۱۰۰",
    "بدون هیچ عارضه",
    "درمان قطعی",
    "best in the world",
    "guaranteed results",
    "clinically proven",
    "100% guaranteed",
    "cures",
)


@dataclass(frozen=True)
class CaptionResult:
    generation_id: uuid.UUID
    caption: str
    hashtags: list[str]
    cta: str | None
    model: str
    cost_cents: int
    warnings: list[str]


@dataclass(frozen=True)
class PlanItem:
    day_index: int
    content_type: str
    idea: str
    caption_draft: str
    hashtags: list[str]


@dataclass(frozen=True)
class PlanResult:
    generation_id: uuid.UUID
    items: list[PlanItem]
    model: str
    cost_cents: int
    status: str = "SUCCEEDED"
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ReelScript:
    generation_id: uuid.UUID
    hook: str
    scenes: list[dict]
    cover_title: str
    caption: str
    hashtags: list[str]
    model: str
    cost_cents: int
    status: str = "SUCCEEDED"
    warnings: list[str] = field(default_factory=list)


SYSTEM_PROMPT_BASE = """You are InstaMind AI, a social-media content assistant for businesses.

Non-negotiable rules:
- Write in the language requested by the user.
- Only state facts that appear in the BRAND FACTS block. Never invent prices, stock levels, delivery times, certifications, medical claims, awards or customer reviews.
- If a fact is missing, omit it or phrase the sentence without the claim.
- Do not use unverifiable superlatives.
- Do not add emojis unless the brand tone explicitly asks for them.
- Never claim the content is human-written or that the brand said something it did not.
- Keep captions within Instagram's 2200-character limit and at most 30 hashtags.
"""

REEL_SCRIPT_SYSTEM_PROMPT = """You are InstaMind AI, writing a short Instagram reel script in Persian.

Non-negotiable rules:
- Only use facts from the BRAND FACTS block. Never invent prices, stock, delivery times, certifications or reviews.
- Write everything in Persian. Transliterate any Latin word into Persian script (API -> ای‌پی‌آی, CRM -> سی‌آر‌ام); never leave Latin letters in the text — the renderer font has no Latin glyphs.
- The hook is the first thing the viewer reads: one short sentence (5-140 chars) that states the problem or the promise. The first scene's text MUST be the hook.
- Scenes: 3 to 5, each 2-3 seconds, each one short line of on-screen text.
- Cover title: 2 short lines, bold, the promise of the reel.
- Caption: at most 2200 characters. Hashtags: at most 10.
"""

PLAN_SYSTEM_PROMPT = """You are InstaMind AI, planning an Instagram content calendar for a business.

Non-negotiable rules:
- Only use facts from the BRAND FACTS block.
- Produce exactly the number of items requested.
- Vary content types (IMAGE, CAROUSEL, REELS) unless the brand forbids one.
- Never invent prices, stock, delivery times, certifications or reviews.
"""


class AIContentService:
    def __init__(
        self,
        session: AsyncSession,
        scope: TenantScope,
        *,
        provider,
        audit: AuditService | None = None,
        monthly_budget_cents: int | None = None,
    ) -> None:
        self.session = session
        self.scope = scope
        self.provider = provider
        self.audit = audit or AuditService(session)
        self.monthly_budget_cents = monthly_budget_cents

    # ------------------------------------------------------------------ budget
    async def _month_bounds(self) -> tuple[datetime, datetime]:
        now = datetime.now(UTC)
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = (start + timedelta(days=32)).replace(day=1)
        return start, end

    async def spent_this_month(self) -> int:
        from app.modules.billing.models import UsageRecord

        start, end = await self._month_bounds()
        total = (
            await self.session.execute(
                select(func.coalesce(func.sum(UsageRecord.quantity), 0)).where(
                    UsageRecord.workspace_id == self.scope.workspace_id,
                    UsageRecord.metric == "ai_cost_cents",
                    UsageRecord.period_start >= start,
                    UsageRecord.period_start < end,
                )
            )
        ).scalar_one()
        return int(total or 0)

    async def _enforce_budget(self) -> None:
        from app.core.config import settings

        budget = self.monthly_budget_cents
        if budget is None:
            budget = settings.AI_DEFAULT_MONTHLY_BUDGET_CENTS
        if budget <= 0:
            return
        spent = await self.spent_this_month()
        if spent >= budget:
            raise QuotaExceeded(
                "This workspace has reached its monthly AI budget.",
                context={"spent_cents": spent, "budget_cents": budget, "code": "ai_budget_exhausted"},
            )

    async def _record_usage(self, cost_cents: int, generations: int = 1) -> None:
        from app.modules.billing.models import UsageRecord

        start, end = await self._month_bounds()
        for metric, quantity in (("ai_cost_cents", cost_cents), ("ai_generations", generations)):
            row = (
                await self.session.execute(
                    select(UsageRecord).where(
                        UsageRecord.workspace_id == self.scope.workspace_id,
                        UsageRecord.metric == metric,
                        UsageRecord.period_start == start,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                self.session.add(
                    UsageRecord(
                        workspace_id=self.scope.workspace_id,
                        metric=metric,
                        period_start=start,
                        period_end=end,
                        quantity=quantity,
                        limit_value=self.monthly_budget_cents if metric == "ai_cost_cents" else None,
                    )
                )
            else:
                row.quantity = int(row.quantity or 0) + quantity
        await self.session.flush()

    # ------------------------------------------------------------------- brand
    async def _brand_facts(self, brand_profile_id: uuid.UUID | None) -> tuple[str, BrandProfile | None]:
        if brand_profile_id is None:
            return "No brand profile supplied. Avoid any specific factual claim.", None
        profile = await self.scope.find(BrandProfile, brand_profile_id)
        if profile is None:
            raise ValidationError("Brand profile not found in this workspace.")
        lines = [f"Brand name: {profile.name}"]
        if profile.industry:
            lines.append(f"Industry: {profile.industry}")
        if profile.target_audience:
            lines.append(f"Target audience: {profile.target_audience}")
        if profile.tone_of_voice:
            lines.append(f"Tone of voice: {', '.join(map(str, profile.tone_of_voice))}")
        if profile.marketing_goals:
            lines.append(f"Marketing goals: {', '.join(map(str, profile.marketing_goals))}")
        if profile.products:
            lines.append(f"Products: {', '.join(map(str, profile.products))}")
        if profile.verified_claims:
            lines.append("VERIFIED FACTS (the only facts you may state):")
            lines.extend(f"- {claim}" for claim in map(str, profile.verified_claims))
        if profile.banned_words:
            lines.append(f"Never use these words: {', '.join(map(str, profile.banned_words))}")
        if profile.prohibited_topics:
            lines.append(f"Never discuss: {', '.join(map(str, profile.prohibited_topics))}")
        return "\n".join(lines), profile

    @staticmethod
    def _guard_output(text: str, profile: BrandProfile | None) -> list[str]:
        warnings: list[str] = []
        lowered = text.lower()
        for pattern in UNVERIFIABLE_PATTERNS:
            if pattern.lower() in lowered:
                warnings.append(f"unverifiable_claim:{pattern}")
        if profile is not None:
            for banned in map(str, profile.banned_words or []):
                if banned and banned.lower() in lowered:
                    warnings.append(f"banned_word:{banned}")
        if len(text) > 2200:
            warnings.append("caption_too_long")
        return warnings

    # ---------------------------------------------------------------- caption
    async def generate_caption(
        self,
        *,
        topic: str,
        brand_profile_id: uuid.UUID | None = None,
        language: str = "fa-IR",
        tone: list[str] | None = None,
        max_hashtags: int = 10,
        include_cta: bool = True,
        product_names: list[str] | None = None,
        content_id: uuid.UUID | None = None,
        extra_instructions: str | None = None,
    ) -> CaptionResult:
        await self._enforce_budget()
        facts, profile = await self._brand_facts(brand_profile_id)
        started = time.perf_counter()

        user_prompt = "\n".join(
            [
                f"Task: write ONE Instagram caption about: {topic}",
                f"Output language: {language}",
                f"Tone: {', '.join(tone) if tone else 'follow the brand tone'}",
                f"Hashtags: up to {max_hashtags}, relevant, no banned tags",
                f"CTA: {'include one clear call to action' if include_cta else 'omit the CTA'}",
                f"Products to reference (only if listed in BRAND FACTS): {', '.join(product_names or []) or 'none'}",
                "",
                "BRAND FACTS:",
                facts,
                "",
                'Respond with JSON only: {"caption": "...", "hashtags": ["..."], "cta": "..." }',
            ]
        )
        if extra_instructions:
            # Harness state, not a prompt edit: an instruction addendum from
            # the active HarnessConfig, appended per-run to the *user*
            # prompt. SYSTEM_PROMPT_BASE is never mutated.
            user_prompt += f"\nAdditional harness instructions:\n{extra_instructions}"
        completion = await self.provider.complete(
            [
                AIMessage(role="system", content=SYSTEM_PROMPT_BASE),
                AIMessage(role="user", content=user_prompt),
            ],
            temperature=0.8,
            max_tokens=900,
            response_format="json",
        )
        latency_ms = int((time.perf_counter() - started) * 1000)

        try:
            parsed = parse_json_object(completion.text)
            caption = str(parsed.get("caption") or "").strip()
            hashtags = [str(tag).lstrip("#") for tag in (parsed.get("hashtags") or []) if str(tag).strip()]
            cta = parsed.get("cta")
            cta = str(cta).strip() if cta else None
            status = "SUCCEEDED"
            error: str | None = None
        except Exception as exc:  # noqa: BLE001 - recorded, then re-raised
            caption, hashtags, cta = completion.text.strip(), [], None
            status = "FAILED"
            error = str(exc)[:500]

        warnings = self._guard_output(caption, profile)
        if warnings:
            status = "SUCCEEDED" if status == "SUCCEEDED" else status

        generation = AIGeneration(
            workspace_id=self.scope.workspace_id,
            kind="caption",
            provider=getattr(self.provider, "name", "unknown"),
            model=completion.model,
            prompt_tokens=completion.usage.prompt_tokens,
            completion_tokens=completion.usage.completion_tokens,
            total_tokens=completion.usage.total_tokens,
            cost_cents=completion.usage.cost_cents,
            input_payload={"topic": topic, "language": language, "tone": tone or []},
            output_text=caption,
            output_payload={"hashtags": hashtags, "cta": cta},
            status=status,
            moderation_flags=warnings,
            latency_ms=latency_ms,
            content_id=content_id,
            brand_profile_id=brand_profile_id,
            error=error,
            created_by=self.scope.context.user_id,
        )
        self.session.add(generation)
        await self.session.flush()
        await self._record_usage(completion.usage.cost_cents)

        if content_id is not None and status == "SUCCEEDED":
            content = await self.scope.find(Content, content_id)
            if content is not None:
                content.caption = caption
                content.hashtags = hashtags[:30]
                content.ai_generated = True
                content.ai_generation_id = generation.id
                self.scope.touch(content)
                await self.session.flush()

        await self.audit.record(
            action="ai.caption_generated",
            workspace_id=self.scope.workspace_id,
            actor_id=self.scope.context.user_id,
            resource_type="ai_generation",
            resource_id=str(generation.id),
            metadata={
                "cost_cents": completion.usage.cost_cents,
                "model": completion.model,
                "warnings": len(warnings),
            },
        )
        if error:
            logger.warning("ai_caption_parse_failed", generation_id=str(generation.id))
        return CaptionResult(
            generation_id=generation.id,
            caption=caption,
            hashtags=hashtags[:30],
            cta=cta,
            model=completion.model,
            cost_cents=completion.usage.cost_cents,
            warnings=warnings,
        )

    # ---------------------------------------------------------- reel script
    async def generate_reel_script(
        self,
        *,
        topic: str,
        language: str = "fa-IR",
        brand_profile_id: uuid.UUID | None = None,
        content_id: uuid.UUID | None = None,
        extra_instructions: str | None = None,
    ) -> ReelScript:
        """Writer agent's structured output: hook, timed scenes, cover title,
        caption and hashtags — all Persian, all from brand facts only.

        Same budget/audit/record contract as ``generate_caption``. The first
        scene's text is forced to equal the hook so the hook lands in the
        first 3 seconds; a provider that returns anything else is repaired
        here, not silently passed on.
        """
        await self._enforce_budget()
        facts, profile = await self._brand_facts(brand_profile_id)
        started = time.perf_counter()

        user_prompt = "\n".join(
            [
                f"Task: write a short Instagram reel script about: {topic}",
                f"Output language: {language}",
                "",
                "BRAND FACTS:",
                facts,
                "",
                'Respond with JSON only: {"hook": "...", "scenes": '
                '[{"text": "...", "start_s": 0, "end_s": 3}], "cover_title": "...", '
                '"caption": "...", "hashtags": ["..."]}',
            ]
        )
        if extra_instructions:
            # Harness state, not a prompt edit: see generate_caption.
            user_prompt += f"\nAdditional harness instructions:\n{extra_instructions}"
        completion = None
        hook = ""
        scenes: list[dict] = []
        cover_title = ""
        caption = ""
        hashtags: list[str] = []
        status = "SUCCEEDED"
        error: str | None = None
        try:
            completion = await self.provider.complete(
                [
                    AIMessage(role="system", content=REEL_SCRIPT_SYSTEM_PROMPT),
                    AIMessage(role="user", content=user_prompt),
                ],
                temperature=0.7,
                max_tokens=1500,
                response_format="json",
            )
            parsed = parse_json_object(completion.text)
            hook = str(parsed.get("hook") or "").strip()
            raw_scenes = parsed.get("scenes") or []
            for raw in raw_scenes:
                if not isinstance(raw, dict):
                    continue
                text = str(raw.get("text") or "").strip()
                if not text:
                    continue
                scenes.append(
                    {
                        "text": text,
                        "start_s": float(raw.get("start_s") or 0),
                        "end_s": float(raw.get("end_s") or 0),
                    }
                )
            cover_title = str(parsed.get("cover_title") or "").strip()
            caption = str(parsed.get("caption") or "").strip()
            hashtags = [str(tag) for tag in (parsed.get("hashtags") or [])][:10]
            if not scenes:
                raise ValueError("Provider returned no usable scenes.")
            # The hook must be the first thing on screen.
            if scenes[0]["text"] != hook:
                scenes.insert(0, {"text": hook, "start_s": 0.0, "end_s": 3.0})
            scenes = scenes[:5]
            # Normalise scene timing: 2-3 s per scene, contiguous.
            cursor = 0.0
            for scene in scenes:
                scene["start_s"] = round(cursor, 1)
                scene["end_s"] = round(cursor + 2.5, 1)
                cursor = scene["end_s"]
            if not hook:
                hook = scenes[0]["text"]
        except Exception as exc:  # noqa: BLE001 - recorded, then re-raised
            hook, scenes, cover_title, caption, hashtags = "", [], "", "", []
            status = "FAILED"
            error = str(exc)[:500]

        warnings = self._guard_output(caption or hook, profile)
        if len(hashtags) > 10:
            warnings.append("Too many hashtags")
            hashtags = hashtags[:10]

        latency_ms = int((time.perf_counter() - started) * 1000)
        usage = completion.usage if completion is not None else None
        generation = AIGeneration(
            workspace_id=self.scope.workspace_id,
            kind="reel_script",
            provider=getattr(self.provider, "name", "unknown"),
            model=completion.model if completion is not None else "unknown",
            prompt_tokens=usage.prompt_tokens if usage else 0,
            completion_tokens=usage.completion_tokens if usage else 0,
            total_tokens=usage.total_tokens if usage else 0,
            cost_cents=usage.cost_cents if usage else 0,
            input_payload={"topic": topic, "language": language},
            output_text=caption,
            output_payload={"hook": hook, "scenes": scenes, "cover_title": cover_title},
            status=status,
            moderation_flags=warnings,
            latency_ms=latency_ms,
            content_id=content_id,
            brand_profile_id=brand_profile_id,
            error=error,
            created_by=self.scope.context.user_id,
        )
        self.session.add(generation)
        await self.session.flush()
        await self._record_usage(usage.cost_cents if usage else 0)

        if status != "SUCCEEDED":
            raise ServiceUnavailable(
                f"Reel script generation failed: {error or 'provider returned unusable output'}.",
                context={"generation_id": str(generation.id)},
            )

        await self.audit.record(
            action="ai.reel_script_generated",
            workspace_id=self.scope.workspace_id,
            actor_id=self.scope.context.user_id,
            resource_type="ai_generation",
            resource_id=str(generation.id),
            metadata={
                "cost_cents": usage.cost_cents if usage else 0,
                "model": completion.model if completion is not None else "unknown",
                "warnings": len(warnings),
            },
        )
        return ReelScript(
            generation_id=generation.id,
            hook=hook,
            scenes=scenes,
            cover_title=cover_title,
            caption=caption,
            hashtags=hashtags,
            model=completion.model if completion is not None else "unknown",
            cost_cents=usage.cost_cents if usage else 0,
            warnings=warnings,
        )

    # -------------------------------------------------------------------- plan
    async def generate_content_plan(
        self,
        *,
        horizon_days: int = 7,
        posts_per_week: int = 3,
        language: str = "fa-IR",
        goals: list[str] | None = None,
        brand_profile_id: uuid.UUID | None = None,
    ) -> PlanResult:
        await self._enforce_budget()
        facts, profile = await self._brand_facts(brand_profile_id)
        total_posts = max(1, round(horizon_days / 7 * posts_per_week))
        started = time.perf_counter()

        user_prompt = "\n".join(
            [
                f"Task: build an Instagram content plan for the next {horizon_days} days.",
                f"Number of items: exactly {total_posts}.",
                f"Output language: {language}",
                f"Goals: {', '.join(goals) if goals else 'general brand growth'}",
                "",
                "BRAND FACTS:",
                facts,
                "",
                'Respond with JSON only: {"items": [{"day_index": 1, "content_type": '
                '"IMAGE|CAROUSEL|REELS", "idea": "...", "caption_draft": "...", '
                '"hashtags": ["..."]}] }',
            ]
        )
        completion = await self.provider.complete(
            [
                AIMessage(role="system", content=PLAN_SYSTEM_PROMPT),
                AIMessage(role="user", content=user_prompt),
            ],
            temperature=0.7,
            max_tokens=2000,
            response_format="json",
        )
        latency_ms = int((time.perf_counter() - started) * 1000)

        items: list[PlanItem] = []
        status = "SUCCEEDED"
        error: str | None = None
        try:
            parsed = parse_json_object(completion.text)
            for raw in parsed.get("items") or []:
                if not isinstance(raw, dict):
                    continue
                content_type = str(raw.get("content_type") or "IMAGE").upper()
                if content_type not in {"IMAGE", "CAROUSEL", "REELS", "STORIES"}:
                    content_type = "IMAGE"
                idea = str(raw.get("idea") or "").strip()
                caption_draft = str(raw.get("caption_draft") or "").strip()
                items.append(
                    PlanItem(
                        day_index=int(raw.get("day_index") or (len(items) + 1)),
                        content_type=content_type,
                        idea=idea,
                        caption_draft=caption_draft,
                        hashtags=[str(tag).lstrip("#") for tag in (raw.get("hashtags") or [])][:30],
                    )
                )
            if not items:
                raise ValueError("The AI returned no plan items.")
        except Exception as exc:  # noqa: BLE001
            status = "FAILED"
            error = str(exc)[:500]

        generation = AIGeneration(
            workspace_id=self.scope.workspace_id,
            kind="content_plan",
            provider=getattr(self.provider, "name", "unknown"),
            model=completion.model,
            prompt_tokens=completion.usage.prompt_tokens,
            completion_tokens=completion.usage.completion_tokens,
            total_tokens=completion.usage.total_tokens,
            cost_cents=completion.usage.cost_cents,
            input_payload={"horizon_days": horizon_days, "posts_per_week": posts_per_week},
            output_text=completion.text[:8000],
            output_payload={"items": [item.__dict__ for item in items]},
            status=status,
            latency_ms=latency_ms,
            brand_profile_id=brand_profile_id,
            error=error,
            created_by=self.scope.context.user_id,
        )
        self.session.add(generation)
        await self.session.flush()
        await self._record_usage(completion.usage.cost_cents)
        await self.audit.record(
            action="ai.plan_generated",
            workspace_id=self.scope.workspace_id,
            actor_id=self.scope.context.user_id,
            resource_type="ai_generation",
            resource_id=str(generation.id),
            metadata={"items": len(items), "cost_cents": completion.usage.cost_cents},
        )
        return PlanResult(
            generation_id=generation.id,
            items=items,
            model=completion.model,
            cost_cents=completion.usage.cost_cents,
            status=status,
            warnings=[error] if error else [],
        )

    # ----------------------------------------------------------------- history
    async def list_generations(self, *, limit: int = 50, kind: str | None = None) -> list[AIGeneration]:
        conditions = [AIGeneration.kind == kind] if kind else []
        return await self.scope.list(
            AIGeneration, *conditions, limit=limit, order_by=AIGeneration.created_at.desc()
        )
