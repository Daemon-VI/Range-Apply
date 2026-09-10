"""Golden-path regression tests for the full matching pipeline.

These exercise :func:`app.intelligence.services.factory.build_orchestrator`
end to end - RequirementExtractor -> EligibilityEngine -> EvidenceResolver ->
PreferenceEvaluator -> FitScoringEngine -> ExplanationGenerator - against a
small fake Career Brain, so the result stays independent of both the real
career data file and any particular policy-weight tuning.

Exact magic-number scores are deliberately avoided in favor of ranges and
ordering assertions: case A (a strong, eligible backend match) must clearly
outscore case B (a real stretch with genuine gaps), which must clearly
outscore case C (an otherwise-strong match capped low by ineligibility).
"""

from app.intelligence.models.enums import EligibilityStatus, MatchType
from app.intelligence.services.factory import build_orchestrator
from app.intelligence.services.match_orchestrator import MatchOrchestrator
from app.jobs.models.enums import EmploymentType, JobSourceType, RemoteType
from app.jobs.models.job import GraduationRequirement, NormalizedJob
from app.models import Experience, Preference, Profile, Project, Skill
from app.models.enums import SkillCategory, VerificationStatus


class FakeCareerBrain:
    """Minimal stand-in for CareerBrainService, backed by real career models."""

    def __init__(self, profile, skills, projects, experience, preferences):
        self._profile = profile
        self._skills = skills
        self._projects = projects
        self._experience = experience
        self._preferences = preferences

    def get_profile(self) -> Profile:
        return self._profile

    def get_skills(self) -> list:
        return list(self._skills)

    def get_projects(self) -> list:
        return list(self._projects)

    def get_experience(self) -> list:
        return list(self._experience)

    def get_preferences(self) -> Preference:
        return self._preferences


def _make_career_brain() -> FakeCareerBrain:
    """A backend-leaning candidate: verified Go/PostgreSQL/Redis/Docker, plus
    distributed-systems and concurrency experience demonstrated in a project."""
    profile = Profile(
        name="Test Candidate",
        location="Hyderabad",
        work_authorization="Indian citizen, no sponsorship required",
        degree="B.Tech",
        branch="Computer Science",
        college="Test Institute of Technology",
        graduation_year=2027,
        current_academic_status="Final year",
        cgpa=8.5,
        backlogs="None",
    )
    skills = [
        Skill(id="s1", name="Go", category=SkillCategory.BACKEND, verification_status=VerificationStatus.VERIFIED),
        Skill(id="s2", name="PostgreSQL", category=SkillCategory.DATABASES, verification_status=VerificationStatus.VERIFIED),
        Skill(id="s3", name="Redis", category=SkillCategory.DATABASES, verification_status=VerificationStatus.VERIFIED),
        Skill(id="s4", name="Docker", category=SkillCategory.DEVOPS, verification_status=VerificationStatus.VERIFIED),
        Skill(id="s5", name="Python", category=SkillCategory.PROGRAMMING, verification_status=VerificationStatus.VERIFIED),
    ]
    projects = [
        Project(
            id="p1",
            name="Ticket Engine",
            status="completed",
            summary="A high-throughput ticket processing backend.",
            technologies=[
                "Go",
                "PostgreSQL",
                "Redis",
                "Docker",
                "Distributed Systems",
                "Concurrency",
            ],
        ),
    ]
    experience: list = []
    preferences = Preference(
        target_roles_tier1=["Backend Engineer", "Software Engineer"],
        remote_preference="open to remote",
        hybrid_preference="open to hybrid",
        internship_preference=True,
        full_time_preference=True,
        preferred_locations=["Hyderabad", "Bengaluru", "Remote"],
    )
    return FakeCareerBrain(profile, skills, projects, experience, preferences)


# A requirement the candidate's profile genuinely satisfies (2027 is inside
# 2026-2028), used so cases A and B get a real ELIGIBLE verdict rather than
# the "nothing was stated" LIKELY_ELIGIBLE default.
_GRAD_OK = GraduationRequirement(
    minimum_year=2026, maximum_year=2028, original_text="Graduating 2026-2028"
)

# A requirement the candidate's profile cannot satisfy at all.
_GRAD_MISMATCH = GraduationRequirement(exact_years=[2024], original_text="Must graduate in 2024.")

