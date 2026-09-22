"""The one notification shape every part of Increment 5 shares.

Privacy contract (notifications can appear outside the window): a
notification carries company, role, status / reason words and CareerOS ids —
never a signal's subject, sender or excerpt, never an answer, document,
artifact hash, employer URL, error message, key, cookie or token.
"""

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Optional

#: Kinds, in the vocabulary of the desktop pages (no new application states).
APPLICATION_READY = "APPLICATION_READY"
ATTENTION_REQUIRED = "ATTENTION_REQUIRED"
SUBMISSION_COMPLETED = "SUBMISSION_COMPLETED"
VERIFICATION_RESULT = "VERIFICATION_RESULT"
EXECUTION_FAILED = "EXECUTION_FAILED"
INTERVIEW_SIGNAL = "INTERVIEW_SIGNAL"
REJECTION_SIGNAL = "REJECTION_SIGNAL"
OTHER_SIGNAL = "OTHER_SIGNAL"
DRY_RUN_COMPLETE = "DRY_RUN_COMPLETE"

KINDS = (APPLICATION_READY, ATTENTION_REQUIRED, SUBMISSION_COMPLETED, VERIFICATION_RESULT, EXECUTION_FAILED, INTERVIEW_SIGNAL, REJECTION_SIGNAL, OTHER_SIGNAL, DRY_RUN_COMPLETE)

#: Human titles; native adapters prefix ``CareerOS — ``.
TITLES = {
    APPLICATION_READY: "Application Ready",
    ATTENTION_REQUIRED: "Attention Required",
    SUBMISSION_COMPLETED: "Application Submitted",
    VERIFICATION_RESULT: "Verification Result",
    EXECUTION_FAILED: "Execution Failed",
    INTERVIEW_SIGNAL: "Interview Signal",
    REJECTION_SIGNAL: "Rejection Signal",
    OTHER_SIGNAL: "New Signal",
    DRY_RUN_COMPLETE: "Dry run complete",
}

MAX_BODY = 200


@dataclass(frozen=True)
class Notification:
    """One thing worth telling the person, derived from an existing row."""

    #: Stable dedupe key, e.g. ``attempt:<application_id>:READY:<event_id>``.
    key: str
    kind: str
    title: str
    body: str
    #: A local desktop / dashboard path (``/desktop/applications/<id>``); never a host, never a secret.
    path: str
    tenant_id: str
    occurred_at: datetime
    application_id: Optional[str] = None
    #: Status word, handoff reason or signal category — vocabulary only.
    reason: Optional[str] = None
    importance: str = "normal"  # "normal" | "high"

    def __post_init__(self):
        if self.kind not in KINDS:
            raise ValueError(f"unknown notification kind {self.kind!r}")
        if len(self.body) > MAX_BODY:
            raise ValueError("notification body too long")
        if not self.path.startswith("/desktop/") and not self.path.startswith("/dashboard/"):
            raise ValueError("notification path must be a local desktop or dashboard path")
        if "://" in self.path or self.path.startswith("//") or "?" in self.path or "#" in self.path:
            raise ValueError("notification path must be a bare local path")

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["occurred_at"] = self.occurred_at.isoformat()
        return data
