"""ATS-specific knowledge, isolated from the generic executor.

A strategy contributes only what differs between systems: how to recognise
the target, which button submits, what a confirmation looks like, and known
field names. Filling, safety gates, handoff and verification semantics live
in the executor and are the same for every target.

Ashby is detected but its public form is React-driven with custom widgets
(comboboxes, custom file drop zones); the executor drives it only when
discovery finds standard controls and hands off as UNSUPPORTED_FORM
otherwise. Nothing here pretends a brittle DOM is stable.
"""

import re
from dataclasses import dataclass, field
from typing import Optional

from app.execution.models import ATSFamily, ExecutionTarget


@dataclass(frozen=True)
class Strategy:
    name: str
    families: tuple[ATSFamily, ...]
    #: Ordered candidates for the submit control (CSS or ``text=`` locators).
    submit_locators: tuple[str, ...]
    #: URL fragments that mean "submitted" after the click.
    success_url_patterns: tuple[str, ...] = ()
    #: Extra success text specific to the ATS.
    success_text: Optional[re.Pattern] = None
    #: Known field names -> canonical question label, used before label heuristics.
    field_labels: dict[str, str] = field(default_factory=dict)
    #: True when discovery must find standard controls or hand off.
    strict_controls: bool = False

    def matches(self, target: ExecutionTarget) -> bool:
        return target.ats_family in self.families


GREENHOUSE = Strategy(
    name="greenhouse",
    families=(ATSFamily.GREENHOUSE,),
    submit_locators=("#submit_app", "button[type=submit]", "input[type=submit]", "text=/^submit( application)?$/i"),
    success_url_patterns=("/confirmation", "confirmation=", "?applied", "/thanks"),
    success_text=re.compile(r"thank you for applying|your application has been submitted", re.IGNORECASE),
    field_labels={
        "job_application[first_name]": "First Name",
        "job_application[last_name]": "Last Name",
        "job_application[email]": "Email",
        "job_application[phone]": "Phone",
        "job_application[resume]": "Resume/CV",
        "job_application[cover_letter]": "Cover Letter",
        "job_application[location]": "Location (City)",
    },
)

LEVER = Strategy(
    name="lever",
    families=(ATSFamily.LEVER,),
    submit_locators=("button[type=submit].postings-btn", "button[type=submit]", "text=/^submit application$/i", "text=/^submit$/i"),
    success_url_patterns=("/thanks", "/thank-you", "/confirmation"),
    success_text=re.compile(r"application submitted|thank you for applying|thanks for applying", re.IGNORECASE),
    field_labels={
        "name": "Full name",
        "email": "Email",
        "phone": "Phone",
        "org": "Current company",
        "resume": "Resume/CV",
        "urls[LinkedIn]": "LinkedIn URL",
        "urls[GitHub]": "GitHub URL",
        "urls[Portfolio]": "Portfolio URL",
        "comments": "Additional information",
    },
)

ASHBY = Strategy(
    name="ashby",
    families=(ATSFamily.ASHBY,),
    submit_locators=("button[type=submit]", "text=/^submit application$/i", "text=/^submit$/i"),
    success_url_patterns=("/application/submitted", "submitted=true", "/thanks"),
    success_text=re.compile(r"application submitted|thank you for applying|we('ve| have) received your application", re.IGNORECASE),
    strict_controls=True,
)

GENERIC = Strategy(
    name="generic",
    families=(ATSFamily.GENERIC_WEB, ATSFamily.CAPTURED_BROWSER_TARGET),
    submit_locators=("button[type=submit]", "input[type=submit]", "text=/^(submit|apply|send application|submit application)$/i"),
    success_url_patterns=("/thank", "/confirmation", "/success", "submitted=", "applied="),
)

STRATEGIES: tuple[Strategy, ...] = (GREENHOUSE, LEVER, ASHBY, GENERIC)


def strategy_for(target: ExecutionTarget) -> Strategy:
    for strategy in STRATEGIES:
        if strategy.matches(target):
            return strategy
    return GENERIC
