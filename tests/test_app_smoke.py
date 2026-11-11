from app import app


def test_root_head_route_returns_success():
    route = next(
        route for route in app.routes
        if getattr(route, "path", None) == "/" and "HEAD" in getattr(route, "methods", set())
    )
    response = route.endpoint()
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
