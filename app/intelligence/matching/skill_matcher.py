"""Layered skill matching.

Four layers, strongest first. Each returns a typed :class:`SkillMatch` carrying
*why* it matched, so an explanation can always name its evidence:

1. **Exact** — identical after normalization.
2. **Alias/canonical** — both sides resolve to the same taxonomy entry, which
   is what makes ``React.js`` / ``ReactJS`` / ``React`` one skill.
3. **Adjacent** — a curated ``related`` edge (Go ↔ gRPC, RAG ↔ Vector
   Databases). Deliberately weaker than a real match.
4. **Lexical** — ``difflib`` ratio on normalized strings, for typos and
   spacing variants. Standard library, no dependency.
5. **Semantic** — *optional*, off by default. Requires the ``semantic`` extra
   (CPU-only sentence-transformers). It only ever *adds* matches the
   deterministic layers missed; it can never overturn them.

Why semantics are optional rather than the default: layers 1–3 already resolve
the alias problem that motivated embeddings, at zero install cost and with
fully reproducible output. A model download plus per-run inference is real
complexity, so it stays opt-in until measurement shows it earns its place.
"""

import logging
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Iterable, List, Optional, Sequence

from app.config import settings
from app.intelligence.models.enums import EvidenceStrength
from app.intelligence.taxonomy.skills import TaxonomyProvider, default_taxonomy

logger = logging.getLogger(__name__)

LEXICAL_THRESHOLD = 0.88


@dataclass
class SkillMatch:
    """One requirement-to-candidate-skill match and its provenance."""

    requirement: str
    matched_skill: Optional[str]
    strength: EvidenceStrength
    method: str
    similarity: float = 1.0

    @property
    def is_match(self) -> bool:
        return self.matched_skill is not None and self.strength is not EvidenceStrength.NONE


def _normalize(term: str) -> str:
    return re.sub(r"[\s._/-]+", "", (term or "").strip().lower())


class SemanticMatcher:
    """Optional CPU-only embedding similarity.

    Loads lazily so importing this module never pulls in torch. If the extra is
    not installed, it degrades to "no semantic matches" with a single warning —
    never an error, because the deterministic layers are the contract.
    """

    def __init__(self, model_name: Optional[str] = None, threshold: Optional[float] = None):
        self.model_name = model_name or settings.semantic_model_name
        self.threshold = (
            threshold if threshold is not None else settings.semantic_similarity_threshold
        )
        self._model = None
        self._unavailable = False

    def _load(self):
        if self._model is not None or self._unavailable:
            return self._model
        try:
            from sentence_transformers import SentenceTransformer  # noqa: PLC0415
        except ImportError:
            logger.warning(
                "SEMANTIC_MATCHING_ENABLED is on but sentence-transformers is not installed. "
                "Install the 'semantic' extra or turn the flag off. "
                "Falling back to deterministic matching only."
            )
            self._unavailable = True
            return None

        logger.info("Loading semantic matching model '%s' (CPU)", self.model_name)
        self._model = SentenceTransformer(self.model_name, device="cpu")
        return self._model

    def best_match(self, requirement: str, candidates: Sequence[str]):
        """Return ``(skill, similarity)`` above threshold, else ``(None, 0.0)``."""
        model = self._load()
        if model is None or not candidates:
            return None, 0.0

        try:
            from sentence_transformers import util  # noqa: PLC0415

            vectors = model.encode([requirement, *candidates], convert_to_tensor=True)
            scores = util.cos_sim(vectors[0], vectors[1:])[0]
            best_index = int(scores.argmax())
            best_score = float(scores[best_index])
        except Exception:  # noqa: BLE001 - never let matching crash a run
            logger.exception("Semantic matching failed; using deterministic result")
            return None, 0.0

        if best_score >= self.threshold:
            return candidates[best_index], best_score
        return None, 0.0


_semantic_singleton: Optional[SemanticMatcher] = None


def get_semantic_matcher() -> Optional[SemanticMatcher]:
    """Shared matcher instance, or ``None`` when semantics are disabled."""
    global _semantic_singleton
    if not settings.semantic_matching_enabled:
        return None
    if _semantic_singleton is None:
        _semantic_singleton = SemanticMatcher()
    return _semantic_singleton


class SkillMatcher:
    """Resolves a required skill against the candidate's skill vocabulary."""

    def __init__(
        self,
        taxonomy: Optional[TaxonomyProvider] = None,
        semantic: Optional[SemanticMatcher] = None,
    ):
        self.taxonomy = taxonomy or default_taxonomy()
        self.semantic = semantic if semantic is not None else get_semantic_matcher()

    def match(self, requirement: str, candidate_skills: Iterable[str]) -> SkillMatch:
        """Best available match for ``requirement`` among ``candidate_skills``."""
        candidates: List[str] = [s for s in candidate_skills if s]
        if not requirement or not candidates:
            return SkillMatch(requirement, None, EvidenceStrength.NONE, "none", 0.0)

        req_norm = _normalize(requirement)
        req_canonical = self.taxonomy.canonicalize(requirement)

        # Layer 1: exact (normalized) equality.
        for skill in candidates:
            if _normalize(skill) == req_norm:
                return SkillMatch(requirement, skill, EvidenceStrength.DIRECT_VERIFIED, "exact")

        # Layer 2: same canonical taxonomy entry.
        if req_canonical:
            for skill in candidates:
                if self.taxonomy.canonicalize(skill) == req_canonical:
                    return SkillMatch(
                        requirement, skill, EvidenceStrength.DIRECT_VERIFIED, "alias"
                    )

        # Layer 3: curated adjacency.
        if req_canonical:
            related = {r.lower() for r in self.taxonomy.related(req_canonical)}
            for skill in candidates:
                canonical = self.taxonomy.canonicalize(skill)
                if canonical and canonical.lower() in related:
                    return SkillMatch(
                        requirement, skill, EvidenceStrength.ADJACENT, "related", 0.6
                    )
                # Adjacency is symmetric: the candidate may list the parent.
                if canonical and req_canonical.lower() in {
                    r.lower() for r in self.taxonomy.related(canonical)
                }:
                    return SkillMatch(
                        requirement, skill, EvidenceStrength.ADJACENT, "related", 0.6
                    )

        # Layer 4: lexical similarity (typos, spacing).
        best_skill, best_ratio = None, 0.0
        for skill in candidates:
            ratio = SequenceMatcher(None, req_norm, _normalize(skill)).ratio()
            if ratio > best_ratio:
                best_skill, best_ratio = skill, ratio
        if best_skill and best_ratio >= LEXICAL_THRESHOLD:
            return SkillMatch(
                requirement, best_skill, EvidenceStrength.SUPPORTING, "lexical", best_ratio
            )

        # Layer 5: optional semantic similarity.
        if self.semantic is not None:
            skill, score = self.semantic.best_match(requirement, candidates)
            if skill:
                return SkillMatch(
                    requirement, skill, EvidenceStrength.INDIRECT, "semantic", score
                )

        return SkillMatch(requirement, None, EvidenceStrength.NONE, "none", best_ratio)
