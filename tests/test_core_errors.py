"""Structured error contract: codes, statuses, retryability, and the API mapping."""

from fastapi import APIRouter
from fastapi.testclient import TestClient

from app.core.errors import (
    BotCheckDetected,
    BudgetExhausted,
    CareerOSError,
    ConflictError,
    ExternalServiceError,
    KillSwitchEngaged,
    NotFoundError,
    PolicyBlocked,
    ValidationFailed,
)
from app.main import app


def test_codes_and_statuses_are_stable():
    assert NotFoundError("x").http_status == 404
    assert ValidationFailed("x").http_status == 422
    assert ConflictError("x").http_status == 409
    assert PolicyBlocked("x").http_status == 409
    assert BudgetExhausted("x").http_status == 429
    assert ExternalServiceError("x").http_status == 502
    assert KillSwitchEngaged("x").code == "kill_switch_engaged"
    assert isinstance(KillSwitchEngaged("x"), PolicyBlocked)
    assert isinstance(BotCheckDetected("x"), PolicyBlocked)


def test_retryable_classification():
    assert ExternalServiceError("boom").retryable is True
    assert BudgetExhausted("spent").retryable is False
    assert BotCheckDetected("captcha").retryable is False
    # Per-instance override for a non-retryable external failure (e.g. 404).
    assert ExternalServiceError("gone", retryable=False).retryable is False


def test_to_dict_and_details():
    err = ValidationFailed("bad", details={"field": "email"}, code="custom_code")
    assert err.to_dict() == {"code": "custom_code", "message": "bad", "details": {"field": "email"}}
    assert str(err) == "bad"


def test_api_maps_careeros_error_to_status_and_body():
    router = APIRouter()

    @router.get("/__test_error")
    def raise_it():
        raise ConflictError("already applied", details={"opportunity_id": "o1"})

    app.include_router(router)
    try:
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/__test_error")
    finally:
        app.router.routes[:] = [r for r in app.router.routes if getattr(r, "path", "") != "/__test_error"]

    assert response.status_code == 409
    body = response.json()
    assert body["detail"] == "already applied"
    assert body["error"]["code"] == "conflict"
    assert body["error"]["details"] == {"opportunity_id": "o1"}
    assert body["request_id"]
    assert response.headers["X-Request-ID"] == body["request_id"]


def test_base_error_is_500():
    assert CareerOSError("x").http_status == 500
    assert CareerOSError("x").code == "internal_error"
