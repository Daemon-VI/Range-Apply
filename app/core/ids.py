"""Identifier generation.

Every table in CareerOS uses a string UUID4 primary key (``String(36)``) so
the schema is identical on SQLite and PostgreSQL and rows can be created
client-side (extension, local runner) without a round trip. New modules
import :func:`new_id` instead of re-declaring a private ``_uuid`` helper.
"""

import uuid


def new_id() -> str:
    """A fresh UUID4 as the canonical 36-character string form."""
    return str(uuid.uuid4())
