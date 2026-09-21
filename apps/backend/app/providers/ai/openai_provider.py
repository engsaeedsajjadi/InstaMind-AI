"""OpenAI-compatible provider + a null provider for unconfigured deployments.

Costs are computed from a small public price table (USD cents per 1M tokens).
Prices change often, so the table is overridable via environment config in a
later milestone; the important property is that *every* generation is metered.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from app.core.config import settings
from app.core.exceptions import ExternalServiceError, ServiceUnavailable, ValidationError
from app.core.logging import logger
from app.providers.ai.base import AICompletion, AIMessage, AIUsage

# USD cents per 1,000,000 tokens: (prompt, completion).
PRICING_CENTS_PER_M: dict[str, tuple[int, int]] = {
    "gpt-4o-mini": (15, 60),
    "gpt-4o": (250, 1000),
    "gpt-4.1-mini": (40, 160),
    "gpt-4.1": (200, 800),
    "o4-mini": (110, 440),
}
DEFAULT_PRICING = (50, 150)


def estimate_cost_cents(model: str, prompt_tokens: int, completion_tokens: int) -> int:
    """Cost in **whole** USD cents, always rounded up so usage is never
    under-billed. A free tier of 0 therefore never appears as 0 when tokens
    were actually consumed."""
    import math

    prompt_price, completion_price = PRICING_CENTS_PER_M.get(model, DEFAULT_PRICING)
    cents = (prompt_tokens * prompt_price + completion_tokens * completion_price) / 1_000_000
    if cents <= 0:
        return 0
    return max(1, math.ceil(cents))


class OpenAIProvider:
    """Chat-completions client. Works with any OpenAI-compatible endpoint."""

    name = "openai"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        default_model: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        key = api_key if api_key is not None else settings.OPENAI_API_KEY.get_secret_value()
        if not key:
            raise ValidationError(
                "AI generation is not configured on this server (OPENAI_API_KEY is missing)."
            )
        self._api_key = key
        self._base_url = (base_url or settings.OPENAI_BASE_URL).rstrip("/")
        self._model = default_model or settings.OPENAI_MODEL
        self._client = client or httpx.AsyncClient(timeout=settings.OPENAI_TIMEOUT_SECONDS)

    async def complete(
        self,
        messages: list[AIMessage],
        *,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 1200,
        response_format: str | None = None,
    ) -> AICompletion:
        model = model or self._model
        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format == "json":
            payload["response_format"] = {"type": "json_object"}

        started = time.perf_counter()
        try:
            response = await self._client.post(
                f"{self._base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=payload,
            )
        except httpx.HTTPError as exc:
            raise ExternalServiceError(f"AI provider unreachable: {exc}") from exc
        latency_ms = int((time.perf_counter() - started) * 1000)

        if response.status_code == 429:
            raise ServiceUnavailable("AI provider rate limit reached. Please retry shortly.")
        if response.status_code >= 400:
            detail = _safe_error_text(response)
            logger.warning("openai_error", status=response.status_code, detail=detail[:300])
            raise ExternalServiceError(f"AI provider error ({response.status_code}).")

        data = response.json()
        try:
            text = data["choices"][0]["message"]["content"] or ""
            usage = data.get("usage") or {}
        except (KeyError, IndexError, TypeError) as exc:
            raise ExternalServiceError("Unexpected AI provider response shape.") from exc

        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        return AICompletion(
            text=text,
            usage=AIUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=int(usage.get("total_tokens") or prompt_tokens + completion_tokens),
                cost_cents=estimate_cost_cents(model, prompt_tokens, completion_tokens),
            ),
            model=model,
            raw={"id": data.get("id"), "finish_reason": _finish_reason(data)},
            latency_ms=latency_ms,
        )

    async def aclose(self) -> None:  # pragma: no cover - thin wrapper
        await self._client.aclose()


class NullProvider:
    """Used when ``AI_PROVIDER=null``. Never fabricates content — it refuses."""

    name = "null"

    async def complete(self, messages: list[AIMessage], **kwargs: Any) -> AICompletion:
        raise ServiceUnavailable(
            "AI generation is disabled on this deployment. Set INSTAMIND_AI_PROVIDER=openai "
            "and provide an API key to enable it."
        )


class FakeAIProvider:
    """Test double. Returns deterministic, clearly-marked output and records
    calls so tests can assert on prompt construction."""

    name = "fake"

    def __init__(self, *, response: str = "{}", cost_cents: int = 0) -> None:
        self.response = response
        self.cost_cents = cost_cents
        self.calls: list[dict[str, Any]] = []

    async def complete(
        self,
        messages: list[AIMessage],
        *,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 1200,
        response_format: str | None = None,
    ) -> AICompletion:
        self.calls.append(
            {
                "messages": [m.model_dump() if hasattr(m, "model_dump") else vars(m) for m in messages],
                "model": model,
                "temperature": temperature,
                "response_format": response_format,
            }
        )
        return AICompletion(
            text=self.response,
            usage=AIUsage(prompt_tokens=10, completion_tokens=20, total_tokens=30, cost_cents=self.cost_cents),
            model=model or "fake-model",
            latency_ms=1,
        )


def _finish_reason(data: dict[str, Any]) -> str | None:
    try:
        return data["choices"][0].get("finish_reason")
    except (KeyError, IndexError, TypeError):  # pragma: no cover
        return None


def _safe_error_text(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except Exception:  # noqa: BLE001
        return response.text[:300]
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or "")[:300]
    return str(payload)[:300]


_provider: object | None = None


def get_ai_provider():
    global _provider
    if _provider is None:
        _provider = NullProvider() if settings.AI_PROVIDER == "null" else OpenAIProvider()
    return _provider


def set_ai_provider(provider: object) -> None:
    """Override the provider (tests inject ``FakeAIProvider``)."""
    global _provider
    _provider = provider


def reset_ai_provider() -> None:
    global _provider
    _provider = None
