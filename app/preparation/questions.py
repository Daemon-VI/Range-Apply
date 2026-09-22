"""Application questions: normalisation, classification, and truthful answers.

Resolution order for a question, all deterministic:

1. exact normalised match in the approved answer bank;
2. an approved answer-bank entry in the question's *category* (so
   "Are you legally authorized to work in India?" and "Do you have the right
   to work in India?" share one entry);
3. a fact stored in the profile (work authorisation, education, location);
4. a templated narrative built from selected, application-safe evidence;
5. otherwise NEEDS_USER_INPUT (a candidate fact we do not hold) or
   NEEDS_REVIEW (a question we cannot ground).

A generated answer is not evidence; it cites the evidence it was built from.
"""

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from app.career.models import EvidenceKind
from app.career.repository import normalize_question
from app.preparation.evidence import EvidenceSelection, EvidenceSnapshot
from app.preparation.models import AnswerSource, PreparedAnswerStatus


class QuestionCategory(str, Enum):
    WORK_AUTHORIZATION = "work_authorization"
    SPONSORSHIP = "sponsorship"
    RELOCATION = "relocation"
    NOTICE_PERIOD = "notice_period"
    SALARY = "salary"
    WORK_MODE = "work_mode"
    START_DATE = "start_date"
    CLEARANCE = "clearance"
    VOLUNTARY = "voluntary"
    LOCATION = "location"
    EDUCATION = "education"
    EXPERIENCE_YEARS = "experience_years"
    #: Facts about the candidate's present situation and the application itself
    #: (real forms, 2026-09-22): current employer / title, how they heard about
    #: the job, earlier employment at this employer, conflicts of interest, and
    #: the acknowledgements every form asks for.
    CURRENT_EMPLOYER = "current_employer"
    CURRENT_TITLE = "current_title"
    REFERRAL_SOURCE = "referral_source"
    PRIOR_EMPLOYMENT = "prior_employment"
    CONFLICT_OF_INTEREST = "conflict_of_interest"
    CONSENT = "consent"
    ABOUT_YOU = "about_you"
    WHY_ROLE = "why_role"
    WHY_COMPANY = "why_company"
    RELEVANT_PROJECT = "relevant_project"
    EXPERIENCE_WITH = "experience_with"
    OTHER = "other"


#: Facts only the candidate can supply: never templated, never guessed.
FACT_CATEGORIES = frozenset(
    {
        QuestionCategory.WORK_AUTHORIZATION,
        QuestionCategory.SPONSORSHIP,
        QuestionCategory.RELOCATION,
        QuestionCategory.NOTICE_PERIOD,
        QuestionCategory.SALARY,
        QuestionCategory.WORK_MODE,
        QuestionCategory.START_DATE,
        QuestionCategory.CLEARANCE,
        QuestionCategory.VOLUNTARY,
        QuestionCategory.LOCATION,
        QuestionCategory.EDUCATION,
        QuestionCategory.EXPERIENCE_YEARS,
        QuestionCategory.CURRENT_EMPLOYER,
        QuestionCategory.CURRENT_TITLE,
        QuestionCategory.REFERRAL_SOURCE,
        QuestionCategory.PRIOR_EMPLOYMENT,
        QuestionCategory.CONFLICT_OF_INTEREST,
        QuestionCategory.CONSENT,
    }
)

#: Categories whose approved answer-bank entry answers every question of the category:
#: candidate facts (never voluntary ones) and narratives that do not depend on the employer
#: or on a topic. "Why this role / company" is employer specific and "experience with X"
#: is topic specific (an entry about Kafka must never answer a question about Redis).
#: A conflict-of-interest question is about *this* employer's people, and education
#: questions ask for different facts under one heading (university, degree, grade, year):
#: neither is answered by category.
_CATEGORY_SHARED_ANSWERS = (FACT_CATEGORIES - {QuestionCategory.VOLUNTARY, QuestionCategory.CONFLICT_OF_INTEREST, QuestionCategory.EDUCATION}) | {QuestionCategory.ABOUT_YOU, QuestionCategory.RELEVANT_PROJECT}

#: Answers written for one employer ("why us", "why this role").
_EMPLOYER_SPECIFIC = frozenset({QuestionCategory.WHY_COMPANY, QuestionCategory.WHY_ROLE})


def _names_company(question_key: str, company: Optional[str]) -> bool:
    """True when the normalised question names ``company`` ("why do you want to work at notion")."""
    name = normalize_question(company or "")
    return bool(name) and re.search(r"(?<![a-z0-9])" + re.escape(name) + r"(?![a-z0-9])", question_key) is not None


