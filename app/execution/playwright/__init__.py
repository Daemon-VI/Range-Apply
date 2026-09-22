"""Local Playwright executor (Blueprint Phase 7).

Runs a browser on the candidate's own machine and drives public application
forms through the Phase 6 :class:`Executor` contract. Nothing here evades
employer protections: a CAPTCHA, login, MFA or unsupported widget stops the
automation and hands the application to the person.

``playwright`` is an optional dependency (the ``browser`` extra); importing
this package never requires it. :func:`is_available` says whether the
runtime and a browser are installed.
"""

from app.execution.playwright.browser import is_available

__all__ = ["is_available"]
