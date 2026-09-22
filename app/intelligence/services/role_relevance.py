"""Role-family relevance: what kind of job a posting is, against the families the candidate targets.

Selection-quality fix (2026-09-14). The only role signal was token overlap
between a title and the candidate's target-role strings, worth 20% of fit
through preferences. Nothing said "Affiliate Marketing" or "Math Video Creator"
is a different discipline from "Software Engineer", so a fresh LOW-band
marketing role could be admitted and take a company's cool-down slot.

This module is deterministic and explainable:

* :func:`classify_role` reads the title's head ("Account Executive" in
  "Account Executive, AI Sales") through ordered family rules — engineering
  combinations that are only *potentially* technical (sales / solutions /
  support engineer), engineering role nouns with a topic (security, data,
  cloud...), then non-technical role nouns (marketing, content, sales, HR,
  finance, operations, education...). An unrecognised title ("Associate
  Analyst") falls back to the description: several technologies make it
  potentially technical; a dominant non-technical vocabulary makes it that
  family; otherwise it stays UNKNOWN and fit decides.
* :func:`assess_relevance` compares the job's family with the families of the
  candidate's own target roles (Career Brain preferences), so a marketing
  candidate is not judged by an engineering yardstick.
"""

import re
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from typing import Any, Optional


class RoleClass(str, Enum):
    TECHNICAL = "TECHNICAL"
    POTENTIALLY_TECHNICAL = "POTENTIALLY_TECHNICAL"
    NON_TECHNICAL = "NON_TECHNICAL"
    UNKNOWN = "UNKNOWN"


class Relevance(str, Enum):
    RELEVANT = "RELEVANT"
    ADJACENT = "ADJACENT"
    UNKNOWN = "UNKNOWN"
    WEAK = "WEAK"
    UNRELATED = "UNRELATED"
    NOT_ASSESSED = "NOT_ASSESSED"


#: Ordering used by the scheduler: relevant roles are considered first.
RELEVANCE_RANK = {Relevance.RELEVANT: 0, Relevance.ADJACENT: 1, Relevance.NOT_ASSESSED: 2, Relevance.UNKNOWN: 2, Relevance.WEAK: 3, Relevance.UNRELATED: 4}
#: Technologies a description must name before a potentially technical or unclassified role counts as technical.
TECHNICAL_EVIDENCE_MIN = 3

_T, _P, _N = RoleClass.TECHNICAL, RoleClass.POTENTIALLY_TECHNICAL, RoleClass.NON_TECHNICAL


def _rx(pattern: str) -> re.Pattern:
    return re.compile(pattern, re.IGNORECASE)


