"""Increment 5: the privacy and safety contract of desktop notifications.

A notification can appear over any other window — a shared screen, a
projector, a lock screen preview. These tests seed every sensitive column an
event source touches with a unique sentinel and then assert that no sentinel
survives into anything the person (or the machine) can see: the derived
:class:`Notification`, the native toast payload, the rendered page and
partial, the cursor file, the INFO/WARNING log, or ``/desktop/status``.

They also pin the other half of the contract: a notification is a *sentence*.
Acknowledging one or clicking one writes no row, starts no run, and cannot be
pointed anywhere but a bare local CareerOS page.
"""

import json
import logging
import sys
import threading
import types
import uuid
import xml.dom.minidom
from datetime import timedelta
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_tenant_id
from app.application.database.models import ApplicationEventRow, ApplicationRow
from app.application.models import ApplicationStatus
from app.career.models import AnswerBankEntryCreate, AnswerStatus
from app.config import settings
from app.core.timeutils import db_now
from app.desktop.launcher import DesktopApp
from app.desktop.notifications import native as native_module
from app.desktop.notifications.center import get_center, reset_center
from app.desktop.notifications.cursor import (
    APPLICATION_EVENTS,
    EXECUTION_RUNS,
    SIGNALS,
    Cursor,
    CursorStore,
)
from app.desktop.notifications.events import derive_events
from app.desktop.notifications.model import TITLES, Notification
from app.desktop.notifications.native import (
    TITLE_PREFIX,
    WinotifyNotifier,
    escape_launch,
    escape_text,
)
from app.desktop.notifications.poller import NotificationPoller
from app.desktop.runner import get_runner
from app.desktop.services import WorkerSupervisor
from app.execution.database.models import ExecutionRunRow
from app.main import app
from app.pipeline.database.models import OpportunityRow
from app.security import DASHBOARD_COOKIE, DESKTOP_HEADER, DESKTOP_HEADER_VALUE
from app.signals.database.models import SignalRow
from tests.scheduler.conftest import (  # noqa: F401
    db_session,
    evidence,
    fake_attempt,
    jobs,
    matches,
    opportunities,
    other_tenant_id,
    scheduler,
    set_policy,
    tenant_id,
)

COOKIE = {DASHBOARD_COOKIE: settings.api_key}
DESKTOP = {DESKTOP_HEADER: DESKTOP_HEADER_VALUE}

COMPANY = "Notifiable"
ROLE = "Platform Engineer"

# --------------------------------------------------------------- sentinels
#
# One unique token per sensitive column. None of them may appear in anything
# a notification produces; each is distinctive enough that a substring match
# cannot pass by accident.

SIGNAL_SUBJECT = "ZZSENTINELSUBJECT-interview-with-the-CTO"
SIGNAL_SENDER = "ZZSENTINELSENDER-recruiter@employer.invalid"
SIGNAL_DOMAIN = "ZZSENTINELDOMAIN.invalid"
SIGNAL_EXCERPT = "ZZSENTINELEXCERPT we would love to meet you next Tuesday"
SIGNAL_PAYLOAD = "ZZSENTINELPAYLOAD raw message body"
SIGNAL_STATUS_REASON = "ZZSENTINELSIGNALREASON matched by hand"
RUN_ERROR_MESSAGE = "ZZSENTINELERROR timeout on https://zzsentinel-apply.invalid/x for zzsentinel.person@example.invalid"
EVENT_MESSAGE = "ZZSENTINELEVENTMESSAGE the page said: enter the code we emailed you"
STATUS_REASON = "ZZSENTINELSTATUSREASON page text captured at handoff"
APPLY_URL = "https://zzsentinel-apply.invalid/jobs/42/apply?token=zzsentinelurl"
ANSWER_TEXT = "ZZSENTINELANSWER I expect a competitive package"
ARTIFACT_HASH = "ZZSENTINELHASH0123456789abcdef"
CONFIRMATION = "ZZSENTINELCONFIRMATION reference 991"

