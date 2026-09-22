"""Geographic targeting (2026-09-14).

A candidate in Hyderabad was shown a US-heavy opportunity pool: locations were
opaque strings, the eligibility location gate only fired for postings
extracted as ON_SITE, and nothing in admission looked at geography. These tests
pin the distinctions the fix draws — primary target, India remote, other Indian
cities, international, unconfirmed — and that work authorization is never
inferred from a city.
"""

from dataclasses import dataclass
from datetime import date
from typing import Optional

import pytest

from app.intelligence.models.enums import EligibilityStatus
from app.intelligence.services.eligibility_engine import EligibilityEngine, EligibilityEvaluation
from app.intelligence.services.fit_scoring_engine import UNRELATED_ROLE_FIT_CAP, FitScoringEngine
from app.intelligence.services.preference_evaluator import PreferenceEvaluator
from app.jobs.geography import (
    DiscoveryGeography,
    GeoClass,
    GeographyPolicy,
    GeoTier,
    policy_from_preferences,
)
from app.jobs.models.enums import EmploymentType, JobSourceType, RemoteType
from app.jobs.models.job import NormalizedJob
from app.models.preference import Preference
from app.pipeline.gates import Gate, evaluate_gates
from app.pipeline.models import AdmissionReason, ApplicationPolicy, EligibilityDecision, FitBand
from app.pipeline.policy import evaluate_admission

HYDERABAD = "Hyderabad, Telangana, India"


def policy(**preferences) -> GeographyPolicy:
    return policy_from_preferences(Preference(**preferences), HYDERABAD)


def assess(location: Optional[str], geography: Optional[GeographyPolicy] = None, **metadata):
    return (geography or policy()).assess_job(location, None, metadata or None)


@dataclass
class Candidate:
    graduation_year: Optional[int] = 2027
    location: Optional[str] = HYDERABAD
    work_authorization: Optional[str] = None


class Brain:
    def __init__(self, profile: Candidate):
        self._profile = profile

    def get_profile(self):
        return self._profile

    def get_skills(self):
        return []

    def get_projects(self):
        return []

    def get_experience(self):
        return []

    def get_preferences(self):
        return None


def job(**overrides) -> NormalizedJob:
    defaults = dict(
        canonical_key="geo-1",
        source=JobSourceType.GREENHOUSE,
        source_job_id="1",
        company="Acme",
        title="Software Engineer",
        original_title="Software Engineer",
        source_url="https://example.com/job/1",
        description="",
        original_description="",
    )
    defaults.update(overrides)
    if defaults.get("location") and "locations" not in overrides:
        defaults["locations"] = [defaults["location"]]
    return NormalizedJob(**defaults)


def eligibility(posting: NormalizedJob, candidate: Optional[Candidate] = None) -> EligibilityEvaluation:
    return EligibilityEngine(Brain(candidate or Candidate()), today=lambda: date(2026, 9, 14)).evaluate_detailed(posting)


# ------------------------------------------------------------------ #
# classification
# ------------------------------------------------------------------ #


@pytest.mark.parametrize(
    "location",
    ["Hyderabad", "Hyderabad, Telangana", "Hyderabad, India", "Hyderabad, Telangana, India", "Secunderabad", "Gachibowli, Hyderabad", "Telangana, India", "Hyderabad, IND", "Hyderabad Metropolitan Area"],
)
def test_hyderabad_variants_are_primary(location):
    result = assess(location)
    assert result.geo_class is GeoClass.PRIMARY and result.tier is GeoTier.PRIMARY and result.in_policy


@pytest.mark.parametrize("location", ["Remote - India", "India Remote", "Remote, India", "Anywhere in India"])
def test_india_remote_is_a_secondary_target_that_can_be_switched_off(location):
    result = assess(location)
    assert result.geo_class is GeoClass.COUNTRY_REMOTE and result.tier is GeoTier.SECONDARY and result.in_policy
    switched_off = assess(location, policy(location_include_country_remote=False))
    assert not switched_off.in_policy and switched_off.tier is GeoTier.EXCLUDED


def test_bangalore_is_not_primary_but_a_valid_india_secondary_when_configured():
    default = assess("Bangalore, Karnataka")
    assert default.geo_class is GeoClass.COUNTRY_OTHER and default.tier is GeoTier.EXCLUDED and not default.in_policy
    assert not default.excluded_at_discovery, "another Indian city is kept at discovery; only admission follows the preference"
    assert assess("Bengaluru", policy(location_include_other_cities=True)).tier is GeoTier.SECONDARY
    assert assess("Bengaluru", policy(preferred_locations=["Bengaluru"])).tier is GeoTier.SECONDARY
    assert not assess("Pune", policy(preferred_locations=["Bengaluru"])).in_policy