JOB_A_DESCRIPTION = """
Minimum Qualifications
- Backend services written in Go (golang) with PostgreSQL as the primary datastore.
- Hands-on experience with Redis and Docker-based deployments.
- Comfortable working on distributed systems and high concurrency workloads.
"""

# Rust and Kafka are required skills the candidate has no evidence for, and -
# unlike Kubernetes/gRPC - neither is a curated "related" neighbor of Go, so
# they resolve to genuine gaps rather than weak adjacency matches.
JOB_B_DESCRIPTION = """
Minimum Qualifications
- Backend services written in Go (golang).
- Production experience with Rust and Kafka.
- Familiarity with PostgreSQL.
"""


def _make_job(canonical_key: str, description: str, **overrides) -> NormalizedJob:
    defaults = dict(
        canonical_key=canonical_key,
        source=JobSourceType.GREENHOUSE,
        source_job_id=canonical_key,
        company="TechCorp",
        title="Backend Engineer",
        original_title="Backend Engineer",
        source_url=f"https://example.com/jobs/{canonical_key}",
        description=description,
        employment_type=EmploymentType.FULL_TIME,
        remote_type=RemoteType.REMOTE,
    )
    defaults.update(overrides)
    return NormalizedJob(**defaults)


def _job_a(canonical_key: str = "job-a") -> NormalizedJob:
    return _make_job(canonical_key, JOB_A_DESCRIPTION, graduation_requirement=_GRAD_OK)


def _job_b(canonical_key: str = "job-b") -> NormalizedJob:
    return _make_job(canonical_key, JOB_B_DESCRIPTION, graduation_requirement=_GRAD_OK)


def _job_c(canonical_key: str = "job-c") -> NormalizedJob:
    # Same strong technical content as case A - the point is that
    # ineligibility caps the score regardless of how good the fit is.
    return _make_job(canonical_key, JOB_A_DESCRIPTION, graduation_requirement=_GRAD_MISMATCH)


def _orchestrator() -> MatchOrchestrator:
    return build_orchestrator(career_brain=_make_career_brain())


def test_golden_case_a_strong_backend_match_scores_highly():
    match = _orchestrator().evaluate_job(_job_a())

    assert match.eligibility.status == EligibilityStatus.ELIGIBLE
    assert match.match_type == MatchType.CORE_MATCH
    assert match.priority in ("P0", "P1")
    assert match.fit_score >= 75
    assert not match.gaps
    assert "Go" in match.strengths


def test_golden_case_b_stretch_match_has_real_gaps():
    match = _orchestrator().evaluate_job(_job_b())

    assert match.eligibility.status == EligibilityStatus.ELIGIBLE
    assert match.match_type == MatchType.STRETCH_MATCH
    assert "Rust" in match.gaps
    assert "Kafka" in match.gaps
    assert "Go" in match.strengths


def test_golden_case_c_ineligible_job_is_capped_low():
    match = _orchestrator().evaluate_job(_job_c())

    assert match.eligibility.status == EligibilityStatus.INELIGIBLE
    assert match.eligibility.blocking_reasons
    assert match.priority == "IGNORE"
    assert match.fit_score <= 20


def test_case_scores_are_strictly_ordered():
    """Ordering, not magic numbers, so these survive policy tuning: a clean
    eligible match must beat a real stretch, which must beat a job the
    candidate is outright ineligible for."""
    orchestrator = _orchestrator()
    score_a = orchestrator.evaluate_job(_job_a()).fit_score
    score_b = orchestrator.evaluate_job(_job_b()).fit_score
    score_c = orchestrator.evaluate_job(_job_c()).fit_score

    assert score_a > score_b > score_c


def test_scoring_is_reproducible():
    """Same job, same Career Brain, same policy - identical numbers both
    times. No randomness, no clock, no model call in the scoring path."""
    orchestrator = _orchestrator()
    job = _job_b("job-repro")

    first = orchestrator.evaluate_job(job)
    second = orchestrator.evaluate_job(job)

    assert first.fit_score == second.fit_score
    assert (
        first.generated_metadata["component_scores"]
        == second.generated_metadata["component_scores"]
    )
