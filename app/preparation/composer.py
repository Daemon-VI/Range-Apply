"""Deterministic composition of resume and cover-letter blocks.

Every CLAIM block is string-built from the cited nodes' own text, so the
validator can always trace it. FRAMING blocks carry candidate-authored
text (positioning headline/summary, profile statement) or job facts we were
given (company, title) — never an invented company compliment.
"""

from typing import Any, Optional

from app.career.database.models import EvidenceNodeRow, PositioningVariantRow
from app.career.models import RelationType
from app.pipeline.models import TailoringLevel
from app.preparation.evidence import LEVEL_LIMITS, EvidenceSelection, EvidenceSnapshot
from app.preparation.models import Block, BlockKind, CoverLetterMode


def _attr(node: EvidenceNodeRow, name: str, default: Any = None) -> Any:
    value = (node.attributes or {}).get(name)
    return default if value is None else value


def _trim(text: str, limit: int = 220) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def compose_resume(
    snapshot: EvidenceSnapshot,
    selection: EvidenceSelection,
    variant: Optional[PositioningVariantRow],
    level: TailoringLevel,
    job_title: str,
) -> list[Block]:
    limits = LEVEL_LIMITS[level]
    blocks: list[Block] = []

    # Summary: candidate-authored framing, ordered by preference.
    if variant is not None and (variant.headline or variant.summary):
        headline = variant.headline.strip()
        summary = variant.summary.strip()
        text = f"{headline}. {summary}".strip(". ").strip() if headline and summary else (headline or summary)
        blocks.append(Block(kind=BlockKind.FRAMING, section="summary", text=text, source=f"variant:{variant.id}"))
    elif snapshot.profile and snapshot.profile.positioning_statement:
        blocks.append(
            Block(kind=BlockKind.FRAMING, section="summary", text=snapshot.profile.positioning_statement, source="profile")
        )

    # Skills: one claim per skill so each name is individually traceable.
    skill_nodes = [snapshot.nodes[k] for k in selection.skills if k in snapshot.nodes]
    if skill_nodes:
        blocks.append(
            Block(
                kind=BlockKind.CLAIM,
                section="skills",
                text="Skills: " + ", ".join(n.label for n in skill_nodes),
                evidence_keys=[n.key for n in skill_nodes],
            )
        )

    for key in selection.projects:
        project = snapshot.nodes[key]
        technologies = list(_attr(project, "technologies", []))
        text = f"{project.label}: {_trim(project.claim)}"
        if technologies:
            text += f" Technologies: {', '.join(technologies)}."
        requirement = _requirement_for(selection, key)
        blocks.append(Block(kind=BlockKind.CLAIM, section="projects", text=text, evidence_keys=[key], requirement=requirement))
        for metric in snapshot.safe_children(key, RelationType.HAS_METRIC)[: limits["metrics"]]:
            blocks.append(
                Block(
                    kind=BlockKind.CLAIM,
                    section="projects",
                    text=f"{project.label} — {metric.label}: {_attr(metric, 'value', '')}",
                    evidence_keys=[metric.key],
                )
            )

    for key in selection.experiences:
        experience = snapshot.nodes[key]
        blocks.append(Block(kind=BlockKind.CLAIM, section="experience", text=experience.label, evidence_keys=[key]))
        for resp in snapshot.safe_children(key, RelationType.HAS_RESPONSIBILITY)[: limits["responsibilities"]]:
            blocks.append(
                Block(kind=BlockKind.CLAIM, section="experience", text=_trim(resp.claim), evidence_keys=[resp.key])
            )

    for key in selection.education:
        education = snapshot.nodes[key]
        blocks.append(Block(kind=BlockKind.CLAIM, section="education", text=education.claim, evidence_keys=[key]))

    return blocks


def _requirement_for(selection: EvidenceSelection, key: str) -> Optional[str]:
    for requirement, keys in selection.matched_requirements.items():
        if key in keys:
            return requirement
    return None


def compose_cover_letter(
    mode: CoverLetterMode,
    snapshot: EvidenceSnapshot,
    selection: EvidenceSelection,
    variant: Optional[PositioningVariantRow],
    company: str,
    job_title: str,
) -> Optional[list[Block]]:
    if mode is CoverLetterMode.DISABLED:
        return None
    blocks: list[Block] = []
    name = snapshot.profile.name if snapshot.profile else ""
    blocks.append(Block(kind=BlockKind.FRAMING, section="greeting", text=f"Dear {company} Hiring Team,", source="job"))
    statement = (variant.summary or variant.headline) if variant else (snapshot.profile.positioning_statement if snapshot.profile else "")
    intro = f"I am applying for the {job_title} position at {company}."
    if statement:
        intro += f" {statement.strip()}"
    blocks.append(Block(kind=BlockKind.FRAMING, section="intro", text=intro, source="job+profile"))

    if mode in (CoverLetterMode.LIGHT, CoverLetterMode.TARGETED) and selection.matched_requirements:
        for requirement, keys in list(selection.matched_requirements.items())[:4]:
            labels = [snapshot.nodes[k].label for k in keys if k in snapshot.nodes]
            if not labels:
                continue
            blocks.append(
                Block(
                    kind=BlockKind.CLAIM,
                    section="fit",
                    text=f"Your posting asks for {requirement}; I bring {', '.join(labels)}.",
                    evidence_keys=[k for k in keys if k in snapshot.nodes],
                    requirement=requirement,
                )
            )

    for key in selection.projects[:2]:
        project = snapshot.nodes[key]
        blocks.append(
            Block(kind=BlockKind.CLAIM, section="body", text=f"{project.label}: {_trim(project.claim, 200)}", evidence_keys=[key])
        )
    if not selection.projects and selection.skills:
        labels = [snapshot.nodes[k].label for k in selection.skills[:6]]
        blocks.append(
            Block(kind=BlockKind.CLAIM, section="body", text="Relevant skills: " + ", ".join(labels), evidence_keys=selection.skills[:6])
        )

    closing = f"Thank you for your consideration. I would welcome the chance to discuss how I can contribute to {company}."
    if name:
        closing += f"\n\n{name}"
    blocks.append(Block(kind=BlockKind.FRAMING, section="closing", text=closing, source="job+profile"))
    return blocks


def render(blocks: list[Block]) -> str:
    """Plain-text rendering: sections as headers, claims as bullets."""
    lines: list[str] = []
    current = None
    for block in blocks:
        if block.section != current:
            if block.section not in ("greeting", "intro", "closing", "fit", "body"):
                lines.append("")
                lines.append(block.section.upper())
            current = block.section
        if block.kind is BlockKind.CLAIM and block.section not in ("fit", "body"):
            lines.append(f"- {block.text}")
        else:
            lines.append(block.text)
    return "\n".join(lines).strip()
