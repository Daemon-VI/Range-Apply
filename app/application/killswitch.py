"""Phase 6-lite: a global and per-source kill switch.

Deliberately minimal safety primitive: no queues, no workers, just a row the
application engine checks before every submission. A source-scoped switch
(e.g. ``"GREENHOUSE"``) pauses only that ATS; the row with id ``"global"``
pauses everything.
"""

from typing import Optional

from sqlalchemy import Boolean, Column, DateTime, String
from sqlalchemy.orm import Session

from app.core.timeutils import db_now
from app.jobs.database.models import Base

GLOBAL_ID = "global"


class KillSwitchRow(Base):
    """A pause flag, keyed by ``"global"`` or a source name."""

    __tablename__ = "kill_switches"

    id = Column(String(64), primary_key=True)
    paused = Column(Boolean, nullable=False, default=False)
    reason = Column(String(256), nullable=True)
    updated_at = Column(DateTime, default=db_now, onupdate=db_now)


def is_paused(db: Session, source: Optional[str]) -> bool:
    """True if the global switch, or the source-scoped switch, is paused."""
    ids = [GLOBAL_ID]
    if source:
        ids.append(source)
    rows = db.query(KillSwitchRow).filter(KillSwitchRow.id.in_(ids)).all()
    return any(row.paused for row in rows)


def set_paused(
    db: Session,
    paused: bool,
    reason: Optional[str] = None,
    source: Optional[str] = None,
) -> KillSwitchRow:
    """Set (get-or-create) the global or source-scoped switch."""
    switch_id = source or GLOBAL_ID
    row = db.query(KillSwitchRow).filter_by(id=switch_id).first()
    if row is None:
        row = KillSwitchRow(id=switch_id)
        db.add(row)
    row.paused = paused
    row.reason = reason
    db.commit()
    db.refresh(row)
    return row
