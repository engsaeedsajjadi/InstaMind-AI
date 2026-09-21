"""Instagram media rules — the constraints Meta actually enforces."""

from __future__ import annotations

import pytest

from app.core.exceptions import ValidationError
from app.modules.instagram.media_rules import (
    CAPTION_MAX_CHARS,
    CAPTION_MAX_HASHTAGS,
    CAROUSEL_MAX_CHILDREN,
    MediaType,
    raise_for_issues,
    validate_caption,
    validate_media_set,
    validate_publish_request,
)

BUSINESS_ACCOUNT = {
    "account_type": "BUSINESS",
    "capabilities": {"content_publish": True, "stories_publish": True},
}
CREATOR_ACCOUNT = {
    "account_type": "CREATOR",
    "capabilities": {"content_publish": True, "stories_publish": False},
}


def image(**overrides):
    return {"kind": "IMAGE", "mime": "image/jpeg", "size_bytes": 500_000, "width": 1080, "height": 1350, **overrides}


def video(**overrides):
    return {
        "kind": "VIDEO",
        "mime": "video/mp4",
        "size_bytes": 20_000_000,
        "width": 1080,
        "height": 1920,
        "duration_ms": 30_000,
        **overrides,
    }


class TestCaptionRules:
    def test_within_limits_passes(self):
        assert validate_caption("Hello #brand", media_type=MediaType.IMAGE) == []

    def test_over_2200_characters_is_rejected(self):
        issues = validate_caption("a" * (CAPTION_MAX_CHARS + 1), media_type=MediaType.IMAGE)
        assert [issue.code for issue in issues] == ["caption_too_long"]

    def test_more_than_30_hashtags_is_rejected(self):
        caption = " ".join(f"#tag{i}" for i in range(CAPTION_MAX_HASHTAGS + 1))
        codes = {issue.code for issue in validate_caption(caption, media_type=MediaType.IMAGE)}
        assert "too_many_hashtags" in codes

    def test_more_than_20_mentions_is_rejected(self):
        caption = " ".join(f"@user{i}" for i in range(21))
        codes = {issue.code for issue in validate_caption(caption, media_type=MediaType.IMAGE)}
        assert "too_many_mentions" in codes

    def test_stories_cannot_have_a_caption(self):
        """Meta silently drops Story captions; we refuse instead."""
        issues = validate_caption("Launch day", media_type=MediaType.STORIES)
        assert [issue.code for issue in issues] == ["stories_caption_unsupported"]


class TestMediaSetRules:
    def test_image_post_needs_exactly_one_image(self):
        assert validate_media_set(media_type=MediaType.IMAGE, assets=[image()]) == []
        codes = {i.code for i in validate_media_set(media_type=MediaType.IMAGE, assets=[])}
        assert "media_required" in codes
        codes = {i.code for i in validate_media_set(media_type=MediaType.IMAGE, assets=[image(), image()])}
        assert "single_image_required" in codes

    def test_carousel_needs_2_to_10_items(self):
        assert {i.code for i in validate_media_set(media_type=MediaType.CAROUSEL, assets=[image()])} == {
            "carousel_item_count"
        }
        assert validate_media_set(
            media_type=MediaType.CAROUSEL, assets=[image() for _ in range(5)]
        ) == []
        too_many = [image() for _ in range(CAROUSEL_MAX_CHILDREN + 1)]
        assert {i.code for i in validate_media_set(media_type=MediaType.CAROUSEL, assets=too_many)} == {
            "carousel_item_count"
        }

    def test_oversized_image_is_rejected(self):
        codes = {
            i.code
            for i in validate_media_set(
                media_type=MediaType.IMAGE, assets=[image(size_bytes=9 * 1024 * 1024)]
            )
        }
        assert any(code.startswith("media_too_large") for code in codes)

    def test_unsupported_mime_is_rejected(self):
        codes = {
            i.code
            for i in validate_media_set(media_type=MediaType.IMAGE, assets=[image(mime="image/svg+xml")])
        }
        assert any(code.startswith("invalid_media_mime") for code in codes)

    def test_reel_too_short_is_rejected(self):
        codes = {
            i.code
            for i in validate_media_set(media_type=MediaType.REELS, assets=[video(duration_ms=1_000)])
        }
        assert any(code.startswith("video_too_short") for code in codes)

    def test_story_video_over_60s_is_rejected(self):
        codes = {
            i.code
            for i in validate_media_set(media_type=MediaType.STORIES, assets=[video(duration_ms=90_000)])
        }
        assert any(code.startswith("video_too_long") for code in codes)


class TestPublishPreflight:
    def _base(self, **overrides):
        params = {
            "media_type": MediaType.IMAGE,
            "caption": "Hello",
            "assets": [image()],
            "account": BUSINESS_ACCOUNT,
        }
        params.update(overrides)
        return params

    def test_happy_path(self):
        assert validate_publish_request(**self._base()) == []

    def test_story_for_creator_account_is_blocked(self):
        issues = validate_publish_request(
            **self._base(media_type=MediaType.STORIES, account=CREATOR_ACCOUNT, caption=None)
        )
        assert "stories_not_supported" in {i.code for i in issues}

    def test_story_for_business_account_is_allowed(self):
        assert validate_publish_request(
            **self._base(media_type=MediaType.STORIES, account=BUSINESS_ACCOUNT, caption=None)
        ) == []

    def test_missing_publish_permission_is_blocked(self):
        account = {"account_type": "BUSINESS", "capabilities": {"content_publish": False}}
        issues = validate_publish_request(**self._base(account=account))
        assert "content_publish_not_granted" in {i.code for i in issues}

    def test_more_than_three_collaborators_is_rejected(self):
        issues = validate_publish_request(**self._base(collaborators=["a", "b", "c", "d"]))
        assert "too_many_collaborators" in {i.code for i in issues}

    def test_reels_alt_text_is_rejected(self):
        issues = validate_publish_request(
            **self._base(media_type=MediaType.REELS, assets=[video()], alt_text="A description")
        )
        assert "reels_alt_text_unsupported" in {i.code for i in issues}

    def test_share_to_feed_only_applies_to_reels(self):
        issues = validate_publish_request(**self._base(media_type=MediaType.CAROUSEL, assets=[image(), image()], share_to_feed=True))
        assert "share_to_feed_carousel" in {i.code for i in issues}

    def test_raise_for_issues_raises_a_validation_error_with_details(self):
        issues = validate_publish_request(**self._base(caption="x" * (CAPTION_MAX_CHARS + 1)))
        with pytest.raises(ValidationError) as exc:
            raise_for_issues(issues)
        assert exc.value.errors and exc.value.errors[0]["code"] == "caption_too_long"

    def test_media_without_a_public_url_is_not_a_publish_problem_here(self):
        """Public-URL enforcement lives in the publishing engine (it needs the
        stored assets); the rule set itself only checks shapes."""
        issues = validate_publish_request(**self._base())
        assert all(issue.code != "media_not_public" for issue in issues)
