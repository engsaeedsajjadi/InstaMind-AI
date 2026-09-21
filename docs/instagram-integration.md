# Instagram Integration Guide

This document is the contract between InstaMind AI and Meta. It records what the
official API actually allows, which permission grants each feature, and how we
handle limits and failures. **If a capability is not listed here as supported,
it is not implemented — and will not be faked.**

Last verified against Meta's published documentation in September 2026. Meta
changes limits and deprecates fields regularly; re-verify before each release.

---

## 1. Two official authentication paths

Meta offers two configurations of the Instagram Platform API. Both are
supported, selected per connection via `api_path`.

| | Instagram Login | Facebook Login |
|---|---|---|
| Internal name | `INSTAGRAM_LOGIN` | `FACEBOOK_LOGIN` |
| Requires | Business or Creator account | Business or Creator account **linked to a Facebook Page** |
| Authorize URL | `https://api.instagram.com/oauth/authorize` | `https://www.facebook.com/v23.0/dialog/oauth` |
| Token host | `graph.instagram.com` | `graph.facebook.com` |
| PKCE | Supported (we use it) | Not applicable |
| Long-lived token | `refresh_access_token` (`ig_refresh_token`) — refresh at least every 60 days | `fb_exchange_token` exchange |
| Business Discovery | ✗ | ✓ |
| Hashtag search | ✗ | ✓ |
| Product tagging | ✗ | ✓ |
| Partnership ads | ✗ | ✓ |

**Personal (consumer) accounts cannot use either path.** There is no API access
for them and no workaround. The UI must say so plainly rather than offering a
connection that will fail.

### Permission sets requested

`INSTAGRAM_LOGIN` (`app/modules/instagram/service.py::SCOPES_INSTAGRAM_LOGIN`):

```
instagram_business_basic
instagram_business_content_publish
instagram_business_manage_comments
instagram_business_manage_messages
instagram_business_manage_insights
```

`FACEBOOK_LOGIN` (`SCOPES_FACEBOOK_LOGIN`):

```
instagram_basic
instagram_content_publish
instagram_manage_comments
instagram_manage_messages
instagram_manage_insights
pages_show_list
pages_read_engagement
```

Every one of these requires **Advanced Access** through Meta App Review before
it works for accounts you do not own. Standard Access only reaches assets
attached to your own app. Plan for 1–4 weeks per review round and expect
rejections; keep a screen-recording of each flow ready.

## 2. Capability matrix

Derived at connect time by `derive_capabilities()` and stored on
`social_accounts.capabilities`, so the UI disables unsupported actions instead
of letting them fail at Meta.

| Capability | Condition |
|---|---|
| `content_publish` | `instagram_business_content_publish` **or** `instagram_content_publish` granted |
| `stories_publish` | publish permission **and** `account_type == BUSINESS` |
| `comments_manage` | comments permission granted |
| `messages_manage` | messages permission granted |
| `insights_read` | insights permission granted |
| `business_discovery` | Facebook Login path only |

> **Stories are Business-only.** Meta documents Story publishing as unavailable
> for Creator accounts. Attempting it returns an API error, so we refuse it
> client-side with `stories_not_supported`.

## 3. Content publishing

Two-step container flow for every type:

```
POST /{ig-user-id}/media          → container id
  (REELS/CAROUSEL: poll GET /{container-id}?fields=status_code until FINISHED)
POST /{ig-user-id}/media_publish  → media id
```

| Type | `media_type` | Media parameter | Caption | alt_text | Notes |
|---|---|---|---|---|---|
| Image | *(omit)* | `image_url` | ✓ | ✓ | 1 image only |
| Carousel | `CAROUSEL` | `children` (2–10 container ids) | ✓ (parent) | ✓ (per child) | mix of images and videos allowed |
| Reels | `REELS` | `video_url` | ✓ | ✗ | `cover_url` / `thumb_offset`, `share_to_feed` |
| Story | `STORIES` | `image_url` **or** `video_url` | ✗ | ✗ | no stickers, polls, links or countdowns |

**Meta fetches the media from a URL we provide.** There is no upload endpoint.
That is why every `MediaAsset` must have a publicly reachable `public_url`, and
why `PublishingService._asset_payloads` refuses to publish when it is missing.
Presigned URLs must outlive the publish attempt.

### Limits we validate before calling Meta

| Rule | Value | Where enforced |
|---|---|---|
| Caption length | 2200 chars | `validate_caption` |
| Hashtags per post | 30 | `validate_caption` |
| @mentions per post | 20 | `validate_caption` |
| Carousel items | 2–10 | `validate_media_set` |
| Collaborators | max 3, not on Stories | `validate_publish_request` |
| User tags | max 20 | `validate_publish_request` |
| Image size | ≤ 8 MB | `validate_media_set` |
| Video size | ≤ 100 MB (API accepts more; keep the safe bound) | `validate_media_set` |
| Reel duration | 3 s – 15 min (5–90 s + 9:16 to appear in the Reels tab) | `validate_media_set` |
| Story video duration | ≤ 60 s | `validate_media_set` |
| Image MIME | JPEG/PNG (no SVG — script-capable) | `storage.validate_upload` |
| Video MIME | MP4/MOV | `storage.validate_upload` |

