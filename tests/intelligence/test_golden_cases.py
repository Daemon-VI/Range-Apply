import pytest
from app.intelligence.services.evidence_resolver import EvidenceResolver, ResolvedEvidence
from app.intelligence.services.eligibility_engine import EligibilityEngine
from app.intelligence.services.requirement_interpreter import RequirementInterpreter
from app.intelligence.services.fit_scoring_engine import FitScoringEngine
from app.intelligence.services.explanation_generator import ExplanationGenerator
from app.intelligence.services.match_orchestrator import MatchOrchestrator
from app.intelligence.models.enums import EvidenceStrength, MatchType, EligibilityStatus
from app.jobs.models.job import NormalizedJob

# Mock CareerBrainService
class MockCareerBrainService:
    def get_skills(self):
        return []
    def get_projects(self):
        return []
    def get_profile(self):
        class Profile:
            graduation_year = 2027
        return Profile()

class MockEvidenceResolver(EvidenceResolver):
    def resolve_skill(self, skill_name: str) -> ResolvedEvidence:
        skill_name_lower = skill_name.lower()
        if skill_name_lower in ["go", "redis", "postgresql"]:
            return ResolvedEvidence(strength=EvidenceStrength.DIRECT_VERIFIED, references=[], description="Verified skill")
        if skill_name_lower in ["high concurrency", "distributed systems"]:
            return ResolvedEvidence(strength=EvidenceStrength.STRONG_DEMONSTRATED, references=["proj_1"], description="Ticket Engine")
        return ResolvedEvidence(strength=EvidenceStrength.NONE, references=[], description="No evidence")

@pytest.fixture
def orchestrator():
    cb = MockCareerBrainService()
    resolver = MockEvidenceResolver(cb)
    interpreter = RequirementInterpreter()
    eligibility = EligibilityEngine(cb)
    scoring = FitScoringEngine()
    explanation = ExplanationGenerator()
    return MatchOrchestrator(resolver, interpreter, eligibility, scoring, explanation)


def test_golden_case_a_strong_backend_match(orchestrator):
    job = NormalizedJob(
        canonical_key="job-a",
        source="GREENHOUSE",
        source_job_id="123",
        company="TechCorp",
        title="Backend Engineer",
        original_title="Backend Engineer",
        source_url="http://example.com",
        required_skills=["Go", "Redis", "PostgreSQL", "High concurrency"]
    )
    
    match = orchestrator.evaluate_job(job)
    assert match.eligibility.status == EligibilityStatus.ELIGIBLE
    assert match.match_type == MatchType.CORE_MATCH
    assert match.priority in ["P0", "P1"]
    assert "Go" in match.strengths
    assert not match.gaps

def test_golden_case_b_stretch_opportunity(orchestrator):
    job = NormalizedJob(
        canonical_key="job-b",
        source="GREENHOUSE",
        source_job_id="124",
        company="TechCorp",
        title="Backend Engineer",
        original_title="Backend Engineer",
        source_url="http://example.com",
        required_skills=["Go", "Redis", "Kubernetes", "distributed systems"]
    )
    
    match = orchestrator.evaluate_job(job)
    assert match.match_type == MatchType.STRETCH_MATCH
    assert "Go" in match.strengths
    assert "Kubernetes" in match.gaps
    assert match.eligibility.status == EligibilityStatus.ELIGIBLE
