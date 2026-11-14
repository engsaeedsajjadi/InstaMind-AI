"""Media rendering for the Designer agent.

Ports the proven patterns from the standalone render pipeline
(``~/workspace/github-instagram/render_reels.py``):

* Persian text is drawn with PIL + libraqm — ``direction="rtl"``,
  ``language="fa"``, raw logical text, Noto Sans Arabic. Never pre-shape
  with ``arabic_reshaper``/``bidi``; never use a font without Arabic
  glyphs. Latin words must already be transliterated to Persian by the
  Writer (``API`` → ``ای‌پی‌آی``); Noto Sans Arabic has no Latin glyphs.
* ffmpeg overlays use **plain** ``overlay`` filters with ``enable`` windows.
  The known quirk: ``fade`` filters with ``alpha=1`` on still PNG overlays
  silently produce transparent output — they are never used here.
* A reel is never an animated still. When no base video is supplied the
  Designer builds a slideshow with ``zoompan`` so frames genuinely change;
  the quality gate measures that motion.

Rendering is deterministic and local: no network, no AI video generation.
``ffmpeg``/``ffprobe`` must be on ``PATH``; a missing binary raises a loud
error instead of producing a fake file.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from app.core.exceptions import ExternalServiceError

# Same font paths as the standalone pipeline.
FONT_BOLD = "/usr/share/fonts/truetype/noto/NotoSansArabic-ExtraBold.ttf"
FONT_REGULAR = "/usr/share/fonts/truetype/noto/NotoSansArabic-Bold.ttf"

REEL_SIZE = (720, 1280)
POST_SIZE = (1080, 1350)
COVER_SIZE = (1080, 1920)

# Dark translucent box behind white text — the quality gate looks for exactly
# this: bright pixels inside the box region.
BOX_FILL = (8, 18, 26, 168)
TEXT_FILL = (255, 255, 255, 255)
ACCENT_FILL = (94, 234, 212, 255)


def _require_ffmpeg() -> str:
    path = shutil.which("ffmpeg")
    if path is None:
        raise ExternalServiceError("ffmpeg is required to render reels but was not found on PATH.")
    return path


def _require_ffprobe() -> str:
    path = shutil.which("ffprobe")
    if path is None:
        raise ExternalServiceError("ffprobe is required to inspect media but was not found on PATH.")
    return path


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=300, check=True)
    except subprocess.CalledProcessError as exc:
        raise ExternalServiceError(
            f"Media command failed: {' '.join(cmd[:3])}…\n{exc.stderr[:500]}"
        ) from exc
    except FileNotFoundError as exc:
        raise ExternalServiceError("ffmpeg/ffprobe binary not found on PATH.") from exc


def find_font(*, bold: bool) -> str | None:
    """Noto Sans Arabic if installed, else ``None`` (caller falls back and
    records it — Persian shaping is wrong without it, so the manifest says so)."""
    candidates = [FONT_BOLD, FONT_REGULAR] if bold else [FONT_REGULAR, FONT_BOLD]
    for candidate in candidates:
        if Path(candidate).is_file():
            return candidate
    return None


def _font(size: int, *, bold: bool) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    path = find_font(bold=bold)
    if path is not None:
        return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)


def _draw_rtl(draw: ImageDraw.ImageDraw, cx: int, y: int, text: str, font, fill) -> None:
    draw.text((cx, y), text, font=font, fill=fill, anchor="ma", direction="rtl", language="fa")


def _gradient(size: tuple[int, int], *, top: tuple[int, int, int], bottom: tuple[int, int, int]) -> Image.Image:
    width, height = size
    img = Image.new("RGB", size)
    draw = ImageDraw.Draw(img)
    for y in range(height):
        ratio = y / max(height - 1, 1)
        colour = tuple(int(top[i] + (bottom[i] - top[i]) * ratio) for i in range(3))
        draw.line([(0, y), (width, y)], fill=colour)
    return img


@dataclass(frozen=True)
class RenderManifest:
    """What the Designer produced, for the quality gate and the audit trail."""

    kind: str  # REELS | IMAGE | CAROUSEL
    files: dict[str, str]  # role -> absolute path
    title_font_px: int
    font_fallback: bool
    raqm_available: bool
    overlay_windows: list[dict[str, float]] = field(default_factory=list)
    motion_source: str = "slideshow-zoompan"  # or "base-video"


def render_cover(
    out_path: Path,
    *,
    title_lines: list[str],
    subtitle: str = "",
    size: tuple[int, int] = COVER_SIZE,
    title_px: int = 118,
) -> RenderManifest:
    """Bold-title cover image: gradient background, dark title box, RTL title.

    The title box position is fixed (top of the frame) so the quality gate
    knows exactly where to look for rendered text.
    """
    width, height = size
    img = _gradient(size, top=(10, 32, 54), bottom=(6, 16, 24))
    overlay = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    title_font = _font(title_px, bold=True)
    subtitle_font = _font(62, bold=False)
    line_h = int(title_px * 1.51)
    box_h = line_h * len(title_lines) + 120 + 44
    box_y = 120
    draw.rounded_rectangle(
        [56, box_y, width - 56, box_y + box_h], radius=44, fill=BOX_FILL
    )
    y = box_y + 36
    for line in title_lines:
        _draw_rtl(draw, width // 2, y, line, title_font, TEXT_FILL)
        y += line_h
    if subtitle:
        _draw_rtl(draw, width // 2, y + 6, subtitle, subtitle_font, ACCENT_FILL)
    out = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out.save(out_path, quality=92)
    return RenderManifest(
        kind="IMAGE",
        files={"cover": str(out_path)},
        title_font_px=title_px,
        font_fallback=find_font(bold=True) is None,
        raqm_available=_raqm_available(),
    )


def render_overlay(
    out_path: Path,
    *,
    lines: list[str],
    font_px: int = 46,
    size: tuple[int, int] = REEL_SIZE,
    box_y: int | None = None,
) -> RenderManifest:
    """Transparent PNG with the scene text in a dark rounded box.

    The box sits in the lower third (``box_y`` default), matching where the
    quality gate samples for rendered text.
    """
    width, height = size
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    font = _font(font_px, bold=True)
    line_h = int(font_px * 1.55)
    box_h = line_h * len(lines) + 44
    if box_y is None:
        box_y = height - box_h - 210
    draw.rounded_rectangle([36, box_y, width - 36, box_y + box_h], radius=28, fill=BOX_FILL)
    y = box_y + 22
    for line in lines:
        _draw_rtl(draw, width // 2, y, line, font, TEXT_FILL)
        y += line_h
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path)
    return RenderManifest(
        kind="IMAGE",
        files={"overlay": str(out_path)},
        title_font_px=font_px,
        font_fallback=find_font(bold=True) is None,
        raqm_available=_raqm_available(),
    )


def _raqm_available() -> bool:
    try:
        from PIL import features

        return bool(features.check("raqm"))
    except Exception:  # noqa: BLE001 - best-effort capability probe
        return False


def render_scene_frame(
    out_path: Path,
    *,
    lines: list[str],
    font_px: int = 52,
    size: tuple[int, int] = REEL_SIZE,
    box_y: int | None = None,
) -> RenderManifest:
    """A full opaque frame: gradient background with the scene text in a dark
    rounded box (lower third). One frame per scene; the slideshow pans across
    them so the reel has real motion."""
    width, height = size
    img = _gradient(size, top=(14, 38, 62), bottom=(7, 18, 28))
    overlay = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    font = _font(font_px, bold=True)
    line_h = int(font_px * 1.55)
    box_h = line_h * len(lines) + 44
    if box_y is None:
        box_y = height - box_h - 210
    draw.rounded_rectangle([36, box_y, width - 36, box_y + box_h], radius=28, fill=BOX_FILL)
    y = box_y + 22
    for line in lines:
        _draw_rtl(draw, width // 2, y, line, font, TEXT_FILL)
        y += line_h
    out = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out.save(out_path, quality=92)
    return RenderManifest(
        kind="IMAGE",
        files={"frame": str(out_path)},
        title_font_px=font_px,
        font_fallback=find_font(bold=True) is None,
        raqm_available=_raqm_available(),
    )


def slideshow_reel(
    image_paths: list[Path],
    out_path: Path,
    *,
    seconds_per_image: float = 2.0,
    size: tuple[int, int] = REEL_SIZE,
    zoom_step: float = 0.0016,
) -> Path:
    """A reel from stills with genuine motion: each image pans/zooms
    (``zoompan``), so consecutive frames differ. An animated still would
    fail the quality gate; this does not pretend otherwise."""
    _require_ffmpeg()
    width, height = size
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Build one zoompan segment per image, then concatenate.
    segments: list[Path] = []
    try:
        for i, image in enumerate(image_paths):
            segment = out_path.with_name(f"{out_path.stem}-seg{i}.mp4")
            frames = max(1, int(seconds_per_image * 30))
            _run(
                [
                    "ffmpeg", "-y", "-loop", "1", "-i", str(image),
                    "-vf",
                    f"scale={width * 2}:{height * 2},zoompan=z='min(zoom+{zoom_step},1.15)':"
                    f"d={frames}:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={width}x{height}:fps=30",
                    "-t", str(seconds_per_image), "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    str(segment),
                ]
            )
            segments.append(segment)
        concat = out_path.with_name(f"{out_path.stem}-concat.txt")
        concat.write_text("".join(f"file '{s}'\n" for s in segments), encoding="utf-8")
        _run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat),
             "-c", "copy", str(out_path)]
        )
        return out_path
    finally:
        for segment in segments:
            segment.unlink(missing_ok=True)
        concat = out_path.with_name(f"{out_path.stem}-concat.txt")
        concat.unlink(missing_ok=True)


def composite_reel(
    base_video: Path,
    overlays: list[tuple[Path, float, float]],
    out_path: Path,
) -> Path:
    """Overlay PNGs onto a base video with plain ``overlay`` filters and
    ``enable='between(t,start,end)'`` windows. No ``fade`` filters: with
    ``alpha=1`` they silently render the overlay transparent."""
    _require_ffmpeg()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd: list[str] = ["ffmpeg", "-y", "-i", str(base_video)]
    for overlay_path, _, _ in overlays:
        cmd += ["-i", str(overlay_path)]
    # Chain the overlays: each step consumes the previous output.
    chain: list[str] = []
    current = "0:v"
    for i in range(len(overlays)):
        chain.append(f"[{current}][{i + 1}:v]overlay=0:0:enable='between(t,{overlays[i][1]},{overlays[i][2]})'[v{i}]")
        current = f"v{i}"
    filtergraph = ";".join(chain)
    cmd += ["-filter_complex", filtergraph, "-map", f"[{current}]", "-map", "0:a?",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "copy", str(out_path)]
    _run(cmd)
    return out_path


def probe_duration(video: Path) -> float:
    """Duration in seconds via ffprobe (JSON output, no guessing)."""
    ffprobe = _require_ffprobe()
    result = _run(
        [ffprobe, "-v", "quiet", "-print_format", "json", "-show_format", str(video)]
    )
    data = json.loads(result.stdout)
    return float(data["format"]["duration"])


def extract_frames(video: Path, *, times: list[float], out_dir: Path) -> list[Path]:
    """Extract one PNG frame per requested timestamp (used by the gate)."""
    _require_ffmpeg()
    out_dir.mkdir(parents=True, exist_ok=True)
    frames: list[Path] = []
    for i, at in enumerate(times):
        frame = out_dir / f"frame-{i:02d}.png"
        _run(
            ["ffmpeg", "-y", "-ss", str(at), "-i", str(video),
             "-frames:v", "1", str(frame)]
        )
        frames.append(frame)
    return frames


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


__all__ = [
    "RenderManifest",
    "composite_reel",
    "extract_frames",
    "file_sha256",
    "find_font",
    "probe_duration",
    "render_cover",
    "render_overlay",
    "render_scene_frame",
    "slideshow_reel",
]
