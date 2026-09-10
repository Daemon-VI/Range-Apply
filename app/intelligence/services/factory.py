"""Construction of a fully wired MatchOrchestrator.

Single place where the Phase 3 object graph is assembled, so API routes,
background tasks, the dashboard and tests all score jobs the same way. A
divergence here would silently make the dashboard and the API disagree.
"""

from typing import Optional

from app.intelligence.extraction.requirement_extractor import RequirementExtractor
from app.intelligence.matching.skill_matcher import SkillMatcher
from app.intelligence.services.eligibility_engine import EligibilityEngine
from app.intelligence.services.evidence_resolver import EvidenceResolver
from app.intelligence.services.explanation_generator import ExplanationGenerator
from app.intelligence.services.fit_scoring_engine import FitScoringEngine
from app.intelligence.services.match_orchestrator import MatchOrchestrator
from app.intelligence.services.preference_evaluator import PreferenceEvaluator
from app.services.career_brain import CareerBrainService


def build_orchestrator(
    career_brain: Optional[CareerBrainService] = None,
    policy_weights: Optional[dict] = None,
) -> MatchOrchestrator:
    """Build a MatchOrchestrator backed by the Career Brain.

    Args:
        career_brain: Loaded service; a fresh one is created and loaded if omitted.
        policy_weights: Override the scoring policy (used to test alternatives).
    """
    if career_brain is None:
        career_brain = CareerBrainService()
        career_brain.load()

    matcher = SkillMatcher()
    return MatchOrchestrator(
        evidence_resolver=EvidenceResolver(career_brain, matcher=matcher),
        eligibility_engine=EligibilityEngine(career_brain),
        scoring_engine=FitScoringEngine(policy_weights=policy_weights),
        explanation_gen=ExplanationGenerator(),
        requirement_extractor=RequirementExtractor(),
        preference_evaluator=PreferenceEvaluator(),
    )
