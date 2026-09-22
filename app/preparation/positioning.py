"""Deterministic positioning-variant selection.

A variant is chosen, never invented: the score is title-token overlap with
the variant's role family plus how much of the variant's evidence the job's
matched evidence covers. With no active variants the package falls back to
the profile's positioning statement, which is candidate-authored too.
"""

import re
from dataclasses import dataclass
from typing import Optional

from app.career.database.models import PositioningVariantRow

_STOP = {"and", "or", "of", "the", "a", "an", "for", "in", "to", "at", "with", "engineer", "engineering", "role", "roles"}
_TOKEN = re.compile(r"[a-z0-9+#]+")


def _tokens(text: str) -> set[str]:
    return {t for t in _TOKEN.findall((text or "").lower()) if t not in _STOP}


@dataclass
class PositioningChoice:
    variant: Optional[PositioningVariantRow]
    reason: str
    score: float = 0.0


def select_variant(
    variants: list[PositioningVariantRow],
    job_title: str,
    matched_keys: list[str],
) -> PositioningChoice:
    if not variants:
        return PositioningChoice(None, "no active positioning variant; using profile statement")
    title_tokens = _tokens(job_title)
    # "engineer" is stripped as a stop word, so keep the discipline words that matter.
    matched = set(matched_keys)
    best: Optional[PositioningChoice] = None
    for variant in sorted(variants, key=lambda v: ((v.updated_at or v.created_at) or 0, v.id), reverse=True):
        family_tokens = _tokens(variant.role_family) | _tokens(variant.name)
        overlap = len(title_tokens & family_tokens)
        family_score = overlap / max(1, len(family_tokens))
        keys = {e.node.key for e in variant.evidence}
        evidence_score = (len(keys & matched) / len(keys)) if keys else 0.0
        score = 2.0 * family_score + evidence_score
        reason = f"role_family={variant.role_family!r} overlap={overlap} evidence_cover={evidence_score:.2f}"
        if best is None or score > best.score:
            best = PositioningChoice(variant, reason, score)
    assert best is not None
    if best.score <= 0.0:
        # Nothing matched the title or the evidence: still a valid, candidate
        # authored variant, just a generic one. Prefer the most recently updated.
        best.reason = f"no title/evidence overlap; most recent variant {best.variant.role_family!r}"
    return best
