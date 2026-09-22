"""/dashboard/profile: view evidence with provenance, and persist edits."""

import uuid

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_tenant_id
from app.career.database.models import TenantRow
from app.career.repository import EvidenceRepository
from app.database import get_session_factory
from app.main import app
from tests.conftest import AUTH_HEADERS

TENANT = f"dash-{uuid.uuid4().hex[:10]}"
KEY = AUTH_HEADERS["X-API-Key"]


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
    # follow_redirects=False so the POST -> 303 -> GET contract is asserted explicitly.
    c = TestClient(app, follow_redirects=False)
    c.get("/dashboard/profile/", params={"key": KEY})  # sets the auth cookie
    return c


def _repo():
    return EvidenceRepository(get_session_factory()(), TENANT)


def test_page_renders_evidence_with_grades_and_provenance(client):
    resp = client.get("/dashboard/profile/")
    assert resp.status_code == 200
    html = resp.text
    assert "Career Brain" in html
    assert "skill-python" in html
    assert "CONFIRMED" in html and "SEED_FILE" in html
    assert "Positioning variants" in html and "Answer bank" in html


def test_quick_add_skill_persists_as_unverified(client):
    resp = client.post("/dashboard/profile/skill", data={"skill_name": "Zzz Dashboard Skill"})
    assert resp.status_code == 303
    assert "notice=skill-added" in resp.headers["location"]
    node = _repo().require_node("skill:zzz-dashboard-skill")
    assert node.verification_status == "UNVERIFIED"
    assert node.source_type == "CANDIDATE_ENTERED"
    assert "skill:zzz-dashboard-skill" in client.get("/dashboard/profile/").text


def test_status_change_edit_remove_restore_are_audited(client):
    resp = client.post(
        "/dashboard/profile/evidence/skill:zzz-dashboard-skill/status",
        data={"verification_status": "VERIFIED"},
    )
    assert resp.status_code == 303
    repo = _repo()
    node = repo.require_node("skill:zzz-dashboard-skill")
    assert node.verification_status == "VERIFIED" and node.verified_by == "dashboard"

    client.post(
        "/dashboard/profile/evidence/skill:zzz-dashboard-skill/edit",
        data={"label": "Zzz Dashboard Skill", "claim": "Edited on the dashboard"},
    )
    client.post("/dashboard/profile/evidence/skill:zzz-dashboard-skill/remove", data={"reason": "typo"})
    repo = _repo()
    assert repo.require_node("skill:zzz-dashboard-skill").status == "REMOVED"
    assert "Show removed evidence" in client.get("/dashboard/profile/").text
    assert "skill:zzz-dashboard-skill" in client.get("/dashboard/profile/", params={"show_removed": "true"}).text

    client.post("/dashboard/profile/evidence/skill:zzz-dashboard-skill/restore")
    repo = _repo()
    assert repo.require_node("skill:zzz-dashboard-skill").status == "ACTIVE"
    actions = [e.action for e in repo.list_audit("evidence_node", "skill:zzz-dashboard-skill")]
    assert actions == ["restored", "removed", "updated", "updated", "created"]


def test_refused_write_shows_error_notice_not_500(client):
    resp = client.post(
        "/dashboard/profile/evidence",
        data={
            "kind": "SKILL",
            "label": "Zzz Extracted",
            "claim": "x",
            "verification_status": "VERIFIED",
            "source_type": "LLM_EXTRACTED",
        },
    )
    assert resp.status_code == 303
    assert "error:validation_failed" in resp.headers["location"]
    assert _repo().get_node("skill:zzz-extracted") is None
    page = client.get(resp.headers["location"])
    assert "Not applied" in page.text


def test_profile_details_positioning_and_answers_persist(client):
    client.post("/dashboard/profile/details", data={"email": "dash@example.com", "location": "Hyderabad, India"})
    assert _repo().get_profile_row().email == "dash@example.com"

    resp = client.post(
        "/dashboard/profile/positioning",
        data={"role_family": "Backend Engineer", "headline": "h", "summary": "s", "evidence_keys": "ticket-engine, skill-go"},
    )
    assert "notice=variant-added" in resp.headers["location"]
    variants = _repo().list_variants()
    assert len(variants) == 1 and [e.node.key for e in variants[0].evidence] == ["ticket-engine", "skill-go"]

    resp = client.post(
        "/dashboard/profile/answers",
        data={"category": "relocation", "question": "Willing to relocate?", "answer": "Yes.", "evidence_keys": ""},
    )
    assert "notice=answer-added" in resp.headers["location"]
    entry = _repo().list_answers()[0]
    assert entry.status == "DRAFT"
    client.post(f"/dashboard/profile/answers/{entry.id}/approve")
    assert _repo().find_answer("willing to relocate").answer == "Yes."
    page = client.get("/dashboard/profile/").text
    assert "Willing to relocate?" in page and "APPROVED" in page