SENTINELS = (
    SIGNAL_SUBJECT,
    SIGNAL_SENDER,
    SIGNAL_DOMAIN,
    SIGNAL_EXCERPT,
    SIGNAL_PAYLOAD,
    SIGNAL_STATUS_REASON,
    RUN_ERROR_MESSAGE,
    EVENT_MESSAGE,
    STATUS_REASON,
    APPLY_URL,
    ANSWER_TEXT,
    ARTIFACT_HASH,
    CONFIRMATION,
    "ZZSENTINEL",  # catches any fragment of any of the above
)


def assert_clean(blob: str, where: str) -> None:
    """No sentinel and no API key anywhere in ``blob``."""
    for sentinel in SENTINELS:
        assert sentinel not in blob, f"{where} leaked {sentinel!r}"
    assert settings.api_key and settings.api_key not in blob, f"{where} leaked the API key"


# ------------------------------------------------------------------ fixtures


@pytest.fixture(autouse=True)
def clean_center():
    reset_center()
    yield
    reset_center()


@pytest.fixture(autouse=True)
def no_shell():
    """No desktop shell leaks between tests through ``app.state``."""
    yield
    app.state.desktop = None


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "desktop_state_dir", str(tmp_path / "state"))
    return tmp_path / "state"


@pytest.fixture
def client(tenant_id):  # noqa: F811
    app.dependency_overrides[get_tenant_id] = lambda: tenant_id
    try:
        yield TestClient(app, follow_redirects=False, cookies=COOKIE)
    finally:
        app.dependency_overrides.pop(get_tenant_id, None)


@pytest.fixture
def fake_winotify(monkeypatch):
    """A stand-in ``winotify`` that records exactly what the adapter handed it."""
    toasts = []

    class FakeToast:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.launch = ""
            toasts.append(self)

        def show(self):
            self.shown = True

    module = types.ModuleType("winotify")
    module.Notification = FakeToast
    monkeypatch.setitem(sys.modules, "winotify", module)
    monkeypatch.setattr(sys, "platform", "win32")
    return toasts


# ------------------------------------------------------------------- seeding


def _job_of(db_session, attempt):  # noqa: F811
    from app.jobs.database.models import JobRow

    opportunity = db_session.get(OpportunityRow, attempt.opportunity_id)
    return db_session.get(JobRow, opportunity.canonical_job_id)


def _event(db_session, attempt, to_status, at, from_status="PREPARING", metadata=None):  # noqa: F811
    row = ApplicationEventRow(
        id=uuid.uuid4().hex,
        application_id=attempt.id,
        event_type=f"attempt:{to_status.lower()}",
        from_status=from_status,
        to_status=to_status,
        metadata_=metadata or {},
        created_at=at,
    )
    db_session.add(row)
    db_session.commit()
    return row


