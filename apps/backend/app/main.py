"""FastAPI application factory.

``create_app()`` is a factory rather than a module-level app so tests can build
isolated instances with their own settings, and so the worker can import the
same wiring without starting a server.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.routes import (
    ai,
    auth,
    billing,
    content,
    health,
    publishing,
    social_accounts,
    webhooks,
    workspaces,
)
from app.core.config import settings
from app.core.exceptions import (
    AppError,
    app_error_handler,
    http_exception_handler,
    unhandled_exception_handler,
    validation_exception_handler,
)
from app.core.logging import configure_logging, logger
from app.middleware import RequestContextMiddleware

DESCRIPTION = """
**InstaMind AI** — multi-tenant SaaS for AI-assisted Instagram management.

* Authentication: `POST /api/v1/auth/login` → `access_token` (15 min) +
  `refresh_token` (rotating).
* Tenant scoping: pass `X-Workspace-Id: <uuid>` on every workspace-scoped call.
* Errors: RFC 9457 `application/problem+json`; branch on `code`, not prose.
* Instagram: official Meta APIs only. No unofficial endpoints are implemented.
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    logger.info(
        "app_started",
        env=settings.APP_ENV.value,
        storage=settings.STORAGE_BACKEND,
        ai=settings.AI_PROVIDER,
    )
    try:
        yield
    finally:
        from app.db.session import dispose_engine

        await dispose_engine()
        logger.info("app_stopped")


def create_app() -> FastAPI:
    configure_logging()
    openapi_tags = [
        {"name": tag, "description": description}
        for tag, description in (
            ("auth", "Registration, login, sessions, password lifecycle"),
            ("workspaces", "Tenants, members, roles, invitations"),
            ("instagram", "Official Meta connection flow and account management"),
            ("content", "Content studio, media library, approvals, calendar"),
            ("publishing", "Publish queue, retries, attempt history"),
            ("ai", "Caption and content-plan generation with cost metering"),
            ("billing", "Plans, subscription, usage"),
            ("webhooks", "Meta webhook verification and ingestion"),
            ("system", "Health, readiness, metrics"),
        )
    ]
    app = FastAPI(
        title=settings.APP_NAME,
        description=DESCRIPTION,
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if not settings.is_prod else None,
        redoc_url="/redoc" if not settings.is_prod else None,
        openapi_url="/openapi.json",
        openapi_tags=openapi_tags,
        swagger_ui_init_oauth={},
        contact={"name": "InstaMind AI", "url": "https://instamind.ai"},
        license_info={"name": "Proprietary"},
    )

    # ---- middleware (order matters: context first, then CORS) -------------- #
    app.add_middleware(RequestContextMiddleware)
    if settings.CORS_ORIGINS:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=[str(origin) for origin in settings.CORS_ORIGINS],
            allow_credentials=True,
            allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
            allow_headers=[
                "Authorization",
                "Content-Type",
                "X-Workspace-Id",
                "X-Request-Id",
                "Idempotency-Key",
            ],
            expose_headers=["X-Request-Id", "X-RateLimit-Remaining", "Retry-After"],
            max_age=600,
        )

    # ---- exception handlers ------------------------------------------------ #
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)

    # ---- routes ------------------------------------------------------------ #
    prefix = settings.API_V1_PREFIX
    app.include_router(health.router)
    app.include_router(auth.router, prefix=prefix)
    app.include_router(workspaces.router, prefix=prefix)
    app.include_router(social_accounts.router, prefix=prefix)
    app.include_router(content.router, prefix=prefix)
    app.include_router(publishing.router, prefix=prefix)
    app.include_router(ai.router, prefix=prefix)
    app.include_router(billing.router, prefix=prefix)
    # Webhooks are mounted under the version prefix but are unauthenticated by
    # design — Meta signs them instead.
    app.include_router(webhooks.router, prefix=prefix)
    return app


app = create_app()
