# Product Requirements Document

## 1. Problem

Businesses running Instagram accounts do the same work by hand: writing
captions, remembering to post, answering the same DMs, tracking which post
performed. Existing tools either (a) automate things Meta's API does not allow,
putting the customer's account at risk, or (b) are not built for Persian-speaking
teams and agencies managing many client accounts.

## 2. Product

InstaMind AI connects professional Instagram accounts through Meta's official
API and adds an AI layer for content creation, plus operational tooling:
scheduling, approval workflow, inbox, comments, CRM and analytics.

## 3. Personas

| Persona | Needs | Primary modules |
|---|---|---|
| **Small business owner** | Post consistently without hiring anyone; answer DMs fast | Content Studio, AI Assistant, Inbox |
| **E-commerce store** | Product posts, order questions in DM, price/stock answered correctly | AI (grounded in product data), Inbox, CRM |
| **Digital agency** | Many client workspaces, per-client roles, approval before publish | Tenants, RBAC, Approvals, Analytics |
| **Social media manager** | Calendar, bulk scheduling, brand voice consistency | Calendar, Publishing, Brand Profiles |
| **Support operator** | Shared inbox, assignment, notes, escalation | Inbox, CRM |

## 4. Goals

1. A user can register, create a workspace, connect an Instagram account
   officially, and publish real content.
2. AI output respects the brand's verified facts and never invents claims.
3. Every action on a customer's account is reversible or auditable.
4. Tenant data is provably isolated.
5. The product is sellable: plans, limits, usage metering, billing hooks.

## 5. Non-goals (explicit)

These are **not** on the roadmap, because Meta's API does not support them:

* Posting to personal (consumer) Instagram accounts.
* Automated following, unfollowing, or liking other users' content.
* Mass/cold DM outreach to users who never engaged.
* Story stickers, polls, quizzes, link stickers, countdowns.
* Story highlights, guides, pinned posts.
* Live streaming.
* Follower/following list export.
* Keyword or location search of public content (only hashtag search, Facebook
  Login path only).
* Buying followers/engagement, or any growth-hacking that violates Meta's terms.

If a customer asks for one of these, the answer is "the official API does not
allow it", not a workaround.

## 6. Functional requirements

### 6.1 Identity & access
* Email + password registration, Argon2id hashing, 12-character minimum.
* Email verification, password reset (single-use, 30-minute token).
* JWT access (15 min) + rotating refresh tokens with reuse detection.
* Session list, per-device revoke, revoke-all.
* TOTP two-factor (schema and settings present; enforcement lands in M2).
* Google OAuth login (schema present; provider lands in M2).
* Seven built-in roles + workspace-scoped custom roles.
* Workspace ownership transfer, member invitation.

### 6.2 Instagram connection
* Both official auth paths.
* Token encryption at rest, automatic refresh before expiry.
* Capability detection (notably Stories = Business only).
* Disconnect with data purge.

### 6.3 Content
* CRUD on content with versions and change history.
* Types: IMAGE, CAROUSEL, REELS, STORIES.
* States: DRAFT → IN_REVIEW → APPROVED → SCHEDULED → PUBLISHING → PUBLISHED /
  FAILED, plus CANCELLED and ARCHIVED.
* Media library with magic-byte validation and size caps.
* Calendar with Jalali and Gregorian views, per-workspace timezone.

### 6.4 Publishing
* Immediate and scheduled, queued, retried with backoff.
* Idempotent, approval-gated, cancellable before execution.
* Per-account daily quota guard.
* Full attempt history including raw Meta errors.

### 6.5 AI
* Caption, hashtags, CTA generation grounded in a Brand Profile.
* Multi-day content plan generation.
* Per-workspace monthly budget in USD cents, enforced before the call.
* Full generation history with cost, latency, moderation flags.
* Guard that flags unverifiable claims and banned words.

### 6.6 Inbox / comments / CRM / analytics
Schema and tenancy are in place; the sync and UI land in M3/M4. See roadmap.

### 6.7 Billing
* Plans with limits (accounts, members, posts/month, AI credits).
* Usage metering per metric per period.
* Stripe and Zarinpal behind one provider interface (M5).
* No card data stored, ever.

## 7. Non-functional requirements

| Area | Requirement |
|---|---|
| Security | OWASP ASVS L2 as the target; see SECURITY.md |
| Isolation | No cross-tenant read is possible; proven by tests |
| Availability | API stateless; workers restartable; jobs survive restarts |
| Latency | API p95 < 300 ms excluding Meta calls; webhooks answered in < 1 s |
| Auditability | Every sensitive action has an audit row with actor and trace id |
| Localisation | fa-IR (RTL) first, en-US second; per-workspace timezone and calendar |
| Accessibility | WCAG 2.2 AA on the frontend |
| Data retention | Purge of cached platform data on disconnect; documented retention |

## 8. Acceptance criteria (M1)

- [x] Register with email + password; weak passwords rejected.
- [x] Log in; access + refresh tokens issued; refresh rotates.
- [x] Reusing a rotated refresh token revokes the whole family.
- [x] Create a workspace; creator becomes OWNER.
- [x] Invite a member; accept the invitation; role applied.
- [x] RBAC blocks a VIEWER from creating content (403).
- [x] Another user's workspace returns 403/404, never data.
- [x] Start an Instagram connection; authorize URL is correct for both paths.
- [x] Complete a connection; token stored encrypted; capabilities derived.
- [x] Upload a JPEG; reject an SVG and an oversized file.
- [x] Create content, submit for review, approve.
- [x] Enqueue a publish; the same idempotency key returns the same job.
- [x] Publish an image, reel, carousel and story against a scripted Meta API.
- [x] Transient Meta error retries with backoff; permanent error fails cleanly.
- [x] Webhook signature verified; duplicates ignored; replays rejected.
- [x] AI caption grounded in brand facts; budget enforced; history stored.
- [x] OpenAPI spec published and complete.
- [x] `alembic upgrade head` creates 50 tables; `downgrade base` removes them.

## 9. Risks

| Risk | Mitigation |
|---|---|
| Meta App Review rejects permissions | Apply early, provide screen recordings, start with Instagram Login |
| Meta changes limits or deprecates fields | Capability matrix in one place; re-verify each release; no hard-coded assumptions scattered in code |
| AI invents product claims | Ground prompts in verified facts only; flag unverifiable phrases; never auto-publish AI output |
| Token leak | Encryption at rest with AAD binding; separate key from JWT; fingerprint only in logs |
| Noisy tenant exhausts Meta's shared quota | Per-account quota guard + per-workspace AI budget + rate limits |
| Customer expects an unsupported feature | Explicit non-goals list, surfaced in the UI and docs |
