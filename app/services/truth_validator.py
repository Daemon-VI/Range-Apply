"""Truth and verification validation layer."""

import logging
from typing import Optional, Union

from app.models.career_fact import CareerFact
from app.models.claim import Claim
from app.models.enums import VerificationStatus


class TruthValidationError(Exception):
    """Raised when a claim fails truth validation."""

    def __init__(self, message: str, fact: Union[CareerFact, Claim, None] = None):
        super().__init__(message)
        self.fact = fact


class TruthValidator:
    """Enforces truth rules for career facts and claims."""

    @staticmethod
    def is_application_safe(status: VerificationStatus) -> bool:
        return status == VerificationStatus.VERIFIED

    @staticmethod
    def is_resume_safe(status: VerificationStatus) -> bool:
        return status == VerificationStatus.VERIFIED

    def validate_fact(self, fact: CareerFact) -> CareerFact:
        if fact.verification_status == VerificationStatus.CONFLICT:
            raise TruthValidationError(
                f"Conflicting fact cannot be used: {fact.statement}",
                fact=fact,
            )

        if fact.verification_status == VerificationStatus.INFERRED:
            if fact.allowed_for_application or fact.allowed_for_resume:
                raise TruthValidationError(
                    f"Inferred fact cannot be promoted to safe: {fact.statement}",
                    fact=fact,
                )

        if fact.verification_status in (
            VerificationStatus.UNVERIFIED,
            VerificationStatus.NEEDS_REVIEW,
        ):
            if fact.allowed_for_application:
                raise TruthValidationError(
                    f"Unverified fact cannot be application-safe: {fact.statement}",
                    fact=fact,
                )

        return fact

    def validate_claim(self, claim: Claim) -> Claim:
        if claim.verification_status == VerificationStatus.CONFLICT:
            raise TruthValidationError(
                f"Conflicting claim rejected: {claim.statement}",
                fact=claim,
            )

        if claim.verification_status == VerificationStatus.INFERRED:
            if claim.allowed_for_application:
                raise TruthValidationError(
                    f"Inferred claim cannot be application-safe: {claim.statement}",
                    fact=claim,
                )

        if claim.verification_status in (
            VerificationStatus.UNVERIFIED,
            VerificationStatus.NEEDS_REVIEW,
        ):
            if claim.allowed_for_application:
                raise TruthValidationError(
                    f"Unsupported claim rejected: {claim.statement}",
                    fact=claim,
                )

        return claim

    def filter_application_safe(self, facts: list[CareerFact]) -> list[CareerFact]:
        safe = []
        for fact in facts:
            try:
                validated = self.validate_fact(fact)
                if validated.allowed_for_application:
                    safe.append(validated)
            except TruthValidationError:
                continue
        return safe

    def filter_resume_safe(self, facts: list[CareerFact]) -> list[CareerFact]:
        safe = []
        for fact in facts:
            try:
                validated = self.validate_fact(fact)
                if validated.allowed_for_resume:
                    safe.append(validated)
            except TruthValidationError:
                continue
        return safe

    def surface_conflicts(self, facts: list[CareerFact]) -> list[CareerFact]:
        return [f for f in facts if f.verification_status == VerificationStatus.CONFLICT]

    def surface_needs_review(self, facts: list[CareerFact]) -> list[CareerFact]:
        return [f for f in facts if f.verification_status == VerificationStatus.NEEDS_REVIEW]
