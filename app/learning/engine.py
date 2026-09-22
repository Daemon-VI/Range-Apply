"""The Learning Engine (``LEARNING_VERSION``): deterministic, explainable,
volume-neutral.

    dataset (as_of, window)  ->  aggregate per dimension  ->  snapshot (persisted,
    versioned, never overwritten)  ->  recommendations  ->  optional
    expected-response signal for ``learned_prior`` (ordering only, opt-in)

What it reads: applications, candidate opportunities, opportunities, jobs,
preparations, execution runs, Phase 10 outcome events. What it writes:
``learning_snapshots``, ``learning_metrics``, ``learning_recommendations``
and audit events. What it never touches: policies, caps, bands,
thresholds, blocklists, eligibility, the Evidence Graph, preparations,
execution gates.
"""

import logging
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import case, func
from sqlalchemy.orm import Session

from app.core.errors import NotFoundError, ValidationFailed
from app.core.timeutils import db_now, ensure_aware, to_db, utc_now
from app.jobs.database.models import JobRow, SourceHealthRow
from app.learning.database.models import (
    LearningMetricRow,
    LearningRecommendationRow,
    LearningSnapshotRow,
)
from app.learning.dataset import build_dataset
from app.learning.models import (
    LEARNING_VERSION,
    MEDIAN_METRICS,
    RATE_METRICS,
    Dimension,
    ExpectedResponse,
    GroupMetrics,
    LearningConfidence,
    LearningResult,
    LearningRow,
    Metric,
    RateEstimate,
    Recommendation,
    TenantLearningSettings,
)
from app.learning.stats import confidence_for, estimate, median, wilson_interval
from app.pipeline.identity import normalize_title_for_identity
from app.pipeline.policy import company_key
from app.pipeline.repository import OpportunityRepository, PolicyRepository
from app.signals.models import ATTRIBUTION_VERSION, OUTCOME_RULES_VERSION

logger = logging.getLogger(__name__)

DEFAULT_ACTOR = "learning"

#: Which row field each dimension groups by (label = the human name).
_GROUPERS: dict[Dimension, tuple[str, str]] = {
    Dimension.SOURCE: ("source", "source"),
    Dimension.COMPANY: ("company_key", "company"),
    Dimension.TITLE: ("title_key", "title"),
    Dimension.ROLE_FAMILY: ("role_family", "role_family"),
    Dimension.FIT_BAND: ("fit_band", "fit_band"),
    Dimension.LANE: ("lane", "lane"),
    Dimension.TAILORING_LEVEL: ("tailoring_level", "tailoring_level"),
    Dimension.COVER_LETTER_MODE: ("cover_letter_mode", "cover_letter_mode"),
    Dimension.POSITIONING_VARIANT: ("positioning_variant_id", "positioning_variant_id"),
    Dimension.EXECUTION_METHOD: ("execution_method", "execution_method"),
}
_POSITIVE = {
    Metric.RESPONSE_RATE: "responded",
    Metric.INTERVIEW_RATE: "interviewed",
    Metric.REJECTION_RATE: "rejected",
    Metric.ASSESSMENT_RATE: "assessed",
    Metric.VERIFIED_SUBMISSION_RATE: "verified",
    Metric.UNCERTAINTY_RATE: "uncertain",
    Metric.REVIEW_RATE: "execution_needs_review",
}
_MEDIAN_FIELD = {Metric.DAYS_TO_RESPONSE: "days_to_response", Metric.DAYS_TO_REJECTION: "days_to_rejection"}
#: Dimensions the expected-response signal may draw on before a preparation exists.
_ORDERING_DIMENSIONS = (Dimension.SOURCE, Dimension.COMPANY, Dimension.TITLE, Dimension.FIT_BAND)
#: The signal blends these (a rejection counts as a response, an interview is the positive one).
_ORDERING_METRICS = (Metric.RESPONSE_RATE, Metric.INTERVIEW_RATE)
_CONFIDENCE_WEIGHT = {LearningConfidence.NONE: 0.0, LearningConfidence.LOW: 0.25, LearningConfidence.MEDIUM: 0.6, LearningConfidence.HIGH: 1.0}
_CONFIDENCE_RANK = {LearningConfidence.NONE: 0, LearningConfidence.LOW: 1, LearningConfidence.MEDIUM: 2, LearningConfidence.HIGH: 3}


