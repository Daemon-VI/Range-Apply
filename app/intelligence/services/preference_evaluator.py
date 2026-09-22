"""Scoring the candidate's stated preferences against a job.

Preferences are **soft**: they shape ranking, never eligibility. A role the
candidate would rather not take is still applyable.

This closes a gap the audit found: the scoring policy reserved 15% of the score
for preferences and then never consulted them.

Every dimension returns a score in [0, 1] plus a reason, and dimensions with no
data are *skipped* rather than scored zero — an unstated preference must not
penalize a job.
"""

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from app.intelligence.services.role_relevance import (
    RoleAssessment,
    assess_relevance,
    technology_count,
)
from app.jobs.geography import GeoClass, GeoTier, job_location_texts, policy_from_preferences
from app.jobs.models.enums import EmploymentType, RemoteType
from app.jobs.models.job import NormalizedJob
from app.models.preference import Preference


@dataclass
class PreferenceSignal:
    name: str
    score: float  # 0.0 - 1.0
    weight: float
    reason: str


@dataclass
class PreferenceAssessment:
    score: float  # weighted average of the applicable signals, 0.0 - 1.0
    signals: List[PreferenceSignal] = field(default_factory=list)
    matches: List[str] = field(default_factory=list)
    mismatches: List[str] = field(default_factory=list)
    # A hard user-level exclusion (e.g. blacklisted company), which the caller
    # may choose to treat as a filter rather than a score.
    excluded: bool = False
    exclusion_reason: Optional[str] = None
    #: False when the title shares nothing with any target role family (None: not evaluated).
    role_family_matched: Optional[bool] = None
    #: Role-family relevance against the candidate's target families (app/intelligence/services/role_relevance.py).
    role: Optional[RoleAssessment] = None

    @property
    def applicable(self) -> bool:
        return bool(self.signals)


def _tokens(value: str) -> set:
    return {t.strip() for t in re.split(r"[,/|]|\s+-\s+", (value or "").lower()) if len(t.strip()) > 2}


def _role_tokens(role: str) -> set:
    return {t for t in re.split(r"[^a-z0-9+#]+", (role or "").lower()) if len(t) > 2}


