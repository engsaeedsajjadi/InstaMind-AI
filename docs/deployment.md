# Deployment Guide

## 1. Topology

```
                 ┌──────────────┐
   internet ───► │ nginx / LB   │  TLS termination, HSTS
                 └──┬────────┬──┘
                    │        │
              ┌─────▼──┐  ┌──▼───────┐
              │ api ×N │  │ frontend │  Next.js
              └─┬───┬──┘  └──────────┘
                │   │
     ┌──────────▼┐ ┌▼──────────┐   ┌──────────────┐
     │ Postgres  │ │  Redis    │   │ S3 / MinIO   │
     │ (primary  │ │ broker +  │   │ media        │
     │ + replica)│ │ cache     │   └──────────────┘
     └───────────┘ └─────┬─────┘
                         │
              ┌──────────▼──────────┐
              │ worker ×N (Celery)  │  publishing / default queues
              │ beat ×1             │  schedules
              └─────────────────────┘
```

The API is stateless: scale it horizontally. Only `beat` must run as a single
instance.

## 2. Environments

| | development | staging | production |
|---|---|---|---|
| `INSTAMIND_APP_ENV` | `development` | `staging` | `production` |
| Database | local Postgres / SQLite | managed Postgres | managed Postgres + replica |
| Storage | MinIO | S3 | S3 |
| `/docs` | on | on | **off** |
| HSTS | not sent over HTTP | sent | sent |
| Meta app | Dev mode | Dev mode | **Live + App Review passed** |

The app **refuses to boot** in production if `SECRET_KEY` is short,
`TOKEN_ENCRYPTION_KEY` is missing, `CORS_ORIGINS` is empty, or `DEBUG` is on.
That check is in `app/core/config.py::_check_production_hardening`.

## 3. Secrets

Never in the image, never in the repo. Inject at runtime from a secret manager
(Vault, AWS Secrets Manager, GCP Secret Manager, Doppler).

| Secret | Notes |
|---|---|
| `INSTAMIND_SECRET_KEY` | JWT signing. 32+ chars. |
| `INSTAMIND_TOKEN_ENCRYPTION_KEY` | Fernet master key. **Must differ** from `SECRET_KEY`. |
| `META_APP_SECRET` | Webhook signature verification. |
| `META_IG_CLIENT_SECRET` / `META_FB_CLIENT_SECRET` | OAuth. |
| `META_WEBHOOK_VERIFY_TOKEN` | Must match the Meta dashboard. |
| `OPENAI_API_KEY` | Omit to disable AI (`AI_PROVIDER=null`). |
| Database / Redis / S3 credentials | Per environment. |

### Rotating `TOKEN_ENCRYPTION_KEY`

The ciphertext envelope carries a key id, so rotation needs no downtime:

1. Add the new key: `TokenCipher.register_key("k2", new_secret)`.
2. `set_current_key("k2")` — new writes use it, old rows still decrypt.
3. Background job re-encrypts existing `oauth_tokens` rows under `k2`.
4. Drop `k1`.

### Rotating `SECRET_KEY`

Dual-key rollout: accept both during a window at least as long as the access
token TTL, then remove the old one. Refresh tokens issued under the old key
become invalid, which signs users out — schedule it deliberately.

## 4. Database

```bash
alembic upgrade head          # apply
alembic revision --autogenerate -m "…"   # create
alembic downgrade -1          # roll back one
```

Run migrations **before** starting new API pods, not inside them, so N replicas
do not race. In `docker-compose.yml` the backend container runs
`alembic upgrade head` then starts uvicorn, which is correct for a single
replica; with multiple replicas move it to an init job.

Postgres settings that matter:

* `timezone = UTC`
* `shared_buffers` ≈ 25 % of RAM, `work_mem` conservative (many small queries)
* `max_connections` sized for `pool_size × replicas + overflow`
* `idle_in_transaction_session_timeout = 60s`

## 5. Object storage

Meta fetches media from `public_url`, so:

