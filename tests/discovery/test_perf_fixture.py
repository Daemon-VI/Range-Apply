"""Synthetic-volume fixture: thousands of jobs, mostly deterministic, zero AI.

``test_volume_500`` always runs. ``test_volume_5000`` is the blueprint's
5,000-record fixture and runs only with ``CAREEROS_PERF=1`` so CI stays
fast; it is repeatable (seeded) rather than a benchmark.

The generator mixes, deterministically: many companies and titles, several
locations, cross-source duplicates that should converge on one opportunity,
exact source duplicates, malformed records, and — on the second pass — a
slice of changed descriptions plus reposts of closed postings.
"""

import os
import random
import time
import tracemalloc
from dataclasses import dataclass
from typing import List

import pytest

from app.jobs.database.models import JobRow, JobVersionRow, SourceReferenceRow
from app.jobs.models.enums import JobSourceType
from app.jobs.models.raw_job import RawJob
from app.jobs.pipeline.discovery_service import JobDiscoveryService
from app.pipeline.database.models import OpportunityJobRow, OpportunityRow
from tests.discovery.conftest import fake_source

TITLES = [
    "Backend Engineer", "Senior Backend Engineer", "Data Engineer", "ML Engineer",
    "Platform Engineer", "Frontend Engineer", "Software Engineer", "SDE I", "QA Engineer",
    "DevOps Engineer", "Product Engineer", "Site Reliability Engineer",
]
TEAMS = [
    "Payments", "Risk", "Search", "Growth", "Infra", "Identity", "Ledger", "Messaging", "Maps",
    "Ads", "Billing", "Trust", "Data Platform", "Mobile", "Checkout", "Storage", "Observability",
    "Marketplace", "Logistics", "Fraud", "Recommendations", "Core", "Developer Tools", "Security",
    "Analytics", "Onboarding", "Support Tools", "Notifications", "Pricing", "Catalog", "Lending",
    "Insurance", "Wallet", "Health", "Education", "Media", "Streaming", "Gaming", "Travel", "Fleet",
]
LOCATIONS = ["Remote", "Remote - India", "Hyderabad, India", "Bangalore, India", "Pune, India", "Remote (US)"]
SOURCES = [JobSourceType.GREENHOUSE, JobSourceType.LEVER, JobSourceType.ASHBY]
CONTENT = (
    "<h2>About the role</h2><p>We are hiring a {title} to build {domain} systems in Python, Go and PostgreSQL. "
    "Requirements: 2+ years experience, strong SQL, Docker. {extra}</p><ul><li>Design services</li>"
    "<li>Own reliability</li></ul>"
)


@dataclass
class Generated:
    jobs: List[RawJob]
    malformed: int
    exact_duplicates: int


