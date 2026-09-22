"""Candidate application policy: bands, caps, lanes, admission.

The policy is the *only* place an eligible opportunity can be kept out of
the pipeline, and every rule in it is the candidate's own setting. Nothing
here looks at priority: a low-priority opportunity in an enabled band is
admitted exactly like a high-priority one and merely processed later.
"""

import re
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

from app.learning.models import TenantLearningSettings
from app.pipeline.database.models import ApplicationPolicyRow
from app.pipeline.models import (
    AdmissionReason,
    ApplicationPolicy,
    EligibilityDecision,
    FitBand,
    FitBandConfig,
    FitBandRange,
    Lane,
    TailoringLevel,
    TenantAISettings,
)

if TYPE_CHECKING:
    from app.jobs.geography import GeoAssessment
    from app.pipeline.gates import GateReport

#: Re-exported for callers that record the ruleset with a decision.
GATE_RULESET_VERSION = "tier1-gates-v3"

DEFAULT_BAND_THRESHOLDS = {"HIGH": 70, "MEDIUM": 45}
DEFAULT_ENABLED_BANDS = ["HIGH", "MEDIUM", "LOW"]
DEFAULT_TAILORING = {"HIGH": "L2", "MEDIUM": "L1", "LOW": "L0"}
DEFAULT_LANES = {"HIGH": "REVIEW", "MEDIUM": "REVIEW", "LOW": "REVIEW"}
DEFAULT_COVER_LETTERS = {"HIGH": "LIGHT", "MEDIUM": "TEMPLATE", "LOW": "DISABLED"}
VALID_COVER_LETTER_MODES = ("DISABLED", "TEMPLATE", "LIGHT", "TARGETED")


def band_for(fit_score: Optional[int], thresholds: Optional[dict] = None) -> Optional[FitBand]:
    if fit_score is None:
        return None
    thresholds = thresholds or DEFAULT_BAND_THRESHOLDS
    high = int(thresholds.get("HIGH", DEFAULT_BAND_THRESHOLDS["HIGH"]))
    medium = int(thresholds.get("MEDIUM", DEFAULT_BAND_THRESHOLDS["MEDIUM"]))
    if fit_score > high:
        return FitBand.HIGH
    if fit_score >= medium:
        return FitBand.MEDIUM
    return FitBand.LOW


def policy_from_row(row: ApplicationPolicyRow) -> ApplicationPolicy:
    return ApplicationPolicy(
        tenant_id=row.tenant_id,
        enabled_bands=[FitBand(b) for b in (row.enabled_bands or DEFAULT_ENABLED_BANDS)],
        band_thresholds=dict(row.band_thresholds or DEFAULT_BAND_THRESHOLDS),
        daily_cap=row.daily_cap,
        weekly_cap=row.weekly_cap,
        tailoring_by_band={k: TailoringLevel(v) for k, v in (row.tailoring_by_band or DEFAULT_TAILORING).items()},
        lane_by_band={k: Lane(v) for k, v in (row.lane_by_band or DEFAULT_LANES).items()},
        cover_letter_by_band=dict(row.cover_letter_by_band or DEFAULT_COVER_LETTERS),
        cooldown_days=row.cooldown_days,
        blocked_companies=list(row.blocked_companies or []),
        preferred_locations=list(row.preferred_locations or []),
        preferred_role_families=list(row.preferred_role_families or []),
        minimum_eligibility=EligibilityDecision(row.minimum_eligibility),
        duplicate_policy=row.duplicate_policy,
        priority_weights=dict(row.priority_weights or {}),
        minimum_fit_score=row.minimum_fit_score,
        timezone=row.timezone or "UTC",
        ai_settings=TenantAISettings(**(row.ai_settings or {})),
        learning_settings=TenantLearningSettings(**(row.learning_settings or {})),
        version=row.version,
        updated_at=row.updated_at,
    )


def fit_band_config(policy: ApplicationPolicy) -> FitBandConfig:
    """The band configuration as a versioned object (Phase 8b)."""
    high = int(policy.band_thresholds.get("HIGH", DEFAULT_BAND_THRESHOLDS["HIGH"]))
    medium = int(policy.band_thresholds.get("MEDIUM", DEFAULT_BAND_THRESHOLDS["MEDIUM"]))
    ranges = {FitBand.HIGH: (high + 1, 100), FitBand.MEDIUM: (medium, high), FitBand.LOW: (0, max(medium - 1, 0))}
    bands = [
        FitBandRange(
            band=band,
            min_score=lo,
            max_score=hi,
            enabled=band in policy.enabled_bands,
            lane=policy.lane_by_band.get(band.value, Lane.REVIEW),
            tailoring_level=policy.tailoring_by_band.get(band.value, TailoringLevel.L0),
            cover_letter=policy.cover_letter_by_band.get(band.value, "DISABLED"),
        )
        for band, (lo, hi) in ranges.items()
    ]
    return FitBandConfig(tenant_id=policy.tenant_id, version=policy.version, thresholds={"HIGH": high, "MEDIUM": medium}, enabled_bands=list(policy.enabled_bands), bands=bands, minimum_fit_score=policy.minimum_fit_score)


