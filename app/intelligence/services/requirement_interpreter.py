import uuid
from typing import List

from app.intelligence.models.enums import ConfidenceLevel, RequirementCategory, Strictness
from app.intelligence.models.requirements import StructuredRequirement
from app.jobs.models.job import NormalizedJob


class RequirementInterpreter:
    """Parses NormalizedJob into StructuredRequirement objects."""
    
    def interpret(self, job: NormalizedJob) -> List[StructuredRequirement]:
        requirements = []
        
        # Required Skills
        for skill in job.required_skills:
            requirements.append(
                StructuredRequirement(
                    id=str(uuid.uuid4()),
                    original_text=skill,
                    normalized_name=skill,
                    category=RequirementCategory.TECHNICAL_SKILL,
                    strictness=Strictness.REQUIRED,
                    confidence=ConfidenceLevel.HIGH
                )
            )
            
        # Preferred Skills
        for skill in job.preferred_skills:
            requirements.append(
                StructuredRequirement(
                    id=str(uuid.uuid4()),
                    original_text=skill,
                    normalized_name=skill,
                    category=RequirementCategory.TECHNICAL_SKILL,
                    strictness=Strictness.PREFERRED,
                    confidence=ConfidenceLevel.HIGH
                )
            )
            
        # Technologies
        for tech in job.technologies:
            # Depending on Phase 2, tech might overlap with required_skills. We'll add them as nice-to-have or signals if not explicitly in required/preferred.
            if tech not in job.required_skills and tech not in job.preferred_skills:
                requirements.append(
                    StructuredRequirement(
                        id=str(uuid.uuid4()),
                        original_text=tech,
                        normalized_name=tech,
                        category=RequirementCategory.TECHNICAL_SKILL,
                        strictness=Strictness.SIGNAL,
                        confidence=ConfidenceLevel.MEDIUM
                    )
                )

        # Responsibilities
        for resp in job.responsibilities:
            requirements.append(
                StructuredRequirement(
                    id=str(uuid.uuid4()),
                    original_text=resp,
                    normalized_name=resp,
                    category=RequirementCategory.RESPONSIBILITY,
                    strictness=Strictness.SIGNAL,
                    confidence=ConfidenceLevel.LOW
                )
            )
            
        return requirements