@pytest.fixture
def seeded(db_session, tenant_id, opportunities, evidence):  # noqa: F811
    """Every source, with a sentinel in every sensitive column it can reach."""
    now = db_now()
    made = {"ids": set(), "signal_ids": set()}

    # Distinct titles: one opportunity per attempt (``applications.job_id`` is unique).
    ready = fake_attempt(db_session, tenant_id, opportunities.make(title=ROLE, company=COMPANY), status=ApplicationStatus.READY, submitted_days_ago=None)
    blocked = fake_attempt(db_session, tenant_id, opportunities.make(title=f"{ROLE} II", company=COMPANY), status=ApplicationStatus.BLOCKED, submitted_days_ago=None)
    loose = fake_attempt(db_session, tenant_id, opportunities.make(title=f"{ROLE} III", company=COMPANY), status=ApplicationStatus.SUBMITTED)

    for attempt in (ready, blocked, loose):
        attempt.status_reason = STATUS_REASON
        attempt.result_url = APPLY_URL
        attempt.external_application_id = ARTIFACT_HASH
        attempt.confirmation = CONFIRMATION
        attempt.tailored_artifact_ids = [ARTIFACT_HASH]
        job = _job_of(db_session, attempt)
        job.application_url = APPLY_URL
        job.source_url = APPLY_URL
        job.description = f"{EVENT_MESSAGE} {ANSWER_TEXT}"
    blocked.blocked_reason = "CAPTCHA_REQUIRED"
    db_session.commit()

    made["ids"].add(_event(db_session, ready, "READY", now - timedelta(minutes=5), metadata={"message": EVENT_MESSAGE, "detail": SIGNAL_EXCERPT}).id)
    made["ids"].add(
        _event(
            db_session,
            blocked,
            "BLOCKED",
            now - timedelta(minutes=4),
            from_status="SUBMITTING",
            metadata={"message": EVENT_MESSAGE, "reason": "CAPTCHA_REQUIRED", "url": APPLY_URL},
        ).id
    )
    made["ids"].add(_event(db_session, loose, "SUBMITTED", now - timedelta(minutes=3), from_status="SUBMITTING", metadata={"confirmation": CONFIRMATION}).id)

    run = ExecutionRunRow(
        tenant_id=tenant_id,
        application_id=ready.id,
        executor_kind="PLAYWRIGHT_LOCAL",
        executor_version="test",
        idempotency_key=uuid.uuid4().hex,
        run_number=1,
        status="FAILED_RETRYABLE",
        outcome="RETRYABLE_FAILURE",
        error_class="TIMEOUT",
        error_message=RUN_ERROR_MESSAGE,
        application_url=APPLY_URL,
        source_url=APPLY_URL,
        confirmation_reference=CONFIRMATION,
        verification_detail=SIGNAL_EXCERPT,
        diagnostics={"page": EVENT_MESSAGE, "resume_hash": ARTIFACT_HASH},
        started_at=now - timedelta(minutes=3),
        finished_at=now - timedelta(minutes=2),
    )
    db_session.add(run)
    db_session.commit()
    made["run_id"] = run.id

    for category, application_id in (("INTERVIEW_INVITATION", loose.id), ("REJECTION", None), ("RECRUITER_CONTACT", None)):
        signal = SignalRow(
            tenant_id=tenant_id,
            source="EMAIL",
            content_hash=uuid.uuid4().hex,
            dedupe_key=uuid.uuid4().hex,
            subject=SIGNAL_SUBJECT,
            sender=SIGNAL_SENDER,
            sender_domain=SIGNAL_DOMAIN,
            excerpt=SIGNAL_EXCERPT,
            payload={"body": SIGNAL_PAYLOAD, "headers": {"from": SIGNAL_SENDER}},
            classification={"rationale": SIGNAL_EXCERPT},
            ai={"prompt": SIGNAL_PAYLOAD},
            category=category,
            status="NEW",
            status_reason=SIGNAL_STATUS_REASON,
            application_id=application_id,
            created_at=now - timedelta(minutes=1),
        )
        db_session.add(signal)
        db_session.commit()
        made["signal_ids"].add(signal.id)

    evidence.create_answer(
        AnswerBankEntryCreate(category="salary", question="What are your salary expectations?", answer=ANSWER_TEXT, status=AnswerStatus.APPROVED),
        actor="test",
    )
    evidence.commit()

    made["attempts"] = {"ready": ready, "blocked": blocked, "loose": loose}
    made["now"] = now
    return made


@pytest.fixture
def derived(db_session, tenant_id, seeded):  # noqa: F811
    cursor = Cursor()
    cursor.start(seeded["now"] - timedelta(hours=1))
    events, moved = derive_events(db_session, tenant_id, cursor, now=seeded["now"])
    assert events, "the fixture must produce something to inspect"
    return events, moved


# ----------------------------------------------------- (i) nothing leaks out


def test_derivation_carries_no_sentinel_and_no_key(derived):
    events, _ = derived
    kinds = {n.kind for n in events}
    assert {"APPLICATION_READY", "ATTENTION_REQUIRED", "SUBMISSION_COMPLETED"} <= kinds, kinds
    assert_clean(json.dumps([n.as_dict() for n in events], default=str, ensure_ascii=False), "the derived notifications")
    for note in events:
        assert note.reason is None or note.reason.replace("_", "").isalnum(), note.reason
        assert note.path.startswith(("/desktop/", "/dashboard/")) and "?" not in note.path


