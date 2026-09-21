# Security

Target: OWASP ASVS Level 2, OWASP Top 10 (2021) coverage, and Meta Platform
Policy compliance. This document describes what is actually implemented.

## Reporting a vulnerability

Email **security@instamind.ai**. Please include reproduction steps and impact.
We aim to acknowledge within 2 business days and fix critical issues within 7.
Do not open a public issue.

---

## 1. Authentication

| Control | Implementation |
|---|---|
| Password hashing | Argon2id (`time_cost=3`, `memory=64 MiB`, `parallelism=2`), per-password salt |
| Rehash on login | `check_needs_rehash` upgrades weaker hashes transparently |
| Password policy | ≥ 12 chars + low-entropy rejection (NIST SP 800-63B: no composition rules) |
| Account lockout | 5 failures → 15-minute lock, `security_events` logged |
| Enumeration resistance | A dummy Argon2 verify runs for unknown emails; reset always returns `202` |
| Access tokens | HS256 JWT, 15 min, `iss`/`aud`/`exp`/`iat`/`jti` all required |
| Refresh tokens | Opaque id stored as SHA-256; rotated on every use |
| Reuse detection | Replaying a rotated token revokes the whole family and logs `auth.refresh_reuse_detected` |
| Retry tolerance | A 10-second grace window returns the same replacement instead of signing the user out |
| Session lifetime | Absolute chain expiry (90 d) that rotation cannot extend |
| Session control | List, revoke one device, revoke all; password change/reset revokes everything |
| Rate limiting | Per-IP limits on every credential-handling route |
| MFA | TOTP schema + settings present; enforcement lands in M2 |

## 2. Authorization

* RBAC with 7 built-in roles; permissions are `resource:action` strings resolved
  per request from the membership (plus optional custom role).
* Every route declares its requirement with `require(Permission.X)`; there is no
  "default allow".
* Platform admin (`is_superuser`) is a separate flag, never granted by a
  workspace role.

## 3. Tenant isolation

* `workspace_id` comes from the `X-Workspace-Id` header, never from a body.
* `TenantScope` injects the tenant predicate into every query **and** re-verifies
  the ownership of each returned row.
* A foreign row yields `404`, not `403` — existence is not disclosed.
* Six dedicated tests assert that a second tenant cannot read, list, count or
  verify another tenant's rows.

## 4. Secrets and encryption

| Data | Treatment |
|---|---|
| Passwords | Argon2id hash; never reversible |
| Meta access tokens | Fernet (AES-128-CBC + HMAC-SHA256) envelope, AAD bound to the row id |
| TOTP secrets | Encrypted at rest (same cipher) |
| JWT key vs encryption key | **Separate** secrets, so leaking one does not compromise the other |
| Payment data | Not stored. Provider references only |
| Logs | `redact_secrets` strips token/password/secret keys before emission |
| Token logging | Only a 16-char SHA-256 fingerprint |

Ciphertext format: `base64(json{v:"v1", k:"<key_id>", t:<fernet>, aad:"<row>"})`.
Rotating the master key adds a key generation without a migration blackout.

## 5. Input validation & output encoding

* Pydantic v2 models on every request body; unknown fields rejected.
* SQLAlchemy Core/ORM with bound parameters everywhere — no string-built SQL.
* Uploads: size cap per kind, **magic-byte** MIME detection (client headers are
  not trusted), declared-vs-detected mismatch rejected, SVG/HTML/executables
  refused, filenames never used as storage keys.
* `is_safe_public_media_url` blocks private, loopback and link-local hosts so a
  user-supplied media URL cannot become an SSRF vector.
* Error bodies never echo raw input beyond the offending field name; internal
  details go to the log with a `trace_id`.

## 6. Transport & headers

`RequestContextMiddleware` sets:

```
X-Content-Type-Options: nosniff
X-Frame-Options: DENY
Referrer-Policy: no-referrer
Cross-Origin-Opener-Policy: same-origin
Cross-Origin-Resource-Policy: same-site
Permissions-Policy: geolocation=(), microphone=(), camera=(), payment=()
Content-Security-Policy: default-src 'self'; … frame-ancestors 'none'; base-uri 'none'
Strict-Transport-Security: …   (only when the request is actually HTTPS)
```

CORS is an explicit allow-list from `CORS_ORIGINS`; production refuses to boot
with an empty list. Cookies are not used for API auth (bearer tokens), which
sidesteps CSRF for the API; the frontend stores tokens in memory with a rotating
refresh call.

## 7. Webhooks

* `X-Hub-Signature-256` verified with `hmac.compare_digest` over the raw body.
* In production, a missing app secret means verification **fails closed**.
* Events older than 600 s are rejected (replay protection).
* Unique `(platform, event_key)` makes a duplicate delivery a no-op.
* The handler persists and queues, then returns `200` — never blocks on Meta
  work, and never errors in a way that would make Meta retry a processed event.

## 8. Publishing safety

* Approval gate: a job cannot execute without an `APPROVED` review row.
* Idempotency key: a retried request cannot publish twice.
* Atomic lease: two workers cannot both execute one job.
* Pre-flight validation: invalid content never reaches Meta.
* Per-account daily quota guard below Meta's ceiling.
* Every attempt logged with the raw Meta error.

## 9. AI safety

* Prompts are grounded in verified brand facts; the system prompt forbids
  inventing prices, stock, certifications or reviews.
* Output is scanned for unverifiable claims and banned words; flags are surfaced
  and stored, never silently rewritten.
* Per-workspace monthly budget enforced **before** the provider call.
* No AI output is published without a human approval step.
* `AI_PROVIDER=null` returns `503` rather than fabricating content.

## 10. Audit & monitoring

* `audit_logs`: every connect/disconnect, publish, approval, role change,
  ownership transfer, AI spend. Append-only, with actor, IP, trace id and
  before/after.
* `security_events`: logins, failures, lockouts, refresh reuse, password
  changes.
* Structured logs with `trace_id`, returned to the client as `X-Request-Id`.
* `/health/live`, `/health/ready`, `/metrics`.
* Sentry DSN configurable; alert on 5xx and on `auth.refresh_reuse_detected`.

## 11. Dependency & supply chain

* Pinned, reviewed dependencies; `pip` installs from a lockfile in CI.
* Docker image is multi-stage: no compiler toolchain, runs as UID 1001, `tini`
  as PID 1.
* No secrets in the image; all configuration via environment.

## 12. Data retention

* Cached platform data (messages, comments, insights) is soft-deleted on
  account disconnect, with `purge_requested_at` / `purge_completed_at` recorded.
* Unpublished uploads are purged after 7 days.
* Audit logs are archived, never mutated.

## 13. What is explicitly *not* done

* No unofficial Instagram endpoints, no scraping, no session reuse, no password
  storage for social accounts.
* No automated cold DMs, mass messaging, auto-follow or auto-like.
* No card data storage.
* No `eval`, no dynamic SQL, no user-controlled deserialization.

## 14. Testing

Security-relevant tests (all passing):

* Argon2id hashing, salting, malformed-hash rejection
* Ciphertext AAD binding, tamper detection, unknown key rejection, rotation
* HMAC signature verification and tamper detection
* Refresh rotation + reuse revocation + grace window
* Account lockout, enumeration resistance
* Six tenant-isolation tests
* RBAC denial at the API layer
* Upload rejection (SVG, oversize, MIME mismatch)
* Webhook signature, replay, duplicate handling
* SSRF guard on public media URLs

Run them with `make test`.