def company_key(name: Optional[str]) -> str:
    """Normalised company identity for blocklist and cool-down comparisons.

    Lower-case, punctuation folded to spaces, whitespace collapsed, and common
    legal suffixes dropped, so "ACME, Inc." and "acme inc" and "Acme" agree.
    """
    folded = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-z0-9]+", " ", folded.lower())
    words = [w for w in text.split() if w not in _COMPANY_SUFFIXES]
    return " ".join(words)


_COMPANY_SUFFIXES = {"inc", "llc", "ltd", "limited", "gmbh", "bv", "plc", "corp", "corporation", "co", "pvt", "private"}


@dataclass(frozen=True)
class Admission:
    admitted: bool
    reason: str
    lane: Optional[Lane] = None
    tailoring_level: Optional[TailoringLevel] = None
    code: AdmissionReason = AdmissionReason.ADMITTED
    #: The Tier-1 gate report this admission was derived from (Phase 8b).
    gates: Optional["GateReport"] = None

    @property
    def ruleset_version(self) -> str:
        return self.gates.ruleset_version if self.gates else GATE_RULESET_VERSION

    @property
    def policy_version(self) -> Optional[int]:
        return self.gates.policy_version if self.gates else None


def _reason_for(report: "GateReport", decision: Optional[EligibilityDecision], fit_band: Optional[FitBand], fit_score: Optional[int], policy: ApplicationPolicy) -> str:
    """The stable, human-readable reason strings Phase 2/5 introduced."""
    failed = report.failed
    if failed is None:
        return f"admitted:{decision.value}:{fit_band.value}"
    code = failed.code
    if code is AdmissionReason.NOT_EVALUATED:
        return "not_evaluated"
    if code is AdmissionReason.INELIGIBLE:
        return "ineligible"
    if code is AdmissionReason.BELOW_MINIMUM_ELIGIBILITY:
        return f"below_minimum_eligibility:{decision.value}"
    if code is AdmissionReason.COMPANY_BLOCKED:
        return "company_blocked"
    if code is AdmissionReason.OUTSIDE_TARGET_GEOGRAPHY:
        return f"outside_target_geography:{failed.detail.split(':')[0]}"
    if code is AdmissionReason.IRRELEVANT_ROLE:
        return f"irrelevant_role:{failed.detail.split(':')[0].replace(' ', '/')}"
    if code is AdmissionReason.NOT_SCORED:
        return "not_scored"
    if code is AdmissionReason.BAND_DISABLED:
        return f"band_disabled:{fit_band.value}"
    if code is AdmissionReason.BELOW_FIT_THRESHOLD:
        return f"below_fit_threshold:{fit_score}<{policy.minimum_fit_score}"
    if code is AdmissionReason.OPPORTUNITY_CLOSED:
        return "opportunity_closed"
    if code is AdmissionReason.USER_BLOCKED:
        return "skipped_by_candidate"
    return code.value.lower()


def evaluate_admission(
    policy: ApplicationPolicy,
    decision: Optional[EligibilityDecision],
    fit_band: Optional[FitBand],
    company: str,
    fit_score: Optional[int] = None,
    opportunity_status: Optional[str] = None,
    candidate_state: Optional[str] = None,
    geography: Optional["GeoAssessment"] = None,
    role: Optional[Any] = None,
) -> Admission:
    """Whether an opportunity may enter the candidate's application pipeline.

    Phase 8b: the decision is the Tier-1 gate set (``app.pipeline.gates``),
    run in its fixed order — candidate skip, opening closed, evaluated, hard
    ineligibility, the candidate's minimum eligibility, the blocklist, scored,
    band enabled, the optional fit floor. Priority is deliberately absent.
    Caps, cool-down, duplicates and the run window are stateful and are
    applied by the scheduler on top of this; the reason strings and codes are
    unchanged from earlier phases.
    """
    from app.pipeline.gates import evaluate_gates

    report = evaluate_gates(policy, decision, fit_band, company, fit_score, opportunity_status, candidate_state, geography, role)
    reason = _reason_for(report, decision, fit_band, fit_score, policy)
    if not report.passed:
        return Admission(False, reason, code=report.code, gates=report)
    return Admission(
        True,
        reason,
        lane=policy.lane_by_band.get(fit_band.value, Lane.REVIEW),
        tailoring_level=policy.tailoring_by_band.get(fit_band.value, TailoringLevel.L0),
        code=AdmissionReason.ADMITTED,
        gates=report,
    )
