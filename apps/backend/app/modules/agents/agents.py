"""The five agents of the content pipeline.

Each agent is a small class with one job. The agents do not talk to the
database directly except through the services they are given; the
:class:`PipelineOrchestrator` owns the state machine, retries and audit.

* :class:`PlannerAgent` — decides *what* to make (format, angle, publish
  window) from a topic.
* :class:`WriterAgent` — writes the Persian copy: hook, timed scenes, cover
  title, caption, hashtags. All Persian, all from brand facts only.
* :class:`DesignerAgent` — renders the media (cover, scene frames, reel)
  and uploads it to storage.
* :class:`QualityGateAgent` — the automated, blocking gate. It inspects the
  media bytes, never the Designer's claims.
* :class:`PublisherAgent` — publishes only through the official Meta API
  publishing service, then verifies the post is live before reporting
  success. Never starts a second publish while one is unverified.
* :class:`AnalystAgent` — reads Insights, stores exactly what Meta returns
  (missing metrics stay NULL), and turns per-format aggregates into
  recommendations the Planner can use next time.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from app.core.exceptions import ValidationError
from app.modules.agents.harness_revision import DEFAULT_HARNESS, HarnessConfig
from app.modules.agents.quality import QualityReport, run_quality_gate
from app.modules.agents.render import (
    POST_SIZE,
    composite_reel,
    probe_duration,
    render_cover,
    render_overlay,
    render_scene_frame,
    slideshow_reel,
)
from app.modules.ai.service import AIContentService
from app.modules.inbox.models import AnalyticsSnapshot
from app.modules.storage.backend import StorageBackend, build_storage_key, validate_upload

# --------------------------------------------------------------------------- #
# Shared context passed to every agent.
# --------------------------------------------------------------------------- #


@dataclass
class AgentContext:
    """Everything an agent needs, assembled by the orchestrator."""

    run_id: uuid.UUID
    workspace_id: uuid.UUID
    topic: str
    content_type: str  # REELS | IMAGE | CAROUSEL
    account_id: uuid.UUID | None = None
    brand_profile_id: uuid.UUID | None = None
    base_video_path: Path | None = None
    workdir: Path = Path("/tmp/instamind-agent-runs")
    banned_words: list[str] = field(default_factory=list)
    analyst_recommendations: list[str] = field(default_factory=list)


def split_title(title: str, *, max_chars: int = 22) -> list[str]:
    """Split a cover title into short lines for the title box."""
    words = title.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) <= max_chars:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines[:3] or [title]


# --------------------------------------------------------------------------- #
# Planner
# --------------------------------------------------------------------------- #


class PlannerAgent:
    """Decides what to make. Uses the LLM plan service (budget-checked, audited
    like any other AI call) and folds the Analyst's recommendations in."""

    def __init__(self, ai: AIContentService) -> None:
        self.ai = ai

    async def plan(self, ctx: AgentContext) -> dict:
        result = await self.ai.generate_content_plan(
            horizon_days=1,
            posts_per_week=1,
            language="fa-IR",
            goals=[ctx.topic, *ctx.analyst_recommendations],
        )
        if not result.items:
            raise ValidationError(
                "Planner produced an empty content plan.",
                context={"code": "empty_plan"},
            )
        item = result.items[0]
        # CAROUSEL rendering is not implemented yet — the Planner must not
        # choose it; fall back to the run's requested format instead.
        content_type = item.content_type if item.content_type in {"IMAGE", "REELS"} else ctx.content_type
        return {
            "content_type": content_type,
            "idea": item.idea,
            "caption_draft": item.caption_draft,
            "hashtags": item.hashtags,
            "generation_id": str(result.generation_id),
            "cost_cents": result.cost_cents,
        }


# --------------------------------------------------------------------------- #
# Writer
# --------------------------------------------------------------------------- #


class WriterAgent:
    """Writes the Persian copy. For reels the structured reel script
    (hook == first scene); for images/carousels a caption, with the hook
    taken from its first line."""

    def __init__(self, ai: AIContentService) -> None:
        self.ai = ai

    async def write(
        self, ctx: AgentContext, *, idea: str, instruction_addendum: str | None = None
    ) -> dict:
        """``instruction_addendum`` is harness state, not a prompt edit: it
        is passed per-run to the AI service, which appends it to the *user*
        prompt. The live system prompts in ``ai/service.py`` are never
        mutated — during a revision round the solver (prompts + model) is
        frozen and only the harness config changes."""
        if ctx.content_type == "REELS":
            script = await self.ai.generate_reel_script(
                topic=idea or ctx.topic, extra_instructions=instruction_addendum
            )
            return {
                "hook": script.hook,
                "scenes": script.scenes,
                "cover_title": script.cover_title,
                "caption": script.caption,
                "hashtags": script.hashtags,
                "generation_id": str(script.generation_id),
                "cost_cents": script.cost_cents,
            }
        result = await self.ai.generate_caption(
            topic=idea or ctx.topic, language="fa-IR", extra_instructions=instruction_addendum
        )
        caption = result.caption
        hook = caption.split("\n")[0].strip()[:140]
        return {
            "hook": hook,
            "scenes": [{"text": hook, "start_s": 0.0, "end_s": 2.5}],
            "cover_title": hook,
            "caption": caption,
            "hashtags": result.hashtags,
            "generation_id": str(result.generation_id),
            "cost_cents": result.cost_cents,
        }


