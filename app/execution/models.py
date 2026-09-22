"""Domain models for execution: targets, packages, forms, results, runs."""

import re
from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.pipeline.models import Lane, TailoringLevel


class ExecutorKind(str, Enum):
    MOCK = "MOCK"
    MANUAL = "MANUAL"
    PLAYWRIGHT_LOCAL = "PLAYWRIGHT_LOCAL"
    BROWSER_EXTENSION = "BROWSER_EXTENSION"


class ATSFamily(str, Enum):
    GREENHOUSE = "greenhouse"
    LEVER = "lever"
    ASHBY = "ashby"
    GENERIC_WEB = "generic_web"
    CAPTURED_BROWSER_TARGET = "captured_browser_target"
    MANUAL = "manual"


class ApplicationMethod(str, Enum):
    """How a submission can happen for a target. Discovery adapters are
    read-only; a browser (local runner / extension) or a person applies."""

    BROWSER_FORM = "browser_form"
    EXTERNAL_LINK = "external_link"
    MANUAL = "manual"


class FieldType(str, Enum):
    TEXT = "text"
    TEXTAREA = "textarea"
    EMAIL = "email"
    PHONE = "phone"
    SELECT = "select"
    MULTI_SELECT = "multi_select"
    CHECKBOX = "checkbox"
    RADIO = "radio"
    NUMERIC = "numeric"
    DATE = "date"
    FILE = "file"
    #: A searchable dropdown (react-select and similar): the options are only
    #: rendered once the person types, so they are matched on the page at fill
    #: time, never guessed (Greenhouse Country / Location, Ashby Location).
    COMBOBOX = "combobox"
    #: A question answered by pressing one of two buttons over a hidden
    #: checkbox (Ashby yes / no questions).
    YESNO = "yesno"
    UNKNOWN = "unknown"


class FieldAnswerStatus(str, Enum):
    ANSWERED = "ANSWERED"
    NEEDS_USER_INPUT = "NEEDS_USER_INPUT"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    #: Optional field we deliberately leave empty (no safe answer).
    SKIPPED = "SKIPPED"


class FieldAnswerSource(str, Enum):
    PREPARATION = "PREPARATION"
    ANSWER_BANK = "ANSWER_BANK"
    ARTIFACT = "ARTIFACT"
    PROFILE = "PROFILE"
    USER = "USER"
    NONE = "NONE"


class ExecutionOutcome(str, Enum):
    """What one executor invocation reports."""

    SUBMITTED = "SUBMITTED"
    RETRYABLE_FAILURE = "RETRYABLE_FAILURE"
    PERMANENT_FAILURE = "PERMANENT_FAILURE"
    HANDOFF = "HANDOFF"
    NEEDS_USER_INPUT = "NEEDS_USER_INPUT"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    FORM_CHANGED = "FORM_CHANGED"
    #: The executor cannot say whether submit happened (timeout after click,
    #: browser crash, lost connection). Never resubmitted automatically.
    UNKNOWN = "UNKNOWN"
    #: Dry run: navigated, inspected and mapped; submit was deliberately not pressed.
    DRY_RUN = "DRY_RUN"


class ExecutionStatus(str, Enum):
    """Persistent status of one execution run."""

    RUNNING = "RUNNING"
    DRY_RUN = "DRY_RUN"
    SUBMITTED = "SUBMITTED"
    VERIFIED = "VERIFIED"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_PERMANENT = "FAILED_PERMANENT"
    HANDOFF = "HANDOFF"
    NEEDS_USER_INPUT = "NEEDS_USER_INPUT"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    PRECONDITION_FAILED = "PRECONDITION_FAILED"
    STALE = "STALE"
    UNKNOWN = "UNKNOWN"
    CANCELLED = "CANCELLED"


class HandoffReason(str, Enum):
    CAPTCHA_REQUIRED = "CAPTCHA_REQUIRED"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    MFA_REQUIRED = "MFA_REQUIRED"
    AMBIGUOUS_FORM = "AMBIGUOUS_FORM"
    UNKNOWN_REQUIRED_FIELD = "UNKNOWN_REQUIRED_FIELD"
    USER_CONFIRMATION_REQUIRED = "USER_CONFIRMATION_REQUIRED"
    #: A required upload has no real local file (Phase 4 artifacts are text).
    ARTIFACT_FILE_REQUIRED = "ARTIFACT_FILE_REQUIRED"
    #: The page uses widgets the executor cannot drive safely (UNSUPPORTED_FORM).
    UNSUPPORTED_FORM = "UNSUPPORTED_FORM"


