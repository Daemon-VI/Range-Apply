from app.jobs.models.job import NormalizedJob
from app.services.career_brain import CareerBrainService
from app.intelligence.models.job_match import EligibilityResult
from app.intelligence.models.enums import EligibilityStatus

class EligibilityEngine:
    """Evaluates hard-gate eligibility rules."""
    
    def __init__(self, career_brain: CareerBrainService):
        self.career_brain = career_brain
        
    def evaluate(self, job: NormalizedJob) -> EligibilityResult:
        profile = self.career_brain.get_profile()
        eligibility_reasons = []
        blocking_reasons = []
        status = EligibilityStatus.ELIGIBLE
        
        # Evaluate Graduation Year
        if job.graduation_year_requirement or job.graduation_requirement:
            job_grad_year = job.graduation_year_requirement
            if job.graduation_requirement and not job_grad_year:
                # E.g., if there's a range, we check if candidate year fits.
                # For simplicity in this implementation, we take exact_years if available.
                if job.graduation_requirement.exact_years:
                    if profile.graduation_year in job.graduation_requirement.exact_years:
                        eligibility_reasons.append(f"Graduation year {profile.graduation_year} matches requirements.")
                    else:
                        blocking_reasons.append(f"Job requires graduation years: {job.graduation_requirement.exact_years}, candidate has {profile.graduation_year}")
                        status = EligibilityStatus.INELIGIBLE
                elif job.graduation_requirement.minimum_year:
                    if profile.graduation_year >= job.graduation_requirement.minimum_year:
                        eligibility_reasons.append(f"Graduation year {profile.graduation_year} meets minimum.")
                    else:
                        blocking_reasons.append(f"Job requires graduation year >= {job.graduation_requirement.minimum_year}, candidate has {profile.graduation_year}")
                        status = EligibilityStatus.INELIGIBLE
            elif job_grad_year:
                if profile.graduation_year == job_grad_year:
                    eligibility_reasons.append(f"Graduation year {profile.graduation_year} matches {job_grad_year}.")
                else:
                    blocking_reasons.append(f"Job requires graduation year {job_grad_year}, candidate has {profile.graduation_year}")
                    status = EligibilityStatus.INELIGIBLE

        # We could add more rules here (e.g. Work Authorization) if available on the model
        
        if not blocking_reasons and not eligibility_reasons:
            # If there were no explicit hard blockers evaluated but we didn't confirm anything
            # we are still technically ELIGIBLE from a gate perspective, or maybe UNCERTAIN if
            # there were constraints we couldn't parse. We'll default to ELIGIBLE.
            pass
            
        return EligibilityResult(
            status=status,
            eligibility_reasons=eligibility_reasons,
            blocking_reasons=blocking_reasons
        )
