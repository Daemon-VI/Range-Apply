"""High-volume ingestion behaviour: fast path, versions, rejections, isolation,
opportunity invariants, provenance, projection."""

import pytest
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.jobs.database.models import JobRow, JobVersionRow, SourceReferenceRow
from app.jobs.models.enums import JobSourceType
from app.jobs.normalization.normalizer import JobNormalizer, compute_raw_hash, validate_raw_job
from app.jobs.pipeline.discovery_service import JobDiscoveryService
from app.jobs.sources.base import SourceError
from app.pipeline.database.models import CandidateOpportunityRow, OpportunityJobRow, OpportunityRow
from tests.discovery.conftest import fake_source, make_raw


class CountingNormalizer(JobNormalizer):
    def __init__(self):
        super().__init__()
        self.calls = 0

    def normalize(self, raw_job, company_name=None):
        self.calls += 1
        return super().normalize(raw_job, company_name=company_name)


async def _run(db_session, jobs, service=None, identifier="acme", company_name="Acme", **kwargs):
    service = service or JobDiscoveryService()
    service.register_source(JobSourceType.OTHER, fake_source(jobs=jobs))
    run = await service.run_discovery(
        db_session, JobSourceType.OTHER, identifier, company_name=company_name, trigger="test", **kwargs
    )
    return run, service


@pytest.mark.asyncio
async def test_unchanged_postings_skip_normalisation_and_ai(db_session, days_ago):
    jobs = [make_raw(f"j{i}", title=f"Engineer {i}", posted_at=days_ago(1)) for i in range(5)]
    normalizer = CountingNormalizer()
    service = JobDiscoveryService(normalizer=normalizer)
    first, _ = await _run(db_session, jobs, service)
    assert first.jobs_new == 5 and normalizer.calls == 5 and first.ai_calls == 0

    second, _ = await _run(db_session, jobs, service)
    assert second.jobs_unchanged == 5 and second.jobs_duplicate == 5 and second.jobs_new == 0
    assert normalizer.calls == 5, "unchanged raw payloads are not normalised again"
    assert second.jobs_versioned == 0 and db_session.query(JobVersionRow).count() == 0
    assert second.opportunities_new == 0 and second.opportunities_linked == 0
    assert service.llm_fallback.calls == 0
    ref = db_session.query(SourceReferenceRow).filter_by(source_job_id="j0").one()
    assert ref.content_hash == compute_raw_hash(jobs[0]) and ref.fetched_at is not None and ref.parse_status == "NORMALIZED"
    assert all(j.freshness == "FRESH" for j in db_session.query(JobRow).all())


@pytest.mark.asyncio
async def test_changed_content_creates_exactly_one_version(db_session):
    original = make_raw("j1", content="<p>Python role.</p>")
    first, service = await _run(db_session, [original])
    changed = make_raw("j1", content="<p>Python role. Now also Go.</p>")
    second, _ = await _run(db_session, [changed], service)
    assert second.jobs_updated == 1 and second.jobs_versioned == 1 and second.jobs_unchanged == 0
    assert db_session.query(JobVersionRow).count() == 1
    third, _ = await _run(db_session, [changed], service)
    assert third.jobs_unchanged == 1 and db_session.query(JobVersionRow).count() == 1
    assert db_session.query(JobRow).count() == 1 and db_session.query(OpportunityRow).count() == 1


@pytest.mark.asyncio
async def test_malformed_records_are_rejected_and_counted_not_crashed(db_session):
    jobs = [make_raw("ok-1"), make_raw("", title="No id"), make_raw("no-title", title="   "), make_raw("ok-2", title="Data Engineer")]
    run, _ = await _run(db_session, jobs)
    assert run.status == "completed"
    assert run.jobs_new == 2 and run.jobs_rejected == 2 and run.jobs_failed == 0
    assert any("[rejected]" in e and "missing source_job_id" in e for e in run.errors)
    assert validate_raw_job(make_raw("x", title="t" * 600)) == "title too long"


@pytest.mark.asyncio
async def test_title_filter_counts_rather_than_hides(db_session):
    jobs = [make_raw("a", title="Backend Engineer"), make_raw("b", title="Sales Manager"), make_raw("c", title="Data Engineer")]
    run, _ = await _run(db_session, jobs, title_include=[r"engineer"])
    assert run.jobs_new == 2 and run.jobs_filtered == 1


