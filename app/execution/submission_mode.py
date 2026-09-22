"""SAFE / LIVE submission mode — the server-side switch in front of every real click.

CareerOS starts in **SAFE** (dry run only). A real employer submission is
possible only after the person explicitly switches the tenant to **LIVE
SUBMISSION ENABLED**; switching back to SAFE takes effect immediately, even
for a run already in progress (its pre-submit gate refuses the click).

Deliberately in-memory and per process: a fresh start of the application
(or of a standalone worker) is always SAFE, and nothing on disk, in the
environment or in a request can make LIVE the default. The mode is enforced
server-side at three places, none of which trusts the UI:

* ``POST /desktop/api/attempts/{id}/run`` refuses ``mode=live`` while SAFE;
* ``LocalRunner.start`` refuses a live job while SAFE;
* ``ExecutionService._pre_submit_gate`` — called immediately before every
  submit click by the Playwright executor, the extension gate and the mock
  executor — fails with ``SAFE_MODE`` while SAFE.

This adds a gate; it replaces none. The typed ``SUBMIT`` confirmation, the
kill switch, caps, idempotency and every precondition still apply in LIVE.
"""

import logging
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from app.core.timeutils import utc_now

logger = logging.getLogger(__name__)

SAFE = "SAFE"
LIVE = "LIVE"

#: The failure the pre-submit gate reports while SAFE is active.
SAFE_MODE_FAILURE = "SAFE_MODE: live submission is not enabled (switch to LIVE SUBMISSION ENABLED first)"


@dataclass(frozen=True)
class ModeState:
    mode: str
    changed_at: Optional[datetime] = None
    changed_by: Optional[str] = None

    @property
    def live(self) -> bool:
        return self.mode == LIVE

    def as_dict(self) -> dict:
        return {"mode": self.mode, "live_submission_enabled": self.live, "changed_at": self.changed_at.isoformat() if self.changed_at else None, "changed_by": self.changed_by}


_lock = threading.Lock()
_states: dict[str, ModeState] = {}


def get_state(tenant_id: str) -> ModeState:
    with _lock:
        return _states.get(tenant_id) or ModeState(SAFE)


def is_live_enabled(tenant_id: Optional[str]) -> bool:
    if not tenant_id:
        return False
    return get_state(tenant_id).live


def set_mode(tenant_id: str, mode: str, actor: str) -> ModeState:
    if not tenant_id:
        raise ValueError("tenant_id is required")
    if mode not in (SAFE, LIVE):
        raise ValueError(f"mode must be {SAFE} or {LIVE}")
    state = ModeState(mode, utc_now(), (actor or "unknown")[:64])
    with _lock:
        previous = (_states.get(tenant_id) or ModeState(SAFE)).mode
        _states[tenant_id] = state
    if previous != mode:
        logger.warning("Submission mode for tenant %s changed %s -> %s by %s", tenant_id, previous, mode, state.changed_by)
    return state


def enable_live(tenant_id: str, actor: str) -> ModeState:
    return set_mode(tenant_id, LIVE, actor)


def set_safe(tenant_id: str, actor: str) -> ModeState:
    return set_mode(tenant_id, SAFE, actor)


def reset() -> None:
    """Back to the fresh-start state: every tenant SAFE."""
    with _lock:
        _states.clear()
