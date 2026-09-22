"""Deterministic signal → application attribution (Blueprint Phase 10 §7).

Strong identifiers first, in a fixed order; textual company/title evidence
last and only when it points at exactly one application. Fuzzy similarity
is never authoritative. When two strong identifiers disagree, or when more
than one application could be meant, the result is AMBIGUOUS (a person
decides); when nothing safe matches it is UNMATCHED (a person may link
later). Every result explains the rule, the evidence and the version.
"""

import re
import unicodedata
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.application.database.models import ApplicationRow
from app.application.models import ApplicationStatus
from app.execution.database.models import ExecutionRunRow
from app.execution.service import _normalize_url
from app.jobs.database.models import JobRow
from app.pipeline.database.models import CandidateOpportunityRow, OpportunityJobRow, OpportunityRow
from app.pipeline.identity import normalize_title_for_identity
from app.pipeline.policy import _COMPANY_SUFFIXES, company_key
from app.signals.database.models import SignalRow
from app.signals.models import ATTRIBUTION_VERSION, AttributionResult, AttributionStatus, Confidence
from app.signals.normalize import NormalizedSignal

#: Attempts that cannot have produced an employer-side signal yet.
PRE_EXECUTION = frozenset(
    {
        ApplicationStatus.DISCOVERED.value,
        ApplicationStatus.QUALIFIED.value,
        ApplicationStatus.SHORTLISTED.value,
        ApplicationStatus.PREPARING.value,
        ApplicationStatus.READY.value,
        ApplicationStatus.AWAITING_APPROVAL.value,
    }
)

_FOLD_RE = re.compile(r"[^a-z0-9]+")


def _fold(text: str) -> str:
    """The same folding ``company_key`` applies (ASCII, lower-case, punctuation
    to spaces, legal suffixes dropped) so a company key is a substring match."""
    folded = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode("ascii").lower()
    words = [w for w in _FOLD_RE.sub(" ", folded).split() if w not in _COMPANY_SUFFIXES]
    return " " + " ".join(words) + " "


def _title_key(title: str) -> str:
    """A title folded exactly like the searched text, so "Senior Engineer -
    Platform (Remote)" is found in a message that mentions it. Real titles
    carry dashes, commas and parentheses (Phase 13); the identity normalizer
    alone keeps that punctuation and never matched folded text."""
    return _fold(normalize_title_for_identity(title)).strip()


class AttemptIndex:
    """The tenant's attempts and what identifies them, loaded once per batch."""

    def __init__(self, db: Session, tenant_id: str):
        self.db = db
        self.tenant_id = tenant_id
        self.loaded = False
        self.attempts: dict[str, ApplicationRow] = {}
        self.opportunities: dict[str, OpportunityRow] = {}
        self.by_opportunity: dict[str, str] = {}
        self.by_job: dict[str, str] = {}
        self.by_reference: dict[str, set[str]] = {}
        self.by_url: dict[str, set[str]] = {}
        self.by_company: dict[str, set[str]] = {}
        self.company_of: dict[str, str] = {}
        self.title_of: dict[str, str] = {}
        self.co_of: dict[str, str] = {}

    def refresh(self) -> None:
        self.loaded = False
        self.load()

    def load(self) -> None:
        if self.loaded:
            return
        self.__init__(self.db, self.tenant_id)  # reset
        rows = self.db.query(ApplicationRow).filter(ApplicationRow.tenant_id == self.tenant_id).all()
        opp_ids = {r.opportunity_id for r in rows if r.opportunity_id}
        opps = {o.id: o for o in self.db.query(OpportunityRow).filter(OpportunityRow.id.in_(opp_ids)).all()} if opp_ids else {}
        job_ids = {r.job_id for r in rows if r.job_id} | {o.canonical_job_id for o in opps.values() if o.canonical_job_id}
        jobs = {j.id: j for j in self.db.query(JobRow).filter(JobRow.id.in_(job_ids)).all()} if job_ids else {}
        links = self.db.query(OpportunityJobRow).filter(OpportunityJobRow.opportunity_id.in_(opp_ids)).all() if opp_ids else []
        jobs_by_opp: dict[str, list[str]] = {}
        for link in links:
            jobs_by_opp.setdefault(link.opportunity_id, []).append(link.job_id)
        extra_job_ids = {jid for ids in jobs_by_opp.values() for jid in ids} - set(jobs)
        if extra_job_ids:
            jobs.update({j.id: j for j in self.db.query(JobRow).filter(JobRow.id.in_(extra_job_ids)).all()})
        for row in rows:
            self.attempts[row.id] = row
            opp = opps.get(row.opportunity_id) if row.opportunity_id else None
            if opp is not None:
                self.opportunities[opp.id] = opp
                self.by_opportunity[opp.id] = row.id
                self.company_of[row.id] = company_key(opp.company)
                self.title_of[row.id] = _title_key(opp.title)
            else:
                job = jobs.get(row.job_id)
                self.company_of[row.id] = company_key(job.company) if job else ""
                self.title_of[row.id] = _title_key(job.title) if job else ""
            if row.candidate_opportunity_id:
                self.co_of[row.id] = row.candidate_opportunity_id
            if self.company_of[row.id]:
                self.by_company.setdefault(self.company_of[row.id], set()).add(row.id)
            for ref in (row.external_application_id, row.confirmation):
                if ref and len(ref.strip()) >= 4:
                    self.by_reference.setdefault(ref.strip().lower(), set()).add(row.id)
            linked_jobs = [row.job_id] if row.job_id else []
            if opp is not None:
                linked_jobs += jobs_by_opp.get(opp.id, [])
                if opp.canonical_job_id:
                    linked_jobs.append(opp.canonical_job_id)
            for jid in dict.fromkeys(linked_jobs):
                self.by_job[jid] = row.id
                job = jobs.get(jid)
                if job is None:
                    continue
                for url in (job.application_url, job.source_url):
                    normalized = _normalize_url(url)
                    if normalized:
                        self.by_url.setdefault(normalized, set()).add(row.id)
        self.loaded = True

    def eligible(self, application_id: str) -> bool:
        row = self.attempts.get(application_id)
        return row is not None and row.status not in PRE_EXECUTION


