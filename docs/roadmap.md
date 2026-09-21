# Roadmap & Status

Honest status as of this commit. **Implemented** means code exists and is
covered by tests. **Schema only** means the tables and tenancy exist but there
is no service or endpoint yet. **Not started** means nothing but a plan.

## M1 — Foundation ✅ (this commit)

| Area | Status | Evidence |
|---|---|---|
| FastAPI app factory, error contract, middleware | Implemented | 44 documented paths, `/openapi.json` |
| Settings with production hardening | Implemented | refuses insecure boot |
| Argon2id passwords, lockout, security events | Implemented | `test_security.py`, `test_jwt_and_sessions.py` |
| JWT access + rotating refresh + reuse detection | Implemented | `test_refresh_reuse_revokes_the_whole_family` |
| Sessions: list, revoke one, revoke all | Implemented | `test_sessions_can_be_listed_and_revoked` |
| Password change/reset | Implemented | `test_password_reset_flow` |
| Workspaces, memberships, invitations | Implemented | `test_member_invitation_flow` |
| RBAC: 7 built-in roles + custom roles | Implemented | `test_tenant_isolation.py` |
| Tenant isolation via `TenantScope` | Implemented | 6 dedicated isolation tests |
| Encryption at rest (Fernet + AAD binding) | Implemented | `TestTokenCipher` |
| Instagram OAuth, both official paths | Implemented | `test_instagram_connection.py` |
| Token refresh before expiry, expiry handling | Implemented | `test_token_is_refreshed_inside_the_safety_margin` |
| Disconnect + data purge | Implemented | `test_disconnect_revokes_tokens_and_purges_platform_data` |
| Media upload with magic-byte validation | Implemented | `test_media_upload_*` |
| Content CRUD, versions, review, approvals | Implemented | `test_content_crud_and_review_flow` |
| Publishing engine (4 content types) | Implemented | `test_publishing_engine.py` |
| Idempotency, leases, backoff, quota guard | Implemented | 13 publishing tests |
| Webhooks: signature, replay, idempotency | Implemented | `test_webhooks.py` |
| AI caption + plan, budget, guardrails | Implemented | `test_ai_service.py` |
| Celery worker + beat, 5 tasks | Implemented | tasks register and import cleanly |
| Alembic initial migration | Implemented | 51 objects up, clean downgrade |
| Docker, compose, nginx, Makefile, `.env.example` | Implemented | files present |
| Documentation | Implemented | `docs/` |

**Test suite: 167 passing.** No network, no Docker required.

### Known gaps inside M1

* `POST /auth/register` creates the user but does not send a verification
  email; `email_verified_at` is not yet enforced as a gate.
* TOTP MFA and Google OAuth have schema but no endpoints.
* The worker is registered and importable; it has not been run against a live
  broker in this environment (no Redis binary available).
* `PublishingService` publishes against a scripted transport in tests. It has
  **not** been run against a real Meta account — that requires a reviewed app
  and a Business account, and is the first task of M2.

## M2 — Real-world Instagram validation

* Run the full OAuth flow against a real Meta app (both paths).
* Publish a real image, reel, carousel and story; record the responses.
* Token refresh against a real long-lived token.
* Configure and verify webhooks in the Meta dashboard.
* Migrate the app through App Review for Advanced Access.
* Email delivery (verification, reset, invitations) via SMTP.
* TOTP MFA enforcement.

## M3 — Engagement

* Inbox: conversation/message sync from webhooks + polling, assignment, labels,
  notes, human reply with `HUMAN_AGENT` only for humans.
* AI reply **suggestions** (never auto-send by default), grounded in product
  data.
* Comments: sync, reply, hide, spam flagging, escalation.
* CRM: customer creation from conversations, tags, notes, lead scoring from real
  signals, CSV export.
* Notifications service (in-app, email, SMS provider).

## M4 — Agents & analytics

* Agent orchestrator: planner, executor, QC, approval gate.
* Tool registry with the permission map in `docs/ai-agents.md`.
* Analytics: insights sync, per-content performance, campaign comparison,
  CSV/PDF export. Only metrics Meta returns; `is_available=false` renders as
  "no data".
* Jalali/Gregorian calendar UI with holidays and drag-and-drop.

## M5 — Billing & admin

* Stripe provider: checkout, webhooks, subscription lifecycle.
* Zarinpal provider for the Iranian market (sandbox first).
* Plan enforcement at the API edge (accounts, members, posts/month, AI credits).
* Invoices, coupons, payment history.
* Admin panel: users, workspaces, plans, usage, errors, revenue, health,
  security log — on a separate route surface and role.

## M6 — Frontend

The Next.js app is scaffolded but not built out. Planned:

* RTL-first layout, fa-IR + en-US, dark/light, WCAG 2.2 AA.
* Landing, auth, dashboard, workspaces, Instagram accounts, Content Studio,
  AI Assistant, Calendar, Media Library, Inbox, Comments, CRM, Analytics,
  Billing, Team, Settings, Admin.
* Skeletons, empty states, error states, toasts — no fake data anywhere.
* Vitest + Playwright.

## Deferred / won't do

Anything in the non-goals list of `docs/product-requirements.md` §5 — those are
blocked by Meta's API, not by our schedule.
