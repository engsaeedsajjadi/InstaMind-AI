from app.main import create_app


def test_engagement_routes_are_registered():
    app = create_app()
    paths = {route.path for route in app.routes}
    assert "/api/v1/inbox/conversations" in paths
    assert "/api/v1/comments" in paths
    assert "/api/v1/crm/customers" in paths
    assert "/api/v1/analytics/snapshots" in paths
