"""Regression tests for job identity resolution in :mod:`JobDeduplicator`.

The deduplicator used to resolve URL identity with ``LIKE '<url>%'``, so a
posting at ``.../jobs/123`` was treated as a match for one at
``.../jobs/1234`` - the shorter URL's row silently absorbed the longer one's
title and description. Identity is now decided by exact equality on
:func:`normalize_url`'s output, stored in indexed columns. These tests use a
real (in-memory) database so the full identity hierarchy - including the
unique constraints that would themselves catch a regression - is exercised.
"""

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.jobs.database.models import Base, JobRow, JobVersionRow, SourceReferenceRow
from app.jobs.deduplication.deduplicator import JobDeduplicator
from app.jobs.models.enums import JobSourceType
from app.jobs.models.job import NormalizedJob


@pytest.fixture
def db_session():
    """Fresh in-memory SQLite database per test."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()


def make_job(
    *,
    source_job_id,
    canonical_key,
    title,
    source_url,
    source=JobSourceType.GREENHOUSE,
    company="Acme Corp",
    application_url=None,
    content_hash=None,
    original_title=None,
):
    """Build a minimal but valid :class:`NormalizedJob` for identity tests."""
    return NormalizedJob(
        canonical_key=canonical_key,
        source=source,
        source_job_id=source_job_id,
        company=company,
        title=title,
        original_title=original_title or title,
        source_url=source_url,
        application_url=application_url if application_url is not None else source_url,
        content_hash=content_hash or uuid.uuid4().hex,
    )


# --- THE REGRESSION -----------------------------------------------------
# A prefix-LIKE match would treat ".../jobs/123" as a match for
# ".../jobs/1234" (or vice versa), collapsing two distinct postings into one
# row and overwriting whichever was inserted first.


def test_prefix_collision_regression_longer_id_first(db_session):
    dedup = JobDeduplicator()
    job_1234 = make_job(
        source_job_id="1234",
        canonical_key="acme-data-scientist-1234",
        title="Data Scientist",
        source_url="https://boards.greenhouse.io/acme/jobs/1234",
    )
    job_123 = make_job(
        source_job_id="123",
        canonical_key="acme-backend-engineer-123",
        title="Backend Engineer",
        source_url="https://boards.greenhouse.io/acme/jobs/123",
    )

    result_1234 = dedup.process(db_session, job_1234)
    result_123 = dedup.process(db_session, job_123)

    assert result_1234.status == "NEW"
    assert result_123.status == "NEW"
    assert result_1234.job_row.id != result_123.job_row.id

    rows = db_session.query(JobRow).all()
    assert len(rows) == 2
    by_title = {row.title for row in rows}
    assert by_title == {"Data Scientist", "Backend Engineer"}


def test_prefix_collision_regression_shorter_id_first(db_session):
    # Same as above but reversed insertion order - a prefix match is
    # direction-sensitive in subtle ways, so both orders must be locked down.
    dedup = JobDeduplicator()
    job_123 = make_job(
        source_job_id="123",
        canonical_key="acme-backend-engineer-123b",
        title="Backend Engineer",
        source_url="https://boards.greenhouse.io/acme/jobs/123",
    )
    job_1234 = make_job(
        source_job_id="1234",
        canonical_key="acme-data-scientist-1234b",
        title="Data Scientist",
        source_url="https://boards.greenhouse.io/acme/jobs/1234",
    )

    result_123 = dedup.process(db_session, job_123)
    result_1234 = dedup.process(db_session, job_1234)

    assert result_123.status == "NEW"
    assert result_1234.status == "NEW"
    assert result_123.job_row.id != result_1234.job_row.id

    rows = db_session.query(JobRow).all()
    assert len(rows) == 2
    by_title = {row.title for row in rows}
    assert by_title == {"Data Scientist", "Backend Engineer"}


def test_prefix_collision_regression_realistic_greenhouse_ids(db_session):
    # Realistic Greenhouse numeric-id lengths: 4012345 is a strict string
    # prefix of 40123456.
    dedup = JobDeduplicator()
    job_short = make_job(
        source_job_id="4012345",
        canonical_key="acme-recruiter-4012345",
        title="Technical Recruiter",
        source_url="https://boards.greenhouse.io/acme/jobs/4012345",
    )
    job_long = make_job(
        source_job_id="40123456",
        canonical_key="acme-sales-eng-40123456",
        title="Sales Engineer",
        source_url="https://boards.greenhouse.io/acme/jobs/40123456",
    )

    dedup.process(db_session, job_short)
    dedup.process(db_session, job_long)

    rows = db_session.query(JobRow).all()
    assert len(rows) == 2
    by_title = {row.title for row in rows}
    assert by_title == {"Technical Recruiter", "Sales Engineer"}

    # And the reverse order.
    engine2 = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine2)
    session2 = sessionmaker(bind=engine2)()
    try:
        dedup.process(session2, job_long)
        dedup.process(session2, job_short)
        rows2 = session2.query(JobRow).all()
        assert len(rows2) == 2
        assert {row.title for row in rows2} == {"Technical Recruiter", "Sales Engineer"}
    finally:
        session2.close()


# --- Genuine duplicates still collapse to one row -----------------------


def test_query_string_and_trailing_slash_variants_dedupe_to_one_row(db_session):
    dedup = JobDeduplicator()
    base = make_job(
        source_job_id="9001",
        canonical_key="acme-devops-9001",
        title="DevOps Engineer",
        source_url="https://boards.greenhouse.io/acme/jobs/9001",
        content_hash="stable-devops-hash",
    )

    # Re-scraped later: same source_job_id, so this collapses via the
    # strongest identity level - proving a benign re-ingestion still dedupes
    # normally after the URL matching was tightened from LIKE to equality.
    rescraped = make_job(
        source_job_id="9001",
        canonical_key="acme-devops-9001",
        title="DevOps Engineer",
        source_url="https://boards.greenhouse.io/acme/jobs/9001/?utm_source=newsletter&ref=abc",
        content_hash="stable-devops-hash",
    )

    # Seen via a differently-captured URL (query/trailing-slash variant) but
    # under a *different* source_job_id, so the match can only come from URL
    # identity (level 2/3), not the (source, source_job_id) level. This is
    # the case exact-equality normalization exists to still catch correctly.
    url_only_variant = make_job(
        source_job_id="9001-syndicated",
        canonical_key="acme-devops-9001-variant",
        title="DevOps Engineer",
        source_url="https://boards.greenhouse.io/acme/jobs/9001/?utm_source=newsletter&ref=abc",
        content_hash="stable-devops-hash",
    )

    result_base = dedup.process(db_session, base)
    result_rescraped = dedup.process(db_session, rescraped)
    result_url_only = dedup.process(db_session, url_only_variant)

    assert result_base.status == "NEW"
    assert result_rescraped.status == "DUPLICATE"
    assert result_rescraped.job_row.id == result_base.job_row.id

    # A new (source, source_job_id) pair always records a new source
    # reference, so this resolves as CROSS_SOURCE_DUPLICATE rather than
    # DUPLICATE - but it must still be the *same* job row, proving the URL
    # match (not a fresh insert) is what found it.
    assert result_url_only.status == "CROSS_SOURCE_DUPLICATE"
    assert result_url_only.matched_by in ("application_url", "source_url")
    assert result_url_only.job_row.id == result_base.job_row.id
    assert db_session.query(JobRow).count() == 1


def test_identity_precedence_source_job_id_wins_over_differing_urls(db_session):
    dedup = JobDeduplicator()
    original = make_job(
        source_job_id="7777",
        canonical_key="acme-sre-7777",
        title="Site Reliability Engineer",
        source_url="https://boards.greenhouse.io/acme/jobs/7777",
        content_hash="hash-a",
    )
    dedup.process(db_session, original)

    # Same (source, source_job_id) but a completely different URL and
    # canonical_key - the strongest identity level must still win.
    moved = make_job(
        source_job_id="7777",
        canonical_key="acme-sre-7777-moved",
        title="Site Reliability Engineer II",
        source_url="https://careers.acme.com/openings/sre-relocated",
        content_hash="hash-b",
    )
    result = dedup.process(db_session, moved)

    assert result.matched_by == "source_job_id"
    assert db_session.query(JobRow).count() == 1


def test_cross_source_duplicate_links_to_same_job_row(db_session):
    dedup = JobDeduplicator()
    gh_job = make_job(
        source=JobSourceType.GREENHOUSE,
        source_job_id="g-500",
        canonical_key="acme-pm-gh-500",
        title="Product Manager",
        source_url="https://boards.greenhouse.io/acme/jobs/500",
        application_url="https://acme.com/apply/pm-role",
        content_hash="same-content-hash",
    )
    lever_job = make_job(
        source=JobSourceType.LEVER,
        source_job_id="l-500",
        canonical_key="acme-pm-lever-500",
        title="Product Manager",
        source_url="https://jobs.lever.co/acme/pm-role-uuid",
        application_url="https://acme.com/apply/pm-role",  # same application URL
        content_hash="same-content-hash",
    )

    result_gh = dedup.process(db_session, gh_job)
    result_lever = dedup.process(db_session, lever_job)

    assert result_gh.status == "NEW"
    assert result_lever.status == "CROSS_SOURCE_DUPLICATE"
    assert result_lever.job_row.id == result_gh.job_row.id
    assert db_session.query(JobRow).count() == 1

    refs = (
        db_session.query(SourceReferenceRow)
        .filter_by(job_id=result_gh.job_row.id)
        .all()
    )
    assert len(refs) == 2
    assert {ref.source for ref in refs} == {"GREENHOUSE", "LEVER"}


def test_content_change_on_same_source_job_id_produces_updated_and_version_row(db_session):
    dedup = JobDeduplicator()
    v1 = make_job(
        source_job_id="8888",
        canonical_key="acme-analyst-8888",
        title="Data Analyst",
        source_url="https://boards.greenhouse.io/acme/jobs/8888",
        content_hash="hash-v1",
    )
    dedup.process(db_session, v1)

    v2 = make_job(
        source_job_id="8888",
        canonical_key="acme-analyst-8888",
        title="Senior Data Analyst",
        source_url="https://boards.greenhouse.io/acme/jobs/8888",
        content_hash="hash-v2",
    )
    result = dedup.process(db_session, v2)

    assert result.status == "UPDATED"
    assert result.job_row.title == "Senior Data Analyst"
    assert db_session.query(JobRow).count() == 1

    versions = db_session.query(JobVersionRow).filter_by(job_id=result.job_row.id).all()
    assert len(versions) == 1
    assert versions[0].title == "Data Analyst"
    assert versions[0].content_hash == "hash-v1"


def test_reprocessing_unchanged_job_is_idempotent(db_session):
    dedup = JobDeduplicator()
    job = make_job(
        source_job_id="9999",
        canonical_key="acme-qa-9999",
        title="QA Engineer",
        source_url="https://boards.greenhouse.io/acme/jobs/9999",
        content_hash="stable-hash",
    )
    dedup.process(db_session, job)
    result = dedup.process(db_session, job)

    assert result.status == "DUPLICATE"
    assert db_session.query(JobRow).count() == 1
    assert db_session.query(SourceReferenceRow).count() == 1
    assert db_session.query(JobVersionRow).count() == 0


def test_literal_underscore_and_percent_do_not_collide_like_wildcards(db_session):
    # Regression guard for the old LIKE-based matching: '_' and '%' are LIKE
    # wildcards, so "abc_123" used to match "abcX123", and "50%off" used to
    # match "50Xoff". Exact equality on the normalized URL must not.
    dedup = JobDeduplicator()
    underscore_job = make_job(
        source_job_id="u-1",
        canonical_key="acme-eng-underscore",
        title="Underscore Job",
        source_url="https://boards.greenhouse.io/acme/jobs/abc_123",
    )
    underscore_wildcard_victim = make_job(
        source_job_id="u-2",
        canonical_key="acme-eng-underscore-victim",
        title="Should Not Match Underscore",
        source_url="https://boards.greenhouse.io/acme/jobs/abcX123",
    )
    percent_job = make_job(
        source_job_id="p-1",
        canonical_key="acme-eng-percent",
        title="Percent Job",
        source_url="https://boards.greenhouse.io/acme/jobs/50%off",
    )
    percent_wildcard_victim = make_job(
        source_job_id="p-2",
        canonical_key="acme-eng-percent-victim",
        title="Should Not Match Percent",
        source_url="https://boards.greenhouse.io/acme/jobs/50Xoff",
    )

    dedup.process(db_session, underscore_job)
    result_underscore_victim = dedup.process(db_session, underscore_wildcard_victim)
    dedup.process(db_session, percent_job)
    result_percent_victim = dedup.process(db_session, percent_wildcard_victim)

    assert result_underscore_victim.status == "NEW"
    assert result_percent_victim.status == "NEW"
    assert db_session.query(JobRow).count() == 4
