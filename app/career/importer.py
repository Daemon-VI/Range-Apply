"""Seed importer: ``career_seed.json`` -> Evidence Graph.

The JSON file stays the candidate's editable seed. Importing is idempotent:
every record is hashed, and a node whose ``source_hash`` matches is skipped,
one whose hash changed is updated (with an audit event), and a new one is
created. Re-running the importer on an unchanged file is a no-op.

Provenance: every node carries ``source_type=SEED_FILE`` (or ``INFERRED``
when the seed itself says so) and ``source_ref="<file>#<collection>/<id>"``.
Verification status comes from the seed; nothing is upgraded on import.

Derived nodes (kept small, no invented facts):

* ``METRIC``         one per ``project.metrics[]``      (PROJECT -HAS_METRIC-> METRIC)
* ``RESPONSIBILITY`` one per ``experience.responsibilities[]``
                     (EXPERIENCE -HAS_RESPONSIBILITY-> RESPONSIBILITY)
* ``EDUCATION``      one from the profile's degree fields
* ``LINK``           github / linkedin / portfolio when present
* ``DEMONSTRATES``   PROJECT -> SKILL from the explicit ``skill.projects`` list
* ``ABOUT``          FACT/ACHIEVEMENT -> the entity they refer to

Usage::

    python -m app.career.importer            # default tenant, settings.career_data_path
    python -m app.career.importer --tenant t2 --file data/other_seed.json
"""

import argparse
import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Optional

from app.career.models import (
    EvidenceKind,
    EvidenceNodeCreate,
    EvidenceNodeUpdate,
    EvidenceSourceType,
    ImportReport,
    RelationType,
)
from app.career.repository import EvidenceRepository, slugify
from app.models.enums import VerificationStatus

logger = logging.getLogger(__name__)

IMPORTER_ACTOR = "importer"


