"""Map discovered form fields to truthful answers.

Resolution per field, all deterministic and offline:

1. file uploads -> the prepared resume / cover-letter artifact by label;
2. identity fields (name, email, phone, links, country) -> profile facts;
3. exact normalised question match -> prepared answer, then the approved bank;
4. category match -> prepared answer, then the bank (facts only; never an
   employer-specific narrative, never a conflict-of-interest or education
   question, whose wording decides which fact is asked);
5. education sub-questions (university, degree, grade, discipline, year) ->
   the matching profile fact;
6. otherwise: a candidate fact (work authorisation, sponsorship, salary,
   demographics, current employer, ...) becomes NEEDS_USER_INPUT, an
   ungroundable narrative NEEDS_REVIEW, an unknown field type is never filled
   (NEEDS_REVIEW when required, SKIPPED otherwise).

Choice fields (select / radio / checkbox / multi-select / yes-no buttons) are
only answered when the answer resolves unambiguously to one of the offered
options; numeric and date fields only when the answer parses. A searchable
dropdown (combobox) renders its options only once someone types, so its
answer is carried as text and the executor picks the option that matches it
on the page, with the same matching rules, or leaves the field alone.
Anything else is a question for the candidate, never a guess.
"""

import hashlib
import json
import re
from datetime import datetime
from typing import Optional

from app.career.repository import normalize_question
from app.execution.models import (
    ExecutionPackage,
    FieldAnswer,
    FieldAnswerSource,
    FieldAnswerStatus,
    FieldType,
    FormField,
    FormSnapshot,
)
from app.preparation.models import PreparedAnswerStatus
from app.preparation.questions import (
    FACT_CATEGORIES,
    QuestionCategory,
    _names_company,
    classify_question,
)

#: Fact categories a stored profile field answers directly (a recorded fact, never an inference).
_CATEGORY_FACTS = {QuestionCategory.LOCATION: "location"}

#: Answers written for one employer or role must not be reused for another by
#: category alone (pilot finding: a "why us?" answer naming company X was
#: mapped to company Y). Only an exact-question match or the tailored
#: preparation may answer these.
_EMPLOYER_SPECIFIC = frozenset({QuestionCategory.WHY_COMPANY, QuestionCategory.WHY_ROLE})
#: The same set as plain category values, for pages that tell the person whether an answer is reusable.
EMPLOYER_SPECIFIC_CATEGORIES = frozenset(c.value for c in _EMPLOYER_SPECIFIC)
#: Categories whose questions ask for *different* facts under one heading: a saved
#: "2027" (graduation year) must never answer "University", and "do you have
#: relatives at Okta?" is not "do you have relatives at Zeta?" (2026-09-22).
_EXACT_ONLY = frozenset({QuestionCategory.EDUCATION, QuestionCategory.CONFLICT_OF_INTEREST})
#: "How many years with SaaS?" is not "total experience": a topic makes the question exact-only.
_TOPIC_HINT = re.compile(r"\b(?:with|in|using|building) (?!total\b|professional\b|work\b|overall\b|relevant\b|industry\b|the\b|your\b|a\b|an\b)[a-z]")
#: A field that asks for a link (first real dry run, Replit: "Project URL" received the
#: relevant-project prose by category). Only an exact-question answer that is itself a link fits.
_URL_HINT = re.compile(r"\burl\b|\blink\b|\bhttps?\b")
_LINK_VALUE = re.compile(r"^https?://\S+$", re.IGNORECASE)
#: Credentials are never filled ("Project Password" on the same form).
_SECRET_HINT = re.compile(r"\bpass ?(word|code|phrase)\b")
#: An upload that fills the form from a resume (Ashby "Autofill from resume"), not the resume field.
_AUTOFILL_HINT = re.compile(r"\bauto ?fill")

