"""Stated experience and title seniority, read deterministically from a posting.

Selection-quality fix (2026-09-14): the years check required the literal word
"experience" right after the number, so "Associate Architect (9 - 12 Years)"
and "8+ years in VMware" were read as no requirement at all, and a title's own
level ("SDE III", "Architect") was ignored. This module returns every
experience statement it can defend, with the snippet it came from, so a gate
can explain itself. It never reads a graduation year, a duration in months, a
company's age or a head count as experience.
"""

import html
import re
from dataclasses import dataclass
from typing import Iterable, Optional

_WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
}
_N = r"(?:\d{1,2}|" + "|".join(_WORD_NUMBERS) + r")"
#: "3 years' experience" (possessive apostrophe) is the same statement as "3 years experience".
_YEARS = r"(?:years?|yrs?)(?:'|’)?"
_RANGE = rf"(?P<min>{_N})\s*(?:\+|plus)?\s*(?:(?:-|–|to)\s*(?P<max>{_N})\s*\+?)?\s*"

#: Statements tied to the word experience: "5+ years of relevant experience",
#: "minimum 5 years", "Experience: 4 - 6 years", "five years of experience".
_STRICT = (
    re.compile(rf"\b{_RANGE}{_YEARS}\s+(?:of\s+)?(?:[\w/&.-]+\s+){{0,5}}?(?:experience|exp)\b", re.IGNORECASE),
    re.compile(rf"\b(?:minimum|min\.?|at\s+least|atleast)\s+(?:of\s+)?{_RANGE}{_YEARS}\b", re.IGNORECASE),
    re.compile(rf"\bexperience\s*(?::|-|–|of|required:?|range:?)?\s*(?:a\s+)?(?:minimum\s+(?:of\s+)?|min\.?\s+|at\s+least\s+)?{_RANGE}{_YEARS}\b", re.IGNORECASE),
)
#: Seniority bands without the word: "(9 - 12 Years)", "8+ years", "4-6 years".
_LOOSE = (
    re.compile(rf"\(\s*{_RANGE}{_YEARS}\s*\)", re.IGNORECASE),
    re.compile(rf"\b(?P<min>\d{{1,2}})\s*(?:\+|(?:-|–|to)\s*(?P<max>\d{{1,2}}))\s*{_YEARS}\b", re.IGNORECASE),
)
#: Company age, history and time since: "for over 20 years", "12 years ago".
#: Not a bare "for": "What we're looking for 5+ years in ML systems" is a requirement (Sarvam, real run).
#: Audit fix (2026-09-14): "graduating within the next 2 years" is not a requirement. ("Bring 8+ years" is one.)
_BEFORE_EXCLUDED = re.compile(r"\b(?:over|past|nearly|almost|more than|since|founded|in business(?: for)?|history of|for the (?:last|past)|next|last|within)\s*$", re.IGNORECASE)
#: Audit fix (2026-09-14): "More than 3 years of experience" / "Over 5 years of experience" were
#: dropped by the company-age wording above, so a pre-career student passed the experience gate.
#: A statement tied to the word experience is excluded only by history / time-window wording, or by
#: "over / more than / nearly / almost" with a company-age sized number ("over 20 years of experience").
_BEFORE_HISTORY = re.compile(r"\b(?:past|since|founded|in business(?: for)?|history of|for the (?:last|past)|next|last|within)\s*$", re.IGNORECASE)
_COMPANY_AGE_MIN_YEARS = 16
_AFTER_EXCLUDED = re.compile(
    r"^\s*(?:ago|old|of (?:history|trust|serving|innovation|operation|growth)|in business|running"
    r"|(?:of\s+)?(?:[\w-]+\s+){0,2}?(?:undergraduate|college|university|coursework|schooling|study|studies|education|degree))\b",
    re.IGNORECASE,
)
#: Audit fix (2026-09-14): study and degree durations are not work experience ("at least 2 years of
#: undergraduate study", "a 5 year integrated M.Tech with experience in ML"), nor is a team's combined tenure.
_EDUCATION_IN_MATCH = re.compile(
    r"\b(?:undergraduate|college|university|coursework|schooling|study|studies|degree|bachelor'?s?|master'?s?|b\.?\s?tech|m\.?\s?tech|integrated|combined|collective)\b",
    re.IGNORECASE,
)
_PREFERRED = re.compile(r"\b(?:preferred|nice to have|a plus|is a plus|bonus|desirable|an advantage|ideally|good to have)\b", re.IGNORECASE)