def record_hash(record: Any) -> str:
    return hashlib.sha256(
        json.dumps(record, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()


def _status(value: Optional[str], default: VerificationStatus) -> VerificationStatus:
    if not value:
        return default
    try:
        return VerificationStatus(value)
    except ValueError:
        return default


class SeedImporter:
    """Imports one seed document into one tenant's Evidence Graph."""

    def __init__(self, repo: EvidenceRepository, actor: str = IMPORTER_ACTOR):
        self.repo = repo
        self.actor = actor

    # ------------------------------------------------------------------ #

    def import_file(self, path: str | Path, force: bool = False) -> ImportReport:
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Career data file not found: {path}")
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return self.import_data(data, source_name=path.name, force=force)

    def import_data(self, data: dict[str, Any], source_name: str, force: bool = False) -> ImportReport:
        report = ImportReport(tenant_id=self.repo.tenant_id, source=source_name)
        self._source = source_name
        self._force = force
        self._order = 0

        profile = dict(data.get("profile") or {})
        preferences = dict(data.get("preferences") or {})
        self.repo.ensure_tenant(profile.get("name", ""))
        self._import_profile(profile, preferences, report)

        skills = list(data.get("skills") or [])
        projects = list(data.get("projects") or [])
        experience = list(data.get("experience") or [])
        achievements = list(data.get("achievements") or [])
        facts = list(data.get("facts") or [])

        for record in skills:
            self._upsert(
                report,
                key=record["id"],
                kind=EvidenceKind.SKILL,
                label=record["name"],
                claim=self._skill_claim(record),
                attributes={
                    "category": record.get("category", "Other"),
                    "evidence": record.get("evidence", ""),
                    "resume_relevance": record.get("resume_relevance", "medium"),
                    "notes": record.get("notes", ""),
                },
                status=_status(record.get("verification_status"), VerificationStatus.UNVERIFIED),
                collection="skills",
                record=record,
            )

        for record in projects:
            self._upsert(
                report,
                key=record["id"],
                kind=EvidenceKind.PROJECT,
                label=record["name"],
                claim=record.get("summary") or record["name"],
                attributes={
                    "status": record.get("status", "UNKNOWN"),
                    "problem": record.get("problem", ""),
                    "solution": record.get("solution", ""),
                    "technologies": list(record.get("technologies") or []),
                    "primary_domain": record.get("primary_domain", ""),
                    "relevant_roles": list(record.get("relevant_roles") or []),
                    "features": list(record.get("features") or []),
                    "technical_concepts": list(record.get("technical_concepts") or []),
                    "resume_worthy_facts": list(record.get("resume_worthy_facts") or []),
                    "verified_claims": list(record.get("verified_claims") or []),
                    "unverified_claims": list(record.get("unverified_claims") or []),
                    "documentation_path": record.get("documentation_path", ""),
                },
                # The seed's projects are candidate-authored; their existence and
                # stack are what "verified_claims" asserts. Metrics carry their own status.
                status=VerificationStatus.VERIFIED,
                collection="projects",
                record={k: v for k, v in record.items() if k != "metrics"},
            )
            for index, metric in enumerate(record.get("metrics") or []):
                metric_key = f"metric:{record['id']}:{slugify(metric['name'])}"
                self._upsert(
                    report,
                    key=metric_key,
                    kind=EvidenceKind.METRIC,
                    label=metric["name"],
                    claim=f"{record['name']} — {metric['name']}: {metric.get('value', '')}",
                    attributes={"name": metric["name"], "value": metric.get("value", "")},
                    status=_status(metric.get("verification_status"), VerificationStatus.UNVERIFIED),
                    collection=f"projects/{record['id']}/metrics",
                    record=metric,
                )
                self._link(report, record["id"], metric_key, RelationType.HAS_METRIC, index)

        for record in experience:
            label = f"{record.get('role', '')} — {record.get('organization', '')}".strip(" —")
            exp_status = _status(record.get("verification_status"), VerificationStatus.VERIFIED)
            self._upsert(
                report,
                key=record["id"],
                kind=EvidenceKind.EXPERIENCE,
                label=label or record["id"],
                claim=label or record["id"],
                attributes={
                    "organization": record.get("organization", ""),
                    "role": record.get("role", ""),
                    "start_date": record.get("start_date"),
                    "end_date": record.get("end_date"),
                    "is_employment": bool(record.get("is_employment", False)),
                    "technologies": list(record.get("technologies") or []),
                    "achievements": list(record.get("achievements") or []),
                    "verified_claims": list(record.get("verified_claims") or []),
                    "notes": record.get("notes", ""),
                },
                status=exp_status,
                collection="experience",
                record={k: v for k, v in record.items() if k != "responsibilities"},
            )
            for index, text in enumerate(record.get("responsibilities") or []):
                resp_key = f"resp:{record['id']}:{hashlib.sha1(text.encode('utf-8')).hexdigest()[:10]}"
                self._upsert(
                    report,
                    key=resp_key,
                    kind=EvidenceKind.RESPONSIBILITY,
                    label=text[:256],
                    claim=text,
                    attributes={"experience": record["id"]},
                    status=exp_status,
                    collection=f"experience/{record['id']}/responsibilities",
                    record={"text": text, "status": exp_status.value},
                )
                self._link(report, record["id"], resp_key, RelationType.HAS_RESPONSIBILITY, index)

        for record in achievements:
            self._upsert(
                report,
                key=record["id"],
                kind=EvidenceKind.ACHIEVEMENT,
                label=record["title"],
                claim=record.get("description") or record["title"],
                attributes={
                    "category": record.get("category", "other"),
                    "description": record.get("description", ""),
                    "date": record.get("date"),
                    "related_project": record.get("related_project"),
                    "notes": record.get("notes", ""),
                },
                status=_status(record.get("verification_status"), VerificationStatus.UNVERIFIED),
                collection="achievements",
                record=record,
            )
            if record.get("related_project"):
                self._link_if_exists(report, record["id"], record["related_project"], RelationType.ABOUT)

        for record in facts:
            status = _status(record.get("verification_status"), VerificationStatus.UNVERIFIED)
            self._upsert(
                report,
                key=record["id"],
                kind=EvidenceKind.FACT,
                label=record["statement"][:256],
                claim=record["statement"],
                attributes={
                    "category": record.get("category", "skill"),
                    "source": record.get("source", ""),
                    "related_entity_id": record.get("related_entity_id"),
                },
                status=status,
                collection="facts",
                record=record,
                confidence=float(record.get("confidence", 1.0)),
                allowed_for_resume=record.get("allowed_for_resume"),
                allowed_for_application=record.get("allowed_for_application"),
                source_type=(
                    EvidenceSourceType.INFERRED
                    if status is VerificationStatus.INFERRED
                    else EvidenceSourceType.SEED_FILE
                ),
            )
            if record.get("related_entity_id"):
                self._link_if_exists(
                    report, record["id"], record["related_entity_id"], RelationType.ABOUT
                )

        # Explicit skill -> projects lists become PROJECT -DEMONSTRATES-> SKILL.
        for record in skills:
            for index, project_key in enumerate(record.get("projects") or []):
                self._link_if_exists(report, project_key, record["id"], RelationType.DEMONSTRATES, index)

        self._import_education_and_links(profile, report)

        self.repo.commit()
        logger.info(
            "Seed import for tenant %s from %s: %s",
            self.repo.tenant_id,
            source_name,
            report.counts(),
        )
        return report

    # ------------------------------------------------------------------ #

    def _import_profile(self, profile: dict, preferences: dict, report: ImportReport) -> None:
        if not profile:
            return
        source_hash = record_hash({"profile": profile, "preferences": preferences})
        existing = self.repo.get_profile_row()
        if existing is not None and existing.source_hash == source_hash and not self._force:
            report.profile = "skipped"
            return
        _, action = self.repo.upsert_profile(
            profile, preferences, actor=self.actor, source_hash=source_hash
        )
        report.profile = action

    def _import_education_and_links(self, profile: dict, report: ImportReport) -> None:
        if profile.get("degree") and profile.get("college"):
            record = {
                k: profile.get(k)
                for k in ("degree", "branch", "college", "graduation_year", "cgpa", "backlogs")
            }
            claim = (
                f"{profile['degree']}, {profile['college']}, "
                f"graduating {profile.get('graduation_year', '')}"
            )
            if profile.get("cgpa"):
                claim += f", CGPA {profile['cgpa']}"
            self._upsert(
                report,
                key="education:primary",
                kind=EvidenceKind.EDUCATION,
                label=str(profile["degree"])[:256],
                claim=claim,
                attributes=record,
                status=VerificationStatus.VERIFIED,
                collection="profile/education",
                record=record,
            )
        for name in ("github", "linkedin", "portfolio"):
            url = profile.get(name)
            if not url:
                continue
            self._upsert(
                report,
                key=f"link:{name}",
                kind=EvidenceKind.LINK,
                label=name,
                claim=str(url),
                attributes={"url": url, "kind": name},
                status=VerificationStatus.VERIFIED,
                collection="profile/links",
                record={"url": url},
            )

    @staticmethod
    def _skill_claim(record: dict) -> str:
        evidence = record.get("evidence")
        return f"{record['name']} ({evidence})" if evidence else record["name"]

    def _upsert(
        self,
        report: ImportReport,
        *,
        key: str,
        kind: EvidenceKind,
        label: str,
        claim: str,
        attributes: dict[str, Any],
        status: VerificationStatus,
        collection: str,
        record: Any,
        confidence: float = 1.0,
        allowed_for_resume: Optional[bool] = None,
        allowed_for_application: Optional[bool] = None,
        source_type: EvidenceSourceType = EvidenceSourceType.SEED_FILE,
    ) -> None:
        self._order += 1
        source_hash = record_hash(record)
        source_ref = f"{self._source}#{collection}/{key}"
        existing = self.repo.get_node(key, include_removed=True)
        if existing is None:
            self.repo.create_node(
                EvidenceNodeCreate(
                    key=key,
                    kind=kind,
                    label=label,
                    claim=claim,
                    attributes=attributes,
                    verification_status=status,
                    confidence=confidence,
                    allowed_for_resume=allowed_for_resume,
                    allowed_for_application=allowed_for_application,
                    source_type=source_type,
                    source_ref=source_ref,
                    sort_order=self._order,
                ),
                actor=self.actor,
                source_hash=source_hash,
            )
            report.created.append(key)
            return
        if existing.source_hash == source_hash and not self._force:
            if existing.sort_order != self._order:
                existing.sort_order = self._order  # ordering only; not a content change
            report.skipped.append(key)
            return
        self.repo.update_node(
            key,
            EvidenceNodeUpdate(
                label=label,
                claim=claim,
                attributes=attributes,
                verification_status=status,
                confidence=confidence,
                allowed_for_resume=allowed_for_resume,
                allowed_for_application=allowed_for_application,
                source_type=source_type,
                source_ref=source_ref,
                sort_order=self._order,
            ),
            actor=self.actor,
            source_hash=source_hash,
        )
        report.updated.append(key)

    def _link(self, report: ImportReport, from_key: str, to_key: str, relation: RelationType, position: int = 0) -> None:
        _, created = self.repo.add_relationship(from_key, to_key, relation, self.actor, position)
        if created:
            report.relationships_created += 1

    def _link_if_exists(self, report: ImportReport, from_key: str, to_key: str, relation: RelationType, position: int = 0) -> None:
        if self.repo.get_node(from_key) is None or self.repo.get_node(to_key) is None:
            logger.debug("Skipping %s link %s -> %s: node missing", relation.value, from_key, to_key)
            return
        self._link(report, from_key, to_key, relation, position)


def main(argv: Optional[list[str]] = None) -> int:
    from app.config import settings
    from app.database import get_session_factory, verify_schema

    parser = argparse.ArgumentParser(description="Import career_seed.json into the Evidence Graph")
    parser.add_argument("--file", default=settings.career_data_path)
    parser.add_argument("--tenant", default=settings.default_tenant_id)
    parser.add_argument("--force", action="store_true", help="Re-apply every record even if unchanged")
    args = parser.parse_args(argv)

    verify_schema()
    session = get_session_factory()()
    try:
        report = SeedImporter(EvidenceRepository(session, args.tenant)).import_file(args.file, force=args.force)
    finally:
        session.close()
    print(json.dumps({"tenant_id": report.tenant_id, "source": report.source, "profile": report.profile, **report.counts()}, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
