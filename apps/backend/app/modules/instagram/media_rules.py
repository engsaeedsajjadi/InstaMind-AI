"""Instagram media validation rules.

These constants mirror Meta's *published* container specifications. They are the
reason a post succeeds or fails at Meta, so validating client-side turns a
confusing ``INSTAGRAM_PLATFORM_API__*`` error into an actionable message before
we burn an API call.

Each rule that Meta enforces is marked with the source of truth in
``docs/instagram-integration.md``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from app.core.exceptions import ValidationError


class MediaType(StrEnum):
    IMAGE = "IMAGE"
    CAROUSEL = "CAROUSEL"
    REELS = "REELS"
    STORIES = "STORIES"


@dataclass(frozen=True)
class MediaSpec:
    kind: str
    max_bytes: int
    allowed_mime: frozenset[str]
    min_width: int | None = None
    max_width: int | None = None
    min_duration_ms: int | None = None
    max_duration_ms: int | None = None


# Meta's documented limits. Images: JPEG only in practice (PNG is accepted by the
# docs but rejected by some pipelines); we allow both and let Meta arbitrate.
IMAGE_SPEC = MediaSpec(
    kind="image",
    max_bytes=8 * 1024 * 1024,
    allowed_mime=frozenset({"image/jpeg", "image/png"}),
    min_width=320,
    max_width=1440,
)
VIDEO_SPEC = MediaSpec(
    kind="video",
    max_bytes=1000 * 1024 * 1024,
    allowed_mime=frozenset({"video/mp4", "video/quicktime"}),
    min_duration_ms=3_000,
    max_duration_ms=15 * 60_000,
)
STORY_IMAGE_SPEC = MediaSpec(
    kind="story_image",
    max_bytes=8 * 1024 * 1024,
    allowed_mime=frozenset({"image/jpeg", "image/png"}),
)
STORY_VIDEO_SPEC = MediaSpec(
    kind="story_video",
    max_bytes=100 * 1024 * 1024,
    allowed_mime=frozenset({"video/mp4", "video/quicktime"}),
    min_duration_ms=3_000,
    max_duration_ms=60_000,
)

CAPTION_MAX_CHARS = 2200
CAPTION_MAX_HASHTAGS = 30
CAPTION_MAX_MENTIONS = 20
CAROUSEL_MIN_CHILDREN = 2
CAROUSEL_MAX_CHILDREN = 10
MAX_COLLABORATORS = 3
MAX_USER_TAGS = 20
ALT_TEXT_MAX_CHARS = 1000
MAX_LOCATION_ID = 64


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    message: str
    field: str | None = None


def _as_dict(issue: ValidationIssue) -> dict[str, Any]:
    return {"field": issue.field or "", "code": issue.code, "message": issue.message}


def validate_caption(caption: str | None, *, media_type: MediaType) -> list[ValidationIssue]:
    """Stories do not accept captions at all — Meta silently drops them, so we
    refuse instead of letting a customer believe their caption will appear."""
    issues: list[ValidationIssue] = []
    if media_type is MediaType.STORIES:
        if caption:
            issues.append(
                ValidationIssue(
                    "stories_caption_unsupported",
                    "Instagram Stories published via the API cannot have a caption.",
                    "caption",
                )
            )
        return issues

    text = caption or ""
    if len(text) > CAPTION_MAX_CHARS:
        issues.append(
            ValidationIssue(
                "caption_too_long",
                f"Caption exceeds {CAPTION_MAX_CHARS} characters ({len(text)} given).",
                "caption",
            )
        )
    hashtags = [token for token in text.split() if token.startswith("#")]
    if len(hashtags) > CAPTION_MAX_HASHTAGS:
        issues.append(
            ValidationIssue(
                "too_many_hashtags",
                f"A maximum of {CAPTION_MAX_HASHTAGS} hashtags is allowed per post.",
                "caption",
            )
        )
    mentions = [token for token in text.split() if token.startswith("@")]
    if len(mentions) > CAPTION_MAX_MENTIONS:
        issues.append(
            ValidationIssue(
                "too_many_mentions",
                f"A maximum of {CAPTION_MAX_MENTIONS} @mentions is allowed per post.",
                "caption",
            )
        )
    return issues


def validate_media_set(
    *,
    media_type: MediaType,
    assets: list[dict[str, Any]],
) -> list[ValidationIssue]:
    """``assets`` are dicts with ``kind``, ``mime``, ``size_bytes``, and optional
    ``width``/``height``/``duration_ms`` coming from the stored MediaAsset."""
    issues: list[ValidationIssue] = []
    if not assets:
        issues.append(ValidationIssue("media_required", "At least one media file is required.", "media"))
        return issues

    if media_type is MediaType.IMAGE and len(assets) != 1:
        issues.append(ValidationIssue("single_image_required", "An IMAGE post needs exactly one image.", "media"))
    if media_type is MediaType.CAROUSEL and not (
        CAROUSEL_MIN_CHILDREN <= len(assets) <= CAROUSEL_MAX_CHILDREN
    ):
        issues.append(
            ValidationIssue(
                "carousel_item_count",
                f"A carousel needs between {CAROUSEL_MIN_CHILDREN} and {CAROUSEL_MAX_CHILDREN} items.",
                "media",
            )
        )
    if media_type in {MediaType.REELS, MediaType.STORIES} and len(assets) != 1:
        issues.append(
            ValidationIssue("single_media_required", f"{media_type.value} accepts exactly one media file.", "media")
        )

    spec = _spec_for(media_type, assets[0])
    for index, asset in enumerate(assets):
        issues.extend(_validate_asset(asset, spec, index))
    return issues


def _spec_for(media_type: MediaType, first_asset: dict[str, Any]) -> MediaSpec:
    if media_type is MediaType.STORIES:
        return STORY_VIDEO_SPEC if first_asset.get("kind") == "VIDEO" else STORY_IMAGE_SPEC
    return VIDEO_SPEC if media_type is MediaType.REELS else IMAGE_SPEC


def _validate_asset(asset: dict[str, Any], spec: MediaSpec, index: int) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    mime = (asset.get("mime") or "").lower()
    size = int(asset.get("size_bytes") or 0)
    if mime not in spec.allowed_mime:
        issues.append(
            ValidationIssue(
                f"invalid_media_mime:{index}",
                f"{mime or 'unknown'} is not supported for {spec.kind} posts.",
                f"media[{index}]",
            )
        )
    if size and size > spec.max_bytes:
        issues.append(
            ValidationIssue(
                f"media_too_large:{index}",
                f"File {index + 1} is {size / (1024 * 1024):.1f} MB; the limit is "
                f"{spec.max_bytes / (1024 * 1024):.0f} MB.",
                f"media[{index}]",
            )
        )
    width = asset.get("width")
    if spec.min_width and width and width < spec.min_width:
        issues.append(
            ValidationIssue(
                f"media_too_small:{index}",
                f"Image must be at least {spec.min_width}px wide.",
                f"media[{index}]",
            )
        )
    if spec.max_width and width and width > spec.max_width:
        issues.append(
            ValidationIssue(
                f"media_too_wide:{index}",
                f"Image must be at most {spec.max_width}px wide.",
                f"media[{index}]",
            )
        )
    duration = asset.get("duration_ms")
    if spec.min_duration_ms is not None and duration is not None and duration < spec.min_duration_ms:
        issues.append(
            ValidationIssue(
                f"video_too_short:{index}",
                f"Video must be at least {spec.min_duration_ms // 1000}s long.",
                f"media[{index}]",
            )
        )
    if spec.max_duration_ms is not None and duration is not None and duration > spec.max_duration_ms:
        issues.append(
            ValidationIssue(
                f"video_too_long:{index}",
                f"Video must be at most {spec.max_duration_ms // 60_000} minutes long.",
                f"media[{index}]",
            )
        )
    return issues


def validate_publish_request(
    *,
    media_type: MediaType,
    caption: str | None,
    assets: list[dict[str, Any]],
    account: dict[str, Any],
    collaborators: list[str] | None = None,
    user_tags: list[dict[str, Any]] | None = None,
    alt_text: str | None = None,
    location_id: str | None = None,
    share_to_feed: bool | None = None,
) -> list[ValidationIssue]:
    """Full pre-flight check for one publish, including capability gating."""
    issues: list[ValidationIssue] = []
    issues.extend(validate_caption(caption, media_type=media_type))
    issues.extend(validate_media_set(media_type=media_type, assets=assets))

    capabilities = account.get("capabilities") or {}
    if media_type is MediaType.STORIES and not capabilities.get("stories_publish"):
        # Only Business accounts may publish Stories; Creator accounts may not.
        issues.append(
            ValidationIssue(
                "stories_not_supported",
                "Stories publishing is only available for Instagram Business accounts.",
                "media_type",
            )
        )
    if not capabilities.get("content_publish", True):
        issues.append(
            ValidationIssue(
                "content_publish_not_granted",
                "This account has not granted the content publishing permission.",
                "media_type",
            )
        )

    if collaborators and len(collaborators) > MAX_COLLABORATORS:
        issues.append(
            ValidationIssue(
                "too_many_collaborators",
                f"A maximum of {MAX_COLLABORATORS} collaborators is allowed.",
                "collaborators",
            )
        )
    if media_type is MediaType.STORIES and collaborators:
        issues.append(
            ValidationIssue("collaborators_not_supported", "Stories do not support collaborators.", "collaborators")
        )
    if user_tags and len(user_tags) > MAX_USER_TAGS:
        issues.append(
            ValidationIssue("too_many_user_tags", f"A maximum of {MAX_USER_TAGS} user tags is allowed.", "user_tags")
        )
    if alt_text and len(alt_text) > ALT_TEXT_MAX_CHARS:
        issues.append(
            ValidationIssue("alt_text_too_long", f"Alt text is limited to {ALT_TEXT_MAX_CHARS} characters.", "alt_text")
        )
    if media_type is MediaType.REELS and alt_text:
        issues.append(
            ValidationIssue("reels_alt_text_unsupported", "Reels do not support alt text via the API.", "alt_text")
        )
    if media_type is MediaType.STORIES and alt_text:
        issues.append(
            ValidationIssue("stories_alt_text_unsupported", "Stories do not support alt text.", "alt_text")
        )
    if location_id and len(str(location_id)) > MAX_LOCATION_ID:
        issues.append(ValidationIssue("invalid_location_id", "Location id is malformed.", "location_id"))
    if media_type is MediaType.CAROUSEL and share_to_feed is not None:
        issues.append(
            ValidationIssue("share_to_feed_carousel", "share_to_feed only applies to Reels.", "share_to_feed")
        )
    return issues


def raise_for_issues(issues: list[ValidationIssue]) -> None:
    if issues:
        raise ValidationError(
            "This content cannot be published to Instagram as-is.",
            errors=[_as_dict(issue) for issue in issues],
            context={"issue_count": len(issues)},
        )
