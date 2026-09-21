"""Health, readiness and metrics endpoints.

``/health/live``  — process is up (no dependency checks; safe for liveness
                    probes, which must not flap on a DB blip).
``/health/ready`` — database reachable; used by load balancers and by
                    ``docker-compose`` healthchecks before traffic is routed.
``/metrics``      — Prometheus text format.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Response
from sqlalchemy import text

from app.api.deps import DBSession
from app.api.schemas import HealthResponse
from app.core.config import settings
from app.core.exceptions import ServiceUnavailable

router = APIRouter(tags=["system"])

VERSION = "0.1.0"


@router.get("/health/live", response_model=HealthResponse, include_in_schema=True)
async def liveness() -> HealthResponse:
    return HealthResponse(
        status="ok",
        version=VERSION,
        environment=settings.APP_ENV.value,
        checks={"process": "up"},
        utc_time=datetime.now(UTC),
    )


@router.get("/health/ready", response_model=HealthResponse)
async def readiness(session: DBSession) -> HealthResponse:
    checks: dict[str, str] = {"process": "up"}
    try:
        await session.execute(text("SELECT 1"))
        checks["database"] = "up"
    except Exception as exc:  # noqa: BLE001
        checks["database"] = f"down: {type(exc).__name__}"
        raise ServiceUnavailable("Database is not reachable.") from exc
    return HealthResponse(
        status="ok",
        version=VERSION,
        environment=settings.APP_ENV.value,
        checks=checks,
        utc_time=datetime.now(UTC),
    )


@router.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    """Minimal Prometheus exposition. A full registry is added with the
    observability milestone; the endpoint shape is already stable."""
    body = "\n".join(
        [
            "# HELP instamind_up 1 if the API process is serving requests",
            "# TYPE instamind_up gauge",
            "instamind_up 1",
            '# HELP instamind_build_info Build metadata',
            "# TYPE instamind_build_info gauge",
            f'instamind_build_info{{version="{VERSION}",env="{settings.APP_ENV.value}"}} 1',
        ]
    )
    return Response(content=body + "\n", media_type="text/plain; version=0.0.4")