#: Fields we fill from the profile, keyed by a normalised label pattern.
_IDENTITY_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^(first|given|preferred( first)?) name$|^first$"), "first_name"),
    (re.compile(r"^(last|family|sur) ?name$|^last$"), "last_name"),
    (re.compile(r"^(full |your )?name$|^name \(full\)$"), "name"),
    (re.compile(r"e-?mail"), "email"),
    (re.compile(r"phone|mobile|telephone"), "phone"),
    (re.compile(r"linkedin"), "linkedin"),
    (re.compile(r"github"), "github"),
    (re.compile(r"portfolio|website|personal site"), "portfolio"),
    # The country is the last part of the recorded location ("Hyderabad, Telangana, India").
    (re.compile(r"^country( of residence)?$|^country you (live|reside) in$|^which country"), "country"),
]
#: Identity facts that may pick an option in a choice field (a country in a
#: dropdown). The others are only ever typed: Notion's "How did you hear about
#: this job?" checkbox "LinkedIn" was ticked because the profile holds a LinkedIn
#: URL (first real dry run, 2026-09-14).
_OPTION_SAFE_FACTS = frozenset({"country"})
_CHOICE_TYPES = frozenset({FieldType.SELECT, FieldType.RADIO, FieldType.CHECKBOX, FieldType.MULTI_SELECT, FieldType.YESNO})
#: Consent / acknowledgement boxes the person's one recorded consent may tick.
_CONSENT_TYPES = frozenset({FieldType.CHECKBOX, FieldType.SELECT, FieldType.RADIO, FieldType.YESNO, FieldType.COMBOBOX})
_RESUME_HINT = re.compile(r"resume|résumé|\bcv\b|curriculum")
_COVER_HINT = re.compile(r"cover ?letter")
_YES = {"yes", "y", "true"}
_NO = {"no", "n", "false"}
#: Never inferred, even when the bank has something in the category.
_SENSITIVE = frozenset({QuestionCategory.VOLUNTARY})
#: A question shaped for a yes / no answer ("Are you currently based in Bangalore?"):
#: a stored fact that is not a yes or a no never answers it.
_YESNO_QUESTION = re.compile(r"^(?:are|do|did|does|have|has|is|can|could|will|would|were|was|should|may) you\b|^(?:are|is) there\b")

#: Education sub-questions answered by a specific profile fact (all recorded, none inferred).
_EDUCATION_FACTS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"school|university|college|institution|institute|alma mater"), "college"),
    (re.compile(r"^degree\b|degree (type|level|name)|highest (level of )?(education|degree|qualification)|^qualification|^education( level)?$"), "degree"),
    (re.compile(r"cgpa|gpa|grade|percentage|marks|aggregate"), "cgpa"),
    (re.compile(r"graduation|year of (passing|completion|graduating)|passing year|completion year"), "graduation_year"),
    (re.compile(r"discipline|major|branch|field of study|speciali[sz]ation|stream|course of study"), "branch"),
]

#: Countries a question may name. A stored work-authorisation / sponsorship answer
#: is about the candidate's own country: "authorized to work in the United States?"
#: is not answered by "yes, in India" (real Zeta Global form, 2026-09-22).
_COUNTRY_ALIASES: dict[str, tuple[str, ...]] = {
    "united states": ("united states", "usa", "us", "u s", "u s a", "america"),
    "united kingdom": ("united kingdom", "uk", "u k", "britain", "great britain", "england"),
    "india": ("india",),
    "canada": ("canada",),
    "germany": ("germany",),
    "france": ("france",),
    "netherlands": ("netherlands",),
    "ireland": ("ireland",),
    "australia": ("australia",),
    "singapore": ("singapore",),
    "japan": ("japan",),
    "united arab emirates": ("united arab emirates", "uae", "dubai"),
    "europe": ("europe", "eu", "european union"),
}


#: Words that make up a "why us?" question without naming anyone ("why do you want to work here").
_GENERIC_WORDS = frozenset(
    "why do does would you want like to work at for with here us our this the a an company role position team join "
    "are interested in be part of apply applying what makes excited about is it organisation organization job opportunity".split()
)


def _generic_question(question_key: str) -> bool:
    """True when the question names no employer or role of its own, so a stored answer could belong to anyone."""
    return not (set(question_key.split()) - _GENERIC_WORDS)


def _word(text: str, key: str) -> bool:
    return re.search(r"(?<![a-z0-9])" + re.escape(text) + r"(?![a-z0-9])", key) is not None


def _canonical_country(name: str) -> Optional[str]:
    wanted = normalize_question(name)
    for canonical, aliases in _COUNTRY_ALIASES.items():
        if wanted in aliases:
            return canonical
    return wanted or None


