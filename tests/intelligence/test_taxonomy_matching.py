"""Regression and quality tests for the skill taxonomy, the layered skill
matcher and the section-aware requirement extractor.

The taxonomy this exercises replaced a ~60-keyword dictionary that flagged
"we go fast" as the Go language, "the rest of the team" as REST APIs, and
"Spring semester" as Spring Boot - false positives that became REQUIRED
requirements downstream and made the fit score measure noise instead of
signal. These tests pin that fix down.
"""

import pytest

from app.intelligence.extraction.requirement_extractor import RequirementExtractor
from app.intelligence.matching.skill_matcher import SkillMatcher, get_semantic_matcher
from app.intelligence.models.enums import EvidenceStrength, RequirementCategory, Strictness
from app.intelligence.taxonomy.skills import canonicalize, find_skills_in_text
from app.jobs.models.enums import JobSourceType
from app.jobs.models.job import NormalizedJob

# --------------------------------------------------------------------- #
# False-positive regressions: ambiguous English must not become a skill.
# --------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "text",
    [
        "we go fast and the rest of the team ships in Spring semester",
        "you will go through onboarding",
        "rest assured, we'll get back to you soon",
        "let's go over the agenda before the rest of the meeting",
    ],
)
def test_ambiguous_tokens_produce_no_phantom_skills(text):
    """Regression: bare 'go'/'rest'/'spring' used to match Go/REST APIs/Spring Boot."""
    found = find_skills_in_text(text)
    assert "Go" not in found
    assert "REST APIs" not in found
    assert "Spring Boot" not in found


# --------------------------------------------------------------------- #
# True positives: the same ambiguous tokens, in real technical context.
# --------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "text, expected_skill",
    [
        ("Our services in Go (golang) power the checkout flow.", "Go"),
        ("Experience with Python and Go is a plus.", "Go"),
        ("Comfortable in Go/Rust for systems work.", "Go"),
        ("You will design REST APIs for internal consumers.", "REST APIs"),
        ("Strong experience with Java and Spring Boot.", "Spring Boot"),
        ("Building RAG pipelines over LLMs with vector embeddings.", "RAG"),
    ],
)
def test_ambiguous_tokens_recognized_in_real_context(text, expected_skill):
    found = find_skills_in_text(text)
    assert expected_skill in found


# --------------------------------------------------------------------- #
# canonicalize()
# --------------------------------------------------------------------- #


@pytest.mark.parametrize("term", ["React.js", "ReactJS", "react", "REACT"])
def test_react_family_canonicalizes_uniformly(term):
    assert canonicalize(term) == "React"


def test_k8s_canonicalizes_to_kubernetes():
    assert canonicalize("k8s") == "Kubernetes"


def test_postgres_canonicalizes_to_postgresql():
    assert canonicalize("postgres") == "PostgreSQL"


def test_nodejs_canonicalizes_to_node_js():
    assert canonicalize("nodejs") == "Node.js"


def test_unknown_term_canonicalizes_to_none():
    assert canonicalize("quantum flux capacitor") is None


def test_react_dot_js_does_not_also_match_javascript():
    """Regression: the 'js' alias must not match inside 'React.js' - that is
    a React mention, not a separate JavaScript one."""
    found = find_skills_in_text("Built with React.js on the frontend.")
    assert "React" in found
    assert "JavaScript" not in found


# --------------------------------------------------------------------- #
# SkillMatcher layers
# --------------------------------------------------------------------- #


def test_matcher_exact_match():
    result = SkillMatcher().match("Go", ["Go", "Python"])
    assert result.method == "exact"
    assert result.strength == EvidenceStrength.DIRECT_VERIFIED


def test_matcher_alias_match():
    result = SkillMatcher().match("ReactJS", ["React"])
    assert result.method == "alias"
    assert result.strength == EvidenceStrength.DIRECT_VERIFIED


