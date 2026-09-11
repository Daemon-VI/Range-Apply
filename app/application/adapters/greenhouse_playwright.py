"""Greenhouse adapter using Playwright for a real (non-dry-run) submission.

Module-level import of this file MUST NOT require playwright to be installed
(see the ``browser`` extra in pyproject.toml) - the dry-run path, which is the
only path exercised by tests and the safe default, never touches it.

The live (dry_run=False) path below is UNVERIFIED against a real Greenhouse
embedded application form. It fills a best-guess set of field selectors and
deliberately stops short of clicking submit or uploading real files, pending
testing with real fixtures. Do not trust it for an actual submission yet.
"""

from typing import Any, Dict

from app.application.adapters.base import ATSAdapter, SubmissionResult


class GreenhousePlaywrightAdapter(ATSAdapter):
    """Prepares and (optionally) submits an application on Greenhouse."""

    name = "greenhouse_playwright"

    def prepare(
        self,
        job_row,
        profile,
        resume_text: str,
        cover_letter_text: str,
    ) -> Dict[str, Any]:
        full_name = (getattr(profile, "name", "") or "").strip()
        parts = full_name.split(" ", 1)
        first_name = parts[0] if parts else ""
        last_name = parts[1] if len(parts) > 1 else ""
        return {
            "first_name": first_name,
            "last_name": last_name,
            "email": getattr(profile, "email", None),
            "phone": getattr(profile, "phone", None),
            "resume_text": resume_text,
            "cover_letter_text": cover_letter_text,
        }

    async def submit(
        self, prepared: Dict[str, Any], application_url: str, dry_run: bool
    ) -> SubmissionResult:
        if dry_run:
            # Exercised default: no browser dependency touched at all.
            return SubmissionResult(
                success=True, confirmation="DRY_RUN", error=None, dry_run=True
            )

        # Lazy import: only the live path needs playwright installed.
        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.goto(application_url)
                selectors = {
                    'input[name="job_application[first_name]"]': prepared.get("first_name"),
                    'input[name="job_application[last_name]"]': prepared.get("last_name"),
                    'input[name="job_application[email]"]': prepared.get("email"),
                    'textarea[name="job_application[cover_letter]"]': prepared.get(
                        "cover_letter_text"
                    ),
                }
                for selector, value in selectors.items():
                    if not value:
                        continue
                    try:
                        await page.fill(selector, str(value))
                    except Exception:
                        # Best-effort: the real form's field names are unverified.
                        continue
            finally:
                await browser.close()

        # Deliberately stops short of clicking submit / uploading files.
        return SubmissionResult(
            success=False,
            confirmation=None,
            error="Live submission not implemented - manual review required",
            dry_run=False,
        )