def test_location_preference_is_saved_and_shown(client):
    repo = _repo()
    if repo.get_profile_row() is None:
        repo.upsert_profile({"name": "Dash Candidate"}, {}, actor="test")
    repo.upsert_profile({"location": "Hyderabad, Telangana, India"}, None, actor="test")
    repo.commit()

    resp = client.post(
        "/dashboard/profile/location-preference",
        data={"location_primary": "", "also_consider": "Bengaluru, Pune", "include_country_remote": "on"},
    )
    assert resp.status_code == 303 and "location-preference-saved" in resp.headers["location"]
    preferences = _repo().get_profile_row().preferences
    assert preferences["location_primary"] is None and preferences["preferred_locations"] == ["Bengaluru", "Pune"]
    assert preferences["location_include_country_remote"] is True
    assert preferences["location_include_other_cities"] is False and preferences["location_allow_international"] is False

    page = client.get("/dashboard/profile/").text
    assert "Job location preference" in page and "Hyderabad, Telangana, India" in page and "India Remote" in page


def test_location_preference_splits_lines_and_flags_an_unrecognised_city(client):
    repo = _repo()
    if repo.get_profile_row() is None:
        repo.upsert_profile({"name": "Dash Candidate"}, {}, actor="test")
        repo.commit()
    resp = client.post("/dashboard/profile/location-preference", data={"location_primary": "Hydrabadd", "also_consider": "Pune\nRemote; Bengaluru", "include_country_remote": "on"})
    assert resp.status_code == 303 and "city-not-recognised" in resp.headers["location"]
    assert _repo().get_profile_row().preferences["preferred_locations"] == ["Pune", "Remote", "Bengaluru"]
    ok = client.post("/dashboard/profile/location-preference", data={"location_primary": "", "also_consider": "", "include_country_remote": "on"})
    assert "city-not-recognised" not in ok.headers["location"]


def test_target_roles_are_edited_in_the_existing_preference_fields(client):
    repo = _repo()
    if repo.get_profile_row() is None:
        repo.upsert_profile({"name": "Dash Candidate"}, {}, actor="test")
        repo.commit()
    before = dict(_repo().get_profile_row().preferences or {})
    resp = client.post(
        "/dashboard/profile/target-roles",
        data={"tier1": "Backend Engineer, Software Engineer\nbackend engineer", "tier2": "Platform Engineer; SRE", "lower_priority": "", "include_unrelated": "on"},
    )
    assert resp.status_code == 303 and "target-roles-saved" in resp.headers["location"]
    preferences = _repo().get_profile_row().preferences
    assert preferences["target_roles_tier1"] == ["Backend Engineer", "Software Engineer"]
    assert preferences["target_roles_tier2"] == ["Platform Engineer", "SRE"]
    assert preferences["target_roles_lower_priority"] == [] and preferences["role_include_unrelated"] is True
    # Nothing else in the preferences moved (location choices survive a role edit).
    for key in ("location_primary", "preferred_locations", "location_include_country_remote"):
        if key in before:
            assert preferences[key] == before[key], key
    page = client.get("/dashboard/profile/").text
    assert 'action="/dashboard/profile/target-roles"' in page and 'value="Backend Engineer, Software Engineer"' in page
    assert "until matching is re-run" in page

    client.post("/dashboard/profile/target-roles", data={"tier1": "Backend Engineer", "tier2": "", "lower_priority": ""})
    preferences = _repo().get_profile_row().preferences
    assert preferences["target_roles_tier1"] == ["Backend Engineer"] and preferences["role_include_unrelated"] is False


def test_target_roles_without_a_profile_is_a_notice_not_a_500():
    other = TestClient(app, follow_redirects=False)
    other.get("/dashboard/profile/", params={"key": KEY})
    previous = app.dependency_overrides.get(get_tenant_id)
    app.dependency_overrides[get_tenant_id] = lambda: f"noprofile-{uuid.uuid4().hex[:8]}"
    try:
        resp = other.post("/dashboard/profile/target-roles", data={"tier1": "Backend Engineer"})
    finally:
        app.dependency_overrides[get_tenant_id] = previous
    assert resp.status_code == 303 and "error:" in resp.headers["location"]