* The bucket must serve objects over **public HTTPS** (or via presigned URLs
  with a TTL longer than the publish window — we use `S3_PRESIGN_TTL_SECONDS`,
  default 1 h).
* Do **not** put the bucket behind auth or a VPN.
* `is_safe_public_media_url` blocks private/link-local hosts so a user-supplied
  URL cannot turn the publisher into an SSRF proxy.
* Lifecycle rule: expire unpublished uploads after 7 days
  (`instamind.storage.purge_expired_media`).

## 6. Webhooks

Meta needs a **public HTTPS** endpoint that answers in seconds:

* URL: `https://<domain>/api/v1/webhooks/meta`
* Verify token: `META_WEBHOOK_VERIFY_TOKEN`
* Subscribe to: `comments`, `mentions`, `messages`, `messaging_postbacks`,
  `message_reactions`, `message_deliveries`, `message_reads`, `message_echoes`

Behind a proxy, forward `X-Forwarded-Proto: https` so HSTS is emitted, and do
not buffer the request body — the HMAC is computed over the raw bytes.

## 7. Health checks

| Endpoint | Use | Depends on |
|---|---|---|
| `/health/live` | liveness probe | nothing |
| `/health/ready` | readiness / LB | database |
| `/metrics` | Prometheus scrape | nothing |

Keep liveness free of dependencies: a database blip must not restart pods.

## 8. Scaling

| Symptom | Action |
|---|---|
| API p95 climbing | add API replicas (stateless) |
| publish backlog growing | add workers on the `publishing` queue |
| webhook latency rising | add workers on `default` |
| Meta 4/17/613 errors | lower `PUBLISH_DAILY_GUARD_LIMIT`; check `content_publishing_limit` |
| DB connection exhaustion | raise pool size *and* Postgres `max_connections` together |

`worker_prefetch_multiplier=1` is deliberate: publishing jobs are long and
rate-limited, so one worker must not hoard the queue.

## 9. Backup & disaster recovery

| Data | Strategy | RPO | RTO |
|---|---|---|---|
| Postgres | continuous WAL archiving + nightly base backup, encrypted at rest | 5 min | 1 h |
| Media (S3) | versioning + cross-region replication | 0 | 30 min |
| Redis | ephemeral (broker/cache) — rebuildable | n/a | n/a |
| Secrets | secret manager with its own replication | 0 | 15 min |

**Restore drill** (quarterly):

1. Restore the latest base backup + WAL to a scratch instance.
2. `alembic current` must match the deployed revision.
3. Run the smoke test: register → workspace → content → enqueue publish (with
   the Meta client pointed at a stub).
4. Verify `audit_logs` continuity across the restore point.

If `TOKEN_ENCRYPTION_KEY` is lost, stored Meta tokens are unrecoverable — every
customer must reconnect. Back the key up in the secret manager **and** an
offline escrow.

## 10. CI/CD

`.github/workflows/ci.yml` runs on every push and PR:

1. ruff (lint + format check)
2. mypy
3. pytest with coverage
4. `alembic upgrade head` against a throwaway Postgres service
5. Docker build

Deploy: build the image once, promote the same digest through staging to
production. Migrations run as a separate, gated step before the rollout.

## 11. Production checklist

- [ ] `INSTAMIND_APP_ENV=production`, `DEBUG=false`
- [ ] `SECRET_KEY` and `TOKEN_ENCRYPTION_KEY` are distinct, 48+ chars, from a CSPRNG
- [ ] `CORS_ORIGINS` is an explicit allow-list (no `*`)
- [ ] TLS terminated upstream; `X-Forwarded-Proto` forwarded
- [ ] `/docs` and `/redoc` disabled (automatic in production)
- [ ] Meta app in Live mode with Advanced Access approved
- [ ] Webhook configured and returning `200` in the Meta dashboard
- [ ] `PUBLIC_MEDIA_BASE_URL` is public HTTPS
- [ ] Sentry DSN set; alerting on 5xx and on `auth.refresh_reuse_detected`
- [ ] Backups verified by an actual restore
- [ ] First superuser created and its password rotated