# --------------------------------------------------------------------------- #
# Designer
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class DesignedMedia:
    """What the Designer produced: local working files + uploaded assets."""

    cover_path: Path
    video_path: Path | None
    overlay_paths: list[Path]
    media_asset_ids: list[str]
    cover_asset_id: str | None
    title_font_px: int
    motion_source: str
    harness_version: int = DEFAULT_HARNESS.version


class DesignerAgent:
    """Renders the media and uploads it. Never invents text: every string on
    screen comes from the Writer's script. Rework hints from a failed quality
    gate (``gate_failures``) are honoured: a failed cover title re-renders
    with a larger font; failed motion rebuilds the slideshow with a stronger
    zoom. Render parameters come from ``harness_config`` (default
    :data:`DEFAULT_HARNESS`, i.e. exactly the values this class used to
    hard-code: title 118 px / rework 140 px, zoom 0.0016 / rework 0.0032) —
    passing a different config changes the rendering, never the prompts."""

    def __init__(self, storage: StorageBackend, *, workdir: Path) -> None:
        self.storage = storage
        self.workdir = workdir

    async def design(
        self,
        ctx: AgentContext,
        *,
        script: dict,
        gate_failures: list[str],
        store_media,
        harness_config: HarnessConfig | None = None,
    ) -> DesignedMedia:
        config = harness_config or DEFAULT_HARNESS
        run_dir = self.workdir / str(ctx.run_id)
        run_dir.mkdir(parents=True, exist_ok=True)

        title_font_px = config.rework_title_font_px if any("cover_title" in failure for failure in gate_failures) else config.title_font_px

        cover_title = str(script.get("cover_title") or script.get("hook") or ctx.topic)
        cover_path = run_dir / "cover.jpg"
        size = POST_SIZE if ctx.content_type == "IMAGE" else (1080, 1920)
        render_cover(
            cover_path,
            title_lines=split_title(cover_title),
            size=size,
            title_px=title_font_px,
        )

        overlay_paths: list[Path] = []
        video_path: Path | None = None
        motion_source = "none"
        if ctx.content_type == "REELS":
            scenes = script.get("scenes") or []
            frame_paths: list[Path] = []
            for i, scene in enumerate(scenes):
                frame = run_dir / f"frame-{i:02d}.jpg"
                render_scene_frame(frame, lines=[str(scene["text"])], font_px=config.scene_font_px)
                frame_paths.append(frame)
            if ctx.base_video_path is not None and ctx.base_video_path.is_file():
                # Composite the scene overlays onto the supplied base video.
                overlays: list[tuple[Path, float, float]] = []
                for i, scene in enumerate(scenes):
                    overlay = run_dir / f"overlay-{i:02d}.png"
                    render_overlay(overlay, lines=[str(scene["text"])], font_px=config.overlay_font_px)
                    overlay_paths.append(overlay)
                    overlays.append((overlay, float(scene.get("start_s", 0)), float(scene.get("end_s", 0))))
                video_path = run_dir / "reel.mp4"
                composite_reel(ctx.base_video_path, overlays, video_path)
                motion_source = "base-video"
            else:
                video_path = run_dir / "reel.mp4"
                zoom_step = config.rework_zoom_step if any("motion" in f for f in gate_failures) else config.zoom_step
                slideshow_reel(frame_paths, video_path, seconds_per_image=2.5, zoom_step=zoom_step)
                motion_source = "slideshow-zoompan"

        # Upload: validate bytes, store, hand back MediaAsset rows.
        media_asset_ids: list[str] = []
        cover_asset_id: str | None = None
        cover_bytes = cover_path.read_bytes()
        mime, _ = validate_upload(data=cover_bytes, declared_filename="cover.jpg", kind="IMAGE")
        stored = await self.storage.put(
            build_storage_key(kind="IMAGE", mime=mime, original_filename="cover.jpg"),
            cover_bytes,
            content_type=mime,
        )
        cover_asset = await store_media(
            kind="IMAGE",
            storage_backend=stored.storage_backend,
            storage_key=stored.key,
            bucket=stored.bucket,
            detected_mime=stored.detected_mime,
            size_bytes=stored.size_bytes,
            checksum_sha256=stored.checksum_sha256,
            width=size[0],
            height=size[1],
        )
        cover_asset_id = str(cover_asset.id)
        media_asset_ids.append(cover_asset_id)
        if video_path is not None:
            video_bytes = video_path.read_bytes()
            mime, _ = validate_upload(data=video_bytes, declared_filename="reel.mp4", kind="VIDEO")
            stored = await self.storage.put(
                build_storage_key(kind="VIDEO", mime=mime, original_filename="reel.mp4"),
                video_bytes,
                content_type=mime,
            )
            duration_ms = int(probe_duration(video_path) * 1000)
            video_asset = await store_media(
                kind="VIDEO",
                storage_backend=stored.storage_backend,
                storage_key=stored.key,
                bucket=stored.bucket,
                detected_mime=stored.detected_mime,
                size_bytes=stored.size_bytes,
                checksum_sha256=stored.checksum_sha256,
                width=720,
                height=1280,
                duration_ms=duration_ms,
            )
            media_asset_ids.append(str(video_asset.id))

        return DesignedMedia(
            cover_path=cover_path,
            video_path=video_path,
            overlay_paths=overlay_paths,
            media_asset_ids=media_asset_ids,
            cover_asset_id=cover_asset_id,
            title_font_px=title_font_px,
            motion_source=motion_source,
            harness_version=config.version,
        )


