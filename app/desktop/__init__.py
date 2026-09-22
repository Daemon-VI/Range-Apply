"""CareerOS desktop control center — the thin local shell (Increment 1).

    python -m app.desktop

One process: the existing FastAPI application served by uvicorn on
``127.0.0.1`` (free port), a pywebview / WebView2 window pointed at the
existing dashboards, an optional dry-run Playwright worker supervised in its
own thread, and a clean shutdown in reverse order. No business logic lives
here; every screen and action goes through the existing routes and services.
"""
