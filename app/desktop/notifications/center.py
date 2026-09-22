"""The in-process store of what the person was already told (Increment 5).

The center is deliberately **memory only**: it holds at most
:data:`MAX_ENTRIES` recent :class:`~app.desktop.notifications.model.Notification`
objects for the life of the desktop process and nothing else. Nothing here is
persisted — no new table, no migration, no file. What must survive a restart
is the *watermark*, and that is the cursor file
(``app/desktop/notifications/cursor.py``): it records how far each source was
read, so a relaunch neither re-announces history nor floods the person. The
list on the Notifications page is a convenience view of this run, not an
audit log; the audit log is the application's own rows.

The poller calls :meth:`NotificationCenter.add` and only shows a native toast
when it returns ``True``, so the dedupe here is the second half of the
"never notify twice" guarantee (the cursor is the first half).

Everything is per tenant: a read for one tenant can never see, count or mark
another tenant's entries.
"""

import logging
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Optional

from app.desktop.notifications.model import Notification

logger = logging.getLogger(__name__)

#: How many notifications the whole center keeps; the oldest is dropped first.
MAX_ENTRIES = 200

#: Default page size for :meth:`NotificationCenter.recent`.
DEFAULT_LIMIT = 50


@dataclass
class NotificationEntry:
    """One held notification plus the only mutable bit of state: seen/unseen."""

    notification: Notification
    seen: bool = False


class NotificationCenter:
    """Bounded, thread-safe, deduplicating, per-tenant notification memory.

    One lock guards the whole store; every operation is short (dict / list
    work only), so a single lock is both correct and cheap. Entries are held
    in insertion order in an ``OrderedDict`` keyed by ``(tenant_id, key)``:
    the tenant is part of the identity so one tenant can never collide with,
    overwrite or suppress another tenant's notification.
    """

    def __init__(self, maxlen: int = MAX_ENTRIES):
        self._maxlen = max(1, int(maxlen))
        self._entries: "OrderedDict[tuple[str, str], NotificationEntry]" = OrderedDict()
        self._lock = threading.Lock()

    # ------------------------------------------------------------- writes

    def add(self, notification: Notification) -> bool:
        """Store ``notification``; ``False`` if its key is already held.

        ``False`` means "already announced, change nothing": the existing
        entry keeps its ``seen`` flag and its position, so a re-derived
        notification never re-marks itself unseen and never re-toasts.
        """
        identity = (notification.tenant_id, notification.key)
        with self._lock:
            if identity in self._entries:
                return False
            self._entries[identity] = NotificationEntry(notification=notification)
            while len(self._entries) > self._maxlen:
                self._entries.popitem(last=False)
            return True

    def mark_seen(self, tenant_id: str, keys: Optional[list[str]] = None) -> int:
        """Mark this tenant's notifications as seen; return how many changed.

        ``keys is None`` marks every entry the tenant holds. Unknown keys and
        other tenants' keys are silently ignored — marking is idempotent and
        can never reach across tenants.
        """
        wanted = None if keys is None else set(keys)
        changed = 0
        with self._lock:
            for (owner, key), entry in self._entries.items():
                if owner != tenant_id or entry.seen:
                    continue
                if wanted is not None and key not in wanted:
                    continue
                entry.seen = True
                changed += 1
        if changed:
            logger.debug("Marked %d desktop notification(s) as seen", changed)
        return changed

    def clear(self) -> None:
        """Forget everything (process shutdown and tests)."""
        with self._lock:
            self._entries.clear()

    # -------------------------------------------------------------- reads

    def recent(self, tenant_id: str, limit: int = DEFAULT_LIMIT) -> list[Notification]:
        """This tenant's most recent notifications, newest first."""
        return [entry.notification for entry in self.recent_entries(tenant_id, limit)]

    def recent_entries(self, tenant_id: str, limit: int = DEFAULT_LIMIT) -> list[NotificationEntry]:
        """Same as :meth:`recent`, but with each entry's ``seen`` flag.

        Sorted by ``occurred_at`` descending; ties keep insertion order
        (newest inserted first), because several notifications derived from
        one poll can share a timestamp.
        """
        if limit <= 0:
            return []
        with self._lock:
            mine = [(index, entry) for index, ((owner, _), entry) in enumerate(self._entries.items()) if owner == tenant_id]
        mine.sort(key=lambda pair: (pair[1].notification.occurred_at, pair[0]), reverse=True)
        return [entry for _, entry in mine[:limit]]

    def unseen_count(self, tenant_id: str) -> int:
        """How many of this tenant's held notifications are still unseen."""
        with self._lock:
            return sum(1 for (owner, _), entry in self._entries.items() if owner == tenant_id and not entry.seen)

    def count(self, tenant_id: Optional[str] = None) -> int:
        """Entries held for one tenant, or in the whole center."""
        with self._lock:
            if tenant_id is None:
                return len(self._entries)
            return sum(1 for owner, _ in self._entries if owner == tenant_id)


_center: Optional[NotificationCenter] = None
_center_lock = threading.Lock()


def get_center() -> NotificationCenter:
    """The one center this process uses (same pattern as ``get_runner()``)."""
    global _center
    with _center_lock:
        if _center is None:
            _center = NotificationCenter()
        return _center


def reset_center() -> None:
    """Tests only: forget every notification and the singleton itself."""
    global _center
    with _center_lock:
        _center = None