def names_other_country(question_key: str, own_country: Optional[str]) -> bool:
    """True when the question names a country and it is not the candidate's own."""
    named = {canonical for canonical, aliases in _COUNTRY_ALIASES.items() if any(_word(a, question_key) for a in aliases)}
    own = _canonical_country(own_country or "")
    if not named or own is None:
        return False  # no country named, or none on record to compare with
    return own not in named


def _polarity(text: str) -> Optional[str]:
    first = normalize_question(text).split(" ")[0] if text else ""
    return "yes" if first in _YES else "no" if first in _NO else None


def _degree_level(text: str) -> Optional[str]:
    """The level a recorded degree title states (a B.Tech *is* a bachelor's degree)."""
    if re.match(r"^(b ?tech|b ?e\b|b ?sc?\b|b ?a\b|bba|bca|bachelor)", text):
        return "bachelor"
    if re.match(r"^(m ?tech|m ?e\b|m ?sc?\b|m ?a\b|mba|mca|master)", text):
        return "master"
    if re.match(r"^(ph ?d|doctor)", text):
        return "doctor"
    return None


def search_terms(text: str, category: str = "other") -> list[str]:
    """What to type into a searchable dropdown to make it show the option for ``text``.

    The answer itself first; then the first part of a "City, State, Country";
    then the bare yes / no of a sentence answer; then the level word of a
    degree. Each term only narrows the list; :func:`choose_option` still
    decides, on the options shown, whether one names the answer.
    """
    terms = [text.strip()]
    if "," in text:
        first = text.split(",")[0].strip()
        if first:
            terms.append(first)
    polarity = _polarity(text)
    if polarity:
        terms.append(polarity)
    if category == QuestionCategory.EDUCATION.value:
        level = _degree_level(normalize_question(text))
        if level:
            terms.append(level)
    seen: list[str] = []
    for term in terms:
        if term and term.lower() not in [s.lower() for s in seen]:
            seen.append(term)
    return seen


def choose_option(labels: list[str], text: str, category: str = "other") -> Optional[str]:
    """The one option label ``text`` unambiguously names, or None.

    Exact normalised match first; then a yes / no by polarity; then, for a
    place written as "City, State, Country", the one option holding every
    part; then the one option that contains (or is contained in) the answer;
    then, for a degree, the one option naming the same level. Two candidates
    are no answer.
    """
    wanted = normalize_question(text)
    if not wanted:
        return None
    norm = [(label, normalize_question(label)) for label in labels]
    exact = [label for label, n in norm if n == wanted]
    if len(exact) == 1:
        return exact[0]
    polarity = _polarity(wanted)
    if polarity:
        hits = [label for label, n in norm if _polarity(n) == polarity]
        if len(hits) == 1:
            return hits[0]
    parts = [normalize_question(p) for p in text.split(",")]
    parts = [p for p in parts if p]
    if len(parts) > 1:
        hits = [label for label, n in norm if all(_word(p, n) for p in parts)]
        if len(hits) == 1:
            return hits[0]
        if hits:
            tight = [label for label in hits if normalize_question(label) == " ".join(parts)]
            return tight[0] if len(tight) == 1 else None
    contained = [label for label, n in norm if n and (n in wanted or wanted in n)]
    if len(contained) == 1:
        return contained[0]
    if category == QuestionCategory.EDUCATION.value:
        level = _degree_level(wanted)
        if level:
            hits = [label for label, n in norm if level in n]
            if len(hits) == 1:
                return hits[0]
    return None


def form_fingerprint(fields: list[FormField]) -> str:
    """Stable digest of the form's structure (not its values)."""
    shape = [
        {
            "id": f.external_id,
            "label": normalize_question(f.label),
            "type": f.field_type.value,
            "required": f.required,
            "options": [normalize_question(o.label) for o in f.options],
        }
        for f in fields
    ]
    return hashlib.sha256(json.dumps(shape, sort_keys=True).encode("utf-8")).hexdigest()


def map_fields(fields: list[FormField], package: ExecutionPackage) -> list[FieldAnswer]:
    prepared = {a.question_key: a for a in package.answers}
    prepared_by_category: dict[str, list] = {}
    for answer in package.answers:
        prepared_by_category.setdefault(answer.category, []).append(answer)
    bank = {b.question_key: b for b in package.answer_bank}
    bank_by_category: dict[str, list] = {}
    for entry in package.answer_bank:
        bank_by_category.setdefault(entry.category, []).append(entry)
    return [_map_one(f, package, prepared, prepared_by_category, bank, bank_by_category) for f in fields]


