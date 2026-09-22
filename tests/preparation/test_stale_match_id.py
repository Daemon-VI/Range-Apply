"""Re-matching alone must not make a prepared package stale.

Autopilot finding (2026-09-22): every full match run mints a new
``JobMatchRow`` id for each job, so a preparation fingerprinted on
``match_id`` went stale on the very next cycle, the execution foundation
marked the run STALE ("inputs changed since preparation: match_id") and the
READY attempt bounced back to PREPARING for ever. The package depends on the
*content* of the match, which is what is fingerprinted now.
"""

import hashlib
import json

import pytest

from app.intelligence.database.models import MatchRunRow, RequirementAssessmentRow
from app.jobs.database.models import JobRow

#: What ``OpportunityFactory.make`` seeds by default.
SAME_REQUIREMENTS = [("Python", ["skill-python"], 30.0), ("Go", ["skill-go"], 20.0)]
OTHER_REQUIREMENTS = [("Python", ["skill-python"], 30.0), ("Docker", ["skill-docker"], 20.0)]


@pytest.fixture
def rematch(db_session, matches):
    """Re-score a candidate opportunity the way a full match run does.

    A new ``MatchRunRow`` (``(job_id, run_id)`` is unique) and therefore a new
    match id and new assessment rows, pointed at by the candidate opportunity.
    """
    created: list[str] = []

    def _make(co, requirements=SAME_REQUIREMENTS):
        job = db_session.get(JobRow, co.opportunity.canonical_job_id)
        keep = matches.run
        matches.run = None
        match = matches.make(job, fit_score=co.fit_score or 80)
        created.append(matches.run.id)
        matches.run = keep
        for name, refs, contribution in requirements:
            db_session.add(
                RequirementAssessmentRow(
                    job_match_id=match.id,
                    requirement_name=name,
                    requirement_category="TECHNICAL_SKILL",
                    requirement_strictness="REQUIRED",
                    status="MATCHED" if refs else "MISSING",
                    evidence_strength="DIRECT_VERIFIED" if refs else "NONE",
                    evidence_references=list(refs),
                    confidence="HIGH",
                    weight=1.0,
                    contribution=contribution,
                )
            )
        co.match_id = match.id
        db_session.commit()
        return match

    yield _make
    db_session.rollback()
    for run_id in created:
        row = db_session.get(MatchRunRow, run_id)
        if row is not None:
            db_session.delete(row)
    db_session.commit()


def _digest(inputs: dict) -> str:
    return hashlib.sha256(json.dumps(inputs, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def test_identical_rematch_with_a_new_match_id_is_not_stale(db_session, service, opportunities, rematch):
    co = opportunities.make(title="Backend Engineer")
    prep = service.prepare(co.id)
    assert prep.inputs["match_fingerprint"]

    previous_match_id = prep.inputs["match_id"]
    rematch(co, SAME_REQUIREMENTS)
    db_session.refresh(co)
    assert co.match_id != previous_match_id

    assert service.stale_inputs(prep) == []


def test_rematch_with_different_evidence_references_is_stale(db_session, service, opportunities, rematch):
    co = opportunities.make(title="Backend Engineer")
    prep = service.prepare(co.id)

    rematch(co, OTHER_REQUIREMENTS)

    assert "match_fingerprint" in service.stale_inputs(prep)


def test_legacy_preparation_without_a_match_fingerprint_is_not_stale(db_session, service, opportunities, rematch):
    """Packages stored before 2026-09-22 have no ``match_fingerprint`` key."""
    co = opportunities.make(title="Backend Engineer")
    prep = service.prepare(co.id)

    legacy = {k: v for k, v in prep.inputs.items() if k != "match_fingerprint"}
    prep.inputs = legacy
    prep.input_fingerprint = _digest(legacy)
    db_session.commit()

    assert service.stale_inputs(prep) == []

    rematch(co, SAME_REQUIREMENTS)
    assert service.stale_inputs(prep) == []