@pytest.mark.parametrize("location", ["San Francisco, CA", "Foster City, CA", "New York, NY", "Seattle", "United States", "NYC (SoHo)", "Indianapolis, IN"])
def test_us_only_posting_is_not_relevant_by_default(location):
    result = assess(location)
    assert result.geo_class is GeoClass.FOREIGN and result.tier is GeoTier.EXCLUDED
    assert not result.in_policy and result.excluded_at_discovery


@pytest.mark.parametrize("location", ["Remote - US", "US Remote", "US-Remote", "United States Remote", "Remote - North America"])
def test_us_remote_is_not_india_remote(location):
    result = assess(location)
    assert result.geo_class is GeoClass.FOREIGN_REMOTE
    assert result.geo_class is not GeoClass.COUNTRY_REMOTE and not result.in_policy


def test_source_remote_flag_and_secondary_locations_are_read():
    assert assess("Bengaluru", secondaryLocations=[{"location": "Hyderabad, India"}]).geo_class is GeoClass.PRIMARY
    assert assess("India", workplaceType="Remote").geo_class is GeoClass.COUNTRY_REMOTE
    assert assess("India").geo_class is GeoClass.COUNTRY_OTHER
    assert assess("Hyderabad or Remote (US)").geo_class is GeoClass.PRIMARY


@pytest.mark.parametrize("location", [None, "", "N", "LOCATION"])
def test_missing_location_is_unconfirmed_never_hyderabad(location):
    result = assess(location)
    assert result.geo_class is GeoClass.UNKNOWN and result.tier is GeoTier.UNCONFIRMED
    assert not assess(location, policy(location_include_unconfirmed=False)).in_policy


def test_remote_without_a_country_is_unconfirmed_not_india():
    result = assess("Remote")
    assert result.geo_class is GeoClass.REMOTE_UNSPECIFIED and result.tier is GeoTier.UNCONFIRMED
    assert "not confirmed" in result.detail


def test_changing_the_target_is_configuration_not_code():
    bangalore = policy_from_preferences(Preference(location_primary="Bangalore"), HYDERABAD)
    assert bangalore.source == "preferences"
    assert bangalore.assess_job("Bengaluru, Karnataka").tier is GeoTier.PRIMARY
    assert bangalore.assess_job("Hyderabad").geo_class is GeoClass.COUNTRY_OTHER
    pune = policy_from_preferences(Preference(location_primary="Pune, Maharashtra"), HYDERABAD)
    assert pune.assess_job("Hinjewadi, Pune").tier is GeoTier.PRIMARY
    outside_gazetteer = policy_from_preferences(Preference(location_primary="Nizamabad, Telangana, India"), None)
    assert outside_gazetteer.assess_job("Nizamabad").tier is GeoTier.PRIMARY
    assert outside_gazetteer.assess_job("Hyderabad").geo_class is GeoClass.COUNTRY_OTHER
    from_profile = policy_from_preferences(Preference(), HYDERABAD)
    assert from_profile.source == "profile" and from_profile.target.metro == "Hyderabad"
    no_target = policy_from_preferences(Preference(), None)
    assert not no_target.active and no_target.assess_job("San Francisco, CA").in_policy


def test_international_remains_available_when_explicitly_enabled():
    international = policy(location_allow_international=True)
    for location in ("San Francisco, CA", "US Remote", "London, United Kingdom"):
        result = international.assess_job(location)
        assert result.in_policy and result.tier is GeoTier.INTERNATIONAL and not result.excluded_at_discovery
    assert international.assess_job("Hyderabad").tier is GeoTier.PRIMARY
    assert policy(preferred_locations=["Singapore"]).assess_job("Singapore").in_policy
    assert DiscoveryGeography((policy(), international)).excluded("San Francisco, CA") is None
    assert DiscoveryGeography((policy(),)).excluded("San Francisco, CA") is not None
    assert DiscoveryGeography((policy(),)).excluded("Remote") is None, "unconfirmed postings are kept"


# ------------------------------------------------------------------ #
# eligibility: hard facts, never inferred from a city
# ------------------------------------------------------------------ #


