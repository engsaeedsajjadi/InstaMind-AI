# AI & Agent Documentation

## 1. Design stance

The AI is a **drafting assistant**, not an autonomous publisher. Two rules
override everything else:

1. **It may only state facts the workspace verified.** Brand profiles carry
   `verified_claims`; the system prompt says those are the only facts it may
   use. Prices, stock, delivery times, certifications and reviews that are not
   in that list must not appear.
2. **Nothing it produces is published without a human.** AI output lands in a
   content draft. Publishing still requires approval and the `content:publish`
   permission.

If the AI is unavailable or unconfigured, the API returns `503` with a clear
message. It never invents content to fill the gap.

## 2. Provider abstraction

```python
class AIProvider(Protocol):
    async def complete(self, messages, *, model=None, temperature=0.7,
                       max_tokens=1200, response_format=None) -> AICompletion
```

| Provider | Use |
|---|---|
| `OpenAIProvider` | Production. Any OpenAI-compatible endpoint via `OPENAI_BASE_URL`. |
| `NullProvider` | Default when `AI_PROVIDER=null`. Refuses with 503. |
| `FakeAIProvider` | Tests. Deterministic output, records every prompt. |

Adding a vendor = one class + one config value. No call site changes.

## 3. Cost control

Every completion is metered into `ai_generations` (tokens, latency, cost) and
aggregated into `usage_records` under `ai_cost_cents` for the calendar month.

The budget check happens **before** the provider call:

```
spent >= budget  →  402 quota_exceeded  (provider is never invoked)
```

Costs are computed from a price table in USD cents per million tokens and
**rounded up**, so usage is never under-billed. The table is the thing to update
when OpenAI changes prices; it lives in
`app/providers/ai/openai_provider.py::PRICING_CENTS_PER_M`.

## 4. Guardrails

| Guard | Where | Behaviour |
|---|---|---|
| No invented claims | system prompt | instructs the model to omit unverifiable facts |
| Unverifiable-phrase scan | `_guard_output` | flags phrases like "تضمین ۱۰۰", "clinically proven", "best in the world" |
| Banned words | `_guard_output` | flags any word in `brand_profiles.banned_words` |
| Length | `_guard_output` | flags captions over 2200 chars |
| Malformed output | `generate_content_plan` | records `FAILED`, returns `status: FAILED` + warning |

Flags are returned to the client **and** stored on the generation row. They
never silently rewrite the text — a human decides.

## 5. Implemented capabilities

### Caption
`POST /api/v1/ai/generate-caption`

Inputs: topic, brand profile, language (`fa-IR`/`en-US`), tone, hashtag count,
CTA toggle, product names, optional `content_id`.
Output: caption, hashtags, CTA, model, cost, warnings. With `content_id` the
result is written back to the draft and `ai_generated` is set.

### Content plan
`POST /api/v1/ai/generate-content-plan`

Inputs: horizon days, posts per week, language, goals, brand profile.
Output: a list of `{day_index, content_type, idea, caption_draft, hashtags}`.
Item count is derived from the horizon and validated against what the model
returned.

### Usage
`GET /api/v1/ai/usage` → `{ai_cost_cents_month, budget_cents}`.

## 6. Agent orchestration (M4 — designed, not built)

The tables exist (`agent_runs`, `ai_tasks`, `agent_tool_calls`); the runtime
does not yet. The design that will be implemented:

```
user instruction
  → Planner      analyse goal, read brand profile, produce a plan
  → Executor     create ai_tasks, run them in order
  → QC           run the guardrails, flag issues
  → Approval     AWAITING_APPROVAL until a human confirms
  → Result       record outcome and spend
```

### Tool permissions

Tools are capability-gated. A sensitive tool cannot run without an explicit
permission **and** an approval row:

| Tool | Permission | Approval |
|---|---|---|
| `draft_content` | `ai:generate` | no |
| `suggest_caption` | `ai:generate` | no |
| `publish_content` | `content:publish` | **yes** |
| `send_direct_message` | `inbox:reply` | **yes** |
| `hide_comment` / `delete_comment` | `comments:hide` | **yes** |
| `update_settings` | `settings:write` | **yes** |
| `change_subscription` | `billing:manage` | **yes** |

Mapped in `app/core/permissions.py::SENSITIVE_AGENT_TOOL_PERMISSIONS`.

Rules that will not be relaxed:

* An agent runs inside one workspace and gets a `TenantScope` like any caller.
* An agent has a per-run budget; exceeding it stops the run.
* Every tool call is written to `agent_tool_calls` with arguments and result,
  and `audit_logs` records the actor as `AGENT`.
* There is no "run arbitrary code" tool and no tool that can grant permissions.

## 7. Prompt hygiene

* Prompts are built from database facts, not from free-form user input
  interpolated into instructions.
* User input is data inside a clearly delimited block, never instructions.
* Outputs are parsed as JSON (`response_format=json`) and validated; a parse
  failure is a recorded failure, not a silent empty result.
* Prompts and outputs are stored, so a bad generation can be reproduced and
  the prompt improved.

## 8. Data handling

* Brand facts and generated text are tenant data, stored in Postgres.
* Nothing is sent to a provider beyond what the prompt needs.
* No customer DM content is sent to an AI provider unless the workspace
  explicitly enabled AI reply suggestions (M3, off by default).
* Provider keys live in the environment; `NullProvider` is the safe default.
