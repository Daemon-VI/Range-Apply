"""Discovery and mapping against markup reduced from real pages (audit 2026-09-14).

Read-only scans of a live Ashby (Notion), Greenhouse embed (HighRadius) and
Lever (Zeta) application page showed:

* Ashby's required "Location" is a combobox titled by a ``<label for>`` that
  names no element; it was discovered as an optional text field labelled with
  the resume drop zone's hint ("or drag and drop here");
* Ashby yes / no questions (two buttons over a hidden checkbox) were not
  discovered at all, so a required one read as "0 required missing";
* Ashby radio groups took the same wrong hint as their question;
* Greenhouse react-select comboboxes were plain text fields that ``fill()``
  would type into without ever selecting an option;
* Lever marks a required resume with a trailing "✱" and no attribute;
* Ashby's "Autofill from resume" upload was mapped as the resume.
"""

import pytest

from app.execution.forms import blocking, map_fields
from app.execution.models import FieldAnswerStatus, FieldType
from app.execution.playwright.discovery import scan_page
from tests.execution.playwright_conftest import make_session, requires_browser
from tests.execution.test_forms import _package

pytestmark = requires_browser

ASHBY_LIKE = """<!doctype html><html><body><div class="_jobPostingForm">
 <div class="_section">
  <div class="_fieldEntry"><label class="_heading _required_f7cvd_91" for="_systemfield_name">Full Name</label><input id="_systemfield_name" name="_systemfield_name" type="text" required></div>
  <div class="_fieldEntry"><label class="_heading" for="_systemfield_autofill">Autofill from resume</label><p>Upload your resume here to autofill key application fields.</p><input type="file"><button>Upload file</button></div>
  <div class="_fieldEntry"><label class="_heading _required_f7cvd_91" for="_systemfield_resume">Resume</label><input id="_systemfield_resume" type="file" required><p>or drag and drop here</p></div>
 </div>
 <div class="_section">
  <div class="_fieldEntry"><label class="_heading _required_f7cvd_91" for="_systemfield_location">Location</label><div class="_inputContainer"><input placeholder="Start typing..." aria-autocomplete="list" role="combobox" value=""><button>v</button></div></div>
  <div class="_fieldEntry"><label class="_heading _required_f7cvd_91" for="e01a85db">Are you able to commit to working from one of our offices on Anchor Days each week?</label><div class="_yesno"><button aria-pressed="false" data-option="yes">Yes</button><button aria-pressed="false" data-option="no">No</button><input type="checkbox" tabindex="-1" name="e01a85db" style="position:absolute;opacity:0;width:0;height:0;margin:0;padding:0;border:0"></div></div>
  <fieldset class="_container _fieldEntry"><label class="_heading" for="b0a5aba8">What pronouns would you like our team to use when addressing you?</label>
   <div class="_option"><span><input type="radio" id="p-0" name="pronouns"></span><label for="p-0">He/Him</label></div>
   <div class="_option"><span><input type="radio" id="p-1" name="pronouns"></span><label for="p-1">She/Her</label></div>
  </fieldset>
 </div>
 <button>Submit Application</button>
</div></body></html>"""

GREENHOUSE_AND_LEVER_LIKE = """<!doctype html><html><body><form>
 <div class="field"><label for="country">Country<span>*</span></label><div class="select__control"><input id="country" type="text" role="combobox" aria-autocomplete="list" aria-required="true"></div><input tabindex="-1" aria-hidden="true" required value=""></div>
 <div class="field"><label for="city">City</label><input id="city" type="text"></div>
 <div class="application-question"><label><div class="application-label">Resume/CV <span class="required">✱</span></div><div class="application-field"><input type="file" name="resume"></div></label></div>
 <button type="submit">Submit application</button>
</form></body></html>"""


@pytest.fixture(scope="module")
def browser():
    session = make_session()
    yield session
    session.close()


def _scan_html(browser, html):
    with browser.page() as page:
        page.set_content(html)
        return scan_page(page)


