"""Increment 5: the native toast adapters.

Nothing here touches the real Windows notification platform — ``winotify`` is
replaced in ``sys.modules`` by a fake that records exactly what the adapter
handed it, so the privacy contract (no key, no cookie, no token, no body or
URL in a log line) is asserted on real values rather than on a mock's promise.
"""

import logging
import sys
import types
from datetime import datetime, timezone

import pytest

from app.config import settings
from app.desktop.notifications import native
from app.desktop.notifications.model import (
    APPLICATION_READY,
    ATTENTION_REQUIRED,
    TITLES,
    Notification,
)
from app.desktop.notifications.native import (
    NullNotifier,
    WinotifyNotifier,
    escape_launch,
    escape_text,
    select_notifier,
)

BODY = "Your application for Staff Engineer at Northwind is ready for review."
LAUNCH = "http://127.0.0.1:53123/desktop/open?to=/desktop/applications/app-1&n=deadbeef"


# ------------------------------------------------------------------ fakes


class FakeToast:
    """Stands in for ``winotify.Notification``; records everything it is given."""

    instances: list["FakeToast"] = []
    raise_on_show: BaseException | None = None

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.launch = ""  # winotify's own default
        self.shown = 0
        FakeToast.instances.append(self)

    def show(self):
        self.shown += 1
        if FakeToast.raise_on_show is not None:
            raise FakeToast.raise_on_show


@pytest.fixture
def fake_winotify(monkeypatch):
    """Install a fake ``winotify`` module and pretend we are on Windows."""
    FakeToast.instances = []
    FakeToast.raise_on_show = None
    module = types.ModuleType("winotify")
    module.Notification = FakeToast
    monkeypatch.setitem(sys.modules, "winotify", module)
    monkeypatch.setattr(sys, "platform", "win32")
    return module


def make_notification(kind=APPLICATION_READY, body=BODY, **kwargs) -> Notification:
    fields = dict(
        key="attempt:app-1:READY:ev-1",
        kind=kind,
        title=TITLES[kind],
        body=body,
        path="/desktop/applications/app-1",
        tenant_id="tenant-1",
        occurred_at=datetime(2026, 9, 13, 10, 0, tzinfo=timezone.utc),
        application_id="app-1",
    )
    fields.update(kwargs)
    return Notification(**fields)


# ------------------------------------------------------------------ payload


def test_title_carries_the_careeros_prefix_and_the_body_is_passed_verbatim(fake_winotify):
    notification = make_notification()

    assert WinotifyNotifier().send(notification) is True

    toast = FakeToast.instances[-1]
    assert toast.kwargs["app_id"] == "CareerOS"
    assert toast.kwargs["title"] == "CareerOS \u2014 Application Ready"
    assert toast.kwargs["title"].startswith("CareerOS \u2014 ")
    assert toast.kwargs["msg"] == BODY
    assert toast.kwargs["duration"] == "short"
    assert toast.shown == 1


def test_launch_is_only_set_when_a_url_is_passed(fake_winotify):
    notification = make_notification()

    WinotifyNotifier().send(notification)
    assert FakeToast.instances[-1].launch == ""

    WinotifyNotifier().send(notification, "http://127.0.0.1:53123/desktop/")
    assert FakeToast.instances[-1].launch == "http://127.0.0.1:53123/desktop/"


def test_a_query_string_launch_url_is_xml_escaped(fake_winotify):
    """winotify interpolates ``launch`` into a toast XML attribute unescaped.

    A raw ``&`` makes the document malformed, PowerShell's ``LoadXml`` throws
    in a detached process whose stderr is discarded, and the toast silently
    never appears. Escaping is what makes the click target survive.
    """
    WinotifyNotifier().send(make_notification(), LAUNCH)

    launch = FakeToast.instances[-1].launch
    assert "&amp;n=deadbeef" in launch
    assert "&n=" not in launch
    assert launch.startswith("http://127.0.0.1:53123/desktop/open?to=/desktop/applications/app-1")


def test_powershell_expansion_in_text_is_neutralised():
    """``winotify`` embeds the text in an *expandable* PowerShell here-string."""
    assert escape_text("Role at A$AP") == "Role at A`$AP"
    assert escape_text("$env:USERNAME") == "`$env:USERNAME"
    assert escape_text("back`tick") == "back``tick"
    # Ordinary notification text is untouched.
    assert escape_text(BODY) == BODY
    assert escape_launch("http://127.0.0.1:1/desktop/") == "http://127.0.0.1:1/desktop/"


