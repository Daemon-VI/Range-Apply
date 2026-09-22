"""Career Brain service — the stable read interface over candidate evidence.

Phase 1 of the blueprint moved the data underneath this class from a JSON
file into the tenant-scoped Evidence Graph (``app/career``). The public
surface every consumer relies on (``get_profile``, ``get_skills``,
``get_projects``, ``get_experience``, ``get_preferences``, ``get_facts``, the
``*_safe`` filters, search and summary) is unchanged.

Two modes:

* **Database mode** (default, ``CareerBrainService()``): reads the tenant's
  graph. If the tenant has no profile yet, the seed file at
  ``settings.career_data_path`` is imported first (bootstrap), so a fresh
  clone still works with zero manual steps. Pass ``db=`` to reuse a request
  session, otherwise a short-lived session is opened for the load.
* **Legacy JSON mode** (``CareerBrainService(data_path=...)``): reads the
  file directly, read-only. Kept for fixtures and tests that supply their
  own seed; nothing in the app uses it at runtime.

Both modes materialise a snapshot in memory on ``load()``; call ``reload()``
after a write to see it.
"""

import json
import logging
from pathlib import Path
from typing import Optional, Union

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    Achievement,
    CareerFact,
    Experience,
    Preference,
    Profile,
    Project,
    Skill,
)
from app.models.enums import VerificationStatus
from app.services.truth_validator import TruthValidator

logger = logging.getLogger(__name__)


class CareerSummary(BaseModel):
    profile: Profile
    skills_count: int
    verified_skills_count: int
    projects_count: int
    experience_count: int
    achievements_count: int
    verified_achievements_count: int
    verified_facts_count: int
    application_safe_facts_count: int
    needs_review_count: int
    conflict_count: int
    target_roles: list[str] = Field(default_factory=list)
    top_projects: list[str] = Field(default_factory=list)