@pytest.mark.asyncio
async def test_every_persisted_job_has_exactly_one_opportunity(db_session):
    jobs = [
        make_raw("a1", title="Backend Engineer", location="Remote"),
        make_raw("a2", title="Backend Engineer", location="Remote - US"),  # different canonical_key, same opportunity
        make_raw("a3", title="Backend Engineer", location="Hyderabad, India", content="<p>On-site in our Hyderabad office. Python.</p>"),  # different bucket
        make_raw("a4", title="Senior Backend Engineer", location="Remote"),  # seniority differs
        make_raw("a5", title="Backend Engineer (Remote)", location="Remote"),  # record-level duplicate of a1
    ]
    run, service = await _run(db_session, jobs)
    # a5 normalises to a1's title/location: the *record-level* deduplicator merges it
    # (exact canonical key), so it is a duplicate, not a new job.
    assert run.jobs_new == 4 and run.jobs_duplicate == 1
    assert run.opportunities_new == 3 and run.opportunities_linked == 1
    # Same title at another company is its own opportunity.
    other, _ = await _run(db_session, [make_raw("b1", company="beta")], service, identifier="beta", company_name="Beta")
    assert other.jobs_new == 1 and other.opportunities_new == 1
    assert db_session.query(JobRow).count() == 5
    assert db_session.query(OpportunityJobRow).count() == 5, "no job without an opportunity link"
    assert db_session.query(OpportunityRow).count() == 4
    remote = db_session.query(OpportunityRow).filter_by(location_bucket="remote", title="Backend Engineer", company="Acme").one()
    assert {link.job.source_job_id for link in remote.jobs} == {"a1", "a2"}
    assert db_session.query(OpportunityRow).filter_by(location_bucket="hyderabad").count() == 1
    assert db_session.query(OpportunityRow).filter_by(title="Senior Backend Engineer").count() == 1


@pytest.mark.asyncio
async def test_repost_after_closure_is_recognised(db_session):
    first, service = await _run(db_session, [make_raw("old", title="Platform Engineer")])
    empty, _ = await _run(db_session, [], service)  # zero results: sweep refuses (cannot tell empty from failure)
    assert db_session.query(JobRow).one().job_status == "ACTIVE"

    # The board drops the posting while another one stays: the scoped sweep closes it
    # and, with every linked job closed, the opportunity too.
    survivor = make_raw("keep", title="QA Engineer")
    await _run(db_session, [make_raw("old", title="Platform Engineer"), survivor], service)
    swept, _ = await _run(db_session, [survivor], service)
    assert swept.jobs_closed == 1
    old_job = db_session.query(JobRow).filter_by(source_job_id="old").one()
    old_opp = db_session.query(OpportunityRow).filter_by(title="Platform Engineer").one()
    assert old_job.job_status == "CLOSED" and old_job.freshness == "STALE" and old_opp.status == "CLOSED"

    # Case 1: the same posting id/key comes back -> the record is reopened, opportunity reposted.
    run, _ = await _run(db_session, [make_raw("new", title="Platform Engineer"), survivor], service)
    assert run.jobs_new == 0 and run.jobs_reposted == 1 and run.opportunities_new == 0
    db_session.refresh(old_opp)
    assert old_opp.repost_count == 1 and old_opp.status == "REPOSTED" and len(old_opp.jobs) == 1
    assert old_job.job_status == "ACTIVE"

    # Case 2: a genuinely distinct source row (different location format) for a closed opening.
    for job in db_session.query(JobRow).filter_by(title="Platform Engineer").all():
        job.job_status = "CLOSED"
    db_session.commit()
    run2, _ = await _run(db_session, [make_raw("new-2", title="Platform Engineer", location="Remote - India"), survivor], service)
    assert run2.jobs_new == 1 and run2.jobs_reposted == 1 and run2.opportunities_linked == 1 and run2.opportunities_new == 0
    db_session.refresh(old_opp)
    assert old_opp.repost_count == 2 and len(old_opp.jobs) == 2
    assert db_session.query(OpportunityRow).count() == 2, "closed opening never became a brand-new opportunity"