def test_native_toast_payload_carries_no_sentinel_and_no_key(derived, fake_winotify):
    events, _ = derived
    notifier = WinotifyNotifier()
    assert notifier.available
    for note in events:
        assert notifier.send(note, f"http://127.0.0.1:5000/desktop/open?to={quote(note.path, safe='/')}&n=nonce-123") is True
    assert len(fake_winotify) == len(events)
    payload = json.dumps([{"kwargs": t.kwargs, "launch": t.launch} for t in fake_winotify], default=str, ensure_ascii=False)
    assert_clean(payload, "the native toast payload")
    assert TITLE_PREFIX in payload


def test_page_and_partial_carry_no_sentinel_and_no_key(client, derived):
    events, _ = derived
    center = get_center()
    for note in events:
        assert center.add(note) is True
    for path in ("/desktop/notifications", "/desktop/partials/notifications", "/desktop/system"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert_clean(response.text, path)
    page = client.get("/desktop/notifications").text
    assert COMPANY in page, "the page must still say something useful"
    assert "X-API-Key" not in page


# ------------------------------------------------- (ii) the cursor file only


def test_cursor_file_holds_only_watermarks_and_ids(derived, seeded, state_dir, tenant_id):  # noqa: F811
    _, moved = derived
    store = CursorStore()
    store.save(tenant_id, moved)
    raw = store.path.read_text(encoding="utf-8")
    assert_clean(raw, "the cursor file")

    data = json.loads(raw)
    assert set(data) == {"version", "tenants"}
    entry = data["tenants"][tenant_id]
    assert set(entry) <= {APPLICATION_EVENTS, EXECUTION_RUNS, SIGNALS}
    known = seeded["ids"] | seeded["signal_ids"] | {seeded["run_id"]}
    for source, mark in entry.items():
        assert set(mark) == {"at", "ids"}, source
        assert isinstance(mark["at"], str)
        assert set(mark["ids"]) <= known, source


def test_cursor_file_ids_are_bounded(state_dir, tenant_id):  # noqa: F811
    cursor = Cursor()
    at = db_now()
    for index in range(500):
        cursor.advance(APPLICATION_EVENTS, at, f"row-{index:04d}")
    store = CursorStore()
    store.save(tenant_id, cursor)
    ids = json.loads(store.path.read_text(encoding="utf-8"))["tenants"][tenant_id][APPLICATION_EVENTS]["ids"]
    assert len(ids) == 200, "a clock-tick tie cannot grow the file without bound"


def test_a_corrupt_cursor_file_starts_fresh_without_raising(state_dir, tenant_id, caplog):  # noqa: F811
    store = CursorStore()
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{not json at all", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        cursor = store.load(tenant_id)
    assert cursor.watermark(APPLICATION_EVENTS) is not None, "a fresh cursor starts at now, never at the beginning of time"
    store.save(tenant_id, cursor)
    assert json.loads(store.path.read_text(encoding="utf-8"))["version"] == 1


# --------------------------------------------------------- (iii) log records


def test_a_poll_logs_no_sentinel_and_no_key(db_session, tenant_id, seeded, state_dir, caplog):  # noqa: F811
    from app.database import get_session_factory

    sent = []

    class Recorder:
        name = "recorder"
        available = True

        def send(self, notification, launch_url=None):
            sent.append(notification)
            return True

    poller = NotificationPoller(
        tenant_id=tenant_id,
        store=CursorStore(),
        notifier=Recorder(),
        session_factory=get_session_factory(),
        launch_url_for=lambda n: f"http://127.0.0.1:5000/desktop/open?to={quote(n.path, safe='/')}&n=nonce",
    )
    poller.store.save(tenant_id, _cursor_at(seeded["now"] - timedelta(hours=1)))
    with caplog.at_level(logging.DEBUG):
        fresh = poller.poll_once()
    assert fresh and sent, "the poll must have produced something to log about"

    loud = [record for record in caplog.records if record.levelno >= logging.INFO]
    assert loud, "a poll with new notifications logs at INFO"
    assert_clean("\n".join(f"{r.name} {r.getMessage()}" for r in loud), "an INFO/WARNING log record")
    for record in loud:
        for note in fresh:
            assert note.body not in record.getMessage(), "a body never reaches INFO"


def _cursor_at(at):
    cursor = Cursor()
    cursor.start(at)
    return cursor


def test_a_failing_poll_logs_the_exception_type_only(state_dir, tenant_id, caplog):  # noqa: F811
    poller = NotificationPoller(tenant_id=tenant_id, store=CursorStore(), notifier=native_module.NullNotifier(), session_factory=lambda: _NullSession(), interval=1.0)

    def boom(db, tenant, cursor, now=None):
        poller.stop()  # one pass only
        raise RuntimeError(RUN_ERROR_MESSAGE)

    poller._derive = boom
    with caplog.at_level(logging.DEBUG):
        poller.run()
    assert poller.totals["errors"] == 1
    assert poller.totals["last_error"] == "RuntimeError", "the message could quote a page, a URL or an address"
    assert_clean("\n".join(r.getMessage() for r in caplog.records if r.levelno >= logging.INFO), "a failed poll's log")


class _NullSession:
    def close(self):
        return None


# ------------------------------------------------------- (iv) /desktop/status


def test_status_json_with_a_running_poller_carries_no_key_or_sentinel(client, state_dir, tenant_id):  # noqa: F811
    polled = threading.Event()

    def boom(db, tenant, cursor, now=None):
        polled.set()
        raise RuntimeError(RUN_ERROR_MESSAGE)

    poller = NotificationPoller(tenant_id=tenant_id, store=CursorStore(), notifier=native_module.NullNotifier(), derive=boom, session_factory=lambda: _NullSession(), interval=1.0)
    shell = DesktopApp(app)
    shell.notifications = WorkerSupervisor(lambda: poller, name="test-notifier")
    app.state.desktop = shell
    assert shell.notifications.start() is True
    try:
        assert polled.wait(5.0), "the supervised poller never ran"
        response = client.get("/desktop/status")
        assert response.status_code == 200
        payload = response.json()
        assert payload["notifications"]["state"] == "running"
        assert payload["notifications"]["totals"]["last_error"] == "RuntimeError"
        assert_clean(json.dumps(payload, ensure_ascii=False), "/desktop/status")
    finally:
        shell.notifications.stop(timeout=5.0)


# ---------------------------------------- (v) nothing runs, nothing is written


def _counts(db_session):  # noqa: F811
    db_session.expire_all()
    return {
        "applications": db_session.query(ApplicationRow).count(),
        "events": db_session.query(ApplicationEventRow).count(),
        "runs": db_session.query(ExecutionRunRow).count(),
    }


def test_the_seen_and_open_routes_write_nothing_and_never_reach_the_runner(client, db_session, tenant_id, derived):  # noqa: F811
    events, _ = derived
    center = get_center()
    for note in events:
        center.add(note)
    before = _counts(db_session)
    assert get_runner().active() == []

    shell = DesktopApp(app)
    app.state.desktop = shell

    seen = client.post("/desktop/api/notifications/seen", json={}, headers=DESKTOP)
    assert seen.status_code == 200 and seen.json()["marked"] == len(events)
    keyed = client.post("/desktop/api/notifications/seen", json={"keys": [events[0].key]}, headers=DESKTOP)
    assert keyed.status_code == 200 and keyed.json()["unseen"] == 0

    opened = client.get(f"/desktop/open?to=/desktop/attention&n={shell.open_nonces.mint()}")
    assert opened.status_code == 303 and opened.headers["location"] == "/desktop/attention"
    assert "set-cookie" not in opened.headers, "a click never signs anyone in"

    assert _counts(db_session) == before, "a notification click or acknowledgement writes nothing"
    assert get_runner().active() == [], "nothing was started"


def test_the_notification_routes_take_no_database_session(client):
    import inspect

    from app.desktop.routes import desktop_notifications_seen, desktop_open

    for endpoint in (desktop_notifications_seen, desktop_open):
        annotations = {str(p.annotation) for p in inspect.signature(endpoint).parameters.values()}
        assert not any("Session" in a for a in annotations), endpoint.__name__
    for route in app.routes:
        if getattr(route, "path", "") in ("/desktop/api/notifications/seen", "/desktop/open"):
            flat = " ".join(str(d.call) for d in route.dependant.flat_dependant.dependencies)
            assert "get_db" not in flat, route.path


# ------------------------------------------------------ (vi) the open route


@pytest.mark.parametrize(
    "target",
    [
        "//evil.example/",
        "https://evil.example/",
        "/api/v1/attempts/x/run",
        "/desktop/../etc",
        "/desktop/a?key=b",
        "/desktop/a#x",
        "\\evil",
        "/desktop/api/attempts/x/run",
    ],
)
def test_open_refuses_everything_but_a_bare_local_page(client, target):
    shell = DesktopApp(app)
    app.state.desktop = shell
    response = client.get(f"/desktop/open?to={quote(target, safe='')}&n={shell.open_nonces.mint()}")
    assert response.status_code == 400, target
    assert "location" not in response.headers


def test_open_needs_a_nonce_and_burns_it(client):
    shell = DesktopApp(app)
    app.state.desktop = shell
    assert client.get("/desktop/open?to=/desktop/attention").status_code == 403, "no nonce"
    assert client.get("/desktop/open?to=/desktop/attention&n=guessed").status_code == 403, "wrong nonce"
    nonce = shell.open_nonces.mint()
    assert client.get(f"/desktop/open?to=/desktop/attention&n={nonce}").status_code == 303
    assert client.get(f"/desktop/open?to=/desktop/attention&n={nonce}").status_code == 403, "a nonce is single use"


# -------------------------------------------------- (vii) the real toast XML


#: Reconstructed from ``winotify.TEMPLATE`` (winotify 1.1.0): the toast XML is
#: interpolated into a PowerShell *expandable* here-string and then handed to
#: ``XmlDocument.LoadXml``. Both layers are what the escaping must survive.
WINOTIFY_XML = """<toast {launch} duration="{duration}">
    <visual>
        <binding template="ToastImageAndText02">
            <image id="1" src="{icon}" />
            <text id="1"><![CDATA[{title}]]></text>
            <text id="2"><![CDATA[{msg}]]></text>
        </binding>
    </visual>
    <actions>
        {actions}
    </actions>
    {audio}
</toast>"""

HOSTILE_BODY = 'Body with `$env:USERNAME` and ]]> and & and <b> and "quotes" and $(Get-Content secret.txt)'
HOSTILE_LAUNCH = 'http://127.0.0.1:5000/desktop/open?to=/desktop/a&n=abc&x=<1>"evil'


def _powershell_expandable(text: str) -> str:
    """What PowerShell renders for an expandable here-string, escapes only.

    A backtick escapes the next character; an unescaped ``$`` would start a
    variable or subexpression, which is exactly what must never remain.
    """
    out = []
    index = 0
    while index < len(text):
        char = text[index]
        if char == "`" and index + 1 < len(text):
            out.append(text[index + 1])
            index += 2
            continue
        assert char != "$", f"an unescaped $ survived at {index}: {text[index : index + 30]!r}"
        out.append(char)
        index += 1
    return "".join(out)


def test_the_real_winotify_template_neither_expands_nor_breaks():
    document = WINOTIFY_XML.format(
        launch=f'activationType="protocol" launch="{escape_launch(HOSTILE_LAUNCH)}"',
        duration="short",
        icon="",
        title=escape_text(TITLE_PREFIX + TITLES["ATTENTION_REQUIRED"]),
        msg=escape_text(HOSTILE_BODY),
        actions="",
        audio='<audio silent="true" />',
    )
    rendered = _powershell_expandable(document)  # asserts no $ expansion survives

    dom = xml.dom.minidom.parseString(rendered)  # LoadXml's job: it must parse
    toast = dom.documentElement
    assert toast.tagName == "toast"
    assert toast.getAttribute("launch") == HOSTILE_LAUNCH, "the XML parser hands Windows the original URL back"
    assert toast.getAttribute("duration") == "short"

    texts = dom.getElementsByTagName("text")
    assert len(texts) == 2, "the CDATA section was not broken out of"
    body = "".join(node.data for node in texts[1].childNodes)
    # ``]]>`` is the one sequence CDATA cannot carry; winotify has no escape for
    # it, so the adapter breaks it up. Everything else is the literal body.
    assert body == HOSTILE_BODY.replace("]]>", "]]&gt;")
    assert "$env:USERNAME" in body, "the variable stayed text; it was never expanded"
    assert "$(Get-Content" in body


def test_every_toast_title_is_safe_in_the_powershell_tag_assignment():
    # winotify also writes the title into ``$Toast.Tag = "<title>"``, a plain
    # double-quoted PowerShell string: a quote, a backtick or a ``$`` there
    # would end the string or expand. The titles are a closed vocabulary.
    for title in TITLES.values():
        escaped = escape_text(TITLE_PREFIX + title)
        assert '"' not in escaped and escaped == TITLE_PREFIX + title


def test_escape_launch_survives_the_ampersand_that_every_click_url_carries():
    url = "http://127.0.0.1:53123/desktop/open?to=/desktop/applications/a-1&n=tok"
    escaped = escape_launch(url)
    assert "&amp;" in escaped and "&n=" not in escaped
    parsed = xml.dom.minidom.parseString(f'<toast launch="{escaped}" />')
    assert parsed.documentElement.getAttribute("launch") == url


# ------------------------------------------------------ (viii) two tenants


def _note(tenant: str, key: str, body: str) -> Notification:
    return Notification(
        key=key,
        kind="APPLICATION_READY",
        title=TITLES["APPLICATION_READY"],
        body=body,
        path="/desktop/applications/app-1",
        tenant_id=tenant,
        occurred_at=db_now(),
        application_id="app-1",
    )


def test_one_tenants_notification_is_invisible_to_another(client, tenant_id, other_tenant_id):  # noqa: F811
    center = get_center()
    mine = _note(tenant_id, "mine", "Your application for Platform Engineer at Mine is ready for review.")
    theirs = _note(other_tenant_id, "theirs", "Your application for Platform Engineer at Theirs is ready for review.")
    assert center.add(mine) and center.add(theirs)

    assert [n.key for n in center.recent(tenant_id)] == ["mine"]
    assert [n.key for n in center.recent(other_tenant_id)] == ["theirs"]
    assert center.count(tenant_id) == 1 and center.unseen_count(other_tenant_id) == 1

    for path in ("/desktop/notifications", "/desktop/partials/notifications"):
        body = client.get(path).text
        assert "at Mine is ready" in body, path
        assert "Theirs" not in body, path

    marked = client.post("/desktop/api/notifications/seen", json={"keys": ["mine", "theirs"]}, headers=DESKTOP)
    assert marked.status_code == 200 and marked.json() == {"marked": 1, "unseen": 0}
    assert center.unseen_count(other_tenant_id) == 1, "another tenant's badge is untouched"


def test_derivation_never_crosses_a_tenant(db_session, tenant_id, other_tenant_id, seeded):  # noqa: F811
    cursor = Cursor()
    cursor.start(seeded["now"] - timedelta(hours=1))
    mine, _ = derive_events(db_session, tenant_id, cursor, now=seeded["now"])
    theirs, _ = derive_events(db_session, other_tenant_id, _cursor_at(seeded["now"] - timedelta(hours=1)), now=seeded["now"])
    assert mine and theirs == []
    assert {n.tenant_id for n in mine} == {tenant_id}


def test_a_cross_tenant_signal_reference_cannot_pull_a_company_name(db_session, tenant_id, other_tenant_id, opportunities, seeded):  # noqa: F811
    """A stray ``application_id`` pointing at another tenant reveals nothing."""
    theirs = seeded["attempts"]["loose"]  # belongs to tenant_id
    signal = SignalRow(
        tenant_id=other_tenant_id,
        source="EMAIL",
        content_hash=uuid.uuid4().hex,
        dedupe_key=uuid.uuid4().hex,
        subject=SIGNAL_SUBJECT,
        category="INTERVIEW_INVITATION",
        status="NEW",
        application_id=theirs.id,
        created_at=seeded["now"],
    )
    db_session.add(signal)
    db_session.commit()
    events, _ = derive_events(db_session, other_tenant_id, _cursor_at(seeded["now"] - timedelta(hours=1)), now=seeded["now"])
    assert len(events) == 1
    assert COMPANY not in events[0].body, "the other tenant's company must not appear"
    assert_clean(events[0].body, "a cross-tenant signal")