_SENIOR_TITLE = re.compile(
    r"\b(?:senior|sr|staff|principal|lead|head|director|vp|vice president|manager|architect|distinguished|fellow)\b"
    r"|\b(?:iii|iv)\b|\b(?:level|l)\s*[3-9]\b|\b(?:sde|swe|engineer|developer)\s*[-_ ]?[3-5]\b",
    re.IGNORECASE,
)
_JUNIOR_MARKER = re.compile(r"\b(?:associate|junior|jr|graduate|intern|internship|trainee|apprentice|entry)\b", re.IGNORECASE)
_ALWAYS_SENIOR = re.compile(r"\b(?:senior|sr|staff|principal|director|vp|vice president|distinguished|fellow|head of|iii|iv)\b", re.IGNORECASE)
_MID_TITLE = re.compile(r"\b(?:ii)\b|\b(?:sde|swe|engineer|developer|analyst)\s*[-_ ]?2\b|\b(?:level|l)\s*2\b|\bmid[- ]level\b", re.IGNORECASE)


@dataclass(frozen=True)
class ExperienceStatement:
    years: int
    text: str
    preferred: bool
    #: True for a band read without the word "experience" ("(9 - 12 Years)").
    loose: bool


def _number(token: Optional[str]) -> Optional[int]:
    if not token:
        return None
    token = token.lower()
    return int(token) if token.isdigit() else _WORD_NUMBERS.get(token)


def _plain(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text or "")))


def experience_statements(texts: Iterable[Optional[str]]) -> list[ExperienceStatement]:
    """Every years-of-experience statement in ``texts``; a zero minimum ("0-2 years") is not a requirement."""
    found: list[ExperienceStatement] = []
    for raw in texts:
        if not raw:
            continue
        text = _plain(raw)
        spans: list[tuple[int, int]] = []
        for loose, patterns in ((False, _STRICT), (True, _LOOSE)):
            for pattern in patterns:
                for match in pattern.finditer(text):
                    years = _number(match.group("min"))
                    if not years or years > 30 or any(match.start() < end and start < match.end() for start, end in spans):
                        continue
                    before = text[max(0, match.start() - 16) : match.start()]
                    if _AFTER_EXCLUDED.search(text[match.end() : match.end() + 30]):
                        continue
                    if loose and _BEFORE_EXCLUDED.search(before):
                        continue
                    if not loose and (_BEFORE_HISTORY.search(before) or (_BEFORE_EXCLUDED.search(before) and years >= _COMPANY_AGE_MIN_YEARS)):
                        continue
                    if _EDUCATION_IN_MATCH.search(match.group(0)):
                        continue
                    spans.append((match.start(), match.end()))
                    context = text[max(0, match.start() - 40) : match.end() + 60]
                    found.append(ExperienceStatement(years, match.group(0).strip(), bool(_PREFERRED.search(context)), loose))
    return found


def title_seniority(title: Optional[str]) -> Optional[str]:
    """"SENIOR", "MID" or None from the title alone ("SDE III", "Staff", "Architect", "Engineer II").

    "Associate"/"Junior" soften manager / architect / lead to MID; they never
    soften "Senior", "Staff", "Principal" or a level III.
    """
    text = title or ""
    if _ALWAYS_SENIOR.search(text):
        return "SENIOR"
    if _SENIOR_TITLE.search(text):
        return "MID" if _JUNIOR_MARKER.search(text) else "SENIOR"
    if _MID_TITLE.search(text):
        return "MID"
    return None
