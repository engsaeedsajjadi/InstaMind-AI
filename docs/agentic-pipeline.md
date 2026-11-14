# Agentic content pipeline

Branch: `feat/agentic-content-pipeline` (local only — never pushed).

One content item is one `AgentRun` driven by five agents over the Celery job
queue. Nothing auto-publishes: a human approves every item before the
Publisher runs, and the Publisher verifies the post is live before the run
is marked published.

## Architecture

```
POST /api/v1/agents/runs            → AgentRun (stage QUEUED)
        │  beat: instamind.agents.process_due_runs (every 60 s)
        ▼
PLANNING  (PlannerAgent  → AIContentService.generate_content_plan)
WRITING   (WriterAgent   → AIContentService.generate_reel_script / generate_caption)
DESIGNING (DesignerAgent → render.py: PIL + libraqm cover/scene frames, ffmpeg slideshow)
QUALITY_GATE (QualityGateAgent → quality.py: pixel + motion checks on the real media)
AWAITING_APPROVAL  ← human approves/rejects via POST /runs/{id}/approve|reject
PUBLISHING (PublisherAgent → PublishingService (official Meta API) + live verify)
PUBLISHED
        │  daily: instamind.agents.analyse_published_runs (04:30)
        ▼
AnalystAgent → Insights API → AnalyticsSnapshot rows → recommendations
```

Module layout (`apps/backend/app/modules/agents/`):

| File | Contents |
| --- | --- |
| `state.py` | Stage enum, allowed transitions, retry/rework bounds |
| `agents.py` | The five agent classes + `AgentContext` |
| `orchestrator.py` | `PipelineOrchestrator`: state machine, retries, dead letter, audit, approval |
| `render.py` | Cover / scene-frame / overlay rendering, slideshow + composite reels, frame extraction |
| `quality.py` | Quality-gate checks (pure functions, unit-tested) |
| `harness_revision.py` | Self-revising harness: held-out scoring set, versioned `HarnessConfig`, deterministic proposer, bounded revision loop |
| `__init__.py` | Public exports |

Also in this branch:

- `app/modules/storage/` — **restored**. The M1 commit imported
  `app.modules.storage.backend` from the API routes and the worker, but the
  package did not exist, so 24 integration tests errored. It now exists:
  magic-byte upload validation, `LocalStorage` (default) and `S3Storage`
  (fails loudly without boto3), `build_storage_key`, `get_storage`.
  **Root cause found**: the repo `.gitignore` has a bare `storage/` pattern
  (meant for runtime upload dirs) that matches *any* directory named
  `storage` — including this module — so `git add` silently skipped it.
  This branch adds a negation (`!apps/backend/app/modules/storage/`) to
  `.gitignore`; without it the module can never be committed.
- `app/modules/ai/service.py` — new `generate_reel_script()` (hook, timed
  scenes, cover title, caption, hashtags; hook forced to be scene 1 so it
  lands in the first 3 s) with the same budget/audit/record contract as
  `generate_caption`. New system prompt `REEL_SCRIPT_SYSTEM_PROMPT`.
- `app/api/routes/agents.py` — `POST /runs`, `GET /runs/{id}`,
  `POST /runs/{id}/approve|reject|cancel`, `GET /runs/{id}/stage`.
  Registered in `app/main.py` under `/api/v1/agents`.
- `app/worker/tasks.py` + `celery_app.py` — `instamind.agents.process_run`,
  `instamind.agents.process_due_runs` (60 s, `publishing` queue),
  `instamind.agents.analyse_published_runs` (daily 04:30).
- `pyproject.toml` — `pillow>=10.0` added (render/quality need PIL in-process).

## Self-revising harness ("The End of Harness Engineering")

The idea, adapted from the paper of that title: *design the test before
you write the code.* Instead of hand-tweaking the pipeline's prompts and
render parameters every time quality slips, the pipeline grades itself
against a fixed exam and proposes its own harness changes — but a human
still approves every change.

- **The exam** — `DEFAULT_SCORING_SET` in `harness_revision.py`: 6
  held-out Persian topics (4 REELS + 2 IMAGE), distinct from every
  development/test topic, graded with the *existing* quality gate
  (`run_quality_gate`) on real rendered media. The per-check weights
  (cover title 2, motion 2, hook/caption/overlay 1) come from the user's
  quality standard, not invented metrics. The proposer never sees the
  topics: an `ExecutionReport` carries only case ids and failure check
  names.
- **Frozen solver** — during a revision round the Writer/Planner system
  prompts in `app/modules/ai/service.py` are never mutated. Only the
  versioned `HarnessConfig` changes: title font (118 px, rework 140 px),
  slideshow zoom (0.0016, rework 0.0032), scene/overlay font sizes, and a
  writer instruction addendum that is passed per-run to the *user* prompt
  (never written into the live defaults). `DesignerAgent` accepts an
  optional `HarnessConfig` and defaults to the current values, so
  existing behaviour is unchanged when no config is passed.
