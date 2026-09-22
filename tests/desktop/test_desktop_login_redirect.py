"""``/desktop/login?next=`` only ever lands on a page of this local server.

Audit 2026-09-14: ``next`` was accepted when it began with one slash, but a
browser reads ``/\\host`` as ``//host``; the redirect after the single-use
sign-in could leave the loopback origin.
"""

from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.desktop.launcher import LoginTokens
from app.main import app


def test_login_next_never_leaves_the_local_origin():
    tokens = LoginTokens()
    previous = getattr(app.state, "desktop", None)
    app.state.desktop = SimpleNamespace(tokens=tokens)
    try:
        client = TestClient(app, follow_redirects=False)
        for hostile in ("/\\evil.example", "/\\/evil.example", "//evil.example", "https://evil.example", "\\\\evil.example"):
            response = client.get("/desktop/login", params={"token": tokens.mint(), "next": hostile})
            assert response.status_code == 303, (hostile, response.status_code)
            assert response.headers["location"] == "/dashboard/", (hostile, response.headers["location"])
        response = client.get("/desktop/login", params={"token": tokens.mint(), "next": "/desktop/notifications"})
        assert response.status_code == 303 and response.headers["location"] == "/desktop/notifications"
    finally:
        app.state.desktop = previous