#: Potentially technical: engineering words attached to a customer-facing or adjacent function.
_POTENTIAL_RULES = (
    ("talent_pool", _N, _rx(r"\btalent (?:community|pool|network)\b|\bgeneral application\b|\bfuture opportunit|\bexpression of interest\b")),
    ("solutions_engineering", _P, _rx(r"\b(?:sales|solutions?|pre-?sales|field application|customer success|partner|alliances?)\s+(?:engineer|architect)\b|\bforward deployed\b")),
    ("technical_support", _P, _rx(r"\b(?:technical support|support engineer|application support|production support|it support|help ?desk|service desk|desktop support)\b")),
    ("implementation_consulting", _P, _rx(r"\b(?:implementation|functional|technical|sap|erp|oracle|salesforce|integration)\s+(?:consultant|specialist|analyst)\b")),
    ("product_management", _P, _rx(r"\b(?:product (?:manager|owner|lead|builder|analyst)|technical program manager|program manager)\b")),
    ("technical_analysis", _P, _rx(r"\b(?:business (?:systems )?analyst|systems analyst|technical analyst|it analyst|technical writer|documentation engineer)\b")),
)
#: A technical role noun; the topic decides the family.
_TECH_NOUN = _rx(
    r"\b(?:engineer|engineering|developer|programmer|sde|swe|sdet|devops|sre|mlops|scientist|architect|administrator|dba|"
    r"data analyst|security analyst|soc analyst|penetration tester|pentester|qa|quality assurance|quality analyst|test analyst|tester)\b"
)
_OTHER_ENGINEERING = _rx(r"\b(?:mechanical|civil|electrical|chemical|structural|hvac|manufacturing|process|mining|petroleum)\s+engineer")
_TECH_TOPICS = (
    ("ml_ai", _rx(r"machine learning|\bml\b|mlops|\bai\b|artificial intelligence|deep learning|\bnlp\b|computer vision|\bllm|genai|inference|perception|applied scien|research scien|research engineer")),
    ("data_science", _rx(r"data scien|\bscientist\b")),
    ("data_engineering", _rx(r"data engineer|analytics engineer|big data|\betl\b|data platform|database|\bdba\b|data infrastructure")),
    ("data_analytics", _rx(r"data analyst|analytics|business intelligence|\bbi\b")),
    ("security", _rx(r"secur|secops|siem|appsec|\bsoc\b|penetration|pentest|cyber|\biam\b")),
    ("devops_cloud", _rx(r"devops|\bsre\b|site reliability|reliability|cloud|platform|infrastructure|\binfra\b|network|systems? engineer|administrator|kubernetes|release engineer|build engineer")),
    ("qa_test", _rx(r"\bqa\b|quality|\btest|sdet")),
    ("mobile", _rx(r"android|\bios\b|mobile")),
    ("software", _rx(r".")),
)
_NON_TECHNICAL_RULES = (
    ("marketing", _rx(r"\b(?:marketing|marketer|seo|sem|paid search|paid social|brand|social media|public relations|\bpr\b|communications|campaign|affiliate|advertising|media buyer|demand generation|abm|community manager|events?)\b")),
    ("content_creative", _rx(r"\b(?:content|copywriter|writer|editor|video|motion|graphic|illustrator|animator|designer|creative|photographer|artist)\b")),
    # 2026-09-14 autopilot: "Account Development Representative" and "capital partnerships" were admitted.
    ("sales", _rx(r"\b(?:sales|account executive|account manager|account development|business development|bdr|sdr|adr|inside sales|telesales|key account|relationship manager|value consultant|solution advisor|gam|partner manager|partnerships?|channel manager|merchant|territory)\b")),
    ("customer_support", _rx(r"\b(?:customer (?:support|service|success|experience|care)|support specialist|product support|client (?:services|support)|call cent(?:er|re)|voice process|chat support)\b")),
    ("hr_recruiting", _rx(r"\b(?:recruit\w*|talent acquisition|human resources|hr|hrbp|people (?:partner|operations|experience)|employee experience|payroll|learning (?:and|&) development|trainer|training)\b")),
    ("finance_accounting", _rx(r"\b(?:account(?:ant|ing)|finance|financial|tax|treasury|billing|ledger|audit|sox|revenue|deal desk|controller|fp&a|payable|receivable|credit|collections?|underwriter|actuar\w*|investment|equity|reconciliation)\b")),
    ("legal", _rx(r"\b(?:legal|counsel|paralegal|compliance|contracts?|regulatory)\b")),
    ("operations", _rx(r"\b(?:m&e|monitoring (?:and|&) evaluation|operations|business operations|strategy|chief of staff|procurement|supply chain|logistics|facilities|workplace|office|administrative|assistant|receptionist|coordinator|team leader|store|warehouse|fleet|driver|quote)\b")),
    ("education", _rx(r"\b(?:teacher|tutor|faculty|lecturer|professor|instructor|educator|curriculum|math|mathematics|chemistry|physics|biology)\b")),
)
_HEAD_SPLIT = re.compile(r"\s[-–|:]\s|,|\(|/|\s-(?=\S)|(?<=\S)-\s")


@dataclass(frozen=True)
class RoleFamily:
    family: str
    role_class: RoleClass
    #: Where the family was read from: "title", "title (full)" or "description".
    basis: str = "title"


def _classify_text(text: str) -> Optional[RoleFamily]:
    for family, role_class, pattern in _POTENTIAL_RULES:
        if pattern.search(text):
            return RoleFamily(family, role_class)
    if _OTHER_ENGINEERING.search(text):
        return RoleFamily("other_engineering", _P)
    if _TECH_NOUN.search(text):
        for family, pattern in _TECH_TOPICS:
            if pattern.search(text):
                return RoleFamily(family, _T)
    for family, pattern in _NON_TECHNICAL_RULES:
        if pattern.search(text):
            return RoleFamily(family, _N)
    return None


