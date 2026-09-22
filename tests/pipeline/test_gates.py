"""Tier-1 gate set: every gate, reason codes, ordering, versioning, no priority
leakage, no hidden top-N, and the versions recorded with each decision."""

import inspect

from app.pipeline.gates import (
    GATE_CATALOG,
    GATE_RULESET_VERSION,
    STATIC_GATES,
    Gate,
    GateReport,
    catalog,
    evaluate_gates,
)
from app.pipeline.models import (
    AdmissionReason,
    ApplicationPolicy,
    ApplicationPolicyUpdate,
    EligibilityDecision,
    FitBand,
    OpportunityState,
    OpportunityStatus,
)
from app.pipeline.policy import evaluate_admission

E, L, U, IN = EligibilityDecision.ELIGIBLE, EligibilityDecision.LIKELY, EligibilityDecision.UNCERTAIN, EligibilityDecision.INELIGIBLE


def _policy(**changes) -> ApplicationPolicy:
    return ApplicationPolicy(tenant_id="t", **changes)


def test_ruleset_is_ordered_versioned_and_maps_onto_existing_codes():
    assert GATE_RULESET_VERSION == "tier1-gates-v3"
    order = [spec.gate for spec in STATIC_GATES]
    assert order == [Gate.CANDIDATE_SKIP, Gate.OPENING_OPEN, Gate.EVALUATED, Gate.HARD_ELIGIBILITY, Gate.MINIMUM_ELIGIBILITY, Gate.COMPANY_BLOCKLIST, Gate.GEOGRAPHY, Gate.ROLE_RELEVANCE, Gate.FIT_SCORED, Gate.BAND_ENABLED, Gate.FIT_FLOOR]
    assert all(isinstance(spec.code, AdmissionReason) for spec in GATE_CATALOG)
    assert {spec.gate for spec in GATE_CATALOG} - {spec.gate for spec in STATIC_GATES} == {Gate.NOT_ALREADY_SUBMITTED, Gate.NO_DUPLICATE_APPLICATION, Gate.CANDIDATE_INFO_COMPLETE, Gate.EVIDENCE_RESOLVED, Gate.APPLICATION_SUPPORTED}
    rows = catalog(_policy(blocked_companies=["Evil"], minimum_fit_score=30))
    by_gate = {r["gate"]: r for r in rows}
    assert by_gate["COMPANY_BLOCKLIST"]["value"] == ["Evil"] and by_gate["FIT_FLOOR"]["value"] == 30 and by_gate["MINIMUM_ELIGIBILITY"]["value"] == "UNCERTAIN"
    assert by_gate["BAND_ENABLED"]["value"] == ["HIGH", "MEDIUM", "LOW"] and by_gate["NO_DUPLICATE_APPLICATION"]["static"] is False


def test_every_static_gate_fails_with_its_own_code_and_detail():
    policy = _policy(blocked_companies=["Evil Corp"], enabled_bands=[FitBand.HIGH, FitBand.MEDIUM], minimum_fit_score=50, minimum_eligibility=L)
    cases = [
        (dict(decision=E, fit_band=FitBand.HIGH, company="Acme", fit_score=90, candidate_state=OpportunityState.SKIPPED.value), Gate.CANDIDATE_SKIP, AdmissionReason.USER_BLOCKED),
        (dict(decision=E, fit_band=FitBand.HIGH, company="Acme", fit_score=90, opportunity_status=OpportunityStatus.CLOSED.value), Gate.OPENING_OPEN, AdmissionReason.OPPORTUNITY_CLOSED),
        (dict(decision=None, fit_band=FitBand.HIGH, company="Acme", fit_score=90), Gate.EVALUATED, AdmissionReason.NOT_EVALUATED),
        (dict(decision=IN, fit_band=FitBand.HIGH, company="Acme", fit_score=90), Gate.HARD_ELIGIBILITY, AdmissionReason.INELIGIBLE),
        (dict(decision=U, fit_band=FitBand.HIGH, company="Acme", fit_score=90), Gate.MINIMUM_ELIGIBILITY, AdmissionReason.BELOW_MINIMUM_ELIGIBILITY),
        (dict(decision=E, fit_band=FitBand.HIGH, company="evil corp inc", fit_score=90), Gate.COMPANY_BLOCKLIST, AdmissionReason.COMPANY_BLOCKED),
        (dict(decision=E, fit_band=None, company="Acme", fit_score=None), Gate.FIT_SCORED, AdmissionReason.NOT_SCORED),
        (dict(decision=E, fit_band=FitBand.LOW, company="Acme", fit_score=40), Gate.BAND_ENABLED, AdmissionReason.BAND_DISABLED),
        (dict(decision=E, fit_band=FitBand.MEDIUM, company="Acme", fit_score=48), Gate.FIT_FLOOR, AdmissionReason.BELOW_FIT_THRESHOLD),
    ]
    for kwargs, gate, code in cases:
        report = evaluate_gates(policy, **kwargs)
        assert report.failed is not None and report.failed.gate is gate and report.code is code, (kwargs, report.to_dict())
        assert report.failed.detail and report.ruleset_version == GATE_RULESET_VERSION and report.policy_version == policy.version
        assert len(report.results) == len(STATIC_GATES), "every gate is reported, not only the first failure"
    ok = evaluate_gates(policy, E, FitBand.HIGH, "Acme", 90)
    assert ok.passed and ok.failed is None and ok.code is AdmissionReason.ADMITTED
    assert ok.to_dict()["failed_gate"] is None and all(g["passed"] for g in ok.to_dict()["gates"])


