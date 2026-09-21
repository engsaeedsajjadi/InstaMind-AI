# Contributing

## Ground rules

These are not style preferences — they are what keep the product trustworthy.

1. **Official APIs only.** Never add an unofficial Instagram endpoint, a
   scraper, a browser-automation path, or password/session storage for social
   accounts. If Meta does not support it, we do not build it.
2. **No fabricated data.** Never hard-code sample metrics, fake dashboard
   numbers, or a placeholder that looks real. If data is unavailable, return
   `null` / `is_available=false` and render "no data".
3. **No secrets in code.** Configuration comes from the environment. A
   hard-coded key fails review.
4. **Tenant scope is mandatory.** Any query over workspace-owned data goes
   through `TenantScope`. Raw `select(Model)` on a tenant model is a bug.
5. **Sensitive operations are audited.** If it changes an account, publishes,
   moves money, or changes access, it writes an `audit_logs` row.
6. **Tests accompany behaviour.** A feature without a test is not finished.

## Setup

```bash
make setup          # install backend dev deps
make env            # create .env from the template
make test           # 167 tests, no Docker or network needed
```

For the full stack: `cp .env.example .env && docker compose up --build`.

## Workflow

1. Branch from `main`: `feat/inbox-sync`, `fix/publish-backoff`, `docs/...`.
2. Small, reviewable commits. One logical change per commit.
3. `make check` (lint + tests) must pass before opening a PR.
4. New tables need an Alembic migration: `make migration m="describe it"`.
5. Update the docs in the same PR — especially `docs/roadmap.md` status and
   `docs/instagram-integration.md` if a Meta capability changed.

## Code conventions

* **Async everywhere** in the backend. No blocking I/O inside a request.
* **Pydantic v2** for every request/response boundary; no dicts across module
  edges.
* **Services hold business rules**, routes stay thin (validate → call → serialise).
* **Models in `modules/<x>/models.py`**, registered in `app/db/models.py`.
* **Errors**: raise a subclass of `AppError`. Never return an ad-hoc error dict.
* **Datetimes**: always timezone-aware UTC in the database; compare via
  `as_utc` / `is_past` so SQLite behaves like Postgres.
* **Logging**: structured, with `trace_id`; secrets pass through
  `redact_secrets`.

## Adding a feature

1. Add permissions to `app/core/permissions.py` and attach them to the right
   roles.
2. Guard the route with `require(Permission.X)`.
3. Put business logic in a service that takes a `TenantScope`.
4. Audit sensitive actions.
5. Write tests: happy path, permission denial, tenant isolation, and the
   failure mode of any external call.

## Adding an external integration

Implement it behind a provider protocol (see `providers/`), with:

* a production implementation,
* a fake for tests that records calls,
* a null implementation that fails loudly rather than faking success.

## Testing expectations

| Type | Where |
|---|---|
| Unit | `tests/test_security.py`, `test_media_rules.py` |
| Integration | `test_publishing_engine.py`, `test_instagram_connection.py`, `test_webhooks.py` |
| API / E2E | `test_api_integration.py` (real ASGI app, in-process) |
| Security | isolation, RBAC, upload validation, signature checks |

Tests must not touch the network. Meta is exercised through `FakeTransport`;
AI through `FakeAIProvider`.

## Commit messages

```
<type>(<scope>): <summary>

feat(publishing): retry transient Meta errors with jittered backoff
fix(identity): revoke the presented refresh token on rotation
docs(instagram): record the April 2026 message-tag deprecation
```

## Definition of done

- [ ] Tests pass locally (`make check`)
- [ ] Migration added and `alembic upgrade head` verified
- [ ] Permissions and audit coverage considered
- [ ] No secrets, no hard-coded config, no fake data
- [ ] Docs updated
- [ ] PR describes what was verified against a real Meta account, if applicable