def _pct(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{100 * value:.0f}%"


class LearningEngine:
    VERSION = LEARNING_VERSION

    def __init__(self, db: Session, tenant_id: str, settings: Optional[TenantLearningSettings] = None, actor: str = DEFAULT_ACTOR):
        if not tenant_id:
            raise ValidationFailed("tenant_id is required")
        self.db = db
        self.tenant_id = tenant_id
        self.actor = actor
        self._settings = settings
        self.repo = OpportunityRepository(db, tenant_id)

    @property
    def settings(self) -> TenantLearningSettings:
        if self._settings is None:
            self._settings = PolicyRepository(self.db, self.tenant_id).get().learning_settings
        return self._settings

    # ------------------------------------------------------------- dataset

    def dataset(self, as_of: Optional[datetime] = None) -> list[LearningRow]:
        return build_dataset(self.db, self.tenant_id, as_of=as_of, window_days=self.settings.window_days, minimum_evidence=self.settings.minimum_evidence)

    # ----------------------------------------------------------- aggregate

    def _group(self, dimension: Dimension, key: str, label: str, rows: list[LearningRow], baseline: Optional[GroupMetrics]) -> GroupMetrics:
        n = len(rows)
        s = self.settings
        estimates: dict[str, RateEstimate] = {}
        for metric in RATE_METRICS:
            positives = sum(1 for r in rows if getattr(r, _POSITIVE[metric]))
            base = baseline.estimates[metric.value].raw_rate if baseline is not None and baseline.estimates[metric.value].raw_rate is not None else (positives / n if n else 0.0)
            # The baseline itself is not shrunk (prior strength 0): it is the reference.
            estimates[metric.value] = estimate(metric, positives, n, base, s.prior_strength if baseline is not None else 0.0, s.min_samples)
        medians = {m.value: median(getattr(r, _MEDIAN_FIELD[m]) for r in rows) for m in MEDIAN_METRICS}
        median_samples = {m.value: sum(1 for r in rows if getattr(r, _MEDIAN_FIELD[m]) is not None) for m in MEDIAN_METRICS}
        evidence: dict[str, int] = defaultdict(int)
        for r in rows:
            evidence[r.evidence_quality.value] += 1
        return GroupMetrics(dimension=dimension, group_key=key, group_label=label, n=n, estimates=estimates, medians=medians, median_samples=median_samples, evidence=dict(evidence), confidence=confidence_for(n, s.min_samples))

    def aggregate(self, rows: list[LearningRow], as_of: Optional[datetime] = None) -> LearningResult:
        """Pure given the rows and settings: the same input gives the same result."""
        as_of = ensure_aware(as_of) or utc_now()
        baseline = self._group(Dimension.GLOBAL, "all", "all executed applications", rows, None)
        groups: list[GroupMetrics] = []
        for dimension, (field, _label_field) in _GROUPERS.items():
            buckets: dict[str, list[LearningRow]] = defaultdict(list)
            labels: dict[str, str] = {}
            for row in rows:
                value = getattr(row, field)
                if value in (None, ""):
                    continue
                key = str(value)
                buckets[key].append(row)
                labels.setdefault(key, row.company if dimension is Dimension.COMPANY else (row.title if dimension is Dimension.TITLE else key))
            for key in sorted(buckets):
                groups.append(self._group(dimension, key, labels[key], buckets[key], baseline))
        summary = {
            "submitted": baseline.n,
            "verified": baseline.estimates[Metric.VERIFIED_SUBMISSION_RATE.value].positives,
            "responded": baseline.estimates[Metric.RESPONSE_RATE.value].positives,
            "interviewed": baseline.estimates[Metric.INTERVIEW_RATE.value].positives,
            "rejected": baseline.estimates[Metric.REJECTION_RATE.value].positives,
            "assessed": baseline.estimates[Metric.ASSESSMENT_RATE.value].positives,
            "uncertain": baseline.estimates[Metric.UNCERTAINTY_RATE.value].positives,
            "needs_review": baseline.estimates[Metric.REVIEW_RATE.value].positives,
            "response_rate": baseline.estimates[Metric.RESPONSE_RATE.value].raw_rate,
            "interview_rate": baseline.estimates[Metric.INTERVIEW_RATE.value].raw_rate,
            "rejection_rate": baseline.estimates[Metric.REJECTION_RATE.value].raw_rate,
            "assessment_rate": baseline.estimates[Metric.ASSESSMENT_RATE.value].raw_rate,
            "verified_submission_rate": baseline.estimates[Metric.VERIFIED_SUBMISSION_RATE.value].raw_rate,
            "uncertainty_rate": baseline.estimates[Metric.UNCERTAINTY_RATE.value].raw_rate,
            "median_days_to_response": baseline.medians[Metric.DAYS_TO_RESPONSE.value],
            "median_days_to_rejection": baseline.medians[Metric.DAYS_TO_REJECTION.value],
            "evidence": baseline.evidence,
            "completeness": {k: sum(1 for r in rows if r.outcome_completeness.value == k) for k in ("terminal", "progress", "submitted_only", "unverified")},
            "groups": len(groups),
            "ai_calls": 0,
        }
        result = LearningResult(
            tenant_id=self.tenant_id,
            learning_version=self.VERSION,
            outcome_rules_version=OUTCOME_RULES_VERSION,
            attribution_version=ATTRIBUTION_VERSION,
            as_of=as_of,
            window_days=self.settings.window_days,
            window_start=(as_of - timedelta(days=self.settings.window_days)) if self.settings.window_days else None,
            dataset_size=len(rows),
            settings=self.settings.model_dump(mode="json"),
            baseline=baseline,
            groups=groups,
            summary=summary,
        )
        result.recommendations = self.recommendations(result)
        return result

    def compute(self, as_of: Optional[datetime] = None) -> LearningResult:
        as_of = ensure_aware(as_of) or utc_now()
        result = self.aggregate(self.dataset(as_of), as_of)
        result.source_discovery = self.source_discovery(as_of)
        result.recommendations += self._discovery_recommendations(result)
        return result

    # ------------------------------------------------------- source (job side)

    def source_discovery(self, as_of: Optional[datetime] = None) -> list[dict[str, Any]]:
        """Discovery-side reliability per source (shared job data, no candidate
        outcome): jobs seen, closed, duplicates, failed runs. Information only."""
        cutoff = to_db(ensure_aware(as_of)) if as_of else db_now()
        window_start = cutoff - timedelta(days=self.settings.window_days) if self.settings.window_days else None
        closed_expr = func.sum(case((JobRow.job_status == "CLOSED", 1), else_=0))
        query = self.db.query(JobRow.source, func.count(JobRow.id), closed_expr).filter(JobRow.first_seen_at <= cutoff)
        if window_start is not None:
            query = query.filter(JobRow.first_seen_at >= window_start)
        jobs = {source: (int(total or 0), int(closed or 0)) for source, total, closed in query.group_by(JobRow.source).all()}
        health = {}
        for row in self.db.query(SourceHealthRow).all():
            h = health.setdefault(row.source, {"runs_total": 0, "runs_failed": 0, "duplicates_total": 0, "jobs_fetched_total": 0, "jobs_rejected_total": 0})
            for key in h:
                h[key] += int(getattr(row, key) or 0)
        out = []
        for source in sorted(set(jobs) | set(health)):
            total, closed = jobs.get(source, (0, 0))
            h = health.get(source, {})
            fetched = h.get("jobs_fetched_total", 0)
            out.append(
                {
                    "source": source,
                    "jobs_seen": total,
                    "jobs_closed": closed,
                    "closure_rate": round(closed / total, 4) if total else None,
                    "duplicates": h.get("duplicates_total", 0),
                    "duplicate_rate": round(h.get("duplicates_total", 0) / fetched, 4) if fetched else None,
                    "rejected_jobs": h.get("jobs_rejected_total", 0),
                    "valid_job_rate": round(1 - h.get("jobs_rejected_total", 0) / fetched, 4) if fetched else None,
                    "runs_total": h.get("runs_total", 0),
                    "runs_failed": h.get("runs_failed", 0),
                    "confidence": confidence_for(total, self.settings.min_samples).value,
                    "scope": "shared job-side discovery data (no candidate outcomes)",
                }
            )
        return out

    # ---------------------------------------------------- recommendations

    def recommendations(self, result: LearningResult) -> list[Recommendation]:
        """Phrased findings. Only groups with at least MEDIUM confidence and a
        Wilson interval clear of the baseline get a higher/lower finding;
        sparse groups get an explicit 'insufficient data'. Never a filter."""
        s = self.settings
        out: list[Recommendation] = []
        base = result.baseline
        insufficient_budget = 20

        def make(kind: str, text: str, g: GroupMetrics, metric: Metric) -> Recommendation:
            e = g.estimates[metric.value]
            return Recommendation(kind=kind, text=text, dimension=g.dimension, group_key=g.group_key, group_label=g.group_label, metric=metric, n=g.n, positives=e.positives, observed_rate=e.raw_rate, smoothed_rate=e.smoothed_rate, ci_low=e.ci_low, ci_high=e.ci_high, baseline_rate=e.baseline_rate, confidence=g.confidence, evidence=g.evidence, learning_version=self.VERSION, window_days=result.window_days, as_of=result.as_of)

        for g in result.groups:
            if g.n == 0:
                continue
            if g.n < s.min_samples:
                if g.dimension in (Dimension.SOURCE, Dimension.FIT_BAND, Dimension.EXECUTION_METHOD, Dimension.LANE, Dimension.TAILORING_LEVEL) or insufficient_budget > 0:
                    if g.dimension not in (Dimension.SOURCE, Dimension.FIT_BAND, Dimension.EXECUTION_METHOD, Dimension.LANE, Dimension.TAILORING_LEVEL):
                        insufficient_budget -= 1
                    out.append(make("INSUFFICIENT_DATA", f"There is insufficient data to estimate the response rate for {g.dimension.value.lower().replace('_', ' ')} '{g.group_label}' ({g.n} of at least {s.min_samples} executed applications).", g, Metric.RESPONSE_RATE))
                continue
            for metric, higher, lower in ((Metric.RESPONSE_RATE, "HIGHER_RESPONSE", "LOWER_RESPONSE"), (Metric.INTERVIEW_RATE, "HIGHER_INTERVIEW", "LOWER_INTERVIEW")):
                e = g.estimates[metric.value]
                b = base.estimates[metric.value].raw_rate or 0.0
                if e.ci_low > b and e.raw_rate is not None and e.raw_rate > b:
                    out.append(make(higher, f"Historically, {g.dimension.value.lower().replace('_', ' ')} '{g.group_label}' has had a higher observed {metric.value.replace('_', ' ')} ({_pct(e.raw_rate)}, {e.positives} of {g.n}; smoothed {_pct(e.smoothed_rate)}) than your overall baseline ({_pct(b)}).", g, metric))
                elif e.ci_high < b and e.raw_rate is not None and e.raw_rate < b:
                    out.append(make(lower, f"Historically, {g.dimension.value.lower().replace('_', ' ')} '{g.group_label}' has had a lower observed {metric.value.replace('_', ' ')} ({_pct(e.raw_rate)}, {e.positives} of {g.n}; smoothed {_pct(e.smoothed_rate)}) than your overall baseline ({_pct(b)}).", g, metric))
            if g.dimension is Dimension.EXECUTION_METHOD:
                e = g.estimates[Metric.UNCERTAINTY_RATE.value]
                b = base.estimates[Metric.UNCERTAINTY_RATE.value].raw_rate or 0.0
                if e.ci_low > b and e.raw_rate is not None and e.raw_rate > b:
                    out.append(make("HIGHER_UNCERTAINTY", f"Execution method '{g.group_label}' is associated with a higher uncertainty rate ({_pct(e.raw_rate)}, {e.positives} of {g.n}) than the baseline ({_pct(b)}); this may reflect the sites routed to it rather than the method itself.", g, Metric.UNCERTAINTY_RATE))
        return out

    def _discovery_recommendations(self, result: LearningResult) -> list[Recommendation]:
        out: list[Recommendation] = []
        for src in result.source_discovery:
            n = src["jobs_seen"]
            if n < self.settings.min_samples:
                continue
            for key, label in (("closure_rate", "closure"), ("duplicate_rate", "duplicate")):
                rate = src.get(key)
                if rate is not None and rate >= 0.5:
                    low, high = wilson_interval(int(round(rate * n)), n)
                    out.append(Recommendation(kind=f"HIGH_{label.upper()}_RATE", text=f"Source '{src['source']}' has a high {label} rate ({_pct(rate)} of {n} jobs seen{' in the last ' + str(result.window_days) + ' days' if result.window_days else ''}); discovery-side information only, nothing is disabled.", dimension=Dimension.SOURCE, group_key=src["source"], group_label=src["source"], metric=Metric.VERIFIED_SUBMISSION_RATE, n=n, positives=int(round(rate * n)), observed_rate=rate, ci_low=round(low, 6), ci_high=round(high, 6), baseline_rate=None, confidence=LearningConfidence(src["confidence"]), window_days=result.window_days, as_of=result.as_of, caveat="shared job-side discovery data; not a candidate outcome"))
        return out

    # ------------------------------------------------------------ snapshots

    def snapshot(self, as_of: Optional[datetime] = None, actor: Optional[str] = None) -> LearningSnapshotRow:
        """Persist what the engine believes at ``as_of``. A new row every time;
        older snapshots are never updated, so history stays reproducible."""
        result = self.compute(as_of)
        row = LearningSnapshotRow(
            tenant_id=self.tenant_id,
            learning_version=result.learning_version,
            feature_version=result.feature_version,
            smoothing_method=result.smoothing_method,
            outcome_rules_version=result.outcome_rules_version,
            attribution_version=result.attribution_version,
            as_of=to_db(result.as_of),
            window_days=result.window_days,
            window_start=to_db(result.window_start) if result.window_start else None,
            dataset_size=result.dataset_size,
            settings=result.settings,
            baseline={k: v.model_dump(mode="json") for k, v in result.baseline.estimates.items()} | {"medians": result.baseline.medians, "n": result.baseline.n, "evidence": result.baseline.evidence},
            summary=result.summary,
            source_discovery=result.source_discovery,
            actor=actor or self.actor,
        )
        self.db.add(row)
        self.db.flush()
        metrics = 0
        for g in [result.baseline] + result.groups:
            for metric in RATE_METRICS:
                e = g.estimates[metric.value]
                self.db.add(LearningMetricRow(tenant_id=self.tenant_id, snapshot_id=row.id, dimension=g.dimension.value, group_key=g.group_key[:256], group_label=g.group_label[:256], metric=metric.value, n=e.n, positives=e.positives, negatives=e.negatives, raw_rate=e.raw_rate, smoothed_rate=e.smoothed_rate, ci_low=e.ci_low, ci_high=e.ci_high, confidence=e.confidence.value, baseline_rate=e.baseline_rate, evidence=g.evidence, learning_version=result.learning_version, feature_version=result.feature_version, smoothing_method=result.smoothing_method))
                metrics += 1
            for metric in MEDIAN_METRICS:
                samples = g.median_samples.get(metric.value, 0)
                self.db.add(LearningMetricRow(tenant_id=self.tenant_id, snapshot_id=row.id, dimension=g.dimension.value, group_key=g.group_key[:256], group_label=g.group_label[:256], metric=metric.value, n=samples, median_value=g.medians.get(metric.value), confidence=confidence_for(samples, self.settings.min_samples).value, evidence=g.evidence, learning_version=result.learning_version, feature_version=result.feature_version, smoothing_method=result.smoothing_method))
                metrics += 1
        for rec in result.recommendations:
            self.db.add(LearningRecommendationRow(tenant_id=self.tenant_id, snapshot_id=row.id, kind=rec.kind, text=rec.text, dimension=rec.dimension.value, group_key=rec.group_key[:256], group_label=rec.group_label[:256], metric=rec.metric.value, n=rec.n, positives=rec.positives, observed_rate=rec.observed_rate, baseline_rate=rec.baseline_rate, confidence=rec.confidence.value, evidence=rec.evidence, learning_version=rec.learning_version, window_days=rec.window_days, as_of=to_db(rec.as_of) if rec.as_of else None, caveat=rec.caveat[:256]))
        row.metric_count = metrics
        row.recommendation_count = len(result.recommendations)
        self.db.flush()
        self.repo.record("learning_snapshot", row.id, "generated", actor or self.actor, None, {"as_of": row.as_of.isoformat(), "window_days": row.window_days, "dataset_size": row.dataset_size, "metrics": metrics, "recommendations": len(result.recommendations), "learning_version": row.learning_version, "feature_version": row.feature_version, "outcome_rules_version": row.outcome_rules_version, "ai_calls": 0})
        return row

    def latest_snapshot(self) -> Optional[LearningSnapshotRow]:
        return self.db.query(LearningSnapshotRow).filter(LearningSnapshotRow.tenant_id == self.tenant_id).order_by(LearningSnapshotRow.generated_at.desc(), LearningSnapshotRow.id.desc()).first()

    def get_snapshot(self, snapshot_id: str) -> Optional[LearningSnapshotRow]:
        return self.db.query(LearningSnapshotRow).filter(LearningSnapshotRow.tenant_id == self.tenant_id, LearningSnapshotRow.id == snapshot_id).first()

    def require_snapshot(self, snapshot_id: str) -> LearningSnapshotRow:
        row = self.get_snapshot(snapshot_id)
        if row is None:
            raise NotFoundError(f"Learning snapshot not found: {snapshot_id}")
        return row

    def list_snapshots(self, limit: int = 50) -> list[LearningSnapshotRow]:
        return self.db.query(LearningSnapshotRow).filter(LearningSnapshotRow.tenant_id == self.tenant_id).order_by(LearningSnapshotRow.generated_at.desc(), LearningSnapshotRow.id.desc()).limit(limit).all()

    def metrics(self, snapshot_id: str, dimension: Optional[Dimension] = None, metric: Optional[Metric] = None, limit: int = 2000) -> list[LearningMetricRow]:
        self.require_snapshot(snapshot_id)
        query = self.db.query(LearningMetricRow).filter(LearningMetricRow.tenant_id == self.tenant_id, LearningMetricRow.snapshot_id == snapshot_id)
        if dimension is not None:
            query = query.filter(LearningMetricRow.dimension == dimension.value)
        if metric is not None:
            query = query.filter(LearningMetricRow.metric == metric.value)
        return query.order_by(LearningMetricRow.dimension.asc(), LearningMetricRow.n.desc(), LearningMetricRow.group_key.asc(), LearningMetricRow.metric.asc()).limit(limit).all()

    def snapshot_recommendations(self, snapshot_id: str) -> list[LearningRecommendationRow]:
        self.require_snapshot(snapshot_id)
        return self.db.query(LearningRecommendationRow).filter(LearningRecommendationRow.tenant_id == self.tenant_id, LearningRecommendationRow.snapshot_id == snapshot_id).order_by(LearningRecommendationRow.kind.asc(), LearningRecommendationRow.n.desc()).all()

    # --------------------------------------------- expected response signal

    def expected_response(self, *, source: Optional[str], company: Optional[str], title: Optional[str], fit_band: Optional[str], candidate_opportunity_id: Optional[str] = None, snapshot: Optional[LearningSnapshotRow] = None, index: Optional[dict] = None) -> ExpectedResponse:
        """0–100 ordering signal from the latest snapshot's response rates.

        Each usable component contributes (smoothed rate − baseline), weighted
        by its confidence; with no usable component the signal is neutral
        (50). It is only computed when ``ordering_enabled`` and only feeds the
        existing ``learned_prior`` priority component; it never decides
        admission.
        """
        enabled = bool(self.settings.ordering_enabled)
        if not enabled:
            return ExpectedResponse(candidate_opportunity_id=candidate_opportunity_id, enabled=False, explanation="learned ordering is off (learning_settings.ordering_enabled=false); priority uses the neutral learned_prior")
        snapshot = snapshot or self.latest_snapshot()
        if snapshot is None:
            return ExpectedResponse(candidate_opportunity_id=candidate_opportunity_id, enabled=True, explanation="no learning snapshot yet; neutral")
        index = index if index is not None else self.response_index(snapshot.id)
        keys = {Dimension.SOURCE: (source or "").upper() or None, Dimension.COMPANY: company_key(company) if company else None, Dimension.TITLE: normalize_title_for_identity(title) if title else None, Dimension.FIT_BAND: fit_band}
        # A rejection is a response too, so the signal blends the observed
        # response rate with the observed interview rate (equal weight): a
        # group that only ever answered "no" does not look promising.
        base_rates = {}
        for metric in _ORDERING_METRICS:
            baseline = index.get((Dimension.GLOBAL.value, "all", metric.value))
            base_rates[metric] = baseline.raw_rate if baseline is not None and baseline.raw_rate is not None else 0.0
        components: list[dict[str, Any]] = []
        weighted = 0.0
        weight_total = 0.0
        for dimension in _ORDERING_DIMENSIONS:
            key = keys[dimension]
            if not key:
                continue
            rows = {metric: index.get((dimension.value, key, metric.value)) for metric in _ORDERING_METRICS}
            m = rows[Metric.RESPONSE_RATE]
            if m is None or not m.n:
                components.append({"dimension": dimension.value, "group": key, "used": False, "reason": "no history"})
                continue
            conf = LearningConfidence(m.confidence)
            w = _CONFIDENCE_WEIGHT[conf]
            deltas = {metric.value: round(((rows[metric].smoothed_rate if rows[metric] is not None else base_rates[metric]) or 0.0) - base_rates[metric], 4) for metric in _ORDERING_METRICS}
            delta = sum(deltas.values()) / len(deltas)
            components.append({"dimension": dimension.value, "group": key, "used": w > 0, "n": m.n, "smoothed_rates": {metric.value: (rows[metric].smoothed_rate if rows[metric] is not None else None) for metric in _ORDERING_METRICS}, "baseline_rates": {metric.value: base_rates[metric] for metric in _ORDERING_METRICS}, "confidence": conf.value, "weight": w, "deltas": deltas, "delta": round(delta, 4)})
            weighted += w * delta
            weight_total += w
        score = 50.0 if weight_total == 0 else max(0.0, min(100.0, 50.0 + 100.0 * weighted / weight_total))
        used = [c for c in components if c.get("used")]
        explanation = "neutral: no component with history" if not used else "; ".join(f"{c['dimension'].lower()} {c['group']}: response {_pct(c['smoothed_rates']['response_rate'])} vs {_pct(c['baseline_rates']['response_rate'])}, interview {_pct(c['smoothed_rates']['interview_rate'])} vs {_pct(c['baseline_rates']['interview_rate'])} (n={c['n']}, {c['confidence']})" for c in used)
        return ExpectedResponse(candidate_opportunity_id=candidate_opportunity_id, enabled=True, score=round(score, 2), snapshot_id=snapshot.id, components=components, explanation=explanation)

    def response_index(self, snapshot_id: str) -> dict[tuple[str, str, str], LearningMetricRow]:
        rows = self.db.query(LearningMetricRow).filter(LearningMetricRow.tenant_id == self.tenant_id, LearningMetricRow.snapshot_id == snapshot_id, LearningMetricRow.metric.in_([m.value for m in _ORDERING_METRICS])).all()
        return {(r.dimension, r.group_key, r.metric): r for r in rows}


class LearnedPrior:
    """The ordering hook used by ``sync_match``: one snapshot index per sync
    run, ``None`` scores when ordering is off, so priority stays exactly as
    it was unless the candidate opted in."""

    def __init__(self, db: Session, tenant_id: str, settings: TenantLearningSettings):
        self.engine = LearningEngine(db, tenant_id, settings)
        self.enabled = bool(settings.ordering_enabled)
        self.snapshot = self.engine.latest_snapshot() if self.enabled else None
        self.index = self.engine.response_index(self.snapshot.id) if self.snapshot is not None else {}

    def score(self, *, source: Optional[str], company: Optional[str], title: Optional[str], fit_band: Optional[str], candidate_opportunity_id: Optional[str] = None) -> Optional[ExpectedResponse]:
        if not self.enabled or self.snapshot is None:
            return None
        return self.engine.expected_response(source=source, company=company, title=title, fit_band=fit_band, candidate_opportunity_id=candidate_opportunity_id, snapshot=self.snapshot, index=self.index)


__all__ = ["LearnedPrior", "LearningEngine"]
