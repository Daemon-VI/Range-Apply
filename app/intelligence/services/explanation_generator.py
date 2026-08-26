from typing import List
from app.intelligence.models.requirements import RequirementAssessment
from app.intelligence.models.job_match import JobMatch
from app.intelligence.models.enums import MatchType

class ExplanationGenerator:
    """Generates human-readable explanations for job match assessments."""
    
    def generate(self, match_type: MatchType, strengths: List[str], gaps: List[str], assessments: List[RequirementAssessment]) -> str:
        lines = []
        
        if match_type == MatchType.CORE_MATCH:
            lines.append("Strong match because the role requirements align well with verified skills.")
        elif match_type == MatchType.STRETCH_MATCH:
            lines.append("Stretch match. There is strong project evidence despite some gaps.")
        else:
            lines.append("Poor match. Missing key required skills.")
            
        if strengths:
            lines.append(f"Strengths: {', '.join(strengths)}")
            
        if gaps:
            lines.append(f"Gaps: {', '.join(gaps)}")
            
        lines.append("Recommendation: " + ("Worth applying." if match_type != MatchType.POOR_MATCH else "Not recommended."))
        
        return "\n".join(lines)