- **Proposer** — deterministic and rule-based in v1 (no new LLM call, no
  API key): cover-title failure → larger title font; motion failure →
  stronger zoom; overlay failure → larger overlay font; hook/caption
  failure → a tighter writer instruction. Each proposal is an auditable
  `HarnessEdit` (`target`, `old_value`, `new_value`, `reason`,
  `failure_evidence`).
- **Bounded loop** — `run_harness_revision_round(run_id)` evaluates the
  baseline on the exam, proposes at most 3 edits, evaluates the single
  candidate on the *same* set, and keeps it only on strict score
  improvement; a worse candidate is discarded and the loop stops. At most
  3 rounds. Every round is recorded in `result["harness_revision"]` and
  as an `AITask` + audit row.
- **Human approval (two separate gates)** — a kept candidate becomes a
  *proposal* in `plan["harness_proposal"]` and never activates itself.
  `approve_harness_revision` / `reject_harness_revision` (both raise
  `IllegalStateTransition` with no proposal pending) make it active or
  discard it. Approving a harness does **not** approve any content: the
  existing `AWAITING_APPROVAL → PUBLISHING` human gate is untouched.

**Not verified / not built**: no live-LLM proposer (the proposer is
rules, not a model); no live Meta run; the Celery beat/worker loop
end-to-end is still unverified (pre-existing); the S3 temp-download
limitation still applies — scoring runs use local storage / a tmp
workdir, like the existing tests. The revision loop is exercised with
the routing fake provider + real ffmpeg rendering and the real quality
gate (`tests/test_harness_revision.py`).

## Job states

`QUEUED → PLANNING → WRITING → DESIGNING → QUALITY_GATE → AWAITING_APPROVAL
→ PUBLISHING → PUBLISHED`, plus terminal `FAILED`, `DEAD`, `CANCELLED`.

- The coarse status lives on `AgentRun.status`; the fine stage lives in
  `AgentRun.plan["stage"]`. Per-stage work is recorded as `AITask` rows.
- Only the transitions in `state.py:ALLOWED_TRANSITIONS` exist. In
  particular `PUBLISHING` is reachable **only** from `AWAITING_APPROVAL`.
- A failed quality gate routes back to `DESIGNING` with the reasons, at
  most 2 rework loops; the 3rd failure dead-letters the run.
- A stage that raises is retried on the next beat tick. After 3 attempts
  the run is dead-lettered: status `FAILED`, `result["dead_letter"]=True`,
  `result["failed_stage"]` + `result["error"]`, and an
  `agents.pipeline_dead_letter` audit row. Nothing is silently lost.
  (The orchestrator deliberately does not re-raise: the Celery task wrapper
  rolls the session back on exception, which would lose the attempt count.)
- `AgentRun.budget_cents` caps a run's AI spend (0 = uncapped); the monthly
  workspace budget in `docs/ai-agents.md` still applies on top.

## Agent responsibilities

- **Planner** — calls `generate_content_plan` (1 item) with the topic plus
  the Analyst's last recommendations as goals. Decides format/angle.
- **Writer** — reels: `generate_reel_script` (Persian only; Latin words must
  already be transliterated — the renderer font has no Latin glyphs).
  Images/carousels: `generate_caption`, hook = first line. The hook is
  forced to be the first scene.
- **Designer** — renders the cover (bold title box, fixed position), one
  full scene frame per scene, and a reel. With no base video the reel is a
  `zoompan` slideshow (real motion); with a base video the scene overlays
  are composited with plain `overlay` + `enable` windows — **never** `fade`
  filters (with `alpha=1` they silently render the overlay transparent).
  Uploads via `StorageBackend` → `MediaAsset` rows, creates the `Content`
  row (DRAFT). For reels only the video is in `media_asset_ids`; the cover
  travels as `cover_asset_id`. On gate rework the Designer re-renders with
  a larger title font (cover failure) or a stronger zoom (motion failure).
- **Quality gate** (blocking, automated) — checks the media bytes, never the
  Designer's claims:
  - hook exists, 5–140 chars;
  - cover title: bright (white) pixels inside the fixed title box;
  - overlays: opaque pixels inside the text box;
  - motion: mean absolute frame difference ≥ 1.0 and peak ≥ 1.5
    (calibrated 2026-11-11: a looped still measures exactly 0.0, a gentle
    zoompan slideshow ~2.4 mean / ~2.7 peak);
  - caption ≤ 2200 chars, ≤ 30 hashtags, no banned words.
- **Publisher** — publishes only through the existing `PublishingService`
  (official Meta Graph API; no CLI, no scraping). Idempotency key
  `agent-run:<run_id>`; an already-SUCCEEDED job is re-verified instead of
  re-published; a QUEUED job or one with an expired lease is driven forward
  instead of duplicated; a RUNNING job with a live lease refuses a second
  publish. After publishing, the media is fetched back with `get_media` —
  a container ID alone is never proof. Requires the `ContentApproval` row
  the human approval created.
