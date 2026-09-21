# Database Schema

50 tables, PostgreSQL 16 in production, SQLite in tests (same models — a
portable `GUID` type descriptor switches between `uuid` and `CHAR(36)`).

Managed by Alembic: `make migrate` / `make migration m="..."`.
Initial revision: `d6a41e4cdff5_initial_schema`.

## Conventions

| Rule | Reason |
|---|---|
| UUID primary keys, generated in the app | No tenant-volume leakage, shard-safe |
| `DateTime(timezone=True)` everywhere, UTC | One time basis; convert only at render |
| `workspace_id` NOT NULL on tenant rows | An orphaned tenant row is a bug, not a state |
| Composite `(workspace_id, created_at)` index | The dominant query shape |
| Soft delete (`deleted_at`) where history matters | Content, media, CRM; never for audit logs |
| `created_by` / `updated_by` | Application-level attribution beyond the audit log |
| Naming constraints via a convention dict | Deterministic migration diffs |

> **SQLite caveat:** SQLite returns naive datetimes. All comparisons go through
> `app.db.base.as_utc` / `is_past`, otherwise a naive-vs-aware `TypeError`
> appears in tests and local dev.

> **`metadata` is reserved** by SQLAlchemy's Declarative API. Columns keep the
> documented name `metadata` while the Python attribute is `detail` (audit) or
> `meta` (calendar, CRM activity).

## Identity

| Table | Purpose |
|---|---|
| `users` | Account, Argon2id hash, locale/timezone, MFA flags, lockout |
| `oauth_identities` | External IdP links (Google, Meta user login) |
| `user_sessions` | A logical login; groups a device's refresh tokens |
| `refresh_tokens` | Rotating tokens: hash only, `family_id`, `replaced_by_id`, `chain_expires_at` |
| `email_verification_tokens` | Hashed, single-use, 48 h |
| `password_reset_tokens` | Hashed, single-use, 30 min |
| `security_events` | Logins, failures, lockouts, refresh-reuse detections |

**Refresh token rotation.** Only the SHA-256 of the token id is stored. On use
the row is marked `is_revoked` with reason `rotated` and linked to its
replacement — that flag is what makes replay detectable. Presenting a rotated
token outside a short grace window revokes the whole `family_id`.

## Tenancy

| Table | Purpose |
|---|---|
| `workspaces` | Tenant root: name, slug, locale, timezone, calendar system |
| `workspace_memberships` | User ↔ workspace with a role code |
| `roles` | Custom roles (workspace-scoped) — built-ins live in code |
| `workspace_invitations` | Hashed token, role, expiry, single-use |
| `system_settings` | Platform-level key/value (admin panel) |
| `notification_preferences` | Per user/channel event opt-in |

## Instagram

| Table | Purpose |
|---|---|
| `social_accounts` | Connected account, `api_path`, capabilities, status |
| `oauth_tokens` | **Encrypted** access token + fingerprint + expiry |
| `oauth_states` | Single-use OAuth CSRF state, PKCE verifier |
| `webhook_events` | Inbound Meta events, unique `(platform, event_key)` |
| `meta_api_usage` | Per-account rate/quota accounting |

`oauth_tokens.access_token_encrypted` is a Fernet envelope
(`v1:<key_id>:<token>`) with AAD `oauth_token:<row_id>`, so copying a
ciphertext to another row fails to decrypt. The raw token appears in no log and
no API response — only `access_token_fingerprint` (16 hex chars of SHA-256).

## Content

| Table | Purpose |
|---|---|
| `media_assets` | Uploaded files: detected MIME, checksum, `public_url` |
| `contents` | The post: caption, type, media order, state, schedule |
| `content_versions` | Immutable snapshot before each mutation |
| `content_approvals` | Review trail (PENDING/APPROVED/REJECTED/CHANGES_REQUESTED) |
| `content_calendar` | Calendar projection (content, holiday, note) |
| `brand_profiles` | Verified facts the AI is allowed to state |
| `campaigns`, `content_categories`, `hashtags` | Organisation |
| `ai_generations` | Every AI call: tokens, cost, latency, flags, error |
| `ai_tasks`, `agent_runs`, `agent_tool_calls` | Agent orchestration (M4) |

## Publishing

| Table | Purpose |
|---|---|
| `publishing_jobs` | One publish per (content, account); unique idempotency key; lease fields |
| `publishing_attempts` | One row per API exchange, with the raw Meta error |
| `publishing_quota` | Rolling 24 h counter per account |

Indexes: `uq_publishing_jobs_ws_idem` (idempotency), `ix_publishing_jobs_due`
(`state, scheduled_for` — the worker's poll query).

## Engagement

| Table | Purpose |
|---|---|
| `conversations` | DM thread + `last_inbound_at` + `messaging_window_expires_at` |
| `messages` | In/out messages, sender kind (HUMAN / AI_SUGGESTED / AI_AUTOMATIC) |
| `comments`, `comment_replies` | Comments and reply history |
| `customers`, `customer_notes`, `customer_activities` | CRM |
| `analytics_snapshots` | Insights metrics; `is_available=false` means Meta returned nothing |

`conversations.messaging_window_expires_at` encodes Meta's 24-hour rule in the
schema, so the reply path can refuse a send that would fail.

## Billing

| Table | Purpose |
|---|---|
| `plans` | Price + `limits` JSON + feature flags |
| `subscriptions` | Current plan, period, provider reference |
| `usage_records` | Unique `(workspace_id, metric, period_start)` counter |
| `coupons`, `invoices`, `payments` | Discounts and billing documents |

`payments` stores a **provider reference only** — no PAN, no CVV, no card data.

## Audit

| Table | Purpose |
|---|---|
| `audit_logs` | Append-only; `workspace_id` may be NULL for platform actions |
| `notifications` | In-app/email/SMS fan-out |

Audit rows are never updated or soft-deleted. Retention is by archiving, not
mutation.

## Indexing notes

* `ix_contents_ws_status_scheduled` — the calendar and the due-jobs query.
* `ix_publishing_jobs_due (state, scheduled_for)` — the worker poll must not
  scan the table.
* `uq_webhook_events_platform_key` — idempotency at the storage layer.
* `uq_usage_records_ws_metric_period` — usage counters are one `UPDATE`, never
  an insert race.
* `uq_publishing_jobs_ws_idem` — a retried publish cannot create a second job.