class PreferenceEvaluator:
    """Turns Career Brain preferences into weighted ranking signals."""

    # Relative importance within the preference component.
    WEIGHTS = {
        "role_family": 3.0,
        "employment_type": 2.0,
        "work_mode": 1.5,
        "location": 1.5,
        "target_company": 1.0,
        "domain": 1.0,
    }

    def evaluate(self, job: NormalizedJob, preferences: Preference, profile_location: Optional[str] = None) -> PreferenceAssessment:
        assessment = PreferenceAssessment(score=0.0)

        # --- hard user exclusion (not an eligibility gate, a user filter) ---
        for excluded in preferences.excluded_companies or []:
            if excluded and excluded.strip().lower() in (job.company or "").lower():
                assessment.excluded = True
                assessment.exclusion_reason = f"'{job.company}' is on the excluded companies list."
                assessment.mismatches.append(assessment.exclusion_reason)

        self._role_signal(job, preferences, assessment)
        assessment.role = assess_relevance(job.title, job.description, technology_count(job.technologies, job.required_skills), preferences)
        self._employment_signal(job, preferences, assessment)
        self._work_mode_signal(job, preferences, assessment)
        self._location_signal(job, preferences, assessment, profile_location)
        self._target_company_signal(job, preferences, assessment)
        self._domain_signal(job, preferences, assessment)

        if assessment.signals:
            total_weight = sum(s.weight for s in assessment.signals)
            assessment.score = sum(s.score * s.weight for s in assessment.signals) / total_weight
        else:
            # Nothing stated: neutral, so preferences neither help nor hurt.
            assessment.score = 0.5

        return assessment

    # ---------------------------------------------------------------- #

    def _add(self, assessment: PreferenceAssessment, name: str, score: float, reason: str) -> None:
        assessment.signals.append(PreferenceSignal(name, score, self.WEIGHTS[name], reason))
        if score >= 0.7:
            assessment.matches.append(reason)
        elif score <= 0.3:
            assessment.mismatches.append(reason)

    def _role_signal(self, job, preferences, assessment) -> None:
        """Tiered role families: tier 1 is a full match, tier 2 partial."""
        title_tokens = _role_tokens(job.title)
        if not title_tokens:
            return

        tiers = (
            (preferences.target_roles_tier1, 1.0, "tier-1"),
            (preferences.target_roles_tier2, 0.7, "tier-2"),
            (preferences.target_roles_lower_priority, 0.2, "low-priority"),
        )
        for roles, score, label in tiers:
            for role in roles or []:
                role_tokens = _role_tokens(role)
                if role_tokens and role_tokens.issubset(title_tokens):
                    self._add(
                        assessment,
                        "role_family",
                        score,
                        f"Title matches {label} target role '{role}'.",
                    )
                    assessment.role_family_matched = True
                    return

        # Partial overlap on a distinctive token ("engineer", "scientist").
        for roles, score, label in tiers:
            for role in roles or []:
                overlap = _role_tokens(role) & title_tokens
                if overlap:
                    self._add(
                        assessment,
                        "role_family",
                        score * 0.6,
                        f"Title partially overlaps {label} target role '{role}' ({', '.join(sorted(overlap))}).",
                    )
                    assessment.role_family_matched = True
                    return

        if preferences.all_target_roles:
            assessment.role_family_matched = False
        self._add(
            assessment,
            "role_family",
            0.2,
            f"Title '{job.title}' does not match any target role family.",
        )

    def _employment_signal(self, job, preferences, assessment) -> None:
        if job.employment_type is EmploymentType.UNKNOWN:
            return
        wanted: Dict[EmploymentType, bool] = {
            EmploymentType.INTERNSHIP: bool(preferences.internship_preference),
            EmploymentType.FULL_TIME: bool(preferences.full_time_preference),
        }
        if job.employment_type not in wanted:
            self._add(
                assessment,
                "employment_type",
                0.4,
                f"Employment type {job.employment_type.value} is outside the stated preferences.",
            )
            return
        if wanted[job.employment_type]:
            self._add(
                assessment,
                "employment_type",
                1.0,
                f"{job.employment_type.value} matches the candidate's stated preference.",
            )
        else:
            self._add(
                assessment,
                "employment_type",
                0.1,
                f"{job.employment_type.value} is not wanted by the candidate.",
            )

    def _work_mode_signal(self, job, preferences, assessment) -> None:
        if job.remote_type is RemoteType.UNKNOWN:
            return
        remote_pref = (preferences.remote_preference or "").strip().lower()
        hybrid_pref = (preferences.hybrid_preference or "").strip().lower()
        if not remote_pref and not hybrid_pref:
            return

        positive = ("yes", "preferred", "strong", "open", "true")
        negative = ("no", "avoid", "not", "false")

        def stated(value: str) -> Optional[bool]:
            if not value:
                return None
            if any(word in value for word in negative):
                return False
            if any(word in value for word in positive):
                return True
            return None

        wants_remote = stated(remote_pref)
        wants_hybrid = stated(hybrid_pref)

        if job.remote_type is RemoteType.REMOTE and wants_remote is not None:
            self._add(
                assessment,
                "work_mode",
                1.0 if wants_remote else 0.2,
                f"Remote role vs remote preference '{preferences.remote_preference}'.",
            )
        elif job.remote_type is RemoteType.HYBRID and wants_hybrid is not None:
            self._add(
                assessment,
                "work_mode",
                1.0 if wants_hybrid else 0.3,
                f"Hybrid role vs hybrid preference '{preferences.hybrid_preference}'.",
            )

    def _location_signal(self, job, preferences, assessment, profile_location: Optional[str] = None) -> None:
        """Against the candidate's geographic target when one is known (``app/jobs/geography.py``).

        A US posting used to be skipped (no preferred locations) or score 0.8 as
        "remote, so location is moot"; it now scores as outside the target.
        """
        policy = policy_from_preferences(preferences, profile_location)
        if policy.active:
            geo = policy.assess_job(job.location, job.locations, job.metadata)
            if geo.geo_class is GeoClass.UNKNOWN and not job_location_texts(job.location, job.locations, job.metadata):
                return  # nothing stated: skipped, never penalised
            score = {GeoTier.PRIMARY: 1.0, GeoTier.SECONDARY: 0.8, GeoTier.INTERNATIONAL: 0.5, GeoTier.UNCONFIRMED: 0.4}.get(geo.tier, 0.0)
            self._add(assessment, "location", score, f"Location: {geo.detail}.")
            return
        preferred = [p for p in (preferences.preferred_locations or []) if p]
        if not preferred or not job.location:
            return
        job_tokens = _tokens(job.location)
        for location in preferred:
            if _tokens(location) & job_tokens:
                self._add(
                    assessment, "location", 1.0, f"Location {job.location} matches preferred '{location}'."
                )
                return
        if job.remote_type is RemoteType.REMOTE:
            self._add(assessment, "location", 0.8, "Remote role, so location preference is moot.")
            return
        self._add(
            assessment,
            "location",
            0.3,
            f"Location {job.location} is not among the preferred locations.",
        )

    def _target_company_signal(self, job, preferences, assessment) -> None:
        targets = [c for c in (preferences.target_companies or []) if c]
        if not targets:
            return
        company = (job.company or "").lower()
        for target in targets:
            if target.strip().lower() in company:
                self._add(assessment, "target_company", 1.0, f"'{job.company}' is a target company.")
                return
        self._add(assessment, "target_company", 0.5, f"'{job.company}' is not a named target company.")

    def _domain_signal(self, job, preferences, assessment) -> None:
        domains = [d for d in (preferences.target_domains or []) if d]
        if not domains:
            return
        haystack = f"{job.title} {job.description[:2000]}".lower()
        for domain in domains:
            tokens = _role_tokens(domain)
            if tokens and any(token in haystack for token in tokens):
                self._add(assessment, "domain", 1.0, f"Job touches target domain '{domain}'.")
                return
        self._add(assessment, "domain", 0.4, "Job does not clearly match a target domain.")