- **Analyst** — pulls Insights for published media, stores exactly what
  Meta returns: a missing metric is stored as `value=NULL`,
  `is_available=False` — never zero, never invented. Computes per-format
  view averages from available snapshots only and writes recommendations
  into `run.result["analyst"]`, which the Planner injects as goals next
  time.

## Audit & tenant isolation

- Every stage transition, approval, rejection, cancellation, gate failure,
  dead letter and publish is an `AuditLog` row; every AI call is an
  `AIGeneration` row with cost (the run's `spent_cents` accumulates it).
- All queries go through the caller's `TenantScope`: a run is invisible
  from another workspace (`NotFoundError`).

## How to run it locally

```bash
cd apps/backend
.venv/bin/python -m pytest tests/test_agentic_pipeline.py tests/test_storage_backend.py -q
```

Full flow (needs a worker + beat; the API only starts runs):

```bash
# API + worker + beat (from the repo root, see README / docker-compose)
celery -A app.worker.celery_app worker -Q default,publishing -l info
celery -A app.worker.celery_app beat -l info
```

```http
POST /api/v1/agents/runs
{"topic": "معرفی قهوه اتیوپی تازه‌برشت", "content_type": "REELS", "account_id": "<uuid>"}
# → 201, stage QUEUED; the beat drives it to AWAITING_APPROVAL
POST /api/v1/agents/runs/<id>/approve   # {"comment": "خوبه"}
# → stage PUBLISHING; the beat publishes and verifies it live
GET  /api/v1/agents/runs/<id>
```

Prerequisites: `ffmpeg`/`ffprobe` on `PATH` (rendering + gate), Noto Sans
Arabic fonts installed (correct Persian shaping; without them the renderer
falls back to PIL's default font and records `font_fallback=True` in the
manifest — text presence still gates, shaping does not), a configured AI
provider (`OPENAI_API_KEY`), and a connected Instagram account.

## What is stubbed vs real — honest status

**Real and tested** (193 tests green, see below): the state machine, the
quality gate on real rendered media (cover/overlay/motion/caption), the
full run to `AWAITING_APPROVAL` with real ffmpeg rendering, approval
blocking, approve → publish through the real `PublishingService` against a
scripted Meta transport with live verification, rejection, dead-letter
after 3 failed attempts, bounded gate rework, tenant isolation, Analyst
NULL handling, the storage backend (magic-byte validation, local
round-trip, path-traversal rejection), and reel-script provider failure
(recorded as a FAILED generation, raised as `ServiceUnavailable`).

**Real but not run end-to-end in this sandbox**: the Celery beat/worker
loop (no Redis/Postgres here — the beat tasks are registered and importable,
and the orchestrator is exercised directly in tests); a publish against
the live Meta API (tests use the scripted `FakeTransport`); the API routes
against a live server (registered in `app/main.py`, exercised only via
imports — no HTTP test client run yet).

**Known limitations**:

- The quality gate reads the Designer's local working files. With the S3
  backend the worker must download the media to a temp dir first — not
  implemented; the gate currently only works with local storage.
- The beat task has no distributed lease on `AgentRun` rows (the publishing
  service has one; agent runs do not). With a single worker this is safe;
  with several workers two could drive the same run. The Publisher's
  idempotency key makes the only irreversible step (publishing) safe under
  double-processing; AI spend and renders could duplicate.
- A stage that fails after creating a `Content` row leaves that DRAFT row
  behind (never published, never visible as published).
- `validate_publish_request` failures leave the `PublishingJob` QUEUED in
  the publishing service (pre-existing behaviour); the Publisher agent
  drives such a job forward on the next attempt instead of duplicating it.
- Carousel rendering is not implemented: the Designer renders a cover +
  reel for `REELS` and a single image for `IMAGE`. `CAROUSEL` is rejected
  at `start_run` with a clear error until multi-image rendering is built.

## Test results (2026-11-11, this sandbox)

- Full suite: **209 passed, 0 failed** (193 pre-existing + 16 new).
  Pre-existing baseline before this branch was 143 passed / 24 errors —
  all 24 errors were the missing `app/modules/storage/` package, restored
  here, so the pre-existing suite is green for the first time in this
  checkout.
- New: `tests/test_agentic_pipeline.py` (19 tests), `tests/test_storage_backend.py` (7 tests),
  `tests/test_harness_revision.py` (16 tests: exam held-out, proposer
  mapping/bounds, pure loop keep/discard, Designer config, orchestrator
  e2e proposal → approve/reject).
- `ruff check` passes on all touched files. `ruff format` is not enforced
  in this repo (pre-existing files are not format-clean either), so no
  reformatting was applied. `mypy` was not run as a gate.