#: The standard set every package answers unless the caller supplies the form's own.
DEFAULT_QUESTIONS: list[str] = [
    "Why are you interested in this role?",
    "Tell us about yourself.",
    "Describe a relevant project.",
    "Are you legally authorized to work in this country?",
    "Will you now or in the future require sponsorship?",
    "What is your notice period?",
    "What are your salary expectations?",
]

_RULES: list[tuple[QuestionCategory, re.Pattern[str]]] = [
    (QuestionCategory.SPONSORSHIP, re.compile(r"\b(sponsor|sponsorship|visa)\b")),
    (QuestionCategory.WORK_AUTHORIZATION, re.compile(r"\b(authori[sz]ed|authori[sz]ation|right to work|eligible to work|legally (able|permitted) to work|work permit)\b")),
    (QuestionCategory.RELOCATION, re.compile(r"\b(relocat)")),
    (QuestionCategory.NOTICE_PERIOD, re.compile(r"\b(notice period|earliest start|how soon|availability to start|when (can|could) you start)\b")),
    (QuestionCategory.START_DATE, re.compile(r"\b(start date|available to start|join(ing)? date)\b")),
    (QuestionCategory.SALARY, re.compile(r"\b(salary|compensation|ctc|pay expectation|expected pay|rate expectation)\b")),
    (QuestionCategory.WORK_MODE, re.compile(r"\b(remote|hybrid|on ?site|in office|work from home)\b")),
    (QuestionCategory.CLEARANCE, re.compile(r"\b(security clearance|clearance)\b")),
    (QuestionCategory.VOLUNTARY, re.compile(r"\b(gender|ethnicity|race|disability|veteran|pronouns|voluntary self|eeo)\b")),
    # Real forms (2026-09-22): acknowledgements, conflicts of interest, earlier employment
    # here, current employer / title and "how did you hear about us". Conflicts come
    # before prior employment ("family member employed by Okta" is a conflict question).
    (QuestionCategory.CONSENT, re.compile(r"^i (?:acknowledge|agree|consent|confirm|certify|accept|have read|understand)\b|\backnowledg|\bconsent to\b|\bprivacy (?:notice|policy|statement)\b|\bterms (?:and|&) conditions\b|\bby (?:checking|ticking) this box\b|\bcertify that\b")),
    (QuestionCategory.CONFLICT_OF_INTEREST, re.compile(r"\bfamily member|\brelatives?\b|\bfamilial\b|\bclose personal relationship|\boutside business|\bconflict of interest\b")),
    (QuestionCategory.PRIOR_EMPLOYMENT, re.compile(r"\b(?:ever|previously|before)\b.*\b(?:worked|employed)\b|\b(?:worked|employed)\b.*\b(?:before|previously)\b|\bbeen employed by\b|\bcurrently or have you ever\b|\bformer (?:employee|employer)\b")),
    (QuestionCategory.CURRENT_EMPLOYER, re.compile(r"^(?:what is )?(?:the name of )?(?:your )?current(?: or most recent)? (?:company|employer|organi[sz]ation)\b|^who is your current|\bmost recent employer\b|^current company$|^(?:company|employer) name$|^employer$")),
    (QuestionCategory.CURRENT_TITLE, re.compile(r"^(?:what is )?(?:your )?current(?: or most recent)? (?:job )?(?:title|role|designation|position)\b|\bmost recent title\b|^(?:job )?title$|^designation$")),
    (QuestionCategory.REFERRAL_SOURCE, re.compile(r"\bhow did you (?:hear|learn|find out|come across|find|discover)\b|\bwhere did you (?:hear|find|see|learn)\b|\breferral source\b|\bsource of (?:application|referral)\b")),
    (QuestionCategory.EDUCATION, re.compile(r"\b(degree|education|university|college|graduat|cgpa|gpa|school|institution|discipline|major|field of study|speciali[sz]ation)\b")),
    (QuestionCategory.EXPERIENCE_YEARS, re.compile(r"\b(how many years|years of (professional |work )?experience|total experience)\b")),
    (QuestionCategory.EXPERIENCE_WITH, re.compile(r"\b(?:experience|familiarity|proficiency|background) (?:with|in|using) (?P<topic>[a-z0-9+#. ]{2,40})")),
    (QuestionCategory.RELEVANT_PROJECT, re.compile(r"\b(project|something you built|work you are proud of)\b")),
    (QuestionCategory.WHY_COMPANY, re.compile(r"\b(why (do you want to work|are you interested in working|us|this company|our company|join)|why .* company)\b")),
    (QuestionCategory.WHY_ROLE, re.compile(r"\b(why (this|the) (role|position|job)|why are you interested|interest(ed)? in (this|the) (role|position)|motivat)")),
    (QuestionCategory.ABOUT_YOU, re.compile(r"\b(about yourself|introduce yourself|tell us about you|who are you)\b")),
    (QuestionCategory.LOCATION, re.compile(r"\bwhere are you (?:based|located)\b|\bcurrent location\b|\bcity\b|^location$|\bwhere do you (?:currently )?(?:reside|live)\b|\bresidence\b|\bcurrently (?:based|residing|located|living)\b|\bbased in\b")),
]