def test_us_job_is_never_likely_because_title_and_remote_fit():
    for location, mode in (("San Francisco, CA", RemoteType.HYBRID), ("US Remote", RemoteType.REMOTE), ("New York, NY", RemoteType.UNKNOWN)):
        result = eligibility(job(location=location, remote_type=mode))
        assert result.status is EligibilityStatus.UNCERTAIN, location
        assert any("not assumed" in reason for reason in result.uncertainties)


@pytest.mark.parametrize("location", ["Hyderabad, Telangana, India", "Secunderabad", "Hyderabad, IND", "Remote - India"])
def test_hyderabad_job_in_any_representation_is_not_rejected(location):
    result = eligibility(job(location=location, remote_type=RemoteType.UNKNOWN))
    assert result.status is EligibilityStatus.ELIGIBLE and not result.blocking_reasons


def test_other_indian_city_is_uncertain_for_relocation_never_blocked():
    result = eligibility(job(location="Bengaluru", remote_type=RemoteType.HYBRID))
    assert result.status is EligibilityStatus.UNCERTAIN
    assert any("Relocation willingness is not recorded" in reason for reason in result.uncertainties)


def test_candidate_profile_location_drives_eligibility():
    bangalore_job = job(location="Bengaluru", remote_type=RemoteType.ON_SITE)
    assert eligibility(bangalore_job, Candidate(location="Bengaluru, Karnataka, India")).status is EligibilityStatus.ELIGIBLE
    assert eligibility(bangalore_job, Candidate(location=HYDERABAD)).status is EligibilityStatus.UNCERTAIN


def test_international_posting_requiring_local_authorization():
    text = "Applicants must be authorized to work in the United States without visa sponsorship."
    us = job(location="Seattle, WA", description=text, original_description=text)
    recorded_indian = eligibility(us, Candidate(work_authorization="Indian citizen"))
    assert recorded_indian.status is EligibilityStatus.UNCERTAIN, "'citizen' used to satisfy any country's requirement"
    assert any("names only India" in reason for reason in recorded_indian.uncertainties)
    unrecorded = eligibility(us, Candidate(work_authorization=None))
    assert unrecorded.status is EligibilityStatus.UNCERTAIN and not unrecorded.blocking_reasons
    local = "You must be authorized to work in India."
    india = eligibility(job(location="Hyderabad", description=local, original_description=local), Candidate(work_authorization="Indian citizen"))
    assert india.status is EligibilityStatus.ELIGIBLE


def test_missing_or_unreadable_location_is_never_claimed_as_hyderabad():
    missing = eligibility(job(location=None, remote_type=RemoteType.REMOTE))
    assert not any("Hyderabad" in reason for reason in missing.eligibility_reasons)
    unreadable = eligibility(job(location="N"))
    assert unreadable.status is EligibilityStatus.UNCERTAIN
    remote_only = eligibility(job(location="Remote", remote_type=RemoteType.REMOTE))
    assert remote_only.status is EligibilityStatus.UNCERTAIN


# ------------------------------------------------------------------ #
# preference, fit and admission
# ------------------------------------------------------------------ #


def _location_signal(assessment):
    return next(signal for signal in assessment.signals if signal.name == "location")


def test_location_preference_ranks_hyderabad_above_us_including_us_remote():
    preferences = Preference(target_roles_tier1=["Software Engineer"])
    evaluator = PreferenceEvaluator()
    hyderabad = evaluator.evaluate(job(location="Hyderabad"), preferences, profile_location=HYDERABAD)
    us = evaluator.evaluate(job(location="San Francisco, CA"), preferences, profile_location=HYDERABAD)
    us_remote = evaluator.evaluate(job(location="US Remote", remote_type=RemoteType.REMOTE), preferences, profile_location=HYDERABAD)
    assert _location_signal(hyderabad).score == 1.0 and _location_signal(us).score == 0.0
    assert _location_signal(us_remote).score == 0.0, "a remote US role used to score 0.8 as 'location is moot'"
    assert hyderabad.score > us.score


