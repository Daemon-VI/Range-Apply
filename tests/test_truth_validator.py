"""Tests for truth validation layer."""

import pytest

from app.models.career_fact import CareerFact
from app.models.claim import Claim
from app.models.enums import FactCategory, VerificationStatus
from app.services.truth_validator import TruthValidationError, TruthValidator


@pytest.fixture
def validator():
    return TruthValidator()


class TestTruthValidator:
    def test_verified_fact_accepted(self, validator):
        fact = CareerFact(
            id="f1",
            category=FactCategory.EDUCATION,
            statement="CGPA 8.52/10",
            verification_status=VerificationStatus.VERIFIED,
            allowed_for_resume=True,
            allowed_for_application=True,
        )
        result = validator.validate_fact(fact)
        assert result.allowed_for_application

    def test_unverified_fact_flagged(self, validator):
        fact = CareerFact(
            id="f2",
            category=FactCategory.METRIC,
            statement="3k req/sec",
            verification_status=VerificationStatus.UNVERIFIED,
            allowed_for_application=True,
        )
        with pytest.raises(TruthValidationError):
            validator.validate_fact(fact)

    def test_inferred_fact_cannot_be_application_safe(self, validator):
        fact = CareerFact(
            id="f3",
            category=FactCategory.IDENTITY,
            statement="Inferred location",
            verification_status=VerificationStatus.INFERRED,
            allowed_for_application=True,
        )
        with pytest.raises(TruthValidationError):
            validator.validate_fact(fact)

    def test_conflicting_fact_surfaced(self, validator):
        fact = CareerFact(
            id="f4",
            category=FactCategory.METRIC,
            statement="Conflicting accuracy",
            verification_status=VerificationStatus.CONFLICT,
        )
        with pytest.raises(TruthValidationError):
            validator.validate_fact(fact)

    def test_unsupported_claim_rejected(self, validator):
        claim = Claim(
            statement="Fabricated achievement",
            verification_status=VerificationStatus.UNVERIFIED,
            allowed_for_application=True,
        )
        with pytest.raises(TruthValidationError):
            validator.validate_claim(claim)

    def test_filter_application_safe(self, validator):
        facts = [
            CareerFact(
                id="f1",
                category=FactCategory.EDUCATION,
                statement="Verified fact",
                verification_status=VerificationStatus.VERIFIED,
                allowed_for_application=True,
            ),
            CareerFact(
                id="f2",
                category=FactCategory.METRIC,
                statement="Unverified metric",
                verification_status=VerificationStatus.UNVERIFIED,
                allowed_for_application=False,
            ),
        ]
        safe = validator.filter_application_safe(facts)
        assert len(safe) == 1
        assert safe[0].id == "f1"

    def test_surface_conflicts(self, validator):
        facts = [
            CareerFact(
                id="f1",
                category=FactCategory.METRIC,
                statement="Conflict A",
                verification_status=VerificationStatus.CONFLICT,
            ),
            CareerFact(
                id="f2",
                category=FactCategory.EDUCATION,
                statement="Verified",
                verification_status=VerificationStatus.VERIFIED,
            ),
        ]
        conflicts = validator.surface_conflicts(facts)
        assert len(conflicts) == 1
