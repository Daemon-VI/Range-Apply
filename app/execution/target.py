"""Normalised execution target for an opportunity.

Discovery adapters (Greenhouse, Lever, Ashby, capture) are read adapters; the
target only says *where* the application form is and which executor family
should handle it. No source has a submission API here.
"""

from typing import Optional

from app.execution.models import ApplicationMethod, ATSFamily, ExecutionTarget, ExecutorKind
from app.jobs.database.models import JobRow
from app.pipeline.database.models import OpportunityRow

_FAMILY_FOR_SOURCE = {
    "GREENHOUSE": ATSFamily.GREENHOUSE,
    "LEVER": ATSFamily.LEVER,
    "ASHBY": ATSFamily.ASHBY,
    "CAPTURE": ATSFamily.CAPTURED_BROWSER_TARGET,
    "EXTENSION": ATSFamily.CAPTURED_BROWSER_TARGET,
    "MANUAL": ATSFamily.MANUAL,
}


def ats_family_for(source: Optional[str], url: Optional[str]) -> ATSFamily:
    family = _FAMILY_FOR_SOURCE.get((source or "").upper())
    if family is not None:
        return family
    host = (url or "").lower()
    if "greenhouse.io" in host:
        return ATSFamily.GREENHOUSE
    if "lever.co" in host:
        return ATSFamily.LEVER
    if "ashbyhq.com" in host:
        return ATSFamily.ASHBY
    return ATSFamily.GENERIC_WEB


def target_for(opportunity: OpportunityRow, job: Optional[JobRow]) -> ExecutionTarget:
    source = (job.source if job else None) or "UNKNOWN"
    canonical_url = (job.application_url or job.source_url) if job else ""
    family = ats_family_for(source, canonical_url)
    if family is ATSFamily.MANUAL or not canonical_url:
        method = ApplicationMethod.MANUAL
        hint = ExecutorKind.MANUAL
    else:
        method = ApplicationMethod.BROWSER_FORM
        # The local Playwright runner is the first real executor (Phase 7); the
        # extension (Phase 9) will take over targets it can handle better.
        hint = ExecutorKind.PLAYWRIGHT_LOCAL
    return ExecutionTarget(
        source=source,
        ats_family=family,
        canonical_url=canonical_url or "",
        application_url=job.application_url if job else None,
        external_job_id=job.source_job_id if job else None,
        company=opportunity.company,
        title=opportunity.title,
        location=(job.location if job else None) or opportunity.location_bucket or None,
        method=method,
        executor_hint=hint,
    )