# --------------------------------------------------------------------------- #
# Quality gate
# --------------------------------------------------------------------------- #


class QualityGateAgent:
    """Runs the automated gate over the Designer's media. A failure is a
    failure: the gate never rewrites media and never passes on trust."""

    def check(
        self,
        *,
        script: dict,
        cover_path: Path | None,
        overlay_paths: list[Path],
        video_path: Path | None,
        caption: str,
        banned_words: list[str],
        title_font_px: int,
    ) -> QualityReport:
        return run_quality_gate(
            script=script,
            cover_path=cover_path,
            overlay_paths=overlay_paths,
            video_path=video_path,
            caption=caption,
            banned_words=banned_words,
            title_font_px=title_font_px,
        )


# --------------------------------------------------------------------------- #
# Publisher
# --------------------------------------------------------------------------- #


class PublisherAgent:
    """Publishes through the existing official Meta API publishing service.
    Two hard rules:

    * Never start a duplicate publish while one is unverified: the
      idempotency key ``agent-run:<run_id>`` is looked up first, and a job
      that already SUCCEEDED is re-verified instead of re-published.
    * A publish is not ``PUBLISHED`` until the media is fetched back from
      the Graph API — a container creation alone is not proof.
    """

    def __init__(self, publishing_service) -> None:
        self.publishing = publishing_service

    async def publish(
        self,
        *,
        content_id: uuid.UUID,
        account_id: uuid.UUID,
        run_id: uuid.UUID,
        verify_client,
    ) -> dict:
        from app.modules.publishing.models import PublishingJob

        key = f"agent-run:{run_id}"
        existing = (
            await self.publishing.session.execute(
                select(PublishingJob).where(
                    PublishingJob.workspace_id == self.publishing.scope.workspace_id,
                    PublishingJob.idempotency_key == key,
                )
            )
        ).scalars().all()
        for job in existing:
            if job.state == "SUCCEEDED":
                media_id = (job.result or {}).get("media_id")
                if media_id:
                    await self._verify_live(verify_client, media_id)
                    return {"media_id": media_id, "permalink": (job.result or {}).get("permalink", ""), "reused": True}
            if job.state == "FAILED":
                raise ValidationError(
                    f"Publish for this run already failed: {job.last_error_message or 'unknown error'}.",
                    context={"code": "publish_failed", "job_id": str(job.id)},
                )
            if job.state == "RUNNING" and job.lease_expires_at and job.lease_expires_at > datetime.now(UTC):
                raise ValidationError(
                    "A publish for this run is already in flight and unverified; refusing to start a duplicate.",
                    context={"code": "publish_in_flight"},
                )
            # QUEUED, or a RUNNING job whose lease expired (a worker died
            # mid-publish): drive the existing job forward instead of
            # creating a second one. The idempotency key guarantees Meta
            # sees one publish.
            result = await self.publishing.process_job(job, worker_id=f"agent-run-{run_id}")
            await self._verify_live(verify_client, result.media_id)
            return {"media_id": result.media_id, "permalink": result.permalink, "reused": False}

        job = await self.publishing.enqueue(
            content_id=content_id,
            social_account_id=account_id,
            idempotency_key=key,
            requires_approval=True,
        )
        result = await self.publishing.process_job(job, worker_id=f"agent-run-{run_id}")
        await self._verify_live(verify_client, result.media_id)
        return {"media_id": result.media_id, "permalink": result.permalink, "reused": False}

    async def _verify_live(self, verify_client, media_id: str) -> None:
        """Fetch the media back from the Graph API. A missing media raises —
        the run is not marked published on a container ID alone."""
        detail = await verify_client.get_media(media_id, fields="id,permalink,media_type,timestamp")
        if str(detail.get("id")) != str(media_id):
            raise ValidationError(
                f"Published media {media_id} could not be verified live on the profile.",
                context={"code": "publish_not_verified"},
            )