class ErrorClass(str, Enum):
    TRANSIENT_NETWORK = "TRANSIENT_NETWORK"
    TIMEOUT = "TIMEOUT"
    TARGET_GONE = "TARGET_GONE"
    FORM_CHANGED = "FORM_CHANGED"
    EXECUTOR_CRASH = "EXECUTOR_CRASH"
    BROWSER_CRASH = "BROWSER_CRASH"
    NAVIGATION = "NAVIGATION"
    VALIDATION = "VALIDATION"
    POLICY = "POLICY"
    UNKNOWN = "UNKNOWN"


class VerificationStatus(str, Enum):
    NOT_ATTEMPTED = "NOT_ATTEMPTED"
    PENDING = "PENDING"
    VERIFIED = "VERIFIED"
    LIKELY = "LIKELY"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class VerificationMethod(str, Enum):
    CONFIRMATION_PAGE = "confirmation_page"
    CONFIRMATION_TEXT = "confirmation_text"
    APPLICATION_ID = "application_id"
    REDIRECT_URL = "redirect_url"
    APPLICATION_HISTORY = "application_history"
    USER_CONFIRMATION = "user_confirmation"
    EXECUTOR_REPORT = "executor_report"
    #: Blueprint Phase 10: an employer confirmation attributed to the attempt by the Signal Inbox.
    CONFIRMATION_EMAIL = "confirmation_email"
    NONE = "none"


class PreconditionCode(str, Enum):
    ATTEMPT_NOT_READY = "ATTEMPT_NOT_READY"
    PREPARATION_MISSING = "PREPARATION_MISSING"
    TENANT_MISMATCH = "TENANT_MISMATCH"
    OPPORTUNITY_MISMATCH = "OPPORTUNITY_MISMATCH"
    PREPARATION_NOT_READY = "PREPARATION_NOT_READY"
    PREPARATION_INVALIDATED = "PREPARATION_INVALIDATED"
    PREPARATION_NOT_CURRENT = "PREPARATION_NOT_CURRENT"
    PREPARATION_STALE = "PREPARATION_STALE"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    USER_INPUT_REQUIRED = "USER_INPUT_REQUIRED"
    OPPORTUNITY_CLOSED = "OPPORTUNITY_CLOSED"
    COMPANY_BLOCKED = "COMPANY_BLOCKED"
    COOLDOWN_ACTIVE = "COOLDOWN_ACTIVE"
    ALREADY_SUBMITTED = "ALREADY_SUBMITTED"
    DUPLICATE_APPLICATION = "DUPLICATE_APPLICATION"
    DUPLICATE_OPPORTUNITY = "DUPLICATE_OPPORTUNITY"
    DAILY_CAP_REACHED = "DAILY_CAP_REACHED"
    WEEKLY_CAP_REACHED = "WEEKLY_CAP_REACHED"
    SLOT_RELEASED = "SLOT_RELEASED"
    #: The kill switch (global or per source) is paused.
    PAUSED = "PAUSED"


# --------------------------------------------------------------------- #
# safe diagnostics
# --------------------------------------------------------------------- #

_SENSITIVE_KEY = re.compile(r"(password|passwd|secret|token|cookie|session|authorization|api[_-]?key|bearer|credential|otp|mfa[_-]?code)", re.IGNORECASE)


def sanitize_diagnostics(data: Optional[dict[str, Any]], depth: int = 0) -> dict[str, Any]:
    """Drop credential-like keys and long blobs from executor diagnostics.

    Executors never get to persist page HTML or secrets by accident: any key
    that looks like a credential is replaced, strings are capped, nesting is
    bounded.
    """
    if not data or depth > 3:
        return {}
    clean: dict[str, Any] = {}
    for key, value in data.items():
        if _SENSITIVE_KEY.search(str(key)):
            clean[str(key)] = "[redacted]"
            continue
        if isinstance(value, dict):
            clean[str(key)] = sanitize_diagnostics(value, depth + 1)
        elif isinstance(value, (list, tuple)):
            clean[str(key)] = [sanitize_diagnostics(v, depth + 1) if isinstance(v, dict) else str(v)[:200] for v in value[:20]]
        elif isinstance(value, str):
            clean[str(key)] = value[:500]
        elif isinstance(value, (int, float, bool)) or value is None:
            clean[str(key)] = value
        else:
            clean[str(key)] = str(value)[:200]
    return clean


# --------------------------------------------------------------------- #
# target / form / package
# --------------------------------------------------------------------- #