def test_first_failing_gate_in_order_decides_and_later_gates_still_report():
    policy = _policy(blocked_companies=["Evil"], enabled_bands=[FitBand.HIGH])
    report = evaluate_gates(policy, IN, FitBand.LOW, "Evil", 10)
    assert report.failed.gate is Gate.HARD_ELIGIBILITY
    failing = [r.gate for r in report.results if not r.passed]
    assert failing == [Gate.HARD_ELIGIBILITY, Gate.COMPANY_BLOCKLIST, Gate.BAND_ENABLED]


def test_gates_are_deterministic_and_never_read_priority():
    policy = _policy()
    a = evaluate_gates(policy, E, FitBand.LOW, "Acme", 20)
    b = evaluate_gates(policy, E, FitBand.LOW, "Acme", 20)
    assert a == b and a.passed, "LOW band, low score: admitted while LOW is enabled"
    source = inspect.getsource(evaluate_gates).replace(evaluate_gates.__doc__ or "", "")
    assert "priority" not in source.lower() and "top_n" not in source.lower() and "limit" not in source.lower(), "no priority input, no top-N"
    assert "priority" not in inspect.signature(evaluate_gates).parameters
    # A thousand admissible opportunities all pass: nothing limits the count.
    assert sum(1 for i in range(1000) if evaluate_gates(policy, E, FitBand.MEDIUM, f"Co {i}", 50).passed) == 1000


def test_evaluate_admission_keeps_phase2_reason_strings_and_carries_the_report():
    policy = _policy(blocked_companies=["Evil"], minimum_fit_score=60)
    assert evaluate_admission(policy, None, FitBand.HIGH, "A").reason == "not_evaluated"
    assert evaluate_admission(policy, IN, FitBand.HIGH, "A").reason == "ineligible"
    assert evaluate_admission(_policy(minimum_eligibility=L), U, FitBand.HIGH, "A").reason == "below_minimum_eligibility:UNCERTAIN"
    assert evaluate_admission(policy, E, FitBand.HIGH, "evil").reason == "company_blocked"
    assert evaluate_admission(policy, E, None, "A").reason == "not_scored"
    assert evaluate_admission(_policy(enabled_bands=[FitBand.HIGH]), E, FitBand.LOW, "A").reason == "band_disabled:LOW"
    assert evaluate_admission(policy, E, FitBand.MEDIUM, "A", 50).reason == "below_fit_threshold:50<60"
    assert evaluate_admission(policy, E, FitBand.HIGH, "A", 90, opportunity_status="CLOSED").code is AdmissionReason.OPPORTUNITY_CLOSED
    admitted = evaluate_admission(policy, E, FitBand.HIGH, "A", 90)
    assert admitted.admitted and admitted.reason == "admitted:ELIGIBLE:HIGH" and admitted.gates.passed
    assert admitted.ruleset_version == GATE_RULESET_VERSION and admitted.policy_version == policy.version


def test_sync_records_versions_and_gate_report_on_admission(db_session, jobs, matches, tenant_id, repo, policy_repo):
    from app.pipeline.sync import sync_match

    job = jobs.make(title="Gate Engineer")
    match = matches.make(job, fit_score=80)
    _, result = sync_match(db_session, tenant_id, match)
    db_session.commit()
    co = result.candidate_opportunity
    assert co.policy_admitted and co.fit_policy_version == 1 and co.admission_policy_version == 1 and co.gate_ruleset_version == GATE_RULESET_VERSION
    event = next(e for e in repo.list_audit("candidate_opportunity", co.id) if e.action == "admission")
    gates = event.after["gates"]
    assert gates["ruleset_version"] == GATE_RULESET_VERSION and gates["passed"] and len(gates["gates"]) == len(STATIC_GATES)

    # Policy change: the next sync records the new version; the old event is untouched.
    policy_repo.update(ApplicationPolicyUpdate(blocked_companies=[jobs.company("Acme")]), "test")
    db_session.commit()
    sync_match(db_session, tenant_id, match)
    db_session.commit()
    db_session.refresh(co)
    assert co.policy_admitted is False and co.policy_reason == "company_blocked" and co.admission_policy_version == 2
    events = [e for e in repo.list_audit("candidate_opportunity", co.id) if e.action == "admission"]
    assert len(events) == 2 and events[-1].after["gates"]["policy_version"] == 1 and events[0].after["gates"]["failed_gate"] == "COMPANY_BLOCKLIST"


def test_closed_opening_fails_the_gate_at_sync(db_session, jobs, matches, tenant_id):
    from app.pipeline.sync import sync_match

    job = jobs.make(title="Closed Engineer")
    match = matches.make(job, fit_score=80)
    _, result = sync_match(db_session, tenant_id, match)
    from app.pipeline.repository import OpportunityRepository

    co = result.candidate_opportunity
    job.job_status = "CLOSED"  # the opportunity's status follows its jobs (discovery closure)
    assert OpportunityRepository.close_opportunity_if_all_jobs_closed(db_session, co.opportunity)
    db_session.commit()
    _, result = sync_match(db_session, tenant_id, match)
    db_session.commit()
    assert result.admitted is False and co.policy_reason == "opportunity_closed"


def test_gate_report_is_serialisable_and_explainable():
    report = evaluate_gates(_policy(), E, FitBand.HIGH, "Acme", 88)
    data = report.to_dict()
    assert set(data) == {"ruleset_version", "policy_version", "passed", "failed_gate", "gates"}
    assert all(set(g) == {"gate", "passed", "code", "detail"} for g in data["gates"])
    assert isinstance(report, GateReport)