def generate(n: int, seed: int = 7, changed_ratio: float = 0.0, repost_ids: set | None = None) -> Generated:
    # Two generators: the structure of record ``base`` must be identical on
    # every pass, so the "did this one change?" draw uses its own stream.
    structure = random.Random(seed)
    changes = random.Random(seed + 1)
    jobs: List[RawJob] = []
    malformed = 0
    exact_dups = 0
    companies = [f"company{i:03d}" for i in range(max(50, n // 10))]
    base = 0
    while len(jobs) < n:
        company = structure.choice(companies)
        # Title + team keeps postings distinct at record level (as real boards
        # are), while the twin below still converges at opportunity level.
        title = f"{structure.choice(TITLES)}, {structure.choice(TEAMS)}"
        location = structure.choice(LOCATIONS)
        source = SOURCES[base % len(SOURCES)]
        job_id = f"{company}-{base}"
        extra = "Changed responsibilities this week." if changed_ratio and changes.random() < changed_ratio else ""
        content = CONTENT.format(title=title, domain=company, extra=extra)
        if repost_ids and job_id in repost_ids:
            job_id = f"{job_id}-repost"
        job = RawJob(
            source=source,
            source_job_id=job_id,
            source_url=f"https://{source.value.lower()}.example/{company}/{job_id}",
            discovered_url=f"https://{source.value.lower()}.example/{company}",
            raw_title=title,
            raw_content=content,
            content_type="html",
            raw_location=location,
            raw_metadata={"board_token": company},
        )
        jobs.append(job)
        # Every 10th job: a cross-source duplicate of the same opening with a
        # differently formatted location (different canonical key, same opportunity).
        if base % 10 == 0 and len(jobs) < n:
            twin_source = SOURCES[(base + 1) % len(SOURCES)]
            jobs.append(
                RawJob(
                    source=twin_source,
                    source_job_id=f"{job_id}-twin",
                    source_url=f"https://{twin_source.value.lower()}.example/{company}/{job_id}-twin",
                    discovered_url=f"https://{twin_source.value.lower()}.example/{company}",
                    raw_title=f"{title} (Remote)" if location.startswith("Remote") else title,
                    raw_content=content,
                    content_type="html",
                    raw_location="Remote - Worldwide" if location.startswith("Remote") else location,
                    raw_metadata={"board_token": company},
                )
            )
        # Every 25th job: the exact same source record again (source duplicate).
        if base % 25 == 0 and len(jobs) < n:
            jobs.append(job)
            exact_dups += 1
        # Every 50th job: a malformed record.
        if base % 50 == 0 and len(jobs) < n:
            jobs.append(job.model_copy(update={"source_job_id": "", "raw_title": "broken"}))
            malformed += 1
        base += 1
    return Generated(jobs[:n], malformed, exact_dups)


async def _ingest(db_session, generated: Generated, service: JobDiscoveryService):
    # One fake adapter per source type so per-source runs stay isolated.
    runs = []
    for source in SOURCES:
        subset = [j for j in generated.jobs if j.source == source]
        service.register_source(source, fake_source(source, jobs=subset))
        run = await service.run_discovery(db_session, source, f"bulk-{source.value.lower()}", trigger="perf", close_missing=False)
        runs.append(run)
    return runs


async def _volume(db_session, write_counter, n: int):
    service = JobDiscoveryService()
    generated = generate(n)
    # First pass timed without tracemalloc (it roughly doubles Python time);
    # peak memory is sampled on the second pass below.
    started = time.perf_counter()
    runs = await _ingest(db_session, generated, service)
    first_seconds = time.perf_counter() - started
    first_writes = write_counter.writes

    totals = {k: sum(getattr(r, k) or 0 for r in runs) for k in ("jobs_new", "jobs_duplicate", "jobs_rejected", "jobs_failed", "opportunities_new", "opportunities_linked", "ai_calls")}
    jobs_in_db = db_session.query(JobRow).count()
    opportunities = db_session.query(OpportunityRow).count()
    assert db_session.query(OpportunityJobRow).count() == jobs_in_db, "every job has exactly one opportunity link"
    assert totals["ai_calls"] == 0 and service.llm_fallback.calls == 0
    assert totals["jobs_failed"] == 0
    assert totals["jobs_rejected"] == generated.malformed
    # Exact source duplicates are always duplicates; a few generated triples
    # may additionally collide on the record-level canonical key.
    assert totals["jobs_duplicate"] >= generated.exact_duplicates
    assert totals["jobs_new"] + totals["jobs_duplicate"] + totals["jobs_rejected"] == n
    assert totals["jobs_new"] == jobs_in_db
    assert opportunities < jobs_in_db, "cross-source twins converged"
    assert totals["opportunities_new"] == opportunities

    # Second pass: 10% changed content, a few reposts of closed postings, rest unchanged.
    closed = db_session.query(JobRow).filter(JobRow.source_job_id.like("%-0")).limit(5).all()
    repost_ids = {j.source_job_id for j in closed}
    for job in closed:
        job.job_status = "CLOSED"
    db_session.commit()
    second_gen = generate(n, changed_ratio=0.10, repost_ids=repost_ids)
    write_counter_before = write_counter.writes
    tracemalloc.start()
    started = time.perf_counter()
    second_runs = await _ingest(db_session, second_gen, service)
    second_seconds = time.perf_counter() - started
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    second = {k: sum(getattr(r, k) or 0 for r in second_runs) for k in ("jobs_new", "jobs_unchanged", "jobs_versioned", "jobs_reposted", "ai_calls")}
    assert second["ai_calls"] == 0
    # A repost with the same company/title/location reopens the stored row
    # (record-level identity) rather than creating a new job.
    assert second["jobs_reposted"] == len(repost_ids)
    assert second["jobs_new"] == 0
    assert 0 < second["jobs_versioned"] <= int(n * 0.15)
    assert second["jobs_unchanged"] > n * 0.7
    assert db_session.query(JobVersionRow).count() == second["jobs_versioned"]
    jobs_after = db_session.query(JobRow).count()
    refs_after = db_session.query(SourceReferenceRow).count()
    # One reference per distinct source posting; reposts and rare record-level
    # key collisions add references to existing rows, never extra rows.
    assert jobs_after == jobs_in_db
    assert jobs_after <= refs_after <= jobs_after + totals["jobs_duplicate"] + len(repost_ids)

    summary = {
        "records": n,
        "jobs_persisted": jobs_in_db,
        "opportunities": opportunities,
        "source_duplicates": totals["jobs_duplicate"],
        "malformed_rejected": totals["jobs_rejected"],
        "first_pass_seconds": round(first_seconds, 2),
        "first_pass_jobs_per_minute": round(jobs_in_db / max(first_seconds, 1e-6) * 60),
        "first_pass_db_writes": first_writes,
        "second_pass_seconds": round(second_seconds, 2),
        "second_pass_db_writes": write_counter.writes - write_counter_before,
        "second_pass_unchanged": second["jobs_unchanged"],
        "second_pass_versioned": second["jobs_versioned"],
        "second_pass_reposts": second["jobs_reposted"],
        "ai_calls": 0,
        "second_pass_peak_memory_mb": round(peak / 1024 / 1024, 1),
    }
    print("\nVOLUME SUMMARY:", summary)
    return summary


@pytest.mark.perf
@pytest.mark.asyncio
async def test_volume_500(db_session, write_counter):
    summary = await _volume(db_session, write_counter, 500)
    assert summary["first_pass_seconds"] < 60


@pytest.mark.perf
@pytest.mark.asyncio
@pytest.mark.skipif(os.environ.get("CAREEROS_PERF") != "1", reason="set CAREEROS_PERF=1 to run the 10,000-job fixture (Phase 12)")
async def test_volume_10000(db_session, write_counter):
    summary = await _volume(db_session, write_counter, 10000)
    expected = 10000 - summary["source_duplicates"] - summary["malformed_rejected"]
    assert summary["jobs_persisted"] >= expected - 50 and summary["ai_calls"] == 0


@pytest.mark.perf
@pytest.mark.asyncio
@pytest.mark.skipif(os.environ.get("CAREEROS_PERF") != "1", reason="set CAREEROS_PERF=1 to run the 5,000-job fixture")
async def test_volume_5000(db_session, write_counter):
    summary = await _volume(db_session, write_counter, 5000)
    # Everything that is neither an exact source duplicate nor malformed is
    # persisted, minus a handful of record-level key collisions.
    expected = 5000 - summary["source_duplicates"] - summary["malformed_rejected"]
    assert summary["jobs_persisted"] >= expected - 25
    assert summary["first_pass_seconds"] < 600