def _ask(base: dict, field: FormField, reason: str, review: bool = False, **extra) -> FieldAnswer:
    """The candidate's question (or a review) when required; skipped when optional."""
    status = FieldAnswerStatus.NEEDS_REVIEW if review else FieldAnswerStatus.NEEDS_USER_INPUT
    return FieldAnswer(**base, status=status if field.required else FieldAnswerStatus.SKIPPED, reason=reason, **extra)


def _map_one(field: FormField, package: ExecutionPackage, prepared, prepared_by_category, bank, bank_by_category) -> FieldAnswer:
    key = normalize_question(field.label)
    classified = classify_question(field.label)
    category = classified.category
    base = dict(external_id=field.external_id, label=field.label, question_key=key, field_type=field.field_type, required=field.required, category=category.value)
    profile = package.profile

    if field.field_type is FieldType.UNKNOWN:
        return _ask(base, field, "unknown field type is never filled automatically", review=True)

    if field.field_type is FieldType.FILE:
        # Real boards label the control "Attach" and name it `resume` /
        # `cover_letter` (Greenhouse, Phase 13): the name counts as much as the label.
        file_key = f"{key} {normalize_question((field.external_id or '').replace('_', ' '))}"
        if _AUTOFILL_HINT.search(file_key) and not field.required:
            # Ashby's "Autofill from resume" parses the upload and rewrites the
            # form's fields itself (audit 2026-09-14): not the application's resume.
            return FieldAnswer(**base, status=FieldAnswerStatus.SKIPPED, reason="an autofill helper, not the application's resume upload")
        if _RESUME_HINT.search(file_key) and package.resume is not None:
            return FieldAnswer(**base, status=FieldAnswerStatus.ANSWERED, source=FieldAnswerSource.ARTIFACT, artifact_type="RESUME", evidence_keys=list(package.resume.evidence_keys), reason="prepared resume artifact")
        if _COVER_HINT.search(file_key) and package.cover_letter is not None:
            return FieldAnswer(**base, status=FieldAnswerStatus.ANSWERED, source=FieldAnswerSource.ARTIFACT, artifact_type="COVER_LETTER", evidence_keys=list(package.cover_letter.evidence_keys), reason="prepared cover letter artifact")
        if _COVER_HINT.search(key) and not field.required:
            return FieldAnswer(**base, status=FieldAnswerStatus.SKIPPED, reason="cover letter disabled for this band")
        return _ask(base, field, "no prepared artifact matches this upload")

    picks_option = field.field_type in _CHOICE_TYPES or field.field_type is FieldType.COMBOBOX
    for pattern, fact in _IDENTITY_PATTERNS:
        # A profile value is typed into an input, never used to pick an option
        # (except a country, which is a plain fact with a plain option).
        if picks_option and fact not in _OPTION_SAFE_FACTS:
            continue
        if pattern.search(key):
            value = profile.get(fact)
            if value:
                return _typed(field, base, str(value), FieldAnswerSource.PROFILE, [f"profile:{fact}"], "profile field")
            return _ask(base, field, f"profile has no {fact}")

    if category in _SENSITIVE:
        return _ask(base, field, "voluntary / demographic question: only the candidate answers this")

    if _SECRET_HINT.search(key):
        return _ask(base, field, "a password or passcode is never filled automatically")

    if _URL_HINT.search(key):
        for candidate, source, evidence, reason, prep_id in (
            (prepared.get(key), FieldAnswerSource.PREPARATION, None, "prepared link (exact question)", True),
            (bank.get(key), FieldAnswerSource.ANSWER_BANK, None, "approved answer bank link (exact question)", False),
        ):
            if candidate is None:
                continue
            if prep_id and not (candidate.status == PreparedAnswerStatus.ANSWERED.value and candidate.answer):
                continue
            value = (candidate.answer or "").strip()
            if _LINK_VALUE.match(value):
                return _typed(field, base, value, source, list(candidate.evidence_keys), reason, preparation_answer_id=candidate.id if prep_id else None)
        return _ask(base, field, "a link is asked for and no recorded answer to this exact question is a link")

    # Priority: exact question key (prepared, then bank) before any category match.
    answer = prepared.get(key)
    if answer is not None and answer.status == PreparedAnswerStatus.ANSWERED.value and answer.answer:
        return _typed(field, base, answer.answer, FieldAnswerSource.PREPARATION, list(answer.evidence_keys), "prepared answer (exact question)", preparation_answer_id=answer.id)
    entry = bank.get(key)
    # Audit (2026-09-14): a bank answer to a generically worded "why us?" question was written for
    # one employer; by exact question it is reused only where the question itself names this company.
    if entry is not None and category in _EMPLOYER_SPECIFIC and _generic_question(key) and not _names_company(key, package.target.company):
        entry = None
    if entry is not None:
        return _typed(field, base, entry.answer, FieldAnswerSource.ANSWER_BANK, list(entry.evidence_keys), "approved answer bank entry (exact question)")
    if answer is not None and answer.status == PreparedAnswerStatus.NEEDS_USER_INPUT.value:
        return FieldAnswer(**base, status=FieldAnswerStatus.NEEDS_USER_INPUT, preparation_answer_id=answer.id, reason=answer.status.lower() + " on the preparation")

    if category is not QuestionCategory.OTHER:
        by_category = next((a for a in prepared_by_category.get(category.value, []) if a.status == PreparedAnswerStatus.ANSWERED.value and a.answer), None)
        if by_category is not None:
            return _typed(field, base, by_category.answer, FieldAnswerSource.PREPARATION, list(by_category.evidence_keys), f"prepared answer ({category.value})", preparation_answer_id=by_category.id)
        guarded = _category_guard(field, base, key, category, package)
        if guarded is not None:
            return guarded
        entries = bank_by_category.get(category.value, []) if _shareable(category, key) else []
        if entries:
            return _typed(field, base, entries[0].answer, FieldAnswerSource.ANSWER_BANK, list(entries[0].evidence_keys), f"approved answer bank entry ({category.value})")

    if category is QuestionCategory.EDUCATION:
        # Different facts under one heading: the label says which one is asked.
        for pattern, fact in _EDUCATION_FACTS:
            if pattern.search(key):
                if profile.get(fact):
                    return _typed(field, base, str(profile[fact]), FieldAnswerSource.PROFILE, [f"profile:{fact}"], f"profile {fact}")
                break

    fact = _CATEGORY_FACTS.get(category)
    if fact and profile.get(fact):
        # A recorded profile fact (pilot finding: "Location (City)" / "Current
        # location" were asked of the candidate although the profile holds it).
        return _typed(field, base, str(profile[fact]), FieldAnswerSource.PROFILE, [f"profile:{fact}"], f"profile {fact}")
    if category in FACT_CATEGORIES:
        return _ask(base, field, f"{category.value}: not recorded in the answer bank or profile")
    return _ask(base, field, "cannot be grounded in prepared material", review=True)