def test_ashby_titles_required_markers_comboboxes_and_yes_no_questions(browser):
    scan = _scan_html(browser, ASHBY_LIKE)
    by_label = {f.label: f for f in scan.fields}
    location = by_label["Location"]
    assert location.field_type is FieldType.COMBOBOX and location.input_type == "combobox" and location.required and location.options == []
    anchor = next(f for f in scan.fields if f.label.startswith("Are you able to commit"))
    assert anchor.field_type is FieldType.YESNO and anchor.input_type == "yesno" and anchor.required and anchor.external_id == "e01a85db"
    assert [o.label for o in anchor.options] == ["Yes", "No"] and anchor.current_value is None
    pronouns = next(f for f in scan.fields if f.field_type is FieldType.RADIO)
    assert pronouns.label.startswith("What pronouns") and not pronouns.required and len(pronouns.options) == 2
    assert "or drag and drop here" not in [f.label for f in scan.fields]
    assert by_label["Full Name"].required and by_label["Autofill from resume"].field_type is FieldType.FILE

    answers = {a.label: a for a in map_fields(scan.fields, _package(profile={"name": "Ribhu S", "email": "r@example.com", "location": "Hyderabad"}))}
    # 2026-09-22: the recorded location is carried as text; the executor picks the option
    # that names it once the dropdown has rendered its choices, or leaves the field alone.
    assert answers["Location"].status is FieldAnswerStatus.ANSWERED and answers["Location"].answer == "Hyderabad" and answers["Location"].selected_values == []
    assert "chosen on the page" in answers["Location"].reason
    # A yes / no question with no stored answer is the candidate's, never a guess.
    assert answers[anchor.label].status is FieldAnswerStatus.NEEDS_REVIEW and answers[anchor.label].selected_values == []
    assert answers["Autofill from resume"].status is FieldAnswerStatus.SKIPPED and answers["Autofill from resume"].artifact_type is None
    assert answers["Resume"].status is FieldAnswerStatus.ANSWERED and answers["Resume"].artifact_type == "RESUME"
    assert blocking(list(answers.values())) == (0, 1)


ASHBY_AUTOFILL_NESTING = """<!doctype html><html><body><div class="_section">
 <div class="_autofillContainer"><div class="_heading_label">Autofill from resume</div><p>Upload your resume here to autofill key application fields.</p><div><div><input type="file"></div><button>Upload file</button></div></div>
 <div class="_fieldEntry"><label class="_heading _required_f7cvd_91" for="_systemfield_location">Location</label><div><input role="combobox" aria-autocomplete="list"></div></div>
</div></body></html>"""


def test_a_neighbouring_questions_required_title_is_not_borrowed(browser):
    """The real Ashby autofill upload has no title of its own within reach; the
    search reached the section and took Location's required title."""
    scan = _scan_html(browser, ASHBY_AUTOFILL_NESTING)
    autofill = next(f for f in scan.fields if f.field_type is FieldType.FILE)
    assert not autofill.required and autofill.label == "Autofill from resume"
    location = next(f for f in scan.fields if f.input_type == "combobox")
    assert location.label == "Location" and location.required
    answers = map_fields(scan.fields, _package())
    assert answers[0].status is FieldAnswerStatus.SKIPPED and answers[0].artifact_type is None


def test_greenhouse_combobox_and_lever_star_marked_resume(browser):
    scan = _scan_html(browser, GREENHOUSE_AND_LEVER_LIKE)
    by_id = {f.external_id: f for f in scan.fields}
    country = by_id["country"]
    assert country.field_type is FieldType.COMBOBOX and country.input_type == "combobox" and country.required
    assert by_id["city"].field_type is FieldType.TEXT and not by_id["city"].required
    assert by_id["resume"].required and by_id["resume"].label == "Resume/CV ✱"
    assert len(scan.fields) == 3, [f.label for f in scan.fields]