def test_unrelated_discipline_cannot_reach_the_medium_band_on_generic_signals():
    preferences = Preference(target_roles_tier1=["Machine Learning Engineer", "Software Engineer"], target_domains=["Production AI/ML"])
    text = "We are looking for a Motion Designer to craft production-quality animation for brand launches and social channels. " * 2
    evaluator, engine = PreferenceEvaluator(), FitScoringEngine()
    context = {"description_length": len(text), "technical_requirement_count": 0}

    motion = job(title="Motion Designer", original_title="Motion Designer", location="Hyderabad", description=text, original_description=text, employment_type=EmploymentType.FULL_TIME)
    motion_preferences = evaluator.evaluate(motion, preferences, profile_location=HYDERABAD)
    assert motion_preferences.role_family_matched is False
    capped = engine.score([], eligibility(motion), motion_preferences, job_context=context)
    assert capped.fit_score == UNRELATED_ROLE_FIT_CAP
    assert any("capped" in note for note in capped.uncertainties)

    engineer = job(title="Software Engineer", location="Hyderabad", description=text, original_description=text, employment_type=EmploymentType.FULL_TIME)
    engineer_preferences = evaluator.evaluate(engineer, preferences, profile_location=HYDERABAD)
    assert engineer_preferences.role_family_matched is True
    assert engine.score([], eligibility(engineer), engineer_preferences, job_context=context).fit_score > UNRELATED_ROLE_FIT_CAP


def test_admission_gate_keeps_postings_outside_the_target_out():
    application_policy = ApplicationPolicy(tenant_id="t")
    us = policy().assess_job("San Francisco, CA")
    report = evaluate_gates(application_policy, EligibilityDecision.LIKELY, FitBand.HIGH, "Acme", 90, geography=us)
    assert report.failed.gate is Gate.GEOGRAPHY and report.code is AdmissionReason.OUTSIDE_TARGET_GEOGRAPHY
    assert evaluate_gates(application_policy, EligibilityDecision.LIKELY, FitBand.HIGH, "Acme", 90, geography=policy().assess_job("Hyderabad")).passed
    assert evaluate_gates(application_policy, EligibilityDecision.LIKELY, FitBand.HIGH, "Acme", 90, geography=None).passed
    admission = evaluate_admission(application_policy, EligibilityDecision.LIKELY, FitBand.HIGH, "Acme", 90, geography=us)
    assert not admission.admitted and admission.reason.startswith("outside_target_geography:EXCLUDED")


def test_office_names_are_a_fallback_never_a_correction():
    # Real run: a Bangalore role filed under the "India - Remote" Greenhouse office.
    filed = assess("Bangalore, India", offices=[{"name": "India - Remote"}])
    assert filed.geo_class is GeoClass.COUNTRY_OTHER, "the office name must not turn a Bangalore role into India Remote"
    assert assess(None, offices=[{"name": "Hyderabad - India"}]).geo_class is GeoClass.PRIMARY
    assert assess("Bengaluru", locations=None, secondaryLocations=None).geo_class is GeoClass.COUNTRY_OTHER


def test_unread_description_cannot_reach_the_high_band():
    from app.intelligence.services.fit_scoring_engine import LOW_COVERAGE_FIT_CAP

    text = "Join our networking team in Hyderabad to keep our cloud connected and our customers happy. " * 2
    posting = job(title="Cloud Network Engineer II", location="Hyderabad", description=text, original_description=text, employment_type=EmploymentType.FULL_TIME)
    preferences = PreferenceEvaluator().evaluate(posting, Preference(target_roles_tier1=["Cloud Network Engineer"]), profile_location=HYDERABAD)
    result = FitScoringEngine().score([], eligibility(posting), preferences, job_context={"description_length": len(text), "technical_requirement_count": 0})
    assert result.fit_score == LOW_COVERAGE_FIT_CAP and LOW_COVERAGE_FIT_CAP < 70


def test_seniority_band_in_parentheses_blocks_a_student():
    # Real run: HighRadius "Associate Architect (9 - 12 Years)" was ELIGIBLE for a student.
    text = "<p>We are looking for a highly skilled <strong>Associate Architect (9 - 12 Years)</strong> for our Cloud team.</p>"
    architect = eligibility(job(title="Associate Architect - Site Reliability", location="Hyderabad", description=text, original_description=text))
    assert architect.status is EligibilityStatus.INELIGIBLE
    assert any("9+ years" in reason for reason in architect.blocking_reasons)
    plus = "Bring 8+ years in VMware and Windows administration."
    assert eligibility(job(location="Hyderabad", description=plus, original_description=plus)).status is EligibilityStatus.INELIGIBLE
    clean = "Graduates welcome; we teach you everything in your first 2 weeks."
    assert eligibility(job(location="Hyderabad", description=clean, original_description=clean)).status is EligibilityStatus.ELIGIBLE