def _shareable(category: QuestionCategory, key: str) -> bool:
    """Whether one approved answer of the category fits every question of the category."""
    if category in _EMPLOYER_SPECIFIC or category in _EXACT_ONLY:
        return False
    if category is QuestionCategory.EXPERIENCE_YEARS and _TOPIC_HINT.search(key):
        return False
    return True


def _category_guard(field: FormField, base: dict, key: str, category: QuestionCategory, package: ExecutionPackage) -> Optional[FieldAnswer]:
    """Cases where a category answer must not be reused, decided before the bank is consulted."""
    profile = package.profile
    if category in (QuestionCategory.WORK_AUTHORIZATION, QuestionCategory.SPONSORSHIP) and names_other_country(key, profile.get("country")):
        return _ask(base, field, f"{category.value}: the question names another country than the profile's; answer it yourself")
    if category is QuestionCategory.PRIOR_EMPLOYMENT:
        employers = [normalize_question(str(e)) for e in (profile.get("employers") or [])]
        company = normalize_question(package.target.company or "")
        if company and any(e and (e in company or company in e) for e in employers):
            return _ask(base, field, "prior_employment: the profile records work at this company; answer it yourself")
    if category is QuestionCategory.CONSENT:
        if field.field_type not in _CONSENT_TYPES:
            return _ask(base, field, "consent: a typed acknowledgement is yours to write")
        if not field.required:
            return FieldAnswer(**base, status=FieldAnswerStatus.SKIPPED, reason="optional consent / opt-in: left to you")
    return None


