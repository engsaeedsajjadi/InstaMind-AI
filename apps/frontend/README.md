# InstaMind AI — Frontend

RTL-first Persian (fa-IR) web app: Next.js 14 (App Router) + TypeScript + Tailwind.

## What exists today

| Screen | Route | Notes |
|---|---|---|
| Landing | `/` | Product intro, honest prerequisites (Business/Creator account) |
| Login | `/login` | Against `POST /api/v1/auth/login`; rotating refresh handled by the API client |
| Register | `/register` | Mirrors the backend password policy (min 12 chars) |
| Dashboard | `/dashboard` | Real counts only (accounts / contents / publish jobs), workspace creation, setup checklist — **no fake data** |
| Instagram connect | `/instagram` | Both official Meta paths; accounts list, token refresh, disconnect + purge |
| OAuth result | `/settings/instagram` | Landing target of the backend callback redirect (success/failure banner) |

## Run

```bash
npm install
npm run dev          # http://localhost:3000
```

The browser only ever calls **relative** `/api/*` URLs; Next rewrites proxy them
to the backend (`BACKEND_ORIGIN`, default `http://localhost:8000`). No CORS, no
localhost in client code — the same code works behind nginx and in Docker.

Production:

```bash
npm run build        # emits .next/standalone (what the Docker image runs)
node .next/standalone/server.js
```

Docker (from the repo root): `docker compose up --build` — nginx serves the app
on http://localhost and proxies `/api` to the backend.

## Auth model (scaffold)

Tokens live in `localStorage` (`instamind.auth`), the active workspace in
`instamind.workspace_id` (sent as `X-Workspace-Id`). On 401 the client rotates
the refresh token once; an empty `refresh_token` in the response (the backend's
rotation grace window) is kept, not overwritten. Hardening to httpOnly cookies
is a later milestone (see `docs/roadmap.md` M6).

## Deliberately not here yet

Content Studio, calendar, inbox/comments/CRM, analytics, billing, admin — those
backend areas exist as APIs; the UI is planned in `docs/roadmap.md` M6. This app
renders real API data or honest empty/error states — never mock data.
