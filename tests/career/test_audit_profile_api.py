"""Backend audit (2026-09-14): Career Brain API writes.

* ``PUT /preferences`` replaced every preference with the model defaults for
  fields the body did not carry: target roles were wiped and a switched-off
  "include unconfirmed locations" switched back on.
* ``POST /import`` with a missing or non-JSON file was an unhandled 500.
* Impossible profile values (graduation year 3027, CGPA 42, a ``javascript:``
  LinkedIn link) were stored from both the API and the dashboard.
"""

import uuid

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_tenant_id
from app.career.database.models import TenantRow
from app.database import get_session_factory
from app.main import app
from tests.conftest import AUTH_HEADERS

TENANT = f"audit-{uuid.uuid4().hex[:10]}"


@pytest.fixture(scope="module", autouse=True)
def _tenant_override():
    previous = app.dependency_overrides.get(get_tenant_id)
    app.dependency_overrides[get_tenant_id] = lambda: TENANT
    yield
    if previous is not None:
        app.dependency_overrides[get_tenant_id] = previous
    else:
        app.dependency_overrides.pop(get_tenant_id, None)
    session = get_session_factory()()
    try:
        row = session.get(TenantRow, TENANT)
        if row is not None:
            session.delete(row)
            session.commit()
    finally:
        session.close()


@pytest.fixture(scope="module")
def client():
    c = TestClient(app, raise_server_exceptions=False)
    assert c.get("/profile").status_code == 200  # bootstraps the tenant from the seed
    return c


def test_partial_preferences_update_changes_only_the_fields_sent(client):
    full = client.get("/api/v1/career/preferences").json()
    full.update(target_roles_tier2=["Data Scientist"], location_include_unconfirmed=False, role_include_unrelated=False)
    assert client.put("/api/v1/career/preferences", json=full, headers=AUTH_HEADERS).status_code == 200

    resp = client.put("/api/v1/career/preferences", json={"target_roles_tier1": ["Backend Engineer"]}, headers=AUTH_HEADERS)
    assert resp.status_code == 200, resp.text
    after = client.get("/api/v1/career/preferences").json()
    assert after["target_roles_tier1"] == ["Backend Engineer"]
    assert after["target_roles_tier2"] == ["Data Scientist"], "unsent fields keep their values"
    assert after["location_include_unconfirmed"] is False, "a switched-off location toggle is not switched back on"
    assert {k: v for k, v in after.items() if k != "target_roles_tier1"} == {k: v for k, v in full.items() if k != "target_roles_tier1"}


def test_import_of_a_missing_or_invalid_file_is_a_structured_error(client, tmp_path):
    missing = client.post("/api/v1/career/import", json={"file": str(tmp_path / "missing.json")}, headers=AUTH_HEADERS)
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "not_found"
    broken = tmp_path / "broken.json"
    broken.write_text("this is not json", encoding="utf-8")
    invalid = client.post("/api/v1/career/import", json={"file": str(broken)}, headers=AUTH_HEADERS)
    assert invalid.status_code == 422 and invalid.json()["error"]["code"] == "validation_failed"


@pytest.mark.parametrize(
    "fields",
    [
        {"graduation_year": 3027},
        {"graduation_year": 202},
        {"cgpa": 142},
        {"cgpa": -1},
        {"linkedin": "javascript:alert(1)"},
        {"github": "data:text/html,hi"},
        {"portfolio": "file:///C:/Windows/win.ini"},
    ],
)
def test_impossible_profile_values_are_refused(client, fields):
    before = client.get("/api/v1/career/profile").json()
    resp = client.put("/api/v1/career/profile", json=fields, headers=AUTH_HEADERS)
    assert resp.status_code == 422, resp.text
    assert client.get("/api/v1/career/profile").json() == before


def test_ordinary_profile_values_are_accepted(client):
    for fields in ({"graduation_year": 2027, "cgpa": 8.4}, {"linkedin": "https://www.linkedin.com/in/example"}, {"github": "github.com/example"}, {"linkedin": None}):
        resp = client.put("/api/v1/career/profile", json=fields, headers=AUTH_HEADERS)
        assert resp.status_code == 200, (fields, resp.text)
