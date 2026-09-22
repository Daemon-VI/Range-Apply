"""Form field mapping: truthful answers or an explicit question, never a guess."""

from app.execution.forms import blocking, form_fingerprint, map_fields
from app.execution.models import (
    ArtifactView,
    ATSFamily,
    BankAnswerView,
    ExecutionPackage,
    ExecutionTarget,
    FieldAnswerSource,
    FieldAnswerStatus,
    FieldType,
    FormField,
    FormOption,
    PreparedAnswerView,
    sanitize_diagnostics,
)


def _package(answers=(), bank=(), profile=None, resume=True, cover=False) -> ExecutionPackage:
    return ExecutionPackage(
        tenant_id="t",
        candidate_opportunity_id="co",
        opportunity_id="opp",
        preparation_id="prep",
        application_id="app",
        attempt_number=1,
        preparation_version=1,
        preparation_fingerprint="x",
        target=ExecutionTarget(source="GREENHOUSE", ats_family=ATSFamily.GREENHOUSE, canonical_url="https://x", company="Acme", title="Engineer"),
        resume=ArtifactView(id="r", artifact_type="RESUME", content="...", evidence_keys=["skill-python"], template_version="prep-v1", validation_status="PASSED") if resume else None,
        cover_letter=ArtifactView(id="c", artifact_type="COVER_LETTER", content="...", evidence_keys=[], template_version="prep-v1", validation_status="PASSED") if cover else None,
        answers=[PreparedAnswerView(id=f"a{i}", question=q, question_key=k, category=c, answer=a, source="ANSWER_BANK", status=s) for i, (q, k, c, a, s) in enumerate(answers)],
        answer_bank=[BankAnswerView(id=f"b{i}", category=c, question_key=k, answer=a) for i, (k, c, a) in enumerate(bank)],
        profile=profile or {"name": "Ribhu S", "email": "r@example.com"},
    )


def _field(label, kind=FieldType.TEXT, required=True, options=()):
    return FormField(label=label, field_type=kind, required=required, options=[FormOption(label=o) for o in options])


def test_files_identity_and_prepared_answers():
    pkg = _package(answers=[("Why are you interested in this role?", "why are you interested in this role", "why_role", "Because ...", "ANSWERED")])
    fields = [_field("Resume/CV", FieldType.FILE), _field("Cover letter", FieldType.FILE, required=False), _field("Full name"), _field("Email"), _field("Phone"), _field("Why are you interested in this role?", FieldType.TEXTAREA)]
    answers = map_fields(fields, pkg)
    assert answers[0].status is FieldAnswerStatus.ANSWERED and answers[0].artifact_type == "RESUME" and answers[0].evidence_keys == ["skill-python"]
    assert answers[1].status is FieldAnswerStatus.SKIPPED
    assert answers[2].answer == "Ribhu S" and answers[2].source is FieldAnswerSource.PROFILE
    assert answers[3].answer == "r@example.com"
    assert answers[4].status is FieldAnswerStatus.NEEDS_USER_INPUT, "no phone on the profile: ask, never invent"
    assert answers[5].status is FieldAnswerStatus.ANSWERED and answers[5].source is FieldAnswerSource.PREPARATION and answers[5].preparation_answer_id == "a0"
    assert blocking(answers) == (1, 0)


def test_choice_fields_only_when_the_answer_matches_an_option():
    bank = [("are you legally authorized to work in this country", "work_authorization", "Yes, I am authorized to work in India."), ("will you now or in the future require sponsorship", "sponsorship", "No, I do not require sponsorship.")]
    pkg = _package(bank=bank)
    fields = [
        _field("Are you legally authorized to work in India?", FieldType.SELECT, options=("Yes", "No")),
        _field("Do you require visa sponsorship?", FieldType.RADIO, options=("Yes", "No")),
        _field("Work authorization status", FieldType.SELECT, options=("US Citizen", "Green Card", "H1B", "Other")),
    ]
    answers = map_fields(fields, pkg)
    assert answers[0].status is FieldAnswerStatus.ANSWERED and answers[0].selected_values == ["Yes"] and answers[0].source is FieldAnswerSource.ANSWER_BANK
    assert answers[1].status is FieldAnswerStatus.ANSWERED and answers[1].selected_values == ["No"]
    assert answers[2].status is FieldAnswerStatus.NEEDS_USER_INPUT and "does not match an offered option" in answers[2].reason


