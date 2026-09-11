"""TailoringEngine: deterministic resume/cover-letter/answer generation.

No LLM calls. Every sentence is string-formatted from real Career Brain data
selected by ``evidence_selector`` (PRD FR-05 truthfulness rule).
"""

from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from app.core.timeutils import db_now
from app.intelligence.database.models import JobMatchRow
from app.intelligence.services.match_persistence import latest_matches_query
from app.jobs.database.models import JobRow
from app.services.career_brain import CareerBrainService
from app.tailoring.database.models import TailoredArtifactRow
from app.tailoring.evidence_selector import select_evidence
from app.tailoring.models import ArtifactType

TEMPLATE_NAME = "deterministic-v1"

QUESTIONS = [
    "Why are you interested in this role?",
    "What relevant experience do you have?",
]


def _trim(text: str, max_len: int = 160) -> str:
    text = " ".join(text.split())
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


class TailoringEngine:
    def generate_resume_bullets(
        self,
        db: Session,
        job_row: JobRow,
        job_match_row: JobMatchRow,
        career_brain: CareerBrainService,
    ) -> List[str]:
        evidence = select_evidence(db, job_match_row, career_brain)
        return [f"{name}: {_trim(text)}" for name, text in evidence]

    def generate_cover_letter(
        self,
        db: Session,
        job_row: JobRow,
        job_match_row: JobMatchRow,
        career_brain: CareerBrainService,
    ) -> str:
        profile = career_brain.get_profile()
        evidence = select_evidence(db, job_match_row, career_brain)[:3]

        greeting = f"Dear {job_row.company} Hiring Team,"
        intro = (
            f"I am writing to apply for the {job_row.title} position at {job_row.company}. "
            f"{profile.positioning_statement}".strip()
        )
        body_lines = [f"- {name}: {_trim(text)}" for name, text in evidence]
        body = "Relevant experience:\n" + "\n".join(body_lines) if body_lines else ""
        closing = f"Thank you for your consideration. I would welcome the chance to discuss how I can contribute to {job_row.company}.\n\n{profile.name}"

        paragraphs = [greeting, "", intro]
        if body:
            paragraphs += ["", body]
        paragraphs += ["", closing]
        return "\n".join(paragraphs)

    def generate_answers(
        self,
        db: Session,
        job_row: JobRow,
        job_match_row: JobMatchRow,
        career_brain: CareerBrainService,
    ) -> Dict[str, str]:
        profile = career_brain.get_profile()
        evidence = select_evidence(db, job_match_row, career_brain)

        why_interested = (
            f"{job_row.title} at {job_row.company} aligns with my background: "
            f"{profile.positioning_statement}".strip()
        )

        if evidence:
            experience_bits = "; ".join(f"{name} ({_trim(text, 80)})" for name, text in evidence[:3])
            relevant_experience = f"Relevant experience includes: {experience_bits}."
        else:
            relevant_experience = profile.positioning_statement

        return {
            QUESTIONS[0]: why_interested,
            QUESTIONS[1]: relevant_experience,
        }

    def generate(
        self, db: Session, job_id: str, run_id: Optional[str] = None
    ) -> List[TailoredArtifactRow]:
        job_row = db.query(JobRow).filter(JobRow.id == job_id).first()
        if job_row is None:
            raise ValueError(f"Job not found: {job_id}")

        query = latest_matches_query(db, run_id)
        job_match_row = (
            query.filter(JobMatchRow.job_id == job_id).first() if query is not None else None
        )
        if job_match_row is None:
            raise ValueError(
                f"No match run exists for job {job_id}. "
                "Run /api/v3/matches/recalculate before generating tailored artifacts."
            )

        career_brain = CareerBrainService()
        career_brain.load()

        bullets = self.generate_resume_bullets(db, job_row, job_match_row, career_brain)
        cover_letter = self.generate_cover_letter(db, job_row, job_match_row, career_brain)
        answers = self.generate_answers(db, job_row, job_match_row, career_brain)
        evidence_refs = [name for name, _ in select_evidence(db, job_match_row, career_brain)]

        contents = [
            (ArtifactType.RESUME, f"Resume bullets - {job_row.title}", "\n".join(f"- {b}" for b in bullets)),
            (ArtifactType.COVER_LETTER, f"Cover letter - {job_row.company}", cover_letter),
            (ArtifactType.ANSWER, "Application answers", "\n\n".join(f"Q: {q}\nA: {a}" for q, a in answers.items())),
        ]

        rows: List[TailoredArtifactRow] = []
        for artifact_type, title, content in contents:
            last = (
                db.query(TailoredArtifactRow)
                .filter(TailoredArtifactRow.job_id == job_id)
                .filter(TailoredArtifactRow.artifact_type == artifact_type.value)
                .order_by(TailoredArtifactRow.version.desc())
                .first()
            )
            version = (last.version + 1) if last else 1
            row = TailoredArtifactRow(
                job_id=job_id,
                match_id=job_match_row.id,
                artifact_type=artifact_type.value,
                version=version,
                title=title,
                content=content,
                evidence_refs=evidence_refs,
                template_name=TEMPLATE_NAME,
                approved=False,
                created_at=db_now(),
            )
            db.add(row)
            rows.append(row)

        db.commit()
        for row in rows:
            db.refresh(row)
        return rows