class CareerBrainService:
    """Stable interface for accessing structured career information."""

    def __init__(
        self,
        data_path: Union[str, Path, None] = None,
        validator: Optional[TruthValidator] = None,
        *,
        db: Optional[Session] = None,
        tenant_id: Optional[str] = None,
    ):
        self._data_path = Path(data_path or settings.career_data_path)
        self._legacy_json = data_path is not None
        self._db = db
        self._tenant_id = tenant_id or settings.default_tenant_id
        self._validator = validator or TruthValidator()
        self._profile: Optional[Profile] = None
        self._skills: list[Skill] = []
        self._projects: list[Project] = []
        self._experience: list[Experience] = []
        self._achievements: list[Achievement] = []
        self._preferences: Optional[Preference] = None
        self._facts: list[CareerFact] = []
        self._loaded = False

    # ------------------------------------------------------------------ #
    # loading
    # ------------------------------------------------------------------ #

    @property
    def tenant_id(self) -> str:
        return self._tenant_id

    @property
    def mode(self) -> str:
        return "json" if self._legacy_json else "db"

    def load(self) -> None:
        if self._legacy_json:
            self._load_json()
        else:
            self._load_db()
        self._loaded = True

    def reload(self) -> None:
        self._loaded = False
        self.load()

    def _load_json(self) -> None:
        if not self._data_path.exists():
            raise FileNotFoundError(f"Career data file not found: {self._data_path}")
        with open(self._data_path, encoding="utf-8") as f:
            data = json.load(f)
        self._profile = Profile(**data["profile"])
        self._skills = [Skill(**s) for s in data.get("skills", [])]
        self._projects = [Project(**p) for p in data.get("projects", [])]
        self._experience = [Experience(**e) for e in data.get("experience", [])]
        self._achievements = [Achievement(**a) for a in data.get("achievements", [])]
        self._preferences = Preference(**data["preferences"])
        self._facts = [CareerFact(**f) for f in data.get("facts", [])]
        logger.info("Career data loaded from %s (legacy JSON mode)", self._data_path)

    def _load_db(self) -> None:
        # Local imports keep app.models/app.services importable without the
        # database layer (fixtures, scripts).
        from app.career.importer import SeedImporter
        from app.career.read_model import build_snapshot
        from app.career.repository import EvidenceRepository
        from app.database import get_session_factory

        session = self._db or get_session_factory()()
        owns_session = self._db is None
        try:
            repo = EvidenceRepository(session, self._tenant_id)
            if repo.get_profile_row() is None:
                if not self._data_path.exists():
                    raise FileNotFoundError(
                        f"Tenant '{self._tenant_id}' has no profile and no seed file exists "
                        f"at {self._data_path}"
                    )
                report = SeedImporter(repo).import_file(self._data_path)
                logger.info(
                    "Bootstrapped tenant %s from %s: %s",
                    self._tenant_id,
                    self._data_path.name,
                    report.counts(),
                )
            snapshot = build_snapshot(repo)
        finally:
            if owns_session:
                session.close()

        if snapshot.profile is None or snapshot.preferences is None:
            raise RuntimeError(f"Tenant '{self._tenant_id}' has no candidate profile")
        self._profile = snapshot.profile
        self._preferences = snapshot.preferences
        self._skills = snapshot.skills
        self._projects = snapshot.projects
        self._experience = snapshot.experience
        self._achievements = snapshot.achievements
        self._facts = snapshot.facts

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    # ------------------------------------------------------------------ #
    # read surface (unchanged contract)
    # ------------------------------------------------------------------ #

    def get_profile(self) -> Profile:
        self._ensure_loaded()
        assert self._profile is not None
        return self._profile

    def get_skills(self) -> list[Skill]:
        self._ensure_loaded()
        return list(self._skills)

    def get_verified_skills(self) -> list[Skill]:
        return [s for s in self.get_skills() if s.is_verified]

    def get_projects(self) -> list[Project]:
        self._ensure_loaded()
        return list(self._projects)

    def get_project(self, project_id: str) -> Project:
        self._ensure_loaded()
        for project in self._projects:
            if project.id == project_id:
                return project
        raise KeyError(f"Project not found: {project_id}")

    def get_experience(self) -> list[Experience]:
        self._ensure_loaded()
        return list(self._experience)

    def get_achievements(self) -> list[Achievement]:
        self._ensure_loaded()
        return list(self._achievements)

    def get_preferences(self) -> Preference:
        self._ensure_loaded()
        assert self._preferences is not None
        return self._preferences

    def get_facts(self) -> list[CareerFact]:
        self._ensure_loaded()
        return list(self._facts)

    def get_verified_facts(self) -> list[CareerFact]:
        return [
            f
            for f in self.get_facts()
            if f.verification_status == VerificationStatus.VERIFIED
        ]

    def get_application_safe_facts(self) -> list[CareerFact]:
        return self._validator.filter_application_safe(self.get_facts())

    def get_resume_safe_facts(self) -> list[CareerFact]:
        return self._validator.filter_resume_safe(self.get_facts())

    def get_needs_review_facts(self) -> list[CareerFact]:
        return self._validator.surface_needs_review(self.get_facts())

    def get_conflict_facts(self) -> list[CareerFact]:
        return self._validator.surface_conflicts(self.get_facts())

    def search_projects(self, query: str) -> list[Project]:
        self._ensure_loaded()
        return [p for p in self._projects if p.matches_query(query)]

    def find_relevant_projects(self, role_or_jd: str, min_score: float = 0.5) -> list[Project]:
        self._ensure_loaded()
        scored = [(p, p.relevance_score(role_or_jd)) for p in self._projects]
        scored = [(p, s) for p, s in scored if s >= min_score]
        scored.sort(key=lambda x: x[1], reverse=True)
        return [p for p, _ in scored]

    def get_career_summary(self) -> CareerSummary:
        self._ensure_loaded()
        assert self._profile is not None
        assert self._preferences is not None

        verified_facts = self.get_verified_facts()
        app_safe = self.get_application_safe_facts()

        return CareerSummary(
            profile=self._profile,
            skills_count=len(self._skills),
            verified_skills_count=len(self.get_verified_skills()),
            projects_count=len(self._projects),
            experience_count=len(self._experience),
            achievements_count=len(self._achievements),
            verified_achievements_count=len(
                [a for a in self._achievements if a.is_verified]
            ),
            verified_facts_count=len(verified_facts),
            application_safe_facts_count=len(app_safe),
            needs_review_count=len(self.get_needs_review_facts()),
            conflict_count=len(self.get_conflict_facts()),
            target_roles=self._preferences.target_roles_tier1,
            top_projects=[p.name for p in self._projects[:3]],
        )
