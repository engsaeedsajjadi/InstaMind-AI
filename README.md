# InstaMind AI

Enterprise multi-tenant SaaS for AI-assisted Instagram management: content
generation, scheduling, publishing, inbox, comments, CRM and analytics — built
exclusively on Meta's official Instagram Platform APIs.

> **Status: Foundation + engagement API/UI pass are implemented; production hardening is in progress.** See
> [Roadmap](docs/roadmap.md) for exactly what exists, what is stubbed, and what
> is not started. Nothing in this repository fakes a capability.

---

## What this is

| | |
|---|---|
| **Product** | Multi-tenant SaaS for managing professional Instagram accounts |
| **Backend** | Python 3.12+, FastAPI, SQLAlchemy 2 (async), Alembic, Celery |
| **Data** | PostgreSQL 16, Redis |
| **Storage** | S3-compatible (MinIO in dev) |
| **AI** | OpenAI behind a provider interface, with per-workspace cost metering |
| **Instagram** | Official Meta APIs only — both the Instagram Login and Facebook Login paths |
| **Frontend** | Next.js + TypeScript + Tailwind (RTL/fa-IR first) — landing, auth, dashboard, Instagram connect, inbox, comments, CRM and analytics views |

## Hard rules this codebase follows

1. **Official APIs only.** No Selenium, no session cookies, no password login,
   no scraping. If Meta does not support an operation, the feature does not
   exist.
2. **No fabricated data.** A metric the Insights API does not return is stored
   as NULL and rendered as "no data" — never as zero, never as a mock.
3. **No plaintext credentials.** Meta tokens are encrypted at rest with Fernet
   and bound to their own row. Passwords are Argon2id.
4. **Real tenant isolation.** Every workspace-owned query goes through
   `TenantScope`, which re-verifies ownership of whatever comes back.
5. **Sensitive operations are audited.** Every publish, connect, disconnect,
   role change and AI spend writes an `audit_logs` row.

## Quick start

```bash
cp .env.example .env          # then fill in the secrets
docker compose up --build     # Postgres, Redis, MinIO, API, worker, beat, nginx
```

* API: <http://localhost:8000>
* Interactive docs: <http://localhost:8000/docs>
* Health: <http://localhost/health/live>, readiness: `/health/ready`

Without Docker:

```bash
make setup
export INSTAMIND_DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/instamind
make migrate
make api
```

### Run the tests

```bash
make test          # 167 tests, SQLite, no network, no Docker
make check         # lint + tests (what CI runs)
```

## Connecting an Instagram account

Two official paths are supported; configure whichever your Meta app uses.

| Path | Requires | Gets you |
|---|---|---|
| **Instagram Login** | Business or Creator account | Publishing, comments, messaging, insights |
| **Facebook Login** | Business/Creator **linked to a Facebook Page** | All of the above **plus** Business Discovery, hashtag search, product tagging |

`POST /api/v1/social-accounts/connect` returns an `authorize_url`; Meta
redirects the browser back to
`/api/v1/social-accounts/oauth/{path}/callback`. The access token never touches
the browser and is never returned by any endpoint.

Full permission list, App Review checklist and rate limits:
**[docs/instagram-integration.md](docs/instagram-integration.md)**

## Documentation

| Document | Contents |
|---|---|
| [Product Requirements](docs/product-requirements.md) | Scope, personas, acceptance criteria, explicit non-goals |
| [Architecture](docs/architecture.md) | Modular monolith, module map, request lifecycle, failure handling |
| [Database Schema](docs/database-schema.md) | 50 tables, tenancy rules, indexing rationale |
| [Instagram Integration](docs/instagram-integration.md) | Permissions, capability matrix, rate limits, error codes |
| [Security](SECURITY.md) | Threat model, controls, key rotation, disclosure policy |
| [Deployment](docs/deployment.md) | Environments, secrets, migrations, backup, DR |
| [API Guide](docs/api-guide.md) | Auth, tenancy headers, error contract, pagination |
| [AI & Agents](docs/ai-agents.md) | Prompt rules, guardrails, budget, tool permissions |
| [Roadmap](docs/roadmap.md) | Milestone status — what is real, what is not |

## Repository layout

```
instamind-ai/
├── apps/backend/          FastAPI app, Celery worker, migrations, tests
│   ├── app/
│   │   ├── core/          config, security, jwt, permissions, errors, rate limit
│   │   ├── db/            engine, base model mixins, model registry
│   │   ├── modules/       identity, tenants, instagram, content, publishing,
│   │   │                  ai, webhooks, billing, audit, storage, inbox, ...
│   │   ├── providers/     meta (Graph client), ai (OpenAI + null + fake)
│   │   ├── api/           routes, schemas, dependencies
│   │   └── worker/        Celery app and tasks
│   ├── migrations/        Alembic
│   └── tests/             167 tests
├── apps/frontend/         Next.js — landing, auth, dashboard, IG connect (see apps/frontend/README.md)
├── infra/                 nginx, monitoring
├── docs/                  the documents above
└── docker-compose.yml
```

## License

Proprietary. See [LICENSE](LICENSE).
