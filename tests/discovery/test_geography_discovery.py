"""Discovery honours the candidate's job-market target (geographic targeting, 2026-09-14)."""

from app.career.repository import EvidenceRepository
from app.config import settings
from app.jobs.database.models import JobRow
from app.jobs.geography import DiscoveryGeography, policy_from_preferences
from app.jobs.models.enums import JobSourceType
from app.jobs.pipeline.discovery_service import JobDiscoveryService, discovery_geography
from app.models.preference import Preference
from tests.discovery.conftest import fake_source, make_raw

HYDERABAD = "Hyderabad, Telangana, India"


def board():
    return [
        make_raw("h1", title="Software Engineer", location=HYDERABAD),
        make_raw("b1", title="Backend Engineer", location="Bengaluru"),
        make_raw("r1", title="Data Engineer", location="Remote - India"),
        make_raw("u1", title="Platform Engineer", location="San Francisco, CA"),
        make_raw("u2", title="ML Engineer", location="US Remote"),
        make_raw("x1", title="SRE", location="Remote"),
    ]


def hyderabad_only() -> DiscoveryGeography:
    return DiscoveryGeography((policy_from_preferences(Preference(), HYDERABAD),))


async def _discover(db, geography, jobs=None, source_cls=None):
    service = JobDiscoveryService()
    service.register_source(JobSourceType.OTHER, source_cls or fake_source(jobs=jobs))
    return await service.run_discovery(db, JobSourceType.OTHER, "acme", geography=geography)


async def test_geography_is_passed_into_sources_that_support_it(db_session):
    seen: dict = {}

    class GeographicSource(fake_source()):
        supports_geography = True

        async def discover(self, identifier, **kwargs):
            seen.update(kwargs)
            return board()[:1]

    plain: dict = {}

    class PlainSource(fake_source()):
        async def discover(self, identifier, **kwargs):
            plain.update(kwargs)
            return []

    geography = hyderabad_only()
    await _discover(db_session, geography, source_cls=GeographicSource)
    assert seen["geography"] is geography
    await _discover(db_session, geography, source_cls=PlainSource)
    assert "geography" not in plain


async def test_source_without_geographic_filtering_is_normalised_then_filtered(db_session):
    raw = board()
    raw[1].raw_metadata["secondaryLocations"] = [{"location": "Hyderabad, India"}]
    run = await _discover(db_session, hyderabad_only(), jobs=raw)

    assert run.jobs_filtered == 2 and run.checkpoint["filtered_geography"] == 2
    stored = {job.location: job for job in db_session.query(JobRow).all()}
    assert set(stored) == {HYDERABAD, "Bengaluru", "Remote - India", "Remote"}, "US-only postings are not ingested; unconfirmed ones are"
    assert stored[HYDERABAD].extraction_metadata["geography"] == {"countries": ["India"], "metros": ["Hyderabad"], "remote": False}
    assert stored["Remote - India"].extraction_metadata["geography"]["remote"] is True
    assert stored["Bengaluru"].locations == ["Bengaluru", "Hyderabad, India"], "secondary locations are part of the normalised posting"


async def test_changing_the_candidate_target_changes_discovery_without_code_changes(db_session, monkeypatch):
    monkeypatch.setattr(settings, "discovery_project_tenants", "geo-tenant")
    repo = EvidenceRepository(db_session, "geo-tenant")
    repo.upsert_profile({"name": "Geo Candidate", "location": HYDERABAD}, {}, actor="test")
    repo.commit()

    hyderabad = discovery_geography(db_session)
    assert hyderabad.excluded("San Francisco, CA") is not None and hyderabad.excluded("Hyderabad") is None

    repo.upsert_profile({}, Preference(location_primary="Berlin, Germany").model_dump(), actor="test")
    repo.commit()
    berlin = discovery_geography(db_session)
    assert berlin.excluded("Berlin") is None and berlin.excluded("Hyderabad") is not None

    repo.upsert_profile({}, Preference(location_allow_international=True).model_dump(), actor="test")
    repo.commit()
    run = await _discover(db_session, discovery_geography(db_session), jobs=board())
    assert run.jobs_filtered == 0 and db_session.query(JobRow).count() == 6


async def test_known_posting_filtered_later_is_not_closed_by_the_sweep(db_session):
    await _discover(db_session, None, jobs=board())
    assert db_session.query(JobRow).count() == 6
    await _discover(db_session, hyderabad_only(), jobs=board())
    db_session.expire_all()
    statuses = {job.location: job.job_status for job in db_session.query(JobRow).all()}
    assert statuses["San Francisco, CA"] != "CLOSED" and statuses["US Remote"] != "CLOSED"
