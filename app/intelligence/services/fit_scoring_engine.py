from typing import List, Dict, Optional
from app.intelligence.models.requirements import RequirementAssessment, StructuredRequirement
from app.intelligence.models.enums import RequirementCategory, Strictness, MatchStatus, EvidenceStrength
from app.intelligence.models.job_match import JobMatch, EligibilityResult, MatchType, ConfidenceLevel

class FitScoringEngine:
    """Deterministic policy-driven scoring engine."""
    
    def __init__(self, policy_weights: Optional[Dict[str, float]] = None):
        self.policy_weights = policy_weights or {
            "technical": 0.40,
            "project_evidence": 0.30,
            "experience": 0.15,
            "preferences": 0.15
        }
        
    def score(self, assessments: List[RequirementAssessment], eligibility: EligibilityResult) -> dict:
        total_score = 0
        max_possible_score = 0
        
        gaps = []
        strengths = []
        
        for assess in assessments:
            # We assign a weight to the requirement based on strictness
            req_weight = 10
            if assess.requirement.strictness == Strictness.REQUIRED:
                req_weight = 20
            elif assess.requirement.strictness == Strictness.PREFERRED:
                req_weight = 5
                
            max_possible_score += req_weight
            
            if assess.status == MatchStatus.MATCHED:
                total_score += req_weight
                strengths.append(assess.requirement.normalized_name)
            elif assess.status == MatchStatus.PARTIAL:
                total_score += req_weight * 0.5
                strengths.append(assess.requirement.normalized_name)
            elif assess.status == MatchStatus.MISSING:
                gaps.append(assess.requirement.normalized_name)
                
        # Calculate fit score (0-100)
        fit_score = int((total_score / max_possible_score) * 100) if max_possible_score > 0 else 0
        
        # Determine Match Type
        match_type = MatchType.UNKNOWN
        if fit_score >= 80:
            match_type = MatchType.CORE_MATCH
        elif fit_score >= 60:
            match_type = MatchType.STRETCH_MATCH
        else:
            match_type = MatchType.POOR_MATCH
            
        # Determine Priority
        if fit_score >= 90:
            priority = "P0"
        elif fit_score >= 80:
            priority = "P1"
        elif fit_score >= 60:
            priority = "P2"
        else:
            priority = "IGNORE"
            
        return {
            "fit_score": fit_score,
            "match_type": match_type,
            "priority": priority,
            "strengths": strengths,
            "gaps": gaps
        }
