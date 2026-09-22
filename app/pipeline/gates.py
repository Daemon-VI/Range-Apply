"""Tier-1 gate set (Blueprint Phase 8b).

The small, deterministic, versioned set of reasons an opportunity does not
proceed. It sits between priority and the stateful scheduler checks:

    ELIGIBILITY → FIT → PRIORITY → **TIER-1 GATES** → APPLICATION POLICY
    (caps, cool-down, duplicates, window) → PREPARATION → EXECUTION

Gates never look at priority (priority orders, it never decides) and never
impose a top-N. Every gate maps onto an existing reason code
(:class:`AdmissionReason`) so nothing downstream learns a second vocabulary;
the report adds *which gate ran, whether it passed, why it failed, and the
ruleset / policy version used* so any historical decision stays explainable.

Static gates run here, in a fixed order, from data already on the candidate
opportunity. Stateful gates (duplicates, already submitted, missing candidate
facts, unresolved evidence, unsupported application) are enforced where the
state lives — scheduler, preparation, execution preconditions — and are
catalogued here with the code they report, so the whole set is inspectable
in one place.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from app.jobs.geography import GeoAssessment
from app.pipeline.models import (
    ELIGIBILITY_RANK,
    AdmissionReason,
    ApplicationPolicy,
    EligibilityDecision,
    FitBand,
    OpportunityState,
    OpportunityStatus,
)

#: Bump when a gate is added, removed, reordered or its rule changes.
#: v2 (2026-09-14): GEOGRAPHY, the candidate's job-market target.
#: v3 (2026-09-14): ROLE_RELEVANCE, the candidate's target role families.
GATE_RULESET_VERSION = "tier1-gates-v3"


class Gate(str, Enum):
    CANDIDATE_SKIP = "CANDIDATE_SKIP"
    OPENING_OPEN = "OPENING_OPEN"
    EVALUATED = "EVALUATED"
    HARD_ELIGIBILITY = "HARD_ELIGIBILITY"
    MINIMUM_ELIGIBILITY = "MINIMUM_ELIGIBILITY"
    COMPANY_BLOCKLIST = "COMPANY_BLOCKLIST"
    GEOGRAPHY = "GEOGRAPHY"
    ROLE_RELEVANCE = "ROLE_RELEVANCE"
    FIT_SCORED = "FIT_SCORED"
    BAND_ENABLED = "BAND_ENABLED"
    FIT_FLOOR = "FIT_FLOOR"
    # Stateful gates, enforced elsewhere (catalogued for inspection):
    NOT_ALREADY_SUBMITTED = "NOT_ALREADY_SUBMITTED"
    NO_DUPLICATE_APPLICATION = "NO_DUPLICATE_APPLICATION"
    CANDIDATE_INFO_COMPLETE = "CANDIDATE_INFO_COMPLETE"
    EVIDENCE_RESOLVED = "EVIDENCE_RESOLVED"
    APPLICATION_SUPPORTED = "APPLICATION_SUPPORTED"


@dataclass(frozen=True)
class GateSpec:
    gate: Gate
    code: AdmissionReason
    description: str
    enforced_at: str
    parameter: Optional[str] = None  # policy field that parametrises it
    static: bool = True


#: The ruleset, in evaluation order. Order is part of the version.
GATE_CATALOG: tuple[GateSpec, ...] = (
    GateSpec(Gate.CANDIDATE_SKIP, AdmissionReason.USER_BLOCKED, "the candidate did not skip this opportunity", "sync / scheduler"),
    GateSpec(Gate.OPENING_OPEN, AdmissionReason.OPPORTUNITY_CLOSED, "the opening is still open at the source", "sync / scheduler / execution precondition"),
    GateSpec(Gate.EVALUATED, AdmissionReason.NOT_EVALUATED, "a Tier 1 eligibility decision exists", "sync / scheduler"),
    GateSpec(Gate.HARD_ELIGIBILITY, AdmissionReason.INELIGIBLE, "not INELIGIBLE (hard constraints from the job description)", "sync / scheduler"),
    GateSpec(Gate.MINIMUM_ELIGIBILITY, AdmissionReason.BELOW_MINIMUM_ELIGIBILITY, "eligibility at or above the candidate's minimum", "sync / scheduler", "minimum_eligibility"),
    GateSpec(Gate.COMPANY_BLOCKLIST, AdmissionReason.COMPANY_BLOCKED, "the company is not on the candidate's blocklist", "sync / scheduler / execution precondition", "blocked_companies"),
    GateSpec(Gate.GEOGRAPHY, AdmissionReason.OUTSIDE_TARGET_GEOGRAPHY, "the posting is inside the candidate's job location preference (Career Brain preferences / profile location)", "sync / scheduler"),
    GateSpec(Gate.ROLE_RELEVANCE, AdmissionReason.IRRELEVANT_ROLE, "the role belongs to a family the candidate targets, or could be technical for a technical candidate (Career Brain target roles)", "sync / scheduler"),
    GateSpec(Gate.FIT_SCORED, AdmissionReason.NOT_SCORED, "a fit score and band exist", "sync / scheduler"),
    GateSpec(Gate.BAND_ENABLED, AdmissionReason.BAND_DISABLED, "the fit band is enabled by the policy", "sync / scheduler", "enabled_bands"),
    GateSpec(Gate.FIT_FLOOR, AdmissionReason.BELOW_FIT_THRESHOLD, "fit score at or above the optional floor", "sync / scheduler", "minimum_fit_score"),
    GateSpec(Gate.NOT_ALREADY_SUBMITTED, AdmissionReason.ALREADY_SUBMITTED, "no submitted attempt for this opportunity (repost handling per duplicate_policy)", "scheduler / execution precondition", "duplicate_policy", static=False),
    GateSpec(Gate.NO_DUPLICATE_APPLICATION, AdmissionReason.DUPLICATE_APPLICATION, "no application to the same title at the same company", "scheduler / execution precondition", "duplicate_policy", static=False),
    GateSpec(Gate.CANDIDATE_INFO_COMPLETE, AdmissionReason.NEEDS_USER_INPUT, "every required question / identity fact has a truthful answer", "preparation / execution (NEEDS_USER_INPUT)", None, static=False),
    GateSpec(Gate.EVIDENCE_RESOLVED, AdmissionReason.NEEDS_REVIEW, "every claim resolves to confirmed evidence (TruthValidator)", "preparation / execution (NEEDS_REVIEW)", None, static=False),
    GateSpec(Gate.APPLICATION_SUPPORTED, AdmissionReason.NEEDS_REVIEW, "the application method can be executed (browser form / manual handoff)", "execution (UNSUPPORTED_FORM handoff)", None, static=False),
)

GATE_BY_NAME = {spec.gate: spec for spec in GATE_CATALOG}
STATIC_GATES = tuple(spec for spec in GATE_CATALOG if spec.static)


@dataclass(frozen=True)
class GateResult:
    gate: Gate
    passed: bool
    code: AdmissionReason
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"gate": self.gate.value, "passed": self.passed, "code": self.code.value, "detail": self.detail}


@dataclass(frozen=True)
class GateReport:
    ruleset_version: str
    policy_version: int
    results: tuple[GateResult, ...] = field(default_factory=tuple)

    @property
    def passed(self) -> bool:
        return all(r.passed for r in self.results)

    @property
    def failed(self) -> Optional[GateResult]:
        """The first failing gate in ruleset order: it decides the code."""
        return next((r for r in self.results if not r.passed), None)

    @property
    def code(self) -> AdmissionReason:
        failed = self.failed
        return failed.code if failed else AdmissionReason.ADMITTED

    def to_dict(self) -> dict[str, Any]:
        failed = self.failed
        return {
            "ruleset_version": self.ruleset_version,
            "policy_version": self.policy_version,
            "passed": self.passed,
            "failed_gate": failed.gate.value if failed else None,
            "gates": [r.to_dict() for r in self.results],
        }


def evaluate_gates(
    policy: ApplicationPolicy,
    decision: Optional[EligibilityDecision],
    fit_band: Optional[FitBand],
    company: Optional[str],
    fit_score: Optional[int] = None,
    opportunity_status: Optional[str] = None,
    candidate_state: Optional[str] = None,
    geography: Optional[GeoAssessment] = None,
    role: Optional[Any] = None,
) -> GateReport:
    """Run every static gate, in order, and report each one.

    Pure and deterministic: same inputs and policy version → same report.
    Nothing here reads priority. ``geography`` is the posting assessed against
    the candidate's location preference; ``None`` (no job location known to the
    caller) passes the gate as "n/a".
    """
    from app.pipeline.policy import company_key  # local import: policy imports this module

    results: list[GateResult] = []

    skipped = candidate_state == OpportunityState.SKIPPED.value
    results.append(GateResult(Gate.CANDIDATE_SKIP, not skipped, AdmissionReason.USER_BLOCKED, "skipped by the candidate" if skipped else "not skipped"))

    closed = opportunity_status == OpportunityStatus.CLOSED.value or candidate_state == OpportunityState.CLOSED.value
    results.append(GateResult(Gate.OPENING_OPEN, not closed, AdmissionReason.OPPORTUNITY_CLOSED, "opening is closed at the source" if closed else "open"))

    evaluated = decision is not None
    results.append(GateResult(Gate.EVALUATED, evaluated, AdmissionReason.NOT_EVALUATED, "no Tier 1 decision yet" if not evaluated else f"decision {decision.value}"))

    # Gates whose prerequisite failed report "n/a" and pass: the failing
    # prerequisite already carries the code, and the report stays readable.
    ineligible = decision is EligibilityDecision.INELIGIBLE
    results.append(GateResult(Gate.HARD_ELIGIBILITY, not ineligible, AdmissionReason.INELIGIBLE, "INELIGIBLE" if ineligible else ("n/a (not evaluated)" if not evaluated else "not ineligible")))

    below_min = evaluated and not ineligible and ELIGIBILITY_RANK[decision] < ELIGIBILITY_RANK[policy.minimum_eligibility]
    results.append(GateResult(Gate.MINIMUM_ELIGIBILITY, not below_min, AdmissionReason.BELOW_MINIMUM_ELIGIBILITY, f"{decision.value} is below minimum_eligibility={policy.minimum_eligibility.value}" if below_min else ("n/a" if not evaluated or ineligible else f"{decision.value} >= {policy.minimum_eligibility.value}")))

    blocked = bool(company) and company_key(company) in {company_key(c) for c in policy.blocked_companies}
    results.append(GateResult(Gate.COMPANY_BLOCKLIST, not blocked, AdmissionReason.COMPANY_BLOCKED, "company is on the blocklist" if blocked else "not blocked"))

    outside = geography is not None and not geography.in_policy
    detail = "n/a (no location assessment)" if geography is None else f"{geography.tier.value}: {geography.detail}"
    results.append(GateResult(Gate.GEOGRAPHY, not outside, AdmissionReason.OUTSIDE_TARGET_GEOGRAPHY, detail[:300]))

    irrelevant = role is not None and not role.in_policy
    role_detail = "n/a (no role assessment)" if role is None else f"{role.label}: {role.detail}"
    results.append(GateResult(Gate.ROLE_RELEVANCE, not irrelevant, AdmissionReason.IRRELEVANT_ROLE, role_detail[:300]))

    scored = fit_band is not None
    results.append(GateResult(Gate.FIT_SCORED, scored, AdmissionReason.NOT_SCORED, "no fit score / band yet" if not scored else f"band {fit_band.value}" + (f", score {fit_score}" if fit_score is not None else "")))

    band_off = scored and fit_band not in policy.enabled_bands
    results.append(GateResult(Gate.BAND_ENABLED, not band_off, AdmissionReason.BAND_DISABLED, f"band {fit_band.value} is disabled by the policy" if band_off else ("n/a" if not scored else f"band {fit_band.value} enabled")))

    floor = policy.minimum_fit_score
    below_floor = floor is not None and fit_score is not None and fit_score < floor
    results.append(GateResult(Gate.FIT_FLOOR, not below_floor, AdmissionReason.BELOW_FIT_THRESHOLD, f"fit {fit_score} < minimum_fit_score {floor}" if below_floor else ("no floor set" if floor is None else f"fit {fit_score} >= {floor}")))

    return GateReport(GATE_RULESET_VERSION, policy.version, tuple(results))


def catalog(policy: Optional[ApplicationPolicy] = None) -> list[dict[str, Any]]:
    """Inspectable description of the ruleset, with the current parameter values."""
    rows = []
    for spec in GATE_CATALOG:
        value: Any = None
        if policy is not None and spec.parameter:
            value = getattr(policy, spec.parameter, None)
            if isinstance(value, list):
                value = [getattr(v, "value", v) for v in value]
            elif hasattr(value, "value"):
                value = value.value
        rows.append({"gate": spec.gate.value, "code": spec.code.value, "description": spec.description, "enforced_at": spec.enforced_at, "static": spec.static, "parameter": spec.parameter, "value": value})
    return rows