def test_matcher_curated_related_edge_is_weaker_than_a_real_match():
    """Go's curated 'related' set includes Kubernetes - a candidate who
    lists Go is weak, adjacent evidence for a Kubernetes requirement, never
    a direct claim."""
    result = SkillMatcher().match("Kubernetes", ["Go"])
    assert result.method == "related"
    assert result.strength == EvidenceStrength.ADJACENT
    assert result.strength != EvidenceStrength.DIRECT_VERIFIED


def test_matcher_lexical_near_typo():
    result = SkillMatcher().match("Kubernetes", ["Kubernets"])
    assert result.method == "lexical"
    assert result.is_match


def test_matcher_no_match_for_absent_skill():
    result = SkillMatcher().match("Kubernetes", ["Cooking", "Painting"])
    assert not result.is_match
    assert result.strength == EvidenceStrength.NONE


def test_semantic_matching_is_off_by_default():
    """The deterministic layers must work with zero extra install: semantic
    matching only activates when explicitly turned on in settings."""
    assert get_semantic_matcher() is None


# --------------------------------------------------------------------- #
# RequirementExtractor: section awareness
# --------------------------------------------------------------------- #

_SECTION_AWARE_JD = """
Minimum Qualifications
- Experience with Python and PostgreSQL.

Nice to have
- Experience with Kubernetes.

Benefits
- We offer Docker-based development environments and free lunch.
"""


def _make_job(description: str, canonical_key: str = "job-x") -> NormalizedJob:
    return NormalizedJob(
        canonical_key=canonical_key,
        source=JobSourceType.GREENHOUSE,
        source_job_id=canonical_key,
        company="Acme",
        title="Backend Engineer",
        original_title="Backend Engineer",
        source_url=f"https://example.com/{canonical_key}",
        description=description,
    )


def test_section_awareness_drives_strictness():
    job = _make_job(_SECTION_AWARE_JD)
    requirements = RequirementExtractor().extract(job)
    skills_by_name = {
        r.normalized_name: r
        for r in requirements
        if r.category == RequirementCategory.TECHNICAL_SKILL
    }

    assert skills_by_name["Python"].strictness == Strictness.REQUIRED
    assert skills_by_name["PostgreSQL"].strictness == Strictness.REQUIRED
    assert skills_by_name["Kubernetes"].strictness == Strictness.PREFERRED
    # Mentioned only under Benefits - not a requirement at all.
    assert "Docker" not in skills_by_name


def test_requirement_ids_are_stable_across_extractions():
    """Random ids would make two runs over an unchanged job produce
    "different" requirements, which breaks score reproducibility."""
    job = _make_job(_SECTION_AWARE_JD)
    extractor = RequirementExtractor()
    first_ids = {r.id for r in extractor.extract(job)}
    second_ids = {r.id for r in extractor.extract(job)}
    assert first_ids
    assert first_ids == second_ids


# --------------------------------------------------------------------- #
# RequirementExtractor: experience duration and the degree-regex regression
# --------------------------------------------------------------------- #


def test_years_of_experience_extracted_as_months():
    job = _make_job(
        "Minimum Qualifications\n- 3+ years of experience building backend services.",
        canonical_key="job-years",
    )
    requirements = RequirementExtractor().extract(job)
    experience_reqs = [r for r in requirements if r.category == RequirementCategory.EXPERIENCE]
    assert any(r.experience_duration_months == 36 for r in experience_reqs)


def test_work_authorization_phrase_does_not_produce_education_requirement():
    """Regression: the old degree regex's bare 'b.?\\s?e' alternative
    matched the plain word 'be', turning a work-authorization sentence into
    a bogus Bachelor's degree requirement."""
    job = _make_job(
        "Minimum Qualifications\n- Must be legally authorized to work in the country of employment.",
        canonical_key="job-workauth",
    )
    requirements = RequirementExtractor().extract(job)
    assert not any(r.category == RequirementCategory.EDUCATION for r in requirements)
