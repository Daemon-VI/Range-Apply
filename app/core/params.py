"""Lenient number parsing for HTML form / query parameters.

A browser submits an empty input as ``""``; declaring the parameter as ``int``
makes FastAPI answer 422 and the whole page fails. Dashboards take the raw
string and parse it here: empty or invalid means "use the default", and the
result is clamped to a safe range.
"""

from typing import Optional


def to_int(value: Optional[str], default: Optional[int], lo: Optional[int] = None, hi: Optional[int] = None) -> Optional[int]:
    text = (value if value is not None else "").strip()
    if not text:
        return default
    try:
        number = int(float(text))
    except ValueError:
        return default
    if lo is not None:
        number = max(lo, number)
    if hi is not None:
        number = min(hi, number)
    return number
