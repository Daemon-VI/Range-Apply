"""Button audit 2026-09-14: an empty number box must never fail a page with 422.

The Signals and Shortlist "Apply" buttons broke on first use (the numeric box
renders blank); Opportunities, Scheduler and Policy broke when a box was cleared.
"""

import pytest
from fastapi.testclient import TestClient

from app.core.params import to_int
from app.main import app
from tests.conftest import AUTH_HEADERS

KEY = AUTH_HEADERS["X-API-Key"]


@pytest.fixture(scope="module")
def client():
    c = TestClient(app, follow_redirects=False)
    c.get("/dashboard/", params={"key": KEY})
    return c


def test_to_int_treats_blank_and_garbage_as_the_default():
    assert to_int("", 5) == 5 and to_int(None, 5) == 5 and to_int("  ", None) is None
    assert to_int("abc", 7) == 7 and to_int("12", 0) == 12 and to_int("12.9", 0) == 12
    assert to_int("999", 25, 1, 200) == 200 and to_int("-3", 0, 0) == 0


@pytest.mark.parametrize("path, params", [
    ("/dashboard/signals", {"status": "", "source": "", "category": "", "outcome": "", "company": "", "days": ""}),
    ("/dashboard/signals", {"days": "abc"}),
    ("/dashboard/shortlist", {"eligibility": "", "priority": "", "min_score": "", "company": "", "limit": "", "offset": ""}),
    ("/dashboard/opportunities", {"state": "", "limit": ""}),
    ("/dashboard/scheduler", {"window": ""}),
])
def test_filter_forms_with_blank_numbers_render(client, path, params):
    response = client.get(path, params=params)
    assert response.status_code == 200, response.text[:300]


def test_scheduler_run_with_a_blank_window_does_not_422(client):
    response = client.post("/dashboard/scheduler/run", data={"window": ""})
    assert response.status_code == 303


def test_policy_save_with_cleared_numbers_keeps_the_saved_values(client):
    page = client.get("/dashboard/policy")
    assert page.status_code == 200
    response = client.post("/dashboard/policy", data={
        "enabled_bands": ["HIGH", "MEDIUM"], "high_threshold": "", "medium_threshold": "", "daily_cap": "", "weekly_cap": "", "cooldown_days": "",
        "minimum_eligibility": "UNCERTAIN", "blocked_companies": "", "lane_high": "REVIEW", "lane_medium": "REVIEW", "lane_low": "REVIEW",
        "tailoring_high": "L2", "tailoring_medium": "L1", "tailoring_low": "L0", "minimum_fit_score": "",
    })
    assert response.status_code == 303 and "policy-saved" in response.headers["location"]
