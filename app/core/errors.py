"""Structured application errors.

One exception hierarchy for the whole codebase so that:

* API handlers map an error to an HTTP status and a stable machine-readable
  ``code`` in one place (``app/main.py``) instead of every route re-deriving
  ``HTTPException`` from ``ValueError``/``RuntimeError`` strings.
* Background lanes (batch worker, executors, gateway) can classify failures
  (``retryable`` or not) without parsing messages.
* Nothing is swallowed: an error either propagates as one of these, or is
  logged with a traceback and re-raised. There is no bare ``except: pass``.

The response body keeps ``detail`` (what existing clients and tests read)
and adds ``error`` with ``code``/``message``/``details``.
"""

from typing import Any, Optional


class CareerOSError(Exception):
    """Base class. ``code`` is stable and safe to branch on; ``message`` is for humans."""

    #: Machine-readable identifier, e.g. ``"not_found"``.
    code: str = "internal_error"
    #: HTTP status used when this error escapes an API handler.
    http_status: int = 500
    #: Whether a queue consumer may retry the operation that raised this.
    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        details: Optional[dict[str, Any]] = None,
        code: Optional[str] = None,
        retryable: Optional[bool] = None,
    ):
        super().__init__(message)
        self.message = message
        self.details = dict(details or {})
        if code is not None:
            self.code = code
        if retryable is not None:
            self.retryable = retryable

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}

    def __str__(self) -> str:
        return self.message


class NotFoundError(CareerOSError):
    code = "not_found"
    http_status = 404


class ValidationFailed(CareerOSError):
    """The request or artifact was well-formed but violated a rule (e.g. factuality)."""

    code = "validation_failed"
    http_status = 422


class ConflictError(CareerOSError):
    """State conflict: duplicate application, illegal state transition, stale version."""

    code = "conflict"
    http_status = 409


class PolicyBlocked(CareerOSError):
    """A policy (lane rules, cool-down, blocklist, approval) refused the action.

    Not an error in the system; the action is simply not allowed right now.
    """

    code = "policy_blocked"
    http_status = 409


class KillSwitchEngaged(PolicyBlocked):
    code = "kill_switch_engaged"


class BudgetExhausted(CareerOSError):
    """An AI or infrastructure budget is spent. Callers must degrade, not fail."""

    code = "budget_exhausted"
    http_status = 429
    retryable = False


class ExternalServiceError(CareerOSError):
    """A source, ATS, model provider or fetcher failed. Usually retryable."""

    code = "external_service_error"
    http_status = 502
    retryable = True


class BotCheckDetected(PolicyBlocked):
    """Automation hit a CAPTCHA or anti-bot wall. Pause and hand to the human."""

    code = "bot_check_detected"
    retryable = False


class ConfigurationError(CareerOSError):
    code = "configuration_error"
    http_status = 503