def classify_role(title: Optional[str], description: Optional[str] = None, technologies: int = 0) -> RoleFamily:
    """The role family of a posting (or of a target-role string when only a title is given)."""
    title = (title or "").strip()
    head = next((part.strip() for part in _HEAD_SPLIT.split(title) if part and part.strip()), title)
    found = _classify_text(head) or _classify_text(title)
    if found is not None:
        if found.role_class is _T and head and _classify_text(head) is None:
            return RoleFamily(found.family, found.role_class, "title (full)")
        return found
    if technologies >= TECHNICAL_EVIDENCE_MIN:
        return RoleFamily("unclassified_technical", _P, "description")
    if description:
        text = description.lower()
        counts = [(len(pattern.findall(text)), family) for family, pattern in _NON_TECHNICAL_RULES]
        hits, family = max(counts)
        if hits >= 3 and technologies < 2:
            return RoleFamily(family, _N, "description")
    return RoleFamily("unclassified", RoleClass.UNKNOWN)


def technology_count(*lists: Any) -> int:
    """Distinct recognised technologies across a posting's extracted lists."""
    return len({item for values in lists for item in (values or [])})


def relevance_for_row(job: Any, preferences: Any) -> "RoleAssessment":
    """Relevance of a stored ``JobRow`` (title, description and extracted technologies)."""
    return assess_relevance(job.title, job.description, technology_count(job.technologies, job.required_skills), preferences)


@dataclass(frozen=True)
class RoleAssessment:
    relevance: Relevance
    role: RoleFamily
    detail: str
    #: False only for an unrelated role while the candidate has not opted into unrelated roles.
    in_policy: bool = True

    @property
    def label(self) -> str:
        return f"{self.relevance.value} {self.role.family}"


@lru_cache(maxsize=64)
def _target_families(targets: tuple) -> tuple[frozenset, bool]:
    """The candidate's target families, classified once per distinct target list.

    Performance audit (2026-09-14): re-classifying every target role for each of
    ~2,000 postings made the scheduler preview take ~3 s.
    """
    roles = [classify_role(target) for target in targets]
    return frozenset(r.family for r in roles), any(r.role_class is RoleClass.TECHNICAL for r in roles)


def assess_relevance(title: Optional[str], description: Optional[str], technologies: int, preferences: Any) -> RoleAssessment:
    """Relevance of a posting to the candidate's own target-role families."""
    targets = list(getattr(preferences, "all_target_roles", None) or []) if preferences is not None else []
    role = classify_role(title, description, technologies)
    if not targets:
        return RoleAssessment(Relevance.NOT_ASSESSED, role, "the candidate records no target roles")
    families, technical_candidate = _target_families(tuple(targets))
    what = f"'{title}' reads as {role.family.replace('_', ' ')} ({role.role_class.value.lower().replace('_', ' ')}, from the {role.basis})"
    if role.family in families:
        return RoleAssessment(Relevance.RELEVANT, role, f"{what}, one of the candidate's target families")
    if role.role_class is RoleClass.TECHNICAL:
        if technical_candidate:
            return RoleAssessment(Relevance.RELEVANT, role, f"{what}; the candidate targets technical roles")
        return RoleAssessment(Relevance.WEAK, role, f"{what}; the candidate's target roles are not technical")
    if role.role_class is RoleClass.POTENTIALLY_TECHNICAL:
        if technical_candidate and technologies >= TECHNICAL_EVIDENCE_MIN:
            return RoleAssessment(Relevance.ADJACENT, role, f"{what} and the posting names {technologies} technologies")
        return RoleAssessment(Relevance.WEAK, role, f"{what} and the posting names only {technologies} technologies")
    if role.role_class is RoleClass.NON_TECHNICAL:
        include = bool(getattr(preferences, "role_include_unrelated", False))
        return RoleAssessment(Relevance.UNRELATED, role, f"{what}, outside the candidate's target families ({', '.join(sorted(families))})", in_policy=include)
    return RoleAssessment(Relevance.UNKNOWN, role, f"'{title}' matches no known role family; fit decides")
