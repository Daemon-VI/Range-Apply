"""Turn a preparation artifact's blocks into a :class:`DocumentModel`.

This is a *structural* mapping: sections in the order the blocks appear,
each block's text verbatim, claims as bullets. The only text not taken from
the blocks is the identity header (copied from the candidate profile), the
job facts already in the preparation (title / company) and an optional
configured date line. Nothing is summarised, reordered or "improved".
"""

import re
from typing import Any, Optional

from app.documents.models import Contact, ContactLink, DocumentModel, Section

SECTION_TITLES = {
    "summary": "Summary",
    "skills": "Skills",
    "experience": "Experience",
    "projects": "Projects",
    "education": "Education",
    "certifications": "Certifications",
    "links": "Links",
}
#: Sections whose blocks read as prose rather than bullets.
_PROSE_SECTIONS = {"summary"}
_COVER_ORDER = ("greeting", "intro", "fit", "body", "closing")


def contact_from_profile(profile: dict[str, Any]) -> Contact:
    links = []
    for key, label in (("linkedin", "LinkedIn"), ("github", "GitHub"), ("portfolio", "Portfolio")):
        url = (profile.get(key) or "").strip()
        if url:
            links.append(ContactLink(label=label, url=url))
    return Contact(
        name=(profile.get("name") or "").strip(),
        email=(profile.get("email") or None),
        phone=(profile.get("phone") or None),
        location=(profile.get("location") or None),
        links=links,
    )


def _title_for(section: str) -> str:
    return SECTION_TITLES.get(section, section.replace("_", " ").title())


def resume_model(blocks: list[dict[str, Any]], contact: Contact, job_title: Optional[str] = None) -> DocumentModel:
    sections: list[Section] = []
    by_key: dict[str, Section] = {}
    for block in blocks:
        text = " ".join(str(block.get("text") or "").split())
        if not text:
            continue
        key = str(block.get("section") or "other")
        section = by_key.get(key)
        if section is None:
            section = Section(key=key, title=_title_for(key))
            by_key[key] = section
            sections.append(section)
        bulleted = key not in _PROSE_SECTIONS
        section.items.append((text, bulleted))
    return DocumentModel(artifact_type="RESUME", contact=contact, title=None, sections=[s for s in sections if s.items])


def cover_letter_model(blocks: list[dict[str, Any]], contact: Contact, company: Optional[str], job_title: Optional[str], date_line: Optional[str] = None) -> Optional[DocumentModel]:
    ordered = sorted(
        (b for b in blocks if str(b.get("text") or "").strip()),
        key=lambda b: (_COVER_ORDER.index(b["section"]) if b.get("section") in _COVER_ORDER else len(_COVER_ORDER)),
    )
    if not ordered:
        return None
    paragraphs: list[str] = []
    signature: Optional[str] = None
    for block in ordered:
        text = str(block.get("text") or "").strip()
        if block.get("section") == "closing" and "\n\n" in text:
            body, _, sign = text.rpartition("\n\n")
            paragraphs.append(" ".join(body.split()))
            signature = sign.strip()
        else:
            paragraphs.append(" ".join(text.split()))
    subject = None
    if job_title and company:
        subject = f"Re: {job_title} at {company}"
    elif job_title:
        subject = f"Re: {job_title}"
    return DocumentModel(artifact_type="COVER_LETTER", contact=contact, subject=subject, date_line=date_line, paragraphs=paragraphs, signature=signature)


_WS = re.compile(r"\s+")
_BULLET = re.compile(r"^[•\-\*•·]\s*")


def normalize_text(text: str) -> str:
    """Whitespace-collapsed, bullet-stripped, case-preserving comparison form."""
    text = text.replace(" ", " ").replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    text = text.replace("—", "-").replace("–", "-").replace("…", "...")
    lines = [_BULLET.sub("", line.strip()) for line in text.splitlines()]
    return _WS.sub(" ", " ".join(line for line in lines if line)).strip()