@pytest.mark.asyncio
async def test_one_failing_source_does_not_stop_the_others(engine):
    service = JobDiscoveryService()
    service.register_source(JobSourceType.GREENHOUSE, fake_source(JobSourceType.GREENHOUSE, jobs=[make_raw("g1", source=JobSourceType.GREENHOUSE)]))
    service.register_source(JobSourceType.LEVER, fake_source(JobSourceType.LEVER, jobs=[make_raw("l1", source=JobSourceType.LEVER, company="beta")]))
    service.register_source(
        JobSourceType.ASHBY,
        fake_source(JobSourceType.ASHBY, error=SourceError("429 too many", JobSourceType.ASHBY, "gamma", 429, kind="rate_limited")),
    )
    targets = [
        {"source": JobSourceType.GREENHOUSE, "identifier": "acme", "company_name": "Acme"},
        {"source": JobSourceType.LEVER, "identifier": "beta", "company_name": "Beta"},
        {"source": JobSourceType.ASHBY, "identifier": "gamma", "company_name": "Gamma"},
    ]
    runs = await service.run_many(targets, sessionmaker(bind=engine), trigger="test")
    by_source = {r.source: r for r in runs}
    assert by_source["GREENHOUSE"].status == "completed" and by_source["LEVER"].status == "completed"
    failed = by_source["ASHBY"]
    assert failed.status == "failed" and failed.failure_kind == "rate_limited" and failed.rate_limit_hits == 1
    assert failed.jobs_new == 0 and "rate_limited" in failed.errors[0]
    assert by_source["GREENHOUSE"].network_requests == 1


@pytest.mark.asyncio
async def test_errors_are_bounded_per_run(db_session, monkeypatch):
    monkeypatch.setattr(settings, "discovery_max_errors_stored", 3)
    jobs = [make_raw("", title=f"bad {i}") for i in range(6)] + [make_raw("ok")]
    run, _ = await _run(db_session, jobs)
    assert run.jobs_rejected == 6 and run.jobs_new == 1
    assert len(run.errors) == 4 and run.errors[-1].startswith("... 3 more")


@pytest.mark.asyncio
async def test_candidate_projection_is_cheap_and_separate(db_session, monkeypatch):
    monkeypatch.setattr(settings, "discovery_project_tenants", "default,other-tenant")
    run, _ = await _run(db_session, [make_raw("p1"), make_raw("p2", title="Data Engineer")])
    assert run.opportunities_new == 2
    rows = db_session.query(CandidateOpportunityRow).all()
    assert len(rows) == 4
    assert {r.tenant_id for r in rows} == {"default", "other-tenant"}
    assert all(r.state == "DISCOVERED" and r.eligibility_status is None and r.fit_score is None and r.priority_score is None for r in rows)

    monkeypatch.setattr(settings, "discovery_project_tenants", "")
    run2, _ = await _run(db_session, [make_raw("p3", title="QA Engineer")])
    assert db_session.query(CandidateOpportunityRow).count() == 4, "projection off: nothing added"


@pytest.mark.asyncio
async def test_moved_apply_link_on_an_unchanged_posting_is_followed(db_session):
    """Phase 13 (real Lever boards): the posting text is identical but the
    source now reports the real application page. The job must point there,
    without a content version and without re-creating anything."""
    original = make_raw("j1")
    first, service = await _run(db_session, [original])
    moved = make_raw("j1")
    moved.raw_metadata = {**moved.raw_metadata, "apply_url": "https://example.com/other/acme/j1/apply"}
    second, _ = await _run(db_session, [moved], service)
    job = db_session.query(JobRow).one()
    assert job.application_url == "https://example.com/other/acme/j1/apply"
    assert job.normalized_application_url == "https://example.com/other/acme/j1/apply"
    assert second.jobs_new == 0 and second.jobs_versioned == 0 and db_session.query(JobVersionRow).count() == 0
    assert db_session.query(OpportunityRow).count() == 1 and db_session.query(SourceReferenceRow).count() == 1
    third, _ = await _run(db_session, [moved], service)
    assert third.jobs_unchanged == 1, "the new link is now the known payload; the fast path applies again"