class ExecutionTarget(BaseModel):
    """Where and how the application is submitted."""

    source: str
    ats_family: ATSFamily
    canonical_url: str
    application_url: Optional[str] = None
    external_job_id: Optional[str] = None
    company: str
    title: str
    location: Optional[str] = None
    method: ApplicationMethod = ApplicationMethod.BROWSER_FORM
    executor_hint: ExecutorKind = ExecutorKind.BROWSER_EXTENSION


class FormOption(BaseModel):
    label: str
    value: Optional[str] = None


class FormField(BaseModel):
    """One discovered form field, as an executor reports it."""

    external_id: Optional[str] = None
    label: str
    field_type: FieldType = FieldType.UNKNOWN
    required: bool = False
    options: list[FormOption] = Field(default_factory=list)
    current_value: Optional[str] = None
    accept: Optional[str] = None
    section: Optional[str] = None
    #: Implementation metadata for the executor that found the field (a stable
    #: CSS selector); never candidate data.
    selector: Optional[str] = None
    input_type: Optional[str] = None


class FormSnapshot(BaseModel):
    """Safe representation of a discovered form: structure only, no HTML."""

    source_url: str
    fields: list[FormField] = Field(default_factory=list)
    executor_kind: ExecutorKind = ExecutorKind.MANUAL
    executor_version: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class FieldAnswer(BaseModel):
    """A form field mapped to a truthful answer (or to a reason we have none)."""

    field_id: Optional[str] = None
    external_id: Optional[str] = None
    label: str
    question_key: str
    field_type: FieldType
    required: bool
    status: FieldAnswerStatus
    source: FieldAnswerSource = FieldAnswerSource.NONE
    category: str = "other"
    answer: Optional[str] = None
    selected_values: list[str] = Field(default_factory=list)
    artifact_type: Optional[str] = None
    preparation_answer_id: Optional[str] = None
    evidence_keys: list[str] = Field(default_factory=list)
    reason: Optional[str] = None


class PreparedAnswerView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    question: str
    question_key: str
    category: str
    answer: Optional[str] = None
    evidence_keys: list[str] = Field(default_factory=list)
    source: str
    status: str
    required: bool = True


class ArtifactView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    artifact_type: str
    content: str
    evidence_keys: list[str] = Field(default_factory=list)
    template_version: str
    validation_status: str


class BankAnswerView(BaseModel):
    id: str
    category: str
    question_key: str
    answer: str
    evidence_keys: list[str] = Field(default_factory=list)


class ExecutionPackage(BaseModel):
    """The canonical execution input: everything an executor needs, prepared.

    Nothing here requires re-tailoring or an AI call; it is the READY
    preparation plus the target and provenance, keyed by the canonical
    identity ``tenant / candidate opportunity / opportunity / preparation /
    application attempt``.
    """

    tenant_id: str
    candidate_opportunity_id: str
    opportunity_id: str
    preparation_id: str
    application_id: str
    attempt_number: int
    preparation_version: int
    preparation_fingerprint: str
    preparation_inputs: dict[str, Any] = Field(default_factory=dict)
    target: ExecutionTarget
    lane: Lane = Lane.REVIEW
    tailoring_level: TailoringLevel = TailoringLevel.L0
    cover_letter_enabled: bool = False
    resume: Optional[ArtifactView] = None
    cover_letter: Optional[ArtifactView] = None
    answers: list[PreparedAnswerView] = Field(default_factory=list)
    answer_bank: list[BankAnswerView] = Field(default_factory=list)
    evidence_keys: list[str] = Field(default_factory=list)
    profile: dict[str, Any] = Field(default_factory=dict)
    execution_config: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------- #
# results
# --------------------------------------------------------------------- #


class VerificationResult(BaseModel):
    status: VerificationStatus
    method: VerificationMethod = VerificationMethod.NONE
    detail: Optional[str] = None
    external_application_id: Optional[str] = None
    confirmation_reference: Optional[str] = None
    application_url: Optional[str] = None


class ExecutionResult(BaseModel):
    """What an executor returns from ``execute`` (or reports through the API)."""

    outcome: ExecutionOutcome
    #: True once the executor pressed / sent submit, whatever happened after.
    submit_attempted: bool = False
    handoff_reason: Optional[HandoffReason] = None
    error_class: Optional[ErrorClass] = None
    message: Optional[str] = None
    application_url: Optional[str] = None
    external_application_id: Optional[str] = None
    confirmation_reference: Optional[str] = None
    #: Human handoff: where execution stopped, what remains, can it resume.
    stopped_at: Optional[str] = None
    remaining_steps: list[str] = Field(default_factory=list)
    resumable: bool = True
    diagnostics: dict[str, Any] = Field(default_factory=dict)
    verification: Optional[VerificationResult] = None


