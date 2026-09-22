"""Integration fixes from the 2026-09-14 audit that sit between owners."""

from app.desktop.routes import _local_page


def test_notification_open_target_refuses_percent_escaped_traversal():
    assert _local_page("/desktop/applications/7c9cb668")
    assert not _local_page("/desktop/%2e%2e/dashboard/policy"), "the browser normalises %2e%2e to .."
    assert not _local_page("/desktop/%2E%2E/x")
    assert not _local_page("/desktop/../dashboard")
