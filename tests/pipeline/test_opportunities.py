"""Shared opportunity identity + candidate opportunity state."""

from typing import List

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.errors import ConflictError, NotFoundError
from app.jobs.database.models import JobRow
from app.jobs.models.enums import JobSourceType
from app.jobs.models.raw_job import RawJob
from app.jobs.pipeline.discovery_service import JobDiscoveryService
from app.jobs.sources.base import JobSource
from app.pipeline.database.models import CandidateOpportunityRow, OpportunityJobRow, OpportunityRow
from app.pipeline.models import OpportunityState
from app.pipeline.repository import OpportunityRepository


def test_two_sources_same_role_converge_on_one_opportunity(db_session, jobs):
    a = jobs.make(source="GREENHOUSE", location="Remote")
    b = jobs.make(source="LEVER", location="Remote - US", remote_type="UNKNOWN")
    opp_a, created_a, how_a = OpportunityRepository.resolve_opportunity(db_session, a)
    opp_b, created_b, how_b = OpportunityRepository.resolve_opportunity(db_session, b)
    db_session.commit()
    assert created_a and how_a == "new"
    assert not created_b and how_b == "identity_key"
    assert opp_a.id == opp_b.id
    assert opp_a.canonical_job_id == a.id
    links = db_session.query(OpportunityJobRow).filter_by(opportunity_id=opp_a.id).all()
    assert {link.job_id for link in links} == {a.id, b.id}
    # Resolving again is a no-op that just refreshes last_seen.
    again, created_again, how_again = OpportunityRepository.resolve_opportunity(db_session, b)
    assert again.id == opp_a.id and not created_again and how_again == "existing_link"


def test_different_city_or_title_is_a_different_opportunity(db_session, jobs):
    remote = jobs.make(location="Remote")
    hyd = jobs.make(location="Hyderabad, India", remote_type="ON_SITE")
    senior = jobs.make(title="Senior Backend Engineer", location="Remote")
    ids = {OpportunityRepository.resolve_opportunity(db_session, j)[0].id for j in (remote, hyd, senior)}
    db_session.commit()
    assert len(ids) == 3


def test_repost_is_detected_not_duplicated(db_session, jobs):
    first = jobs.make(source_job_id="old-1")
    opp, _, _ = OpportunityRepository.resolve_opportunity(db_session, first)
    first.job_status = "CLOSED"
    db_session.commit()
    assert OpportunityRepository.close_opportunity_if_all_jobs_closed(db_session, opp)
    assert opp.status == "CLOSED"

    repost = jobs.make(source_job_id="new-2")
    same, created, how = OpportunityRepository.resolve_opportunity(db_session, repost)
    db_session.commit()
    assert same.id == opp.id and not created and how == "repost"
    assert same.repost_count == 1 and same.status == "REPOSTED"
    assert same.canonical_job_id == repost.id
    link = db_session.query(OpportunityJobRow).filter_by(job_id=repost.id).one()
    assert link.is_repost


def test_candidate_opportunity_is_unique_per_tenant_and_isolated(db_session, jobs, repo, other_repo):
    job = jobs.make()
    opp, _, _ = OpportunityRepository.resolve_opportunity(db_session, job)
    co, created = repo.ensure_candidate_opportunity(opp, "test")
    co_again, created_again = repo.ensure_candidate_opportunity(opp, "test")
    repo.commit()
    assert created and not created_again and co.id == co_again.id
    assert co.state == OpportunityState.DISCOVERED.value

    # The shared opportunity is visible to both tenants; the candidate state is not.
    assert other_repo.get_opportunity(opp.id) is not None
    assert other_repo.for_opportunity(opp.id) is None
    assert other_repo.get_candidate_opportunity(co.id) is None
    with pytest.raises(NotFoundError):
        other_repo.require_candidate_opportunity(co.id)

    other_co, _ = other_repo.ensure_candidate_opportunity(opp, "test")
    other_repo.commit()
    assert other_co.id != co.id
    assert other_repo.list_candidate_opportunities()[1] == 1
    assert repo.list_candidate_opportunities()[1] == 1

    db_session.add(CandidateOpportunityRow(tenant_id=repo.tenant_id, opportunity_id=opp.id))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_state_transitions_are_guarded_and_audited(db_session, jobs, repo):
    job = jobs.make()
    opp, _, _ = OpportunityRepository.resolve_opportunity(db_session, job)
    co, _ = repo.ensure_candidate_opportunity(opp, "test")
    repo.transition(co, OpportunityState.ELIGIBLE, "sync")
    repo.transition(co, OpportunityState.QUEUED, "api", reason="enqueued")
    with pytest.raises(ConflictError):
        repo.transition(co, OpportunityState.OFFER, "api")
    repo.transition(co, OpportunityState.SKIPPED, "dashboard", reason="not interested")
    repo.commit()
    assert co.skipped_reason == "not interested"
    actions = [e.action for e in repo.list_audit("candidate_opportunity", co.id)]
    assert actions == ["state:SKIPPED", "state:QUEUED", "state:ELIGIBLE", "created"]
    event = repo.list_audit("candidate_opportunity", co.id)[0]
    assert event.before["state"] == "QUEUED" and event.after["state"] == "SKIPPED"
    assert event.actor == "dashboard" and event.tenant_id == repo.tenant_id

    repo.transition(co, OpportunityState.ELIGIBLE, "dashboard")  # un-skip
    assert co.skipped_reason is None


class _FakeSource(JobSource):
    JOBS: List[RawJob] = []

    @property
    def source_type(self) -> JobSourceType:
        return JobSourceType.OTHER

    async def discover(self, identifier: str, **kwargs) -> List[RawJob]:
        return list(self.JOBS)


@pytest.mark.asyncio
async def test_discovery_resolves_opportunities_for_persisted_jobs(db_session, jobs):
    company = jobs.company("Nimbus")
    jobs.companies.add(company)

    def raw(job_id: str, location: str) -> RawJob:
        return RawJob(
            source=JobSourceType.OTHER,
            source_job_id=job_id,
            source_url=f"https://example.com/{job_id}",
            discovered_url=f"https://example.com/{job_id}",
            raw_title="Platform Engineer",
            raw_content="Python and Go platform role.",
            content_type="markdown",
            raw_location=location,
            raw_metadata={"company_slug": company},
        )

    _FakeSource.JOBS = [raw("disc-1", "Remote"), raw("disc-2", "Remote (India)")]
    service = JobDiscoveryService()
    service.register_source(JobSourceType.OTHER, _FakeSource)
    run = await service.run_discovery(db_session, JobSourceType.OTHER, "fake-board", trigger="test")
    assert run.status == "completed"

    rows = db_session.query(JobRow).filter(JobRow.source_job_id.in_(["disc-1", "disc-2"])).all()
    jobs.job_ids.extend(r.id for r in rows)
    assert len(rows) == 2
    opportunity_ids = {
        db_session.query(OpportunityJobRow).filter_by(job_id=r.id).one().opportunity_id for r in rows
    }
    assert len(opportunity_ids) == 1
    opp = db_session.get(OpportunityRow, opportunity_ids.pop())
    assert opp.location_bucket == "remote"
