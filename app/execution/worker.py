"""The smallest local execution worker.

    python -m app.execution.worker --tenant default --once --dry-run

Claims SUBMIT items from the existing queue, runs the local Playwright
executor (one browser, one item at a time), lets ``ExecutionService`` persist
every result, and shuts the browser down cleanly on Ctrl-C. No cloud, no
second queue, no in-memory state beyond the current item.
"""

import argparse
import logging
import signal
import socket
import time
from typing import Optional

from app.config import settings
from app.core.logging import configure_logging
from app.database import get_session_factory
from app.execution import submission_mode
from app.execution.models import ExecutorKind
from app.execution.playwright.browser import BrowserSession, BrowserUnavailable
from app.execution.playwright.executor import PlaywrightExecutor
from app.execution.service import ExecutionService

logger = logging.getLogger(__name__)


class LocalWorker:
    def __init__(
        self,
        tenant_id: str,
        worker_id: Optional[str] = None,
        dry_run: Optional[bool] = None,
        limit: int = 1,
        poll_seconds: Optional[float] = None,
        headless: Optional[bool] = None,
        executor: Optional[PlaywrightExecutor] = None,
    ):
        self.tenant_id = tenant_id
        self.worker_id = worker_id or f"local-{socket.gethostname()}"[:128]
        self.limit = max(1, min(limit, settings.execution_worker_concurrency))
        self.poll_seconds = settings.execution_worker_poll_seconds if poll_seconds is None else poll_seconds
        self.session = executor.session if executor else BrowserSession(headless=headless)
        self.executor = executor or PlaywrightExecutor(session=self.session, dry_run=dry_run)
        self._stop = False
        self.totals: dict[str, int] = {}

    def stop(self, *_args) -> None:
        self._stop = True

    def run_once(self) -> dict[str, int]:
        session = get_session_factory()()
        try:
            service = ExecutionService(session, self.tenant_id, actor=f"worker:{self.worker_id}", executors={ExecutorKind.PLAYWRIGHT_LOCAL: self.executor})
            counts = service.run_queue(self.worker_id, limit=self.limit, executor_kind=ExecutorKind.PLAYWRIGHT_LOCAL)
            for key, value in counts.items():
                self.totals[key] = self.totals.get(key, 0) + value
            return counts
        finally:
            session.close()

    def run(self, once: bool = False) -> dict[str, int]:
        try:
            self.session.start()
        except BrowserUnavailable as exc:
            logger.error("%s", exc)
            return {"error": 1}
        logger.info("Local worker %s started for tenant %s (dry_run=%s, headless=%s)", self.worker_id, self.tenant_id, self.executor.dry_run, self.session.headless)
        try:
            while not self._stop:
                counts = self.run_once()
                if once:
                    break
                if not counts.get("claimed"):
                    time.sleep(self.poll_seconds)
        finally:
            self.executor.close()
            logger.info("Local worker %s stopped: %s", self.worker_id, self.totals)
        return dict(self.totals)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="CareerOS local execution worker (Playwright)")
    parser.add_argument("--tenant", default=settings.default_tenant_id)
    parser.add_argument("--worker-id", default=None)
    parser.add_argument("--once", action="store_true", help="process one batch and exit")
    parser.add_argument("--limit", type=int, default=1)
    parser.add_argument("--dry-run", dest="dry_run", action="store_true", default=None, help="never press submit (default from PLAYWRIGHT_DRY_RUN)")
    parser.add_argument("--live", dest="dry_run", action="store_false", help="press submit for real when every gate passes")
    parser.add_argument("--headed", action="store_true", help="show the browser window (lets you clear CAPTCHA/login yourself)")
    args = parser.parse_args(argv)
    configure_logging(settings.log_level)
    if args.dry_run is False:
        # ``--live`` on the command line is this process' explicit switch to LIVE.
        submission_mode.enable_live(args.tenant, actor="worker --live")
    elif args.dry_run is None and not settings.playwright_dry_run:
        # PLAYWRIGHT_DRY_RUN=false alone is not an explicit choice: the gate would refuse every click.
        parser.error("PLAYWRIGHT_DRY_RUN=false needs --live to enable live submission in this worker")
    worker = LocalWorker(args.tenant, args.worker_id, dry_run=args.dry_run, limit=args.limit, headless=False if args.headed else None)
    signal.signal(signal.SIGINT, worker.stop)
    signal.signal(signal.SIGTERM, worker.stop)
    worker.run(once=args.once)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