def test_numeric_date_voluntary_unknown_and_optional():
    bank = [("what are your salary expectations", "salary", "Open to a competitive package"), ("what is your notice period", "notice_period", "I can join from 2026-10-01")]
    pkg = _package(bank=bank)
    fields = [
        _field("Expected salary (LPA)", FieldType.NUMERIC),
        _field("Earliest start date", FieldType.DATE),
        _field("Gender", FieldType.SELECT, options=("Male", "Female", "Prefer not to say")),
        _field("Rate your alignment with our values", FieldType.UNKNOWN),
        _field("Anything else you'd like to share?", FieldType.TEXTAREA, required=False),
        _field("Years of experience with Kubernetes", FieldType.NUMERIC),
    ]
    answers = map_fields(fields, pkg)
    assert answers[0].status is FieldAnswerStatus.NEEDS_USER_INPUT and "not a number" in answers[0].reason
    assert answers[1].status is FieldAnswerStatus.NEEDS_USER_INPUT
    assert answers[2].status is FieldAnswerStatus.NEEDS_USER_INPUT and "voluntary" in answers[2].reason
    assert answers[3].status is FieldAnswerStatus.NEEDS_REVIEW and answers[3].answer is None
    assert answers[4].status is FieldAnswerStatus.SKIPPED
    assert answers[5].status is FieldAnswerStatus.NEEDS_USER_INPUT
    assert blocking(answers) == (4, 1)


def test_date_and_numeric_parse_when_the_answer_fits():
    bank = [("what is your notice period", "notice_period", "2026-10-01"), ("how many years of experience", "experience_years", "About 2 years")]
    pkg = _package(bank=bank)
    answers = map_fields([_field("Earliest start date", FieldType.DATE), _field("Total years of experience", FieldType.NUMERIC)], pkg)
    assert answers[0].answer == "2026-10-01" and answers[0].status is FieldAnswerStatus.ANSWERED
    assert answers[1].answer == "2" and answers[1].status is FieldAnswerStatus.ANSWERED


def test_fingerprint_depends_on_structure_not_values():
    a = [_field("Email"), _field("Resume", FieldType.FILE)]
    b = [FormField(label="Email", field_type=FieldType.TEXT, required=True, current_value="x@y"), _field("Resume", FieldType.FILE)]
    c = [_field("Email", required=False), _field("Resume", FieldType.FILE)]
    assert form_fingerprint(a) == form_fingerprint(b) != form_fingerprint(c)


def test_diagnostics_are_sanitised():
    clean = sanitize_diagnostics({"step": "submit", "Authorization": "Bearer x", "cookies": {"session": "abc"}, "html": "<html>" * 1000, "nested": {"api_key": "k", "ok": 1}})
    assert clean["Authorization"] == "[redacted]" and clean["cookies"] == "[redacted]"
    assert len(clean["html"]) == 500 and clean["nested"] == {"api_key": "[redacted]", "ok": 1}


def test_file_controls_named_resume_or_cover_letter_map_even_when_labelled_attach():
    """Greenhouse's hosted boards label both uploads "Attach" (Phase 13)."""
    resume = FormField(external_id="resume", label="Attach", field_type=FieldType.FILE, required=True)
    cover = FormField(external_id="cover_letter", label="Attach", field_type=FieldType.FILE, required=False)
    answers = map_fields([resume, cover], _package(resume=True, cover=True))
    assert [(a.status, a.artifact_type) for a in answers] == [(FieldAnswerStatus.ANSWERED, "RESUME"), (FieldAnswerStatus.ANSWERED, "COVER_LETTER")]


def test_profile_location_answers_location_questions_before_asking_the_candidate():
    """Pilot finding: Greenhouse "Location (City)" and Lever "Current location"
    were sent to the candidate although the profile records the location."""
    fields = [_field("Location (City)*"), _field("Current location"), _field("City, State, Country", required=False)]
    with_location = map_fields(fields, _package(profile={"name": "Ribhu S", "email": "r@example.com", "location": "Hyderabad, India"}))
    assert [a.status for a in with_location] == [FieldAnswerStatus.ANSWERED] * 3
    assert all(a.answer == "Hyderabad, India" and a.source is FieldAnswerSource.PROFILE and a.evidence_keys == ["profile:location"] for a in with_location)
    without = map_fields(fields, _package(profile={"name": "Ribhu S", "email": "r@example.com"}))
    assert [a.status for a in without] == [FieldAnswerStatus.NEEDS_USER_INPUT, FieldAnswerStatus.NEEDS_USER_INPUT, FieldAnswerStatus.SKIPPED], "no recorded fact: ask, never invent"


