"""Shared helpers for the local Playwright tests (imported by the test modules).

One headless Chromium per test module, fresh page per execution, fixtures
served from ``file://`` URLs. No network. Tests skip cleanly when the
Playwright runtime or browser is not installed.
"""

from pathlib import Path

import pytest

from app.career.models import AnswerBankEntryCreate, AnswerStatus
from app.career.repository import EvidenceRepository
from app.execution.models import ExecutorKind
from app.execution.playwright import is_available
from app.execution.playwright.browser import BrowserSession, BrowserUnavailable, Pacer
from app.execution.playwright.executor import PlaywrightExecutor

FIXTURES = Path(__file__).resolve().parent / "fixtures"

requires_browser = pytest.mark.skipif(not is_available(), reason="playwright is not installed (pip install -e '.[browser]' && playwright install chromium)")


def fixture_url(name: str, **query) -> str:
    url = (FIXTURES / name).resolve().as_uri()
    if query:
        url += "?" + "&".join(f"{k}={v}" for k, v in query.items())
    return url


def make_session() -> BrowserSession:
    session = BrowserSession(headless=True, profile_dir="")
    try:
        session.start()
    except BrowserUnavailable as exc:
        pytest.skip(str(exc))
    return session


def make_executor(session: BrowserSession, resume_file, dry_run: bool = False, **kwargs) -> PlaywrightExecutor:
    return PlaywrightExecutor(
        session=session,
        dry_run=dry_run,
        submit_wait_ms=kwargs.pop("submit_wait_ms", 3000),
        settle_ms=kwargs.pop("settle_ms", 1500),
        handoff_wait_seconds=0,
        pacer=Pacer(0),
        artifact_files={"RESUME": str(resume_file)} if resume_file else {},
        **kwargs,
    )


def add_bank(session, tenant_id: str, entries: list[tuple[str, str, str]]) -> None:
    repo = EvidenceRepository(session, tenant_id)
    for category, question, answer in entries:
        if repo.find_answer(question) is None:
            repo.create_answer(AnswerBankEntryCreate(category=category, question=question, answer=answer, status=AnswerStatus.APPROVED), "test")
    repo.commit()


LEVER_BANK = [
    ("notice_period", "Earliest start date", "2026-10-01"),
    ("experience_years", "Years of professional experience", "2"),
    ("other", "Which areas interest you?", "Backend"),
]
GENERIC_BANK = [
    ("why_company", "Why do you want to work at Delta?", "Delta builds infrastructure I have worked on in projects like Ticket Engine."),
    ("work_mode", "Preferred work mode", "Remote"),
]


def attach(harness, executor: PlaywrightExecutor) -> None:
    harness.service.executors[ExecutorKind.PLAYWRIGHT_LOCAL] = executor
