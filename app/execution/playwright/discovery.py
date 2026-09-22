"""Form discovery: turn a live page into the canonical :class:`FormSnapshot`.

Runs one script in the page that walks visible form controls and reports
structure only (label, type, required, options, name/id, a stable selector).
No HTML is persisted, no values other than what the page already shows.

It also reports the conditions that must stop automation: CAPTCHA / human
verification widgets, login walls, MFA prompts, and custom widgets the
executor cannot drive safely.
"""

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from app.execution.models import FieldType, FormField, FormOption

_SUCCESS_TEXT = re.compile(
    r"thank you for (applying|your application|submitting)|application (has been |was )?(submitted|received|sent)|we('ve| have) received your application|successfully (submitted|applied)|your application is (in|complete)|applied successfully",
    re.IGNORECASE,
)
_REFERENCE_TEXT = re.compile(r"(?:application|reference|confirmation|tracking)\s*(?:id|number|no\.?|#|code)\s*[:#]?\s*([A-Za-z0-9][A-Za-z0-9\-]{3,})", re.IGNORECASE)
_VALIDATION_TEXT = re.compile(r"(is required|required field|please (fill|complete|enter|select|provide)|invalid|must be|can't be blank|cannot be blank|field is missing)", re.IGNORECASE)


@dataclass
class PageScan:
    url: str
    title: str
    fields: list[FormField]
    custom_widgets: int = 0
    captcha: bool = False
    #: a visible widget or challenge text (an invisible reCAPTCHA badge alone is not a wall)
    captcha_visible: bool = False
    #: the widget sits inside the application form (not a wall page)
    captcha_inline: bool = False
    #: the provider's response token is present: a person completed it
    captcha_solved: bool = False
    mfa: bool = False
    login_wall: bool = False
    submit_buttons: list[dict[str, Any]] = field(default_factory=list)
    #: Visible "Apply" links / buttons on a page that shows no form (a listing
    #: page in front of the application form); ``{text, href, selector}``.
    apply_links: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    body_excerpt: str = ""

    @property
    def success_marker(self) -> Optional[str]:
        match = _SUCCESS_TEXT.search(self.body_excerpt)
        return match.group(0) if match else None

    @property
    def reference(self) -> Optional[str]:
        match = _REFERENCE_TEXT.search(self.body_excerpt)
        return match.group(1) if match else None

    @property
    def validation_errors(self) -> list[str]:
        return [e for e in self.errors if _VALIDATION_TEXT.search(e)] or ([e for e in self.errors] if self.errors else [])


#: The discovery script is shared with the browser extension (Phase 9): one
#: source file, one canonical FormSnapshot shape for both executors.
DISCOVER_JS_PATH = Path(__file__).resolve().parents[3] / "extension" / "src" / "discover.js"


@lru_cache(maxsize=1)
def discover_source() -> str:
    if not DISCOVER_JS_PATH.is_file():
        raise FileNotFoundError(f"form discovery script missing: {DISCOVER_JS_PATH} (the extension sources ship with the repository)")
    source = DISCOVER_JS_PATH.read_text(encoding="utf-8")
    return "(() => { " + source + "\n; return careerosDiscover(); })()"


def scan_page(page) -> PageScan:
    """Evaluate the discovery script on ``page`` and build a :class:`PageScan`."""
    raw = page.evaluate(discover_source())
    fields = []
    for item in raw.get("fields", []):
        try:
            ftype = FieldType(item.get("field_type") or "unknown")
        except ValueError:
            ftype = FieldType.UNKNOWN
        fields.append(
            FormField(
                external_id=item.get("external_id"),
                label=(item.get("label") or item.get("external_id") or "")[:512],
                field_type=ftype,
                required=bool(item.get("required")),
                options=[FormOption(label=str(o.get("label") or o.get("value") or ""), value=o.get("value")) for o in item.get("options", [])],
                current_value=(item.get("current_value") or None),
                accept=item.get("accept"),
                selector=item.get("selector"),
                input_type=item.get("input_type"),
            )
        )
    return PageScan(
        url=raw.get("url", ""),
        title=raw.get("title", ""),
        fields=fields,
        custom_widgets=int(raw.get("custom_widgets", 0)),
        captcha=bool(raw.get("captcha")),
        captcha_visible=bool(raw.get("captcha_visible")),
        captcha_inline=bool(raw.get("captcha_inline")),
        captcha_solved=bool(raw.get("captcha_solved")),
        mfa=bool(raw.get("mfa")),
        login_wall=bool(raw.get("login_wall")),
        submit_buttons=list(raw.get("submit_buttons", [])),
        apply_links=list(raw.get("apply_links", [])),
        errors=list(raw.get("errors", [])),
        body_excerpt=raw.get("body_excerpt", ""),
    )