class Attributor:
    VERSION = ATTRIBUTION_VERSION

    def __init__(self, db: Session, tenant_id: str, index: Optional[AttemptIndex] = None):
        self.db = db
        self.tenant_id = tenant_id
        self.index = index or AttemptIndex(db, tenant_id)

    # ------------------------------------------------------------ helpers

    def _result(self, status: AttributionStatus, application_id: Optional[str], rule: str, confidence: Confidence, evidence: dict[str, Any], candidates: Optional[list[str]] = None, explanation: str = "") -> AttributionResult:
        row = self.index.attempts.get(application_id) if application_id else None
        return AttributionResult(
            status=status,
            application_id=application_id,
            opportunity_id=row.opportunity_id if row else None,
            candidate_opportunity_id=row.candidate_opportunity_id if row else None,
            rule=rule,
            confidence=confidence,
            evidence=evidence,
            candidates=sorted(candidates or []),
            attribution_version=self.VERSION,
            explanation=explanation,
        )

    def _run_application(self, run_id: str) -> Optional[str]:
        run = self.db.query(ExecutionRunRow).filter(ExecutionRunRow.tenant_id == self.tenant_id, ExecutionRunRow.id == run_id).first()
        return run.application_id if run else None

    def _thread_application(self, message_id: str) -> Optional[str]:
        prior = (
            self.db.query(SignalRow)
            .filter(SignalRow.tenant_id == self.tenant_id, SignalRow.source_reference == message_id, SignalRow.application_id.isnot(None))
            .order_by(SignalRow.created_at.desc())
            .first()
        )
        return prior.application_id if prior else None

    # --------------------------------------------------------------- run

    def attribute(self, signal: NormalizedSignal) -> AttributionResult:
        self.index.load()
        hints = signal.hints
        # A strong identifier the index has not seen may be newer than the index (long-lived service).
        if (hints.application_id and hints.application_id not in self.index.attempts) or (hints.opportunity_id and hints.opportunity_id not in self.index.by_opportunity) or (hints.job_id and hints.job_id not in self.index.by_job):
            self.index.refresh()
        findings: list[tuple[str, set[str], dict[str, Any]]] = []  # (rule, application ids, evidence)

        def note(rule: str, ids: set[str], **evidence: Any) -> None:
            findings.append((rule, set(ids), evidence))

        # 1. strong identifiers supplied by the connector
        if hints.application_id:
            ids = {hints.application_id} if hints.application_id in self.index.attempts else set()
            note("application_id", ids, application_id=hints.application_id, known=bool(ids))
        if hints.execution_run_id:
            app_id = self._run_application(hints.execution_run_id)
            note("execution_run", {app_id} if app_id else set(), execution_run_id=hints.execution_run_id)
        if hints.candidate_opportunity_id:
            co = self.db.get(CandidateOpportunityRow, hints.candidate_opportunity_id)
            ids = set()
            if co is not None and co.tenant_id == self.tenant_id:
                app_id = self.index.by_opportunity.get(co.opportunity_id)
                if app_id:
                    ids = {app_id}
            note("candidate_opportunity_id", ids, candidate_opportunity_id=hints.candidate_opportunity_id)
        if hints.opportunity_id:
            app_id = self.index.by_opportunity.get(hints.opportunity_id)
            note("opportunity_id", {app_id} if app_id else set(), opportunity_id=hints.opportunity_id)
        if hints.job_id:
            app_id = self.index.by_job.get(hints.job_id)
            note("source_job_id", {app_id} if app_id else set(), job_id=hints.job_id)
        # 2. an email thread already attributed
        if hints.in_reply_to:
            app_id = self._thread_application(hints.in_reply_to)
            note("email_thread", {app_id} if app_id else set(), in_reply_to=hints.in_reply_to[:120])
        # 3. confirmation / reference numbers in the content
        for ref in signal.references:
            ids = self.index.by_reference.get(ref.strip().lower(), set())
            if ids:
                note("confirmation_reference", ids, reference=ref)
        # 4. our own ids embedded in the content (status pages, extension observations)
        for uid in signal.uuids:
            if uid in self.index.attempts:
                note("embedded_application_id", {uid}, id=uid)
            elif uid in self.index.by_opportunity:
                note("embedded_opportunity_id", {self.index.by_opportunity[uid]}, id=uid)
        # 5. application / job URLs
        for url in signal.urls:
            normalized = _normalize_url(url)
            if not normalized:
                continue
            ids = set(self.index.by_url.get(normalized, set()))
            if not ids:
                for known, app_ids in self.index.by_url.items():
                    if normalized.startswith(known + "/") or known.startswith(normalized + "/"):
                        ids |= app_ids
            if ids:
                note("application_url", ids, url=url[:200])

        strong = [(rule, ids, ev) for rule, ids, ev in findings if ids]
        distinct = set().union(*(ids for _, ids, _ in strong)) if strong else set()
        evidence = {rule: ev for rule, _, ev in findings}
        if len(distinct) == 1:
            app_id = next(iter(distinct))
            rule = strong[0][0]
            return self._result(AttributionStatus.MATCHED, app_id, rule, Confidence.HIGH, {"rules": [r for r, _, _ in strong], **evidence}, explanation=f"{rule} identifies exactly one application")
        if len(distinct) > 1:
            return self._result(AttributionStatus.AMBIGUOUS, None, "conflicting_identifiers", Confidence.LOW, {"rules": [r for r, _, _ in strong], **evidence}, candidates=list(distinct), explanation="strong identifiers point at different applications; a person must decide")
        if any(rule in ("application_id", "execution_run") for rule, _, _ in findings):
            return self._result(AttributionStatus.UNMATCHED, None, "unknown_identifier", Confidence.NONE, evidence, explanation="the supplied identifier is not an application of this tenant")

        # 6. company (+ title) evidence: only when exactly one application fits
        return self._company_rule(signal, evidence)

    def _company_rule(self, signal: NormalizedSignal, evidence: dict[str, Any]) -> AttributionResult:
        text = _fold(signal.searchable_text())
        hinted = company_key(signal.hints.company) if signal.hints.company else ""
        domain_label = (signal.employer_domain or "").split(".")[0] if signal.employer_domain else ""
        companies: set[str] = set()
        how: dict[str, str] = {}
        for ckey in self.index.by_company:
            if len(ckey) < 3:
                continue
            if hinted and ckey == hinted:
                companies.add(ckey)
                how[ckey] = "hint"
            elif f" {ckey} " in text:
                companies.add(ckey)
                how[ckey] = "text"
            elif domain_label and (ckey.replace(" ", "") == domain_label or ckey.split(" ")[0] == domain_label):
                companies.add(ckey)
                how[ckey] = "sender_domain"
        if not companies:
            return self._result(AttributionStatus.UNMATCHED, None, "none", Confidence.NONE, evidence, explanation="no identifier, URL, reference, company or title evidence matched an application")
        candidates = {app_id for ckey in companies for app_id in self.index.by_company[ckey] if self.index.eligible(app_id)}
        if not candidates:
            return self._result(AttributionStatus.UNMATCHED, None, "company_no_attempt", Confidence.NONE, {**evidence, "companies": sorted(companies)}, explanation="the company is known but no application to it has been executed")
        hinted_title = _title_key(signal.hints.title) if signal.hints.title else ""
        by_title = {app_id for app_id in candidates if (self.index.title_of.get(app_id) and (f" {self.index.title_of[app_id]} " in text or (hinted_title and hinted_title == self.index.title_of[app_id])))}
        ev = {**evidence, "companies": sorted(companies), "company_evidence": how, "title_matches": len(by_title)}
        if len(by_title) == 1:
            app_id = next(iter(by_title))
            return self._result(AttributionStatus.MATCHED, app_id, "company_title", Confidence.HIGH, ev, explanation="company and job title together identify exactly one application")
        if len(by_title) > 1:
            return self._result(AttributionStatus.AMBIGUOUS, None, "company_title", Confidence.LOW, ev, candidates=list(by_title), explanation="several applications share this company and title")
        if len(candidates) == 1:
            app_id = next(iter(candidates))
            return self._result(AttributionStatus.MATCHED, app_id, "company_only", Confidence.MEDIUM, ev, explanation="the company identifies exactly one executed application (no title evidence)")
        return self._result(AttributionStatus.AMBIGUOUS, None, "company_only", Confidence.LOW, ev, candidates=list(candidates), explanation="several applications to this company; no title evidence")


__all__ = ["AttemptIndex", "Attributor", "PRE_EXECUTION"]
