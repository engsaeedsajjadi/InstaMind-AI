# Software Architecture

## 1. Style: modular monolith

One deployable API process, strictly divided into modules with their own models,
services and routes. Modules communicate through Python calls inside a request
or through Celery tasks across processes — never by reaching into another
module's tables.

Why not microservices on day one: the team is small, the domain boundaries are
still moving, and a monolith gives us one transaction for "approve content and
queue its publish". The seams that matter are already drawn so a module can be
extracted later:

* every module owns its tables (no cross-module foreign keys except to
  `workspaces`, `users`, `social_accounts`, `contents`),
* cross-module work goes through a task, not a direct import of another module's
  service internals,
* all external systems sit behind an injectable provider
  (`Transport`, `AIProvider`, `StorageBackend`).

### Module map

| Module | Owns | Status |
|---|---|---|
| `identity` | users, sessions, refresh tokens, security events | ✅ implemented |
| `tenants` | workspaces, memberships, roles, invitations | ✅ implemented |
| `instagram` | social accounts, encrypted OAuth tokens, OAuth states | ✅ implemented |
| `content` | contents, versions, approvals, media assets, brand profiles, calendar | ✅ implemented |
| `publishing` | jobs, attempts, quota, retry/backoff | ✅ implemented |
| `ai` | generations, tasks, agent runs and tool calls (models) | ✅ caption + plan; agents pending |
| `webhooks` | Meta event ingestion, signature, replay, idempotency | ✅ implemented |
| `billing` | plans, subscriptions, usage, invoices, payments (models) | ⚠️ plans/usage only |
| `audit` | audit log, notifications | ✅ log; notifications pending |
| `storage` | object storage + upload validation | ✅ implemented |
| `inbox` | conversations, messages (models) | ⚠️ schema only |
| `comments` | comments, replies (models) | ⚠️ schema only |
| `crm` | customers, notes, activities (models) | ⚠️ schema only |
| `analytics` | snapshots (models) | ⚠️ schema only |

## 2. Request lifecycle

```
client
  → nginx
  → RequestContextMiddleware      trace id, security headers, access log
  → CORSMiddleware
  → route
      → get_current_claims        verify JWT (signature, exp, iss, aud, typ)
      → get_current_user          load user, verify session not revoked, MFA
      → get_current_context       resolve workspace from X-Workspace-Id,
                                  load membership, resolve permissions
      → require(Permission.X)     RBAC gate
      → TenantScope               tenant-scoped queries
      → service                   business rules
      → MetaGraphClient / AI      external systems
  → AppError handler              application/problem+json
```

Every error body carries `trace_id`, which is also returned as `X-Request-Id`
and written to the log line, so a user report maps to one server-side execution.

## 3. Multi-tenancy

`TenantScope` is the only sanctioned way to read or write workspace-owned data.

```python
scope = TenantScope(session, context)
content = await scope.get_one(Content, content_id)   # 404 if foreign
rows    = await scope.list(Content, Content.status == "DRAFT")
scope.attach(new_row)                                 # stamps workspace + actor
await scope.soft_delete(row)
```

Three properties make this safe:

1. `scoped()` injects `workspace_id = :ctx` into every statement and excludes
   soft-deleted rows.
2. `_verify()` re-checks `row.workspace_id` after the fetch, so even a query
   that forgot the predicate cannot leak a row — it raises
   `TenantIsolationViolation`.
3. That violation surfaces as **404**, not 403. Existence of another tenant's
   record is not disclosed.

`workspace_id` is read from the `X-Workspace-Id` header, never from a request
body, so a confused client cannot swap tenants mid-request.

## 4. Publishing engine

```
enqueue → QUEUED → claim (atomic lease) → RUNNING → SUCCEEDED
                        │                    ├→ QUEUED   (transient, backoff)
                        │                    ├→ FAILED   (permanent)
                        │                    └→ DEAD     (attempts exhausted)
                        └→ CANCELLED (only while QUEUED)
```

* **Idempotency** — unique `(workspace_id, idempotency_key)`. A retried HTTP
  request or a duplicate Celery delivery returns the existing job.
* **Single execution** — a worker claims a job by writing `lease_owner` +
  `lease_expires_at` in the same `UPDATE ... WHERE state='QUEUED'`. Two workers
  cannot both win.
* **Approval gate** — a job with `requires_approval` refuses to run until the
  latest `content_approvals` row is `APPROVED`.
* **Pre-flight validation** — caption, media, capabilities and quotas are
  checked *before* any Meta call, so invalid content never burns API budget.
* **Attempt log** — every API exchange lands in `publishing_attempts` with the
  raw Meta error, which is what support needs when a customer says "it didn't
  post".

## 5. External system boundaries

| Boundary | Interface | Production | Test |
|---|---|---|---|
| Meta Graph | `providers.meta.client.Transport` | `HttpxTransport` | `FakeTransport` (scripted) |
| AI | `providers.ai.base.AIProvider` | `OpenAIProvider` | `FakeAIProvider`, `NullProvider` |
| Object storage | `modules.storage.backend.StorageBackend` | `S3Storage` | `LocalStorage` |
| Payments | (billing milestone) provider protocol | Stripe / Zarinpal | fake gateway |

`NullProvider` and `BILLING_PROVIDER=null` fail **loudly** with a 503 and a
clear message. They never return invented content or a fake invoice.

## 6. Background work

Two Celery queues: `publishing` (long, rate-limited, Meta-facing) and `default`
(everything else). `worker_prefetch_multiplier=1` so one worker cannot hoard the
publish backlog.

| Task | Trigger |
|---|---|
| `instamind.publishing.process_due_jobs` | every 60 s |
| `instamind.publishing.process_job` | immediate publish |
| `instamind.instagram.refresh_tokens` | daily 03:00 UTC |
| `instamind.webhooks.process_event` | per webhook |
| `instamind.storage.purge_expired_media` | daily 04:00 UTC |

Workers have no request context, so each task builds its own session and a
`TenantContext` with `actor_type="SYSTEM"` resolved **from the row being
processed**, never from a task argument.

## 7. Failure handling

* **Meta transient** → backoff retry, content stays `SCHEDULED`.
* **Meta permanent** → job `FAILED`, content `FAILED`, Meta message stored on
  `contents.failure_reason` and shown verbatim in the UI.
* **Token expired** → account `TOKEN_EXPIRED`, user asked to reconnect.
* **AI budget exhausted** → `402 quota_exceeded` *before* calling the provider.
* **Malformed AI output** → generation recorded as `FAILED`, surfaced in the
  response as `status: FAILED` with a warning. Never presented as an empty
  successful result.
* **Webhook duplicate** → ignored, `200` returned so Meta stops retrying.
* **Unhandled exception** → 500 with `trace_id`, details to the log, never to
  the client.

## 8. Observability

* Structured JSON logs (`structlog`) with `trace_id`, `workspace_id`,
  `user_id`; secrets redacted before emission.
* `/health/live` (no dependencies) and `/health/ready` (database) — separate so
  a liveness probe does not flap on a DB blip.
* `/metrics` in Prometheus text format.
* `audit_logs` for business-level accountability; `security_events` for
  authentication telemetry.

## 9. Extracting a module later

To move, say, publishing into its own service:

1. Keep the tables; point the new service at the same database or replicate.
2. Replace `PublishingService` calls in the API with an HTTP/gRPC client behind
   the same interface.
3. Move the Celery tasks; the queue names already isolate them.
4. The `TenantContext` becomes an authenticated service token carrying the same
   workspace id — the isolation rule does not change.