# --------------------------------------------------------------------------- #
# Analyst
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AnalystReport:
    snapshots: list[dict]
    recommendations: list[str]
    missing_metrics: list[str]


class AnalystAgent:
    """Reads Insights for a published media, stores exactly what Meta
    returns (a missing metric is stored as NULL, never as zero), and
    computes per-format recommendations from the workspace's available
    snapshots."""

    INSIGHT_METRICS = ["views", "likes", "comments", "shares", "reach", "saved"]

    def __init__(self, session, scope) -> None:
        self.session = session
        self.scope = scope

    async def analyse(
        self,
        *,
        client,
        media_id: str,
        account_id: uuid.UUID,
        content_type: str,
        since: datetime,
        until: datetime,
    ) -> AnalystReport:
        try:
            response = await client.get_insights(
                media_id, metric=list(self.INSIGHT_METRICS), period="day", since=int(since.timestamp()), until=int(until.timestamp())
            )
        except Exception:  # noqa: BLE001 - Insights is best-effort; absence is data
            response = {"data": []}

        snapshots: list[dict] = []
        missing: list[str] = []
        values: dict[str, float | None] = {}
        for entry in response.get("data") or []:
            metric = str(entry.get("name") or "")
            metric_values = entry.get("values") or []
            value = None
            if metric_values:
                raw = metric_values[-1].get("value")
                if raw is not None:
                    value = float(raw)
            values[metric] = value
            if value is None:
                missing.append(metric)
            snapshot = AnalyticsSnapshot(
                workspace_id=self.scope.workspace_id,
                social_account_id=account_id,
                media_id=media_id,
                metric=metric,
                period="DAY",
                since=since,
                until=until,
                value=value,
                breakdown={"content_type": content_type},
                is_available=value is not None,
                fetched_at=datetime.now(UTC),
            )
            self.session.add(snapshot)
            snapshots.append({"metric": metric, "value": value, "is_available": value is not None})
        for metric in self.INSIGHT_METRICS:
            if metric not in values:
                missing.append(metric)
                values[metric] = None
        await self.session.flush()
        recommendations = await self._recommendations(content_type=content_type, current=values)
        return AnalystReport(snapshots=snapshots, recommendations=recommendations, missing_metrics=sorted(set(missing)))

    async def _recommendations(self, *, content_type: str, current: dict[str, float | None]) -> list[str]:
        """Per-format aggregates from *available* snapshots only. The format
        is recorded in each snapshot's ``breakdown`` at fetch time (the
        snapshots table has no content join). Formats with no data are never
        ranked against formats with data."""
        rows = (
            await self.session.execute(
                select(AnalyticsSnapshot.metric, AnalyticsSnapshot.value, AnalyticsSnapshot.breakdown).where(
                    AnalyticsSnapshot.workspace_id == self.scope.workspace_id,
                    AnalyticsSnapshot.value.is_not(None),
                )
            )
        ).all()
        by_format: dict[str, list[float]] = {}
        for metric, value, breakdown in rows:
            if metric == "views" and value is not None:
                by_format.setdefault((breakdown or {}).get("content_type") or "unknown", []).append(float(value))
        recommendations: list[str] = []
        if not by_format:
            if current.get("views") is None:
                recommendations.append(
                    "No insight data is available yet — nothing to recommend. "
                    "Keep the current format mix until Meta returns metrics."
                )
            return recommendations
        averages = {fmt: sum(vals) / len(vals) for fmt, vals in by_format.items() if vals}
        if len(averages) >= 2:
            best = max(averages, key=averages.get)
            worst = min(averages, key=averages.get)
            if best != worst:
                recommendations.append(
                    f"{best} averages {averages[best]:.0f} views vs {worst} at {averages[worst]:.0f} — "
                    f"prefer {best} for the next plan."
                )
        views = current.get("views")
        if views is not None and content_type == "REELS" and averages.get("REELS") and views < averages["REELS"] * 0.5:
            recommendations.append(
                "This reel underperformed the workspace's reel average by more than half — "
                "review the hook and the first 3 seconds before planning a similar reel."
            )
        return recommendations or ["Performance is in line with the workspace average; no change recommended."]


__all__ = [
    "AgentContext",
    "AnalystAgent",
    "AnalystReport",
    "DesignedMedia",
    "DesignerAgent",
    "PlannerAgent",
    "PublisherAgent",
    "QualityGateAgent",
    "WriterAgent",
    "split_title",
]
