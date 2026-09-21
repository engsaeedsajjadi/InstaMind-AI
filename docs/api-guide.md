# API Guide

Base URL: `/api/v1`. Interactive docs: `/docs` (Swagger) and `/redoc`.
Machine-readable spec: `/openapi.json`.

## Authentication

```http
POST /api/v1/auth/login
{"email": "you@example.com", "password": "..."}

200 →
{
  "access_token": "eyJ...",
  "refresh_token": "eyJ...",
  "token_type": "Bearer",
  "expires_in": 900,
  "session_id": "…"
}
```

Send `Authorization: Bearer <access_token>` on every call. Access tokens live
15 minutes.

### Refresh

```http
POST /api/v1/auth/refresh
{"refresh_token": "eyJ..."}
```

The refresh token **rotates on every use**. Keep the new one; the old one is
dead. Replaying an old token outside a 10-second grace window is treated as
theft: the entire token family is revoked and every session for that login is
signed out.

| Endpoint | Effect |
|---|---|
| `POST /auth/logout` | Revoke the current session |
| `POST /auth/logout-all` | Revoke every session for the user |
| `GET /auth/sessions` | List sessions (device, IP, last seen) |
| `DELETE /auth/sessions/{id}` | Revoke one device |
| `POST /auth/password/change` | Change password, revokes all sessions |
| `POST /auth/password/reset-request` | Always `202` — never reveals whether the email exists |
| `POST /auth/password/reset-confirm` | Single-use token, 30 minutes |

## Tenancy

Pass the workspace explicitly:

```http
GET /api/v1/contents
Authorization: Bearer …
X-Workspace-Id: 0f1c…
```

Without the header the first workspace the user belongs to is used. A user who
is not a member gets `403`. A resource that belongs to another workspace gets
`404` — existence is not disclosed.

`GET /api/v1/workspaces/context` returns the effective identity:

```json
{
  "workspace_id": "…",
  "user_id": "…",
  "roles": ["OWNER"],
  "permissions": ["content:publish", "…"],
  "is_superuser": false
}
```

## Error contract

RFC 9457 `application/problem+json`. **Branch on `code`, never on prose.**

```json
{
  "type": "https://errors.instamind.ai/permission-denied",
  "title": "Insufficient permissions",
  "status": 403,
  "detail": "Missing permission: content:create",
  "code": "permission_denied",
  "instance": "/api/v1/contents",
  "trace_id": "3d11787449a0440e521d3c21b077a5f1",
  "errors": [
    {"field": "status", "code": "illegal_state_transition", "message": "Allowed next states: …"}
  ]
}
```

Meta errors add a `meta` object so clients can react precisely:

```json
{"code": "meta_api_error", "meta": {"code": 190, "subcode": 463, "type": "OAuthException", "retryable": false}}
```

| Status | `code` | Meaning |
|---|---|---|
| 400 | `app_error` | Generic bad request |
| 401 | `unauthenticated` | Missing/invalid/expired token, revoked session |
| 402 | `quota_exceeded` | Plan or AI budget limit reached |
| 403 | `permission_denied`, `forbidden`, `resource_not_in_workspace` | RBAC / tenancy |
| 404 | `not_found` | Missing, or belongs to another tenant |
| 409 | `conflict` | Duplicate (e.g. account already connected elsewhere) |
| 412 | `illegal_state_transition` | Content state machine |
| 413 | `payload_too_large` | Upload too big |
| 415 | `unsupported_media_type` | MIME sniffing rejected the file |
| 422 | `validation_error` | Field-level problems in `errors[]` |
| 429 | `rate_limit_exceeded` | `Retry-After` header set |
| 502 | `meta_api_error`, `external_service_error` | Upstream failure |
| 503 | `service_unavailable` | AI disabled, dependency down |

## Rate limits

`X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset` on limited
routes; `Retry-After` on `429`. Auth endpoints are limited per IP
(10/min default) to blunt credential stuffing.

## Endpoint map

| Area | Endpoints |
|---|---|
| Auth | `register`, `login`, `refresh`, `logout`, `logout-all`, `me`, `sessions`, `password/*` |
| Workspaces | `GET/POST /workspaces`, `GET /workspaces/context`, `GET /workspaces/{id}`, `POST /workspaces/{id}/transfer`, `members`, `invitations`, `roles` |
| Instagram | `GET /social-accounts`, `POST /social-accounts/connect`, `GET /social-accounts/oauth/{path}/callback`, `DELETE /social-accounts/{id}`, `POST /social-accounts/{id}/refresh-token` |
| Media | `POST /media?kind=IMAGE|VIDEO`, `GET /media` |
| Content | `GET/POST /contents`, `GET/PATCH/DELETE /contents/{id}`, `POST /contents/{id}/submit`, `POST /contents/{id}/approve`, `GET /calendar` |
| Publishing | `GET/POST /publishing/jobs`, `GET /publishing/jobs/{id}`, `GET /publishing/jobs/{id}/attempts`, `POST /publishing/jobs/{id}/cancel` |
| AI | `POST /ai/generate-caption`, `POST /ai/generate-content-plan`, `GET /ai/usage` |
| Billing | `GET /billing/plans`, `GET /billing/subscription`, `GET /billing/usage` |
| Webhooks | `GET/POST /webhooks/meta` (unauthenticated, signed) |
| System | `GET /health/live`, `GET /health/ready`, `GET /metrics` |

## Idempotency

`POST /api/v1/publishing/jobs` accepts `idempotency_key`. The same key returns
the same job — a retried request never publishes twice. Omitting it defaults to
`{content_id}:{account_id}`, which makes a duplicate publish of the same pair
impossible by default.

## Webhooks (for Meta, not for clients)

`GET /api/v1/webhooks/meta` echoes `hub.challenge` when the verify token
matches. `POST` verifies `X-Hub-Signature-256`, rejects events older than 600 s,
stores each event once, and returns `200` immediately.

## Versioning

`/api/v1` is stable. Breaking changes go to `/api/v2`; v1 is kept for at least
one deprecation cycle announced in the changelog.
