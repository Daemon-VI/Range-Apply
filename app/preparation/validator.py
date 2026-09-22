"""Truth gate for generated content.

``TruthValidator`` (Phase 1) stays the authority on whether evidence may
back a claim; this module feeds it every CLAIM block as a ``Claim`` and adds
the structural checks the blueprint asks for: unresolved or removed
evidence, evidence that is not application-safe, numbers/years absent from
the cited evidence, and skills the taxonomy recognises that the candidate
never confirmed. Nothing is repaired by invention: a failing block is
dropped (deterministic fallback) or the package goes to review.

Cost model: per-node numbers and skills come pre-computed from the
``EvidenceSnapshot``; the only regex work per block is over the block's own
text, and the job/framing *context* is scanned once per preparation.
"""

import re
from dataclasses import dataclass, field
from typing import Optional, Union

from app.career.models import EvidenceKind
from app.intelligence.taxonomy import canonicalize, find_skills_in_text
from app.models.claim import Claim
from app.models.enums import VerificationStatus
from app.preparation.evidence import NUMBER_PATTERN, EvidenceSnapshot
from app.preparation.models import Block, BlockKind, ValidationIssue, ValidationReport
from app.services.truth_validator import TruthValidationError, TruthValidator

_YEAR = re.compile(r"\b(19|20)\d{2}\b")


@dataclass
class ValidationContext:
    """Text that may legitimately appear in a claim without being candidate
    evidence: the job's title, company and requirement wording, and the
    candidate's own positioning statement. Built once per preparation."""

    text: str = ""
    numbers: set[str] = field(default_factory=set)
    skills: set[str] = field(default_factory=set)

    @classmethod
    def from_text(cls, text: str) -> "ValidationContext":
        lowered = (text or "").lower()
        return cls(lowered, set(NUMBER_PATTERN.findall(lowered)), set(find_skills_in_text(text or "")))


_EMPTY = ValidationContext()


class PreparationValidator:
    def __init__(self, snapshot: EvidenceSnapshot, truth_validator: Optional[TruthValidator] = None):
        self.snapshot = snapshot
        self.truth = truth_validator or TruthValidator()
        # Canonical vocabulary the candidate has confirmed: the label itself
        # plus every taxonomy skill the label mentions ("DSA / Problem
        # Solving" covers "Data Structures"), so the check is canonical to canonical.
        self._confirmed_skill_canon: set[str] = set()
        for node in snapshot.of_kind(EvidenceKind.SKILL):
            self._confirmed_skill_canon.add(canonicalize(node.label) or node.label.lower())
            self._confirmed_skill_canon.update(snapshot.node_skills.get(node.key, set()))

    def make_context(self, text: str) -> ValidationContext:
        return ValidationContext.from_text(text)

    @staticmethod
    def _ctx(context: Union[str, ValidationContext, None]) -> ValidationContext:
        if context is None or context == "":
            return _EMPTY
        if isinstance(context, ValidationContext):
            return context
        return ValidationContext.from_text(context)

    def validate_block(
        self,
        block: Block,
        index: Optional[int] = None,
        context: Union[str, ValidationContext, None] = None,
    ) -> list[ValidationIssue]:
        if block.kind is BlockKind.FRAMING:
            return []
        if not block.evidence_keys:
            return [ValidationIssue(code="missing_evidence", message="claim cites no evidence", block_index=index)]

        issues: list[ValidationIssue] = []
        for key in block.evidence_keys:
            node = self.snapshot.node(key)
            if node is None:
                code = "removed_evidence" if self.snapshot.is_removed(key) else "unresolved_evidence"
                issues.append(ValidationIssue(code=code, message=f"evidence {key} is {code.replace('_', ' ')}", block_index=index, evidence_keys=[key]))
                continue
            status = VerificationStatus(node.verification_status)
            try:
                self.truth.validate_claim(
                    Claim(statement=block.text, source=key, verification_status=status, allowed_for_application=True)
                )
            except TruthValidationError as exc:
                issues.append(ValidationIssue(code="truth_validator_rejected", message=str(exc), block_index=index, evidence_keys=[key]))
                continue
            if not self.snapshot.is_safe(key):
                issues.append(
                    ValidationIssue(
                        code="unsafe_evidence",
                        message=f"evidence {key} is {status.value} and not application-safe",
                        block_index=index,
                        evidence_keys=[key],
                    )
                )
        if issues:
            return issues

        ctx = self._ctx(context)
        allowed_numbers: set[str] = set(ctx.numbers)
        allowed_skills: set[str] = set(ctx.skills)
        corpus_parts = [ctx.text]
        for key in block.evidence_keys:
            allowed_numbers |= self.snapshot.node_numbers.get(key, set())
            allowed_skills |= self.snapshot.node_skills.get(key, set())
            corpus_parts.append(self.snapshot.node_text.get(key, ""))
        corpus = "\n".join(corpus_parts)

        for number in NUMBER_PATTERN.findall(block.text.lower()):
            if number in allowed_numbers or number.rstrip("+%") in allowed_numbers:
                continue
            code = "unsupported_date" if _YEAR.fullmatch(number) else "unsupported_number"
            issues.append(ValidationIssue(code=code, message=f"{number!r} does not appear in the cited evidence", block_index=index, evidence_keys=list(block.evidence_keys)))

        for skill in find_skills_in_text(block.text):
            canon = canonicalize(skill) or skill.lower()
            if canon in self._confirmed_skill_canon or skill in allowed_skills or skill.lower() in corpus:
                continue
            issues.append(ValidationIssue(code="unsupported_skill", message=f"skill {skill!r} is not confirmed evidence", block_index=index, evidence_keys=list(block.evidence_keys)))
        return issues

    def validate_blocks(self, blocks: list[Block], context: Union[str, ValidationContext, None] = None) -> ValidationReport:
        ctx = self._ctx(context)
        issues: list[ValidationIssue] = []
        checked = 0
        for index, block in enumerate(blocks):
            if block.kind is BlockKind.CLAIM:
                checked += 1
            issues.extend(self.validate_block(block, index, ctx))
        return ValidationReport(passed=not issues, checked_blocks=checked, issues=issues)

    def drop_failing(self, blocks: list[Block], context: Union[str, ValidationContext, None] = None) -> tuple[list[Block], ValidationReport]:
        """Deterministic fallback: keep only blocks that validate; report what went."""
        ctx = self._ctx(context)
        kept: list[Block] = []
        dropped = 0
        all_issues: list[ValidationIssue] = []
        for index, block in enumerate(blocks):
            block_issues = self.validate_block(block, index, ctx)
            if block_issues:
                dropped += 1
                all_issues.extend(block_issues)
            else:
                kept.append(block)
        # The kept blocks all validate; ``passed`` reports whether anything
        # had to go, so the caller can route a lossy package to review.
        report = ValidationReport(
            passed=dropped == 0,
            checked_blocks=sum(1 for b in kept if b.kind is BlockKind.CLAIM),
            issues=all_issues,
            dropped_blocks=dropped,
        )
        return kept, report
