# syntax=docker/dockerfile:1
#
# Backend image: FastAPI + Celery worker/beat.
# Multi-stage so the runtime image has no compiler toolchain and no test deps.

ARG PYTHON_VERSION=3.12

# --------------------------------------------------------------------------- #
# 1. Builder — install wheels
# --------------------------------------------------------------------------- #
FROM python:${PYTHON_VERSION}-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

# Build deps only needed to compile any wheel with a C extension.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install .

# --------------------------------------------------------------------------- #
# 2. Runtime
# --------------------------------------------------------------------------- #
FROM python:${PYTHON_VERSION}-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    INSTAMIND_APP_ENV=production

# `curl` for healthchecks, `libpq5` for the asyncpg/psycopg runtime.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl libpq5 tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 1001 app \
    && useradd --system --uid 1001 --gid app --home /srv/app app

WORKDIR /srv/app

COPY --from=builder /opt/venv /opt/venv
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./

# Unprivileged, read-only where possible.
RUN mkdir -p /srv/app/storage && chown -R app:app /srv/app
USER app

EXPOSE 8000

# Liveness: the process answers. Readiness: the database answers.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health/live || exit 1

# tini reaps zombies and forwards signals so Celery shuts down cleanly.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
