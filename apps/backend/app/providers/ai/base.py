"""AI provider abstraction.

The rest of the codebase talks to :class:`AIProvider`, never to a vendor SDK,
so a second model vendor is a new class plus one config value.

Hard product rules enforced *here* (not in prompts alone):

* The provider returns usage so cost can be metered and budgeted.
* ``NullProvider`` exists so a deployment without an API key fails loudly with
  a clear error instead of silently returning fabricated content.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.core.exceptions import ExternalServiceError


@dataclass(frozen=True)
class AIUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_cents: int = 0


@dataclass(frozen=True)
class AICompletion:
    text: str
    usage: AIUsage
    model: str
    raw: dict[str, Any] = field(default_factory=dict)
    latency_ms: int = 0


@dataclass(frozen=True)
class AIMessage:
    role: str  # system | user | assistant
    content: str


class AIProvider(Protocol):
    name: str

    async def complete(
        self,
        messages: list[AIMessage],
        *,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 1200,
        response_format: str | None = None,
    ) -> AICompletion: ...


def parse_json_object(text: str) -> dict[str, Any]:
    """Models occasionally wrap JSON in prose or a code fence. Extract it."""
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = candidate.strip("`")
        if candidate.lower().startswith("json"):
            candidate = candidate[4:]
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ExternalServiceError("The AI response was not valid JSON.")
    try:
        parsed = json.loads(candidate[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ExternalServiceError("The AI response was not valid JSON.") from exc
    if not isinstance(parsed, dict):
        raise ExternalServiceError("The AI response was not a JSON object.")
    return parsed