def test_employer_specific_bank_answers_are_never_reused_across_companies_by_category():
    """Pilot finding: an approved 'why us?' answer naming one company was mapped
    by category to another company's question. Only the exact question or the
    tailored preparation may answer WHY_COMPANY / WHY_ROLE."""
    bank = [("why do you want to work at delta", "why_company", "Delta builds infrastructure I know."), ("preferred work mode", "work_mode", "Remote")]
    fields = [_field("Why do you want to work at Discord?", FieldType.TEXTAREA), _field("Why do you want to work at Delta?", FieldType.TEXTAREA), _field("Preferred work mode")]
    answers = map_fields(fields, _package(bank=bank))
    assert answers[0].status is FieldAnswerStatus.NEEDS_REVIEW and answers[0].answer is None, "another company's answer is never reused"
    assert answers[1].status is FieldAnswerStatus.ANSWERED and answers[1].answer.startswith("Delta builds"), "the exact question still matches"
    assert answers[2].status is FieldAnswerStatus.ANSWERED and answers[2].answer == "Remote", "generic categories still reuse the bank"


def test_link_and_password_fields_never_receive_prose_or_secrets():
    """First real dry run (Replit): 'Project URL' and 'Project Password' were filled with the
    relevant-project paragraph by category. A link field takes only an exact-question answer
    that is itself a link; a password field is never filled."""
    project = ("Describe a relevant project.", "describe a relevant project", "relevant_project", "Ticket Engine: a high-concurrency booking system.", "ANSWERED")
    fields = [
        _field("Project URL"),
        _field("Project Password"),
        _field("Please tell us about your submitted project", FieldType.TEXTAREA),
        _field("Replit Profile URL", required=False),
    ]
    answers = map_fields(fields, _package(answers=[project]))
    assert answers[0].status is FieldAnswerStatus.NEEDS_USER_INPUT and answers[0].answer is None, "no paragraph in a link field"
    assert answers[1].status is FieldAnswerStatus.NEEDS_USER_INPUT and answers[1].answer is None and "password" in answers[1].reason
    assert answers[2].status is FieldAnswerStatus.ANSWERED, "the narrative question still uses the prepared answer"
    assert answers[3].status is FieldAnswerStatus.SKIPPED
    linked = map_fields([_field("Replit Profile URL", required=False)], _package(bank=[("replit profile url", "other", "https://replit.com/@candidate")]))
    assert linked[0].status is FieldAnswerStatus.ANSWERED and linked[0].answer == "https://replit.com/@candidate", "an exact-question link answer is used"
    prose = map_fields([_field("Replit Profile URL")], _package(bank=[("replit profile url", "other", "my profile on replit")]))
    assert prose[0].status is FieldAnswerStatus.NEEDS_USER_INPUT, "an answer that is not a link never fills a link field"


def test_profile_links_never_pick_an_option_in_a_choice_field():
    # First real dry run (Notion, Ashby): "How did you hear about this job?" is a checkbox group whose
    # option "LinkedIn" was ticked because the profile holds a LinkedIn URL. A URL is typed, never chosen.
    pkg = _package(profile={"name": "Rithik K", "email": "r@example.com", "linkedin": "https://linkedin.com/in/example", "portfolio": "https://example.com"})
    fields = [
        _field("LinkedIn", FieldType.CHECKBOX, required=False, options=("LinkedIn",)),
        _field("Notion Website", FieldType.CHECKBOX, required=False, options=("Notion Website",)),
        _field("LinkedIn Profile"),
    ]
    answers = map_fields(fields, pkg)
    assert answers[0].status is FieldAnswerStatus.SKIPPED and not answers[0].selected_values
    assert answers[1].status is FieldAnswerStatus.SKIPPED and not answers[1].selected_values
    assert answers[2].status is FieldAnswerStatus.ANSWERED and answers[2].answer == "https://linkedin.com/in/example"


def test_a_generic_why_us_bank_answer_is_never_reused_for_another_employer():
    # Audit (2026-09-14): "Why do you want to work here?" answered once for one employer was reused
    # verbatim, by exact question, for every other employer.
    bank = [
        ("why do you want to work here", "why_company", "Because Globex builds the tools I use."),
        ("why do you want to work at acme", "why_company", "Because Acme ships developer tools I rely on."),
    ]
    answers = map_fields([_field("Why do you want to work here?", FieldType.TEXTAREA), _field("Why do you want to work at Acme?", FieldType.TEXTAREA)], _package(bank=bank))
    assert answers[0].status is not FieldAnswerStatus.ANSWERED, "a generic question names no company: never reused"
    assert answers[1].status is FieldAnswerStatus.ANSWERED and "Acme" in answers[1].answer