def test_the_toast_carries_no_key_cookie_or_token(fake_winotify, monkeypatch):
    monkeypatch.setattr(settings, "api_key", "super-secret-api-key-value", raising=False)
    notification = make_notification(
        kind=ATTENTION_REQUIRED,
        body="A login is required for Northwind.",
        reason="AUTH_REQUIRED",
    )

    WinotifyNotifier().send(notification, LAUNCH)

    toast = FakeToast.instances[-1]
    # Exactly the safe fields, and nothing else.
    assert toast.kwargs["msg"] == "A login is required for Northwind."
    assert toast.kwargs["title"] == "CareerOS \u2014 Attention Required"
    payload = " ".join([toast.kwargs["title"], toast.kwargs["msg"], toast.launch]).lower()
    assert settings.api_key.lower() not in payload
    for forbidden in ("cookie", "token", "authorization", "password", "secret"):
        assert forbidden not in payload


# ------------------------------------------------------------------ failure


def test_an_exception_in_show_returns_false_and_logs_no_body_or_url(fake_winotify, caplog):
    FakeToast.raise_on_show = OSError("powershell.exe could not be spawned at C:\\secret\\path")
    notification = make_notification()

    with caplog.at_level(logging.DEBUG, logger=native.logger.name):
        assert WinotifyNotifier().send(notification, LAUNCH) is False

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "a failed toast must be reported at WARNING"
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "OSError" in text
    assert BODY not in text
    assert "Northwind" not in text
    assert LAUNCH not in text
    assert "127.0.0.1" not in text
    assert "secret" not in text


def test_a_broken_winotify_module_never_raises(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "winotify", None)
    notifier = WinotifyNotifier()

    assert notifier.available is False
    assert notifier.send(make_notification(), LAUNCH) is False


# ------------------------------------------------------------------ null


def test_null_notifier_is_unavailable_and_silent():
    notifier = NullNotifier()

    assert notifier.available is False
    assert notifier.name == "null"
    assert notifier.send(make_notification()) is False
    assert notifier.send(make_notification(), LAUNCH) is False


# ------------------------------------------------------------------ selection


def test_select_notifier_picks_winotify_on_windows(fake_winotify, caplog):
    with caplog.at_level(logging.INFO, logger=native.logger.name):
        notifier = select_notifier()

    assert isinstance(notifier, WinotifyNotifier)
    assert notifier.available is True
    assert notifier.name == "winotify"
    assert any(r.levelno == logging.INFO for r in caplog.records)


def test_select_notifier_falls_back_when_winotify_cannot_be_imported(monkeypatch, caplog):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "winotify", None)

    with caplog.at_level(logging.INFO, logger=native.logger.name):
        notifier = select_notifier()

    assert isinstance(notifier, NullNotifier)
    assert "in-app only" in caplog.text


def test_select_notifier_falls_back_off_windows(monkeypatch, caplog):
    monkeypatch.setattr(sys, "platform", "linux")

    with caplog.at_level(logging.INFO, logger=native.logger.name):
        notifier = select_notifier()

    assert isinstance(notifier, NullNotifier)
    assert "linux" in caplog.text


def test_select_notifier_honours_prefer_native_false(fake_winotify):
    assert isinstance(select_notifier(prefer_native=False), NullNotifier)


def test_select_notifier_never_raises(monkeypatch):
    """Even a module that explodes on attribute access must not take it down."""

    class Exploding(types.ModuleType):
        def __getattr__(self, name):
            raise RuntimeError("boom")

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "winotify", Exploding("winotify"))

    notifier = select_notifier()
    assert notifier.send(make_notification(), LAUNCH) is False


# ------------------------------------------------------------------ logging


def test_info_logs_never_carry_notification_text(fake_winotify, caplog):
    notification = make_notification()

    with caplog.at_level(logging.INFO, logger=native.logger.name):
        notifier = select_notifier()
        notifier.send(notification, LAUNCH)

    info = [r for r in caplog.records if r.levelno >= logging.INFO]
    text = " ".join(r.getMessage() for r in info)
    assert text, "the adapter choice is announced at INFO"
    assert BODY not in text
    assert "Northwind" not in text
    assert notification.title not in text
    assert LAUNCH not in text
    assert "127.0.0.1" not in text


def test_debug_logs_carry_only_the_kind_and_key(fake_winotify, caplog):
    notification = make_notification()

    with caplog.at_level(logging.DEBUG, logger=native.logger.name):
        WinotifyNotifier().send(notification, LAUNCH)

    text = " ".join(r.getMessage() for r in caplog.records)
    assert notification.kind in text
    assert notification.key in text
    assert BODY not in text
    assert LAUNCH not in text