### Publishing rate limits

* Meta: **50–100 API-published posts per rolling 24 h** per account (carousels
  count as one). Query `GET /{ig-user-id}/content_publishing_limit` for the
  authoritative figure.
* Us: `INSTAMIND_PUBLISH_DAILY_GUARD_LIMIT` (default 100) enforced in
  `publishing_quota` **before** the API call, so a tenant cannot silently hit
  Meta's wall.
* General API budget: roughly `4800 × daily impressions` per 24 h, with a low
  floor for small accounts. Read `x-app-usage` / `x-business-use-case-usage`
  response headers; we surface them on every `MetaGraphClient` response.

## 4. Direct messages

| Behaviour | Supported |
|---|---|
| Read conversations and messages | ✓ |
| Send text/media inside the 24 h window | ✓ |
| `HUMAN_AGENT` tag (extends to 7 days) | ✓ — **human replies only** |
| Private reply to a comment | ✓ (750/hour, once per comment) |
| Story mention / story reply inbound | ✓ |
| Cold outreach to arbitrary users | ✗ never, by policy |
| Bulk messaging / broadcasts | ✗ never, by policy |
| Interactive story elements | ✗ not in the API |

**The 24-hour window is the central rule.** A free-form message is only allowed
within 24 h of the participant's last inbound message. We persist
`conversations.last_inbound_at` and `messaging_window_expires_at` from the
webhook so the reply endpoint can refuse a send that would fail — and so the UI
can show why the composer is disabled.

`HUMAN_AGENT` must never be used for automated replies. Meta detects misuse and
revokes API access for the whole app. In this codebase the tag is only settable
on a human-authored reply path.

Deprecated as of April 2026: `CONFIRMED_EVENT_UPDATE`, `ACCOUNT_UPDATE`,
`POST_PURCHASE_UPDATE` return error 100. Do not use them.

## 5. Comments

Read, reply, hide/unhide, enable/disable per post. We never delete a comment
without an explicit user action — deletion is not automated anywhere.

## 6. Insights

`GET /{node}/insights?metric=...&period=...`. Account-level metrics require the
insights permission; some audience breakdowns need 100+ followers. Data can lag
up to ~48 h and is retained ~2 years. Story insights are only available for 24 h
after expiry.

`analytics_snapshots.is_available = false` means "Meta did not return this".
The UI must render that as *no data*, never as zero.

## 7. Webhooks

| Item | Value |
|---|---|
| Verification | `GET` with `hub.mode=subscribe`, `hub.verify_token`, echo `hub.challenge` |
| Signature | `X-Hub-Signature-256: sha256=<hex>` over the raw body |
| Response deadline | seconds — we persist and queue, never process inline |
| Replay protection | events older than 600 s are rejected |
| Idempotency | unique `(platform, event_key)`; duplicates are a no-op |

Subscribed objects: `instagram` (comments, mentions, messages, messaging
postbacks, reactions, deliveries, reads, echoes). Anything else is ignored and
logged.

## 8. Error handling

`MetaGraphClient._build_error` maps Meta's codes to a retry decision:

| Code | Meaning | Action |
|---|---|---|
| 1, 2, 368, 803, 1205, 1609 | Transient | Retry with exponential backoff |
| 4, 17, 32, 613, 80004 | Rate limit | Retry, honour `retry-after` / quota headers |
| 190 | Token expired/invalid | Mark the account `TOKEN_EXPIRED`, ask the user to reconnect |
| 10, 200–205 | Permission denied | Permanent; surface the missing permission |
| 36003 and friends | Unsupported media | Permanent; show the validation message |

`is_transient` drives `PublishingService`: transient errors reschedule with
backoff (`2^n` seconds, capped at 900 s, ±20 % jitter); permanent errors fail the
job and mark the content `FAILED` with the Meta message attached.

## 9. Token lifecycle

1. Exchange the OAuth `code` for a short-lived token (~1 h).
2. Exchange for a long-lived token (~60 days).
3. Store it **encrypted** (Fernet, AAD bound to the row) with a fingerprint.
4. A daily Celery sweep (`instamind.instagram.refresh_tokens`) refreshes any
   token inside a 7-day margin.
5. On error 190 the account is flagged `TOKEN_EXPIRED` and the user is asked to
   reconnect. There is no silent re-auth.

## 10. What we will never build

* Login with an Instagram password, or storing one.
* Reusing browser session cookies.
* Browser automation (Selenium/Playwright) against instagram.com.
* Private APIs or any endpoint Meta does not document.
* Automated following, liking other people's content, or mass DMs.
* Scraping public profiles for data the API does not expose.

Each of these risks the customer's account and our app's API access. They are
excluded by design, not by omission.