@dataclass
class ClassifiedQuestion:
    question: str
    key: str
    category: QuestionCategory
    topic: Optional[str] = None


def classify_question(question: str) -> ClassifiedQuestion:
    key = normalize_question(question)
    for category, pattern in _RULES:
        match = pattern.search(key)
        if match:
            topic = match.groupdict().get("topic") if match.groupdict() else None
            return ClassifiedQuestion(question, key, category, topic.strip() if topic else None)
    return ClassifiedQuestion(question, key, QuestionCategory.OTHER)


@dataclass
class ResolvedAnswer:
    status: PreparedAnswerStatus
    source: AnswerSource
    category: QuestionCategory
    answer: Optional[str] = None
    evidence_keys: list[str] = field(default_factory=list)
    entry_id: Optional[str] = None
    reason: Optional[str] = None


class AnswerResolver:
    def __init__(self, snapshot: EvidenceSnapshot):
        self.snapshot = snapshot

    def resolve(
        self,
        question: str,
        selection: EvidenceSelection,
        job_title: str,
        company: str,
    ) -> ResolvedAnswer:
        classified = classify_question(question)
        category = classified.category

        entry = self.snapshot.answer_by_key.get(classified.key)
        # Audit fix (2026-09-14): the exact-question path had the same leak for
        # generic wording: "Why do you want to work here?" saved while applying
        # to one employer was reused verbatim for every other employer asking
        # the same words. An employer-specific answer is reused only when the
        # question itself names this employer.
        if (
            entry is not None
            and category in _EMPLOYER_SPECIFIC
            and not _names_company(classified.key, company)
        ):
            entry = None
        # Audit fix (2026-09-14): the category fallback reused *any* approved entry of
        # the category, so a saved "Why do you want to work at Acme?" answered another
        # employer's "Why this company?". Only candidate facts (authorization, notice
        # period, salary...) are the same answer whatever the wording; narrative and
        # employer-specific answers need their own question, and voluntary questions
        # are never answered from stored data.
        if entry is None and category in _CATEGORY_SHARED_ANSWERS:
            candidates = self.snapshot.answers_by_category.get(category.value, [])
            entry = candidates[0] if candidates else None
        if entry is not None:
            return ResolvedAnswer(
                PreparedAnswerStatus.ANSWERED,
                AnswerSource.ANSWER_BANK,
                category,
                answer=entry.answer,
                evidence_keys=list(entry.evidence_keys or []),
                entry_id=entry.id,
                reason="approved answer bank entry",
            )

        profile_answer = self._from_profile(category)
        if profile_answer is not None:
            return profile_answer

        if category in FACT_CATEGORIES:
            return ResolvedAnswer(
                PreparedAnswerStatus.NEEDS_USER_INPUT,
                AnswerSource.NONE,
                category,
                reason=f"{category.value}: not recorded in the Career Brain or answer bank",
            )

        generated = self._generate(classified, selection, job_title, company)
        if generated is not None:
            return generated
        return ResolvedAnswer(
            PreparedAnswerStatus.NEEDS_REVIEW,
            AnswerSource.NONE,
            category,
            reason="cannot be grounded in stored evidence; needs a human draft",
        )

    # ------------------------------------------------------------------ #

    def _from_profile(self, category: QuestionCategory) -> Optional[ResolvedAnswer]:
        profile = self.snapshot.profile
        if profile is None:
            return None
        if category is QuestionCategory.WORK_AUTHORIZATION and profile.work_authorization:
            return ResolvedAnswer(PreparedAnswerStatus.ANSWERED, AnswerSource.PROFILE, category, profile.work_authorization, ["profile:work_authorization"], reason="profile field")
        if category is QuestionCategory.LOCATION and profile.location:
            return ResolvedAnswer(PreparedAnswerStatus.ANSWERED, AnswerSource.PROFILE, category, profile.location, ["profile:location"], reason="profile field")
        if category is QuestionCategory.EDUCATION:
            education = self.snapshot.of_kind(EvidenceKind.EDUCATION)
            if education:
                node = education[0]
                return ResolvedAnswer(PreparedAnswerStatus.ANSWERED, AnswerSource.GENERATED, category, node.claim, [node.key], reason="education evidence")
        if category is QuestionCategory.WORK_MODE and self.snapshot.preferences and self.snapshot.preferences.remote_preference:
            return ResolvedAnswer(PreparedAnswerStatus.ANSWERED, AnswerSource.PROFILE, category, self.snapshot.preferences.remote_preference, ["profile:remote_preference"], reason="preference")
        return None

    def _generate(
        self,
        classified: ClassifiedQuestion,
        selection: EvidenceSelection,
        job_title: str,
        company: str,
    ) -> Optional[ResolvedAnswer]:
        snapshot = self.snapshot
        category = classified.category
        skills = [snapshot.nodes[k] for k in selection.skills[:6] if k in snapshot.nodes]
        projects = [snapshot.nodes[k] for k in selection.projects[:2] if k in snapshot.nodes]

        if category is QuestionCategory.EXPERIENCE_WITH and classified.topic:
            node = snapshot.skill_by_label(classified.topic) or self._skill_containing(classified.topic)
            if node is None or not snapshot.is_safe(node.key):
                return ResolvedAnswer(
                    PreparedAnswerStatus.NEEDS_USER_INPUT,
                    AnswerSource.NONE,
                    category,
                    reason=f"no confirmed evidence for {classified.topic!r}",
                )
            demonstrating = [snapshot.nodes[k] for k in snapshot.projects_demonstrating(node.key)]
            text = f"{node.label}: {node.claim}."
            if demonstrating:
                text += " Used in " + ", ".join(p.label for p in demonstrating[:3]) + "."
            return ResolvedAnswer(PreparedAnswerStatus.ANSWERED, AnswerSource.GENERATED, category, text, [node.key] + [p.key for p in demonstrating[:3]], reason="templated from confirmed skill evidence")

        if not skills and not projects:
            return None

        if category is QuestionCategory.RELEVANT_PROJECT and projects:
            project = projects[0]
            technologies = list((project.attributes or {}).get("technologies") or [])
            text = f"{project.label}: {project.claim}"
            if technologies:
                text += f" Built with {', '.join(technologies)}."
            return ResolvedAnswer(PreparedAnswerStatus.ANSWERED, AnswerSource.GENERATED, category, text, [project.key], reason="templated from project evidence")

        if category in (QuestionCategory.WHY_ROLE, QuestionCategory.WHY_COMPANY):
            requirements = list(selection.matched_requirements.keys())[:3]
            parts = [f"The {job_title} role at {company} calls for " + ", ".join(requirements) + "." if requirements else f"The {job_title} role at {company} matches my background."]
            if skills:
                parts.append("I bring " + ", ".join(s.label for s in skills) + ".")
            if projects:
                parts.append("Most relevant: " + "; ".join(f"{p.label} — {p.claim}" for p in projects) + ".")
            keys = [s.key for s in skills] + [p.key for p in projects]
            return ResolvedAnswer(PreparedAnswerStatus.ANSWERED, AnswerSource.GENERATED, category, " ".join(parts), keys, reason="templated from matched requirements and evidence")

        if category is QuestionCategory.ABOUT_YOU:
            statement = snapshot.profile.positioning_statement if snapshot.profile else ""
            parts = [statement] if statement else []
            if skills:
                parts.append("Core skills: " + ", ".join(s.label for s in skills) + ".")
            if projects:
                parts.append("Recent work: " + "; ".join(f"{p.label} — {p.claim}" for p in projects))
            keys = [s.key for s in skills] + [p.key for p in projects]
            return ResolvedAnswer(PreparedAnswerStatus.ANSWERED, AnswerSource.GENERATED, category, " ".join(parts).strip(), keys, reason="templated from profile statement and evidence")

        return None

    def _skill_containing(self, topic: str):
        wanted = topic.lower()
        for node in self.snapshot.of_kind(EvidenceKind.SKILL):
            label = node.label.lower()
            if wanted in label or label in wanted:
                return node
        return None
