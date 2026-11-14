"""Automated quality gate — blocking, and honest about what it measures.

The gate runs *before* any human approval and *before* publishing. It
checks the actual media bytes, not the Designer's claims:

* ``hook`` — a hook exists and is short enough to land in the first 3 s.
* ``cover_title`` — the cover image really has bright (white) title pixels
  inside the fixed title box the renderer uses. A cover without rendered
  text fails; a cover with text passes.
* ``overlay_text`` — each scene overlay PNG really contains rendered text
  (non-transparent pixels inside its text box).
* ``motion`` — a reel's frames actually change. A static image looped into
  a video fails; a zoompan slideshow or a base video with moving overlays
  passes. Measured as mean absolute pixel difference between sampled
  frames, not by trusting the file extension.
* ``caption`` — length/hashtag/banned-word rules (the same guard the AI
  service uses).

A failed gate blocks publishing and routes the item back to the Designer
with the failure reasons. It never rewrites the media itself and never
fabricates a pass.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

from app.modules.agents.render import extract_frames

# Bright-pixel threshold for white title text on the dark title box.
_BRIGHT = 180
# Minimum fraction of bright pixels inside the sampled title region.
_MIN_TITLE_BRIGHT_FRACTION = 0.008
# Minimum fraction of opaque pixels inside an overlay's text box.
_MIN_OVERLAY_OPAQUE_FRACTION = 0.02
# Mean absolute frame difference (0-255) below which a video is "still".
# Calibrated 2026-11-11: a looped still measures exactly 0.0, a gentle
# zoompan slideshow measures mean ~2.4 / peak ~2.7, a stronger zoom ~3.3/3.7.
_MOTION_MEAN_THRESHOLD = 1.0
# At least one frame pair must change by this much (catches a single jump).
_MOTION_PEAK_THRESHOLD = 1.5


@dataclass(frozen=True)
class QualityFailure:
    check: str
    reason: str


@dataclass(frozen=True)
class QualityReport:
    passed: bool
    failures: list[QualityFailure] = field(default_factory=list)

    def failure_reasons(self) -> list[str]:
        return [f"{f.check}: {f.reason}" for f in self.failures]


def check_hook(hook: str | None) -> QualityFailure | None:
    """The hook is the first thing a viewer reads — it must exist and be
    short enough to fit the first 3 seconds of a reel."""
    if hook is None or not hook.strip():
        return QualityFailure("hook", "No hook was produced.")
    if len(hook.strip()) > 140:
        return QualityFailure("hook", f"Hook is {len(hook.strip())} chars (max 140).")
    if len(hook.strip()) < 5:
        return QualityFailure("hook", "Hook is too short to carry a message.")
    return None


def _bright_fraction(image: Image.Image, box: tuple[int, int, int, int]) -> float:
    """Fraction of pixels brighter than ``_BRIGHT`` inside ``box``."""
    gray = image.convert("L").crop(box)
    pixels = list(gray.tobytes())
    if not pixels:
        return 0.0
    bright = sum(1 for value in pixels if value >= _BRIGHT)
    return bright / len(pixels)


def check_cover_title(cover_path: Path, *, min_font_px: int = 72) -> QualityFailure | None:
    """The cover must carry a bold title: bright pixels inside the fixed
    title box at the top of the frame (where the renderer draws it)."""
    if min_font_px < 72:
        return QualityFailure(
            "cover_title", f"Title font is {min_font_px}px (minimum 72px for a bold title)."
        )
    if not cover_path.is_file():
        return QualityFailure("cover_title", f"Cover file is missing: {cover_path.name}.")
    image = Image.open(cover_path)
    width, height = image.size
    # Title box: x in [56, W-56], y in [120, min(520, H*0.3)] — matches the renderer.
    box = (56, 120, width - 56, min(520, int(height * 0.30)))
    fraction = _bright_fraction(image, box)
    if fraction < _MIN_TITLE_BRIGHT_FRACTION:
        return QualityFailure(
            "cover_title",
            f"No rendered title text found in the cover title box "
            f"(bright-pixel fraction {fraction:.4f} < {_MIN_TITLE_BRIGHT_FRACTION}).",
        )
    return None


def check_overlay_text(overlay_path: Path) -> QualityFailure | None:
    """A scene overlay must contain rendered text: opaque pixels inside its
    lower-third text box. A transparent or blank overlay fails."""
    if not overlay_path.is_file():
        return QualityFailure("overlay_text", f"Overlay file is missing: {overlay_path.name}.")
    image = Image.open(overlay_path).convert("RGBA")
    width, height = image.size
    # Text box lives in the lower third (renderer default: H - box_h - 210).
    box = (36, int(height * 0.55), width - 36, height - 150)
    alpha = image.split()[3].crop(box)
    pixels = list(alpha.tobytes())
    if not pixels:
        return QualityFailure("overlay_text", "Overlay text box is empty.")
    opaque = sum(1 for value in pixels if value > 100)
    if opaque / len(pixels) < _MIN_OVERLAY_OPAQUE_FRACTION:
        return QualityFailure(
            "overlay_text",
            f"No rendered text in overlay {overlay_path.name} "
            f"(opaque fraction {opaque / len(pixels):.4f} < {_MIN_OVERLAY_OPAQUE_FRACTION}).",
        )
    return None


def check_motion(video_path: Path, *, sample_times: list[float] | None = None) -> QualityFailure | None:
    """A reel must have real motion: sampled frames must differ from each
    other. A single still image looped into a video has (near-)zero frame
    difference and fails."""
    if not video_path.is_file():
        return QualityFailure("motion", f"Video file is missing: {video_path.name}.")
    times = sample_times or [0.5, 1.5, 2.5, 3.5]
    workdir = video_path.parent / ".gate-frames"
    frames = extract_frames(video_path, times=times, out_dir=workdir)
    try:
        previous: list[int] | None = None
        diffs: list[float] = []
        for frame_path in frames:
            gray = Image.open(frame_path).convert("L").resize((160, 284))
            pixels = list(gray.tobytes())
            if previous is not None:
                diff = sum(abs(a - b) for a, b in zip(pixels, previous, strict=True)) / len(pixels)
                diffs.append(diff)
            previous = pixels
        if not diffs:
            return QualityFailure("motion", "Could not sample frames from the video.")
        mean_diff = sum(diffs) / len(diffs)
        peak_diff = max(diffs)
        if mean_diff < _MOTION_MEAN_THRESHOLD or peak_diff < _MOTION_PEAK_THRESHOLD:
            return QualityFailure(
                "motion",
                f"Video has no real motion (mean frame difference {mean_diff:.2f}, "
                f"peak {peak_diff:.2f}; thresholds {_MOTION_MEAN_THRESHOLD}/{_MOTION_PEAK_THRESHOLD}). "
                "An animated still does not pass.",
            )
        return None
    finally:
        for frame_path in frames:
            frame_path.unlink(missing_ok=True)
        workdir.rmdir() if workdir.exists() and not any(workdir.iterdir()) else None


def check_caption(caption: str, *, banned_words: list[str] | None = None) -> QualityFailure | None:
    """Caption rules: 2200 chars, 30 hashtags, no banned words. The same
    guard the AI service applies — here it blocks, it does not warn."""
    if len(caption) > 2200:
        return QualityFailure("caption", f"Caption is {len(caption)} chars (max 2200).")
    hashtags = [word for word in caption.split() if word.startswith("#")]
    if len(hashtags) > 30:
        return QualityFailure("caption", f"Caption has {len(hashtags)} hashtags (max 30).")
    for banned in banned_words or []:
        if banned and banned.lower() in caption.lower():
            return QualityFailure("caption", f"Caption contains banned word: {banned}.")
    return None


def run_quality_gate(
    *,
    script: dict,
    cover_path: Path | None,
    overlay_paths: list[Path],
    video_path: Path | None,
    caption: str,
    banned_words: list[str] | None = None,
    title_font_px: int = 118,
) -> QualityReport:
    """Run every applicable check. ``video_path`` is only checked for reels;
    image/carousel items skip the motion check."""
    failures: list[QualityFailure] = []
    for check in (
        check_hook(script.get("hook")),
        check_caption(caption, banned_words=banned_words),
    ):
        if check is not None:
            failures.append(check)
    if cover_path is not None:
        failure = check_cover_title(cover_path, min_font_px=title_font_px)
        if failure is not None:
            failures.append(failure)
    for overlay_path in overlay_paths:
        failure = check_overlay_text(overlay_path)
        if failure is not None:
            failures.append(failure)
    if video_path is not None:
        failure = check_motion(video_path)
        if failure is not None:
            failures.append(failure)
    return QualityReport(passed=not failures, failures=failures)


__all__ = [
    "QualityFailure",
    "QualityReport",
    "check_caption",
    "check_cover_title",
    "check_hook",
    "check_motion",
    "check_overlay_text",
    "run_quality_gate",
]