def _typed(field: FormField, base: dict, text: str, source: FieldAnswerSource, evidence: list[str], reason: str, preparation_answer_id: Optional[str] = None) -> FieldAnswer:
    """Fit a textual answer to the field type, or ask the candidate."""
    kind = field.field_type
    common = dict(source=source, evidence_keys=evidence, preparation_answer_id=preparation_answer_id)
    if kind in (FieldType.TEXT, FieldType.TEXTAREA, FieldType.EMAIL, FieldType.PHONE):
        return FieldAnswer(**base, status=FieldAnswerStatus.ANSWERED, answer=text, reason=reason, **common)
    if kind in _CHOICE_TYPES:
        chosen = _match_options(field, text, multi=kind is FieldType.MULTI_SELECT)
        if chosen:
            return FieldAnswer(**base, status=FieldAnswerStatus.ANSWERED, answer=text, selected_values=chosen, reason=f"{reason}; matched option", **common)
        return _ask(base, field, f"{reason}, but it does not match an offered option", answer=text, **common)
    if kind is FieldType.COMBOBOX:
        if field.options:
            chosen = choose_option([o.label for o in field.options], text, category=base["category"])
            if chosen:
                return FieldAnswer(**base, status=FieldAnswerStatus.ANSWERED, answer=text, selected_values=[chosen], reason=f"{reason}; matched option", **common)
            return _ask(base, field, f"{reason}, but it does not match an offered option", answer=text, **common)
        if _YESNO_QUESTION.match(base["question_key"]) and _polarity(text) is None:
            return _ask(base, field, f"{reason}, but a yes / no question needs a yes or a no", answer=text, **common)
        return FieldAnswer(**base, status=FieldAnswerStatus.ANSWERED, answer=text, reason=f"{reason}; the matching option is chosen on the page", **common)
    if kind is FieldType.NUMERIC:
        number = re.search(r"-?\d+(?:[.,]\d+)?", text)
        if number:
            return FieldAnswer(**base, status=FieldAnswerStatus.ANSWERED, answer=number.group(0).replace(",", "."), reason=reason, **common)
        return _ask(base, field, f"{reason}, but it is not a number", answer=text, **common)
    if kind is FieldType.DATE:
        parsed = _parse_date(text)
        if parsed:
            return FieldAnswer(**base, status=FieldAnswerStatus.ANSWERED, answer=parsed, reason=reason, **common)
        return _ask(base, field, f"{reason}, but it is not a date", answer=text, **common)
    return _ask(base, field, "unsupported field type", review=True, answer=text, **common)


def _match_options(field: FormField, text: str, multi: bool) -> list[str]:
    labels = [(o, normalize_question(o.label)) for o in field.options]
    if multi:
        wanted = normalize_question(text)
        exact = [o for o, label in labels if label == wanted]
        if exact:
            return [_value(o) for o in exact]
        contained = [o for o, label in labels if label and (label in wanted or wanted in label)]
        return [_value(o) for o in contained]
    if len(field.options) == 1 and field.field_type is FieldType.CHECKBOX:
        # One box, a yes: tick it ("I acknowledge"); anything else is not a tick.
        return [_value(field.options[0])] if _polarity(text) == "yes" else []
    chosen = choose_option([o.label for o in field.options], text, category=classify_question(field.label).category.value)
    if chosen is None:
        return []
    return [_value(next(o for o in field.options if o.label == chosen))]


def _value(option) -> str:
    return option.value if option.value is not None else option.label


def _parse_date(text: str) -> Optional[str]:
    text = text.strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d %B %Y", "%B %d, %Y", "%Y-%m"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def blocking(answers: list[FieldAnswer]) -> tuple[int, int]:
    """(required fields needing the candidate, required fields needing review)."""
    needs_input = sum(1 for a in answers if a.required and a.status is FieldAnswerStatus.NEEDS_USER_INPUT)
    needs_review = sum(1 for a in answers if a.required and a.status is FieldAnswerStatus.NEEDS_REVIEW)
    return needs_input, needs_review


def snapshot_from(fields: list[FormField], source_url: str, executor_kind, executor_version: str = "", metadata: Optional[dict] = None) -> FormSnapshot:
    return FormSnapshot(source_url=source_url, fields=list(fields), executor_kind=executor_kind, executor_version=executor_version, metadata=dict(metadata or {}))
