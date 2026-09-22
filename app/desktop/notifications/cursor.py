"""What the desktop has already announced, remembered in one tiny local file.

No database table: a notification is derived, never stored. All the poller
needs to remember is *how far it has read* each source — a watermark
timestamp per source plus the ids of the rows that carry exactly that
timestamp (``db_now()`` ties within one clock tick on Windows, so a bare
timestamp would either repeat or skip a row).

The file lives under ``settings.desktop_state_dir`` (git-ignored, local
only) and holds nothing but watermarks and tie ids — no company, no role,
no body text, nothing about a signal.
"""

import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from app.config import settings
from app.core.timeutils import utc_now

logger = logging.getLogger(__name__)

#: The three derivation sources (see ``events.derive_events``).
APPLICATION_EVENTS = "application_events"
EXECUTION_RUNS = "execution_runs"
SIGNALS = "signals"

SOURCES = (APPLICATION_EVENTS, EXECUTION_RUNS, SIGNALS)

VERSION = 1

#: File name inside ``settings.desktop_state_dir``.
FILE_NAME = "notifications.json"

#: Ties are bounded so a pathological clock tick cannot grow the file.
MAX_TIE_IDS = 200


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    """Rows are naive UTC, ``now`` may be aware: compare on one scale."""
    if value is None:
        return None
    return value.replace(tzinfo=None) if value.tzinfo is not None else value


@dataclass
class _Mark:
    """One source's watermark plus the ids seen *at* that exact instant."""

    at: Optional[datetime] = None
    ids: set[str] = field(default_factory=set)


@dataclass
class Cursor:
    """Per-source reading position for one tenant."""

    marks: dict[str, _Mark] = field(default_factory=dict)

    # ------------------------------------------------------------- helpers

    def _mark(self, source: str) -> _Mark:
        return self.marks.setdefault(source, _Mark())

    def watermark(self, source: str) -> Optional[datetime]:
        """The oldest row timestamp still worth querying (naive UTC)."""
        return self._mark(source).at

    # ---------------------------------------------------------------- read

    def is_new(self, source: str, at: Optional[datetime], row_id: str) -> bool:
        """True when this row has not been announced yet."""
        mark = self._mark(source)
        at = _aware(at)
        if mark.at is None:
            return True
        if at is None:
            return False
        if at > mark.at:
            return True
        if at < mark.at:
            return False
        return row_id not in mark.ids

    # --------------------------------------------------------------- write

    def advance(self, source: str, at: Optional[datetime], row_id: str) -> None:
        """Record that this row was handled."""
        mark = self._mark(source)
        at = _aware(at)
        if at is None:
            return
        if mark.at is None or at > mark.at:
            mark.at = at
            mark.ids = {row_id}
            return
        if at == mark.at:
            mark.ids.add(row_id)
            if len(mark.ids) > MAX_TIE_IDS:
                mark.ids = set(sorted(mark.ids)[-MAX_TIE_IDS:])

    def copy(self) -> "Cursor":
        """An independent cursor at the same position (derivation never mutates its input)."""
        return Cursor(marks={source: _Mark(at=mark.at, ids=set(mark.ids)) for source, mark in self.marks.items()})

    def start(self, at: datetime) -> None:
        """Initialise every source to ``at`` (first run: announce no history)."""
        at = _aware(at)
        for source in SOURCES:
            self.marks[source] = _Mark(at=at, ids=set())

    # ------------------------------------------------------- serialisation

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for source in SOURCES:
            mark = self.marks.get(source)
            if mark is None or mark.at is None:
                continue
            out[source] = {"at": mark.at.isoformat(), "ids": sorted(mark.ids)}
        return out

    @classmethod
    def from_dict(cls, data: Any) -> "Cursor":
        cursor = cls()
        if not isinstance(data, dict):
            return cursor
        for source in SOURCES:
            entry = data.get(source)
            if not isinstance(entry, dict):
                continue
            at = entry.get("at")
            parsed: Optional[datetime] = None
            if isinstance(at, str):
                try:
                    parsed = _aware(datetime.fromisoformat(at))
                except ValueError:
                    parsed = None
            if parsed is None:
                continue
            raw_ids = entry.get("ids")
            ids = {str(i) for i in raw_ids} if isinstance(raw_ids, list) else set()
            cursor.marks[source] = _Mark(at=parsed, ids=ids)
        return cursor


def default_path() -> Path:
    return Path(settings.desktop_state_dir) / FILE_NAME


class CursorStore:
    """The cursor file: tolerant load, atomic save, never raises at the poller."""

    def __init__(self, path: Optional[Path | str] = None):
        self._path = Path(path) if path is not None else None

    @property
    def path(self) -> Path:
        """Resolved late so tests can monkeypatch ``settings.desktop_state_dir``."""
        return self._path if self._path is not None else default_path()

    # ---------------------------------------------------------------- read

    def _read(self) -> dict[str, Any]:
        path = self.path
        if not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("desktop notification cursor unreadable (%s); starting fresh", type(exc).__name__)
            return {}
        if not isinstance(raw, dict) or raw.get("version") != VERSION:
            if raw:
                logger.warning("desktop notification cursor has an unexpected shape; starting fresh")
            return {}
        tenants = raw.get("tenants")
        return tenants if isinstance(tenants, dict) else {}

    def load(self, tenant_id: str, now: Optional[datetime] = None) -> Cursor:
        """This tenant's cursor; a missing/corrupt entry starts at ``now``."""
        entry = self._read().get(tenant_id)
        if isinstance(entry, dict):
            cursor = Cursor.from_dict(entry)
            if cursor.marks:
                return cursor
        fresh = Cursor()
        fresh.start(now or utc_now())
        return fresh

    # --------------------------------------------------------------- write

    def save(self, tenant_id: str, cursor: Cursor) -> None:
        """Write the cursor atomically; a failure is logged, never raised."""
        path = self.path
        tenants = self._read()
        tenants[str(tenant_id)] = cursor.to_dict()
        payload = {"version": VERSION, "tenants": tenants}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            handle, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, separators=(",", ":"))
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(temp_name, path)
            except BaseException:
                try:
                    os.unlink(temp_name)
                except OSError:
                    pass
                raise
        except OSError as exc:
            logger.warning("could not save the desktop notification cursor (%s)", type(exc).__name__)
