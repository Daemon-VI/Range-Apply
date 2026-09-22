"""Opportunity identity is exact, normalised, and coarser than canonical_key."""

from app.pipeline.identity import (
    OpportunityIdentity,
    location_bucket,
    normalize_title_for_identity,
)


def test_title_normalisation_strips_noise_but_keeps_seniority():
    assert normalize_title_for_identity("Backend Engineer (Remote)") == "backend engineer"
    assert normalize_title_for_identity("Backend Engineer - Req 12345") == "backend engineer"
    assert normalize_title_for_identity("Backend   Engineer ") == "backend engineer"
    assert normalize_title_for_identity("Senior Backend Engineer") != normalize_title_for_identity("Backend Engineer")


def test_location_bucket():
    assert location_bucket("Remote - US", "UNKNOWN") == "remote"
    assert location_bucket("Hyderabad, India", "REMOTE") == "remote"
    assert location_bucket("Hyderabad, Telangana, India", "ON_SITE") == "hyderabad"
    assert location_bucket("Bangalore (Hybrid)", "HYBRID") == "bangalore"
    assert location_bucket(None, None) == ""


def test_identity_key_is_deterministic_and_discriminating():
    a = OpportunityIdentity("acme", "backend engineer", "remote")
    b = OpportunityIdentity("acme", "backend engineer", "remote")
    c = OpportunityIdentity("acme", "backend engineer", "hyderabad")
    assert a.key == b.key
    assert a.key != c.key
    assert len(a.key) == 64
