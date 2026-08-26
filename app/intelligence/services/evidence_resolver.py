from typing import List, Dict, Tuple, Optional
from pydantic import BaseModel
from app.services.career_brain import CareerBrainService
from app.models import Skill, Project, Experience, Preference
from app.intelligence.models.enums import EvidenceStrength

class ResolvedEvidence(BaseModel):
    strength: EvidenceStrength
    references: List[str]
    description: str

class EvidenceResolver:
    """Resolves career brain data against specific job requirements."""
    
    def __init__(self, career_brain: CareerBrainService):
        self.career_brain = career_brain
        
    def resolve_skill(self, skill_name: str) -> ResolvedEvidence:
        """Looks up a skill in the Career Brain and returns evidence strength."""
        skills = self.career_brain.get_skills()
        skill_name_lower = skill_name.lower()
        
        # Check for exact matches
        for s in skills:
            if s.name.lower() == skill_name_lower:
                return ResolvedEvidence(
                    strength=EvidenceStrength.DIRECT_VERIFIED if s.is_verified else EvidenceStrength.SUPPORTING,
                    references=[s.id],
                    description=f"Explicit skill: {s.name}"
                )
        
        # Check projects for technology usage
        projects = self.career_brain.get_projects()
        related_projects = []
        for p in projects:
            for t in p.technologies:
                if t.lower() == skill_name_lower:
                    related_projects.append(p)
                    
        if related_projects:
            return ResolvedEvidence(
                strength=EvidenceStrength.STRONG_DEMONSTRATED,
                references=[p.id for p in related_projects],
                description=f"Demonstrated in projects: {', '.join(p.name for p in related_projects)}"
            )
            
        return ResolvedEvidence(
            strength=EvidenceStrength.NONE,
            references=[],
            description="No evidence found"
        )

    def resolve_experience(self) -> List[Experience]:
        return self.career_brain.get_experience()
        
    def resolve_preferences(self) -> Preference:
        return self.career_brain.get_preferences()
