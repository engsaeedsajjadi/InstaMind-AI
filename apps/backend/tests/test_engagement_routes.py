"""Smoke coverage that the production engagement routes are wired into the ASGI app."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_engagement_routes_are_registered(client):
    for path in (
        "/api/v1/inbox/conversations",
        "/api/v1/comments",
        "/api/v1/crm/customers",
        "/api/v1/analytics/snapshots",
    ):
        response = await client.get(path)
        # Registered protected routes must challenge authentication; 404 would
        # mean the capability is not wired into the application.
        assert response.status_code == 401, (path, response.status_code, response.text)