class PreconditionFailure(BaseModel):
    code: PreconditionCode
    message: str
    detail: dict[str, Any] = Field(default_factory=dict)


class PreconditionReport(BaseModel):
    ok: bool
    failures: list[PreconditionFailure] = Field(default_factory=list)
    stale_inputs: list[str] = Field(default_factory=list)
    cap_day_key: Optional[str] = None
    cap_week_key: Optional[str] = None


# --------------------------------------------------------------------- #
# read models
# --------------------------------------------------------------------- #


class ExecutionRun(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    tenant_id: str
    application_id: str
    preparation_id: Optional[str] = None
    candidate_opportunity_id: Optional[str] = None
    opportunity_id: Optional[str] = None
    queue_item_id: Optional[str] = None
    executor_kind: ExecutorKind
    executor_version: str
    idempotency_key: str
    run_number: int
    status: ExecutionStatus
    outcome: Optional[ExecutionOutcome] = None
    submit_invoked: bool = False
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    source_url: Optional[str] = None
    application_url: Optional[str] = None
    external_application_id: Optional[str] = None
    confirmation_reference: Optional[str] = None
    verification_status: VerificationStatus = VerificationStatus.NOT_ATTEMPTED
    verification_method: Optional[str] = None
    verification_detail: Optional[str] = None
    error_class: Optional[ErrorClass] = None
    error_message: Optional[str] = None
    handoff_reason: Optional[HandoffReason] = None
    handoff: dict[str, Any] = Field(default_factory=dict)
    preconditions: list[dict[str, Any]] = Field(default_factory=list)
    diagnostics: dict[str, Any] = Field(default_factory=dict)
    form_snapshot_id: Optional[str] = None
    worker_id: Optional[str] = None
    created_at: Optional[datetime] = None


class ApplicationAttempt(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    job_id: str
    tenant_id: Optional[str] = None
    opportunity_id: Optional[str] = None
    candidate_opportunity_id: Optional[str] = None
    preparation_id: Optional[str] = None
    attempt_number: int = 1
    status: str
    status_reason: Optional[str] = None
    blocked_reason: Optional[str] = None
    lane: Optional[str] = None
    tailoring_level: Optional[str] = None
    cap_day: Optional[str] = None
    cap_week: Optional[str] = None
    reserved_at: Optional[datetime] = None
    released_at: Optional[datetime] = None
    execution_count: int = 0
    last_execution_id: Optional[str] = None
    submitted_at: Optional[datetime] = None
    verified_at: Optional[datetime] = None
    external_application_id: Optional[str] = None
    result_url: Optional[str] = None
    confirmation: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class FormFieldRecord(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    snapshot_id: str
    position: int
    external_id: Optional[str] = None
    label: str
    question_key: str
    field_type: FieldType
    required: bool
    options: list[dict[str, Any]] = Field(default_factory=list)
    current_value: Optional[str] = None
    answer: Optional[str] = None
    selected_values: list[str] = Field(default_factory=list)
    artifact_type: Optional[str] = None
    source: FieldAnswerSource
    status: FieldAnswerStatus
    category: str
    preparation_answer_id: Optional[str] = None
    evidence_keys: list[str] = Field(default_factory=list)
    reason: Optional[str] = None


class FormSnapshotRecord(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    tenant_id: str
    application_id: str
    opportunity_id: Optional[str] = None
    executor_kind: ExecutorKind
    executor_version: str
    source_url: str
    fingerprint: str
    field_count: int
    metadata_: dict[str, Any] = Field(default_factory=dict)
    captured_at: Optional[datetime] = None
    fields: list[FormFieldRecord] = Field(default_factory=list)


class ExecutionPreview(BaseModel):
    package: ExecutionPackage
    preconditions: PreconditionReport
    attempt: ApplicationAttempt
    form: Optional[FormSnapshotRecord] = None
    field_answers: list[FieldAnswer] = Field(default_factory=list)
    blocking_fields: int = 0


class ExecutionSummary(BaseModel):
    tenant_id: str
    attempts_by_status: dict[str, int] = Field(default_factory=dict)
    runs_by_status: dict[str, int] = Field(default_factory=dict)
    handoffs_by_reason: dict[str, int] = Field(default_factory=dict)
    queue_by_state: dict[str, int] = Field(default_factory=dict)
    metrics: dict[str, int] = Field(default_factory=dict)
