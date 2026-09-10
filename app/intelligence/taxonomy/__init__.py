"""Skill taxonomy used by requirement extraction and matching.

Taxonomy choice (evaluated per the PRD's free-tier rule):

* **ESCO** — open, multilingual, ~13k skills. Strong on occupations and soft
  skills, weak on concrete engineering tokens (no "FastAPI", "pgvector",
  "Ashby"). Ships as a multi-MB dataset needing ingestion.
* **O*NET** — public domain, US-centric, occupation-level. Same gap: it
  describes job families, not the technology vocabulary a JD actually uses.
* **Lightcast Open Skills** — best raw coverage, but a hosted API requiring a
  key and a network round trip in the ingestion hot path.

None of them fit the deterministic, offline, zero-dependency ingestion path we
need today, so this module carries a curated local taxonomy with explicit
aliases. :class:`TaxonomyProvider` is the seam an ESCO/Lightcast-backed
provider can be plugged into later (most useful in P4 for role-family mapping)
without touching the matcher or extractor.
"""

from app.intelligence.taxonomy.skills import (
    SKILL_TAXONOMY,
    SkillEntry,
    TaxonomyProvider,
    canonicalize,
    default_taxonomy,
    find_skills_in_text,
)

__all__ = [
    "SKILL_TAXONOMY",
    "SkillEntry",
    "TaxonomyProvider",
    "canonicalize",
    "default_taxonomy",
    "find_skills_in_text",
]
