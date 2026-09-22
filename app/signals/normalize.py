"""Deterministic normalization of supplied messages and page observations.

Nothing here talks to a mailbox. It turns whatever a connector supplies into
the minimum the inbox needs: a normalized text (HTML stripped, quoted replies
and signatures cut, whitespace collapsed), a bounded excerpt with
credential-shaped fragments redacted, a content hash, the sender's domain,
and the identifiers a deterministic attribution can use (reference numbers,
URLs, application ids).
"""

import hashlib
import html as html_lib
import re
from datetime import datetime
from typing import Any, Optional

from app.core.timeutils import db_now, ensure_aware, parse_timestamp, to_db
from app.execution.models import sanitize_diagnostics
from app.signals.models import AttributionHints, EmailMessage, SignalIngest, SignalSource

#: Stored excerpt bound (characters of normalized text). Enough for every
#: classification rule; never a whole mailbox thread.
DEFAULT_EXCERPT_CHARS = 2000

_TAG_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_BR_RE = re.compile(r"<\s*(br|/p|/div|/li|/tr|/h[1-6])\s*/?>", re.IGNORECASE)
_HTML_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_BLANK_RE = re.compile(r"\n\s*\n+")
_QUOTE_HEADER_RE = re.compile(r"^(on .{5,120} wrote:|-{2,}\s*original message\s*-{2,}|from:\s.+|_{5,}|-----\s*forwarded message\s*-----)\s*$", re.IGNORECASE)
_SIGNATURE_RE = re.compile(r"^(--\s*|__+\s*|best regards,?|kind regards,?|regards,?|sincerely,?|thanks,?|thank you,?|cheers,?)\s*$", re.IGNORECASE)
_CREDENTIAL_RE = re.compile(
    r"(?i)(bearer\s+[a-z0-9\-_.]{16,}|api[_-]?key\s*[:=]\s*\S{8,}|(password|passwd|passcode|otp|one[- ]time code|verification code|security code)\s*(is|:|=)\s*\S+|sk-[a-z0-9]{20,}|AIza[0-9A-Za-z\-_]{30,}|(cookie|set-cookie|authorization)\s*:\s*[^\n]{1,300})"
)
_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.IGNORECASE)
_REFERENCE_RE = re.compile(
    r"(?:application|reference|confirmation|tracking|candidate|requisition|req\.?|job)\s*(?:id|number|no\.?|#|code|ref\.?)\s*[:#]?\s*([A-Za-z0-9][A-Za-z0-9\-_/]{3,39})",
    re.IGNORECASE,
)
_EMAIL_RE = re.compile(r"[\w.+-]+@([\w-]+(?:\.[\w-]+)+)")
_ID_RE = re.compile(r"\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b", re.IGNORECASE)

#: Mailer domains that belong to an ATS, not to the employer: never treated as company evidence.
ATS_MAIL_DOMAINS = frozenset(
    {
        "greenhouse.io", "greenhouse-mail.io", "lever.co", "hire.lever.co", "ashbyhq.com", "myworkday.com", "myworkdayjobs.com",
        "smartrecruiters.com", "icims.com", "jobvite.com", "workable.com", "workablemail.com", "bamboohr.com", "successfactors.com",
        "taleo.net", "breezy.hr", "recruitee.com", "teamtailor.com", "applytojob.com", "jazz.co", "linkedin.com", "indeed.com",
        "glassdoor.com", "naukri.com", "hirist.com", "instahyre.com", "wellfound.com", "angel.co",
    }
)
#: Senders that are the platform itself (job alerts, marketing), never an employer.
MARKETING_SENDER_RE = re.compile(r"(?i)^(jobalerts?|alerts?|noreply-?alerts|newsletter|news|marketing|jobs-noreply|recommendations?)@")


def strip_html(html: str) -> str:
    """HTML → text: scripts/styles dropped, block tags → newlines, entities decoded."""
    text = _TAG_RE.sub(" ", html or "")
    text = _BR_RE.sub("\n", text)
    text = _HTML_RE.sub(" ", text)
    return html_lib.unescape(text)


def normalize_text(text: Optional[str]) -> str:
    """Collapse whitespace, cut the quoted reply and the signature, keep the body."""
    if not text:
        return ""
    lines = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = _WS_RE.sub(" ", raw).strip()
        if _QUOTE_HEADER_RE.match(line):
            break
        if line.startswith(">"):
            continue
        if lines and _SIGNATURE_RE.match(line):
            break
        lines.append(line)
    text = "\n".join(lines)
    text = _BLANK_RE.sub("\n", text).strip()
    return text


def redact_credentials(text: str) -> str:
    return _CREDENTIAL_RE.sub("[redacted]", text or "")


def excerpt_of(text: str, limit: int = DEFAULT_EXCERPT_CHARS) -> str:
    text = redact_credentials(text or "")
    return text[:limit]


def content_hash(source: str, sender_domain: Optional[str], subject: Optional[str], text: str) -> str:
    """Stable hash of the *normalized* content: two deliveries of one message agree,
    two different messages with similar wording do not (the full text is hashed)."""
    material = "\n".join([source or "", (sender_domain or "").lower(), (subject or "").strip().lower(), text or ""])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def sender_domain(sender: Optional[str]) -> Optional[str]:
    if not sender:
        return None
    match = _EMAIL_RE.search(sender)
    return match.group(1).lower() if match else None


def sender_address(sender: Optional[str]) -> Optional[str]:
    if not sender:
        return None
    match = re.search(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", sender)
    return match.group(0).lower() if match else sender.strip()[:256] or None


def employer_domain(domain: Optional[str]) -> Optional[str]:
    """The registrable part of a sender domain unless it is an ATS mailer."""
    if not domain:
        return None
    domain = domain.lower()
    if domain in ATS_MAIL_DOMAINS or any(domain.endswith("." + d) for d in ATS_MAIL_DOMAINS):
        return None
    parts = domain.split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "net", "ac", "gov") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else domain


def extract_references(text: str) -> list[str]:
    seen: list[str] = []
    for match in _REFERENCE_RE.finditer(text or ""):
        value = match.group(1).strip().rstrip(".,;:)")
        if value.lower() in ("is", "of", "for", "the", "your", "number", "code") or value.isalpha() and len(value) < 5:
            continue
        if value not in seen:
            seen.append(value)
    return seen[:10]


def extract_urls(text: str) -> list[str]:
    seen: list[str] = []
    for match in _URL_RE.finditer(text or ""):
        url = match.group(0).rstrip(".,;:)")
        if url not in seen:
            seen.append(url)
    return seen[:20]


def extract_uuids(text: str) -> list[str]:
    return list(dict.fromkeys(m.group(1).lower() for m in _ID_RE.finditer(text or "")))[:10]


def is_marketing_sender(sender: Optional[str]) -> bool:
    address = sender_address(sender) or ""
    return bool(MARKETING_SENDER_RE.match(address))


def _clean(data: dict[str, Any]) -> dict[str, Any]:
    return sanitize_diagnostics(data or {})


def email_to_ingest(message: EmailMessage) -> SignalIngest:
    """A supplied email → one ingestion request. Only the headers attribution
    needs are read; the message id is the stable external identity."""
    text = normalize_text(message.text) if message.text else normalize_text(strip_html(message.html or ""))
    headers = {k.lower(): v for k, v in (message.headers or {}).items()}
    hints = message.hints.model_copy()
    reply_to = headers.get("in-reply-to") or (headers.get("references") or "").split()[-1:] or None
    if isinstance(reply_to, list):
        reply_to = reply_to[0] if reply_to else None
    if reply_to and not hints.in_reply_to:
        hints.in_reply_to = reply_to.strip()[:512]
    received = message.received_at or parse_timestamp(headers.get("date"))
    payload: dict[str, Any] = {"kind": "email", "recipients": [sender_address(r) for r in message.recipients][:5]}
    if headers.get("list-unsubscribe"):
        payload["list_unsubscribe"] = True
    return SignalIngest(
        source=SignalSource.EMAIL,
        source_reference=(message.message_id or "").strip()[:256] or None,
        subject=message.subject,
        text=text,
        sender=message.sender,
        recipients=list(message.recipients),
        external_at=received,
        observed_at=None,
        payload=payload,
        provenance={"connector": "email", "has_html": bool(message.html), "has_text": bool(message.text)},
        hints=hints,
        process=message.process,
    )


class NormalizedSignal:
    """The fields the ingestion service stores, computed once."""

    __slots__ = ("source", "source_reference", "subject", "text", "excerpt", "sender", "sender_domain", "content_hash", "dedupe_key", "payload", "provenance", "external_at", "observed_at", "hints", "references", "urls", "uuids", "employer_domain", "marketing")

    def __init__(self, request: SignalIngest, excerpt_chars: int = DEFAULT_EXCERPT_CHARS, now: Optional[datetime] = None):
        self.source = request.source
        self.source_reference = (request.source_reference or "").strip()[:256] or None
        self.subject = _WS_RE.sub(" ", (request.subject or "")).strip()[:512] or None
        self.text = normalize_text(request.text)
        self.excerpt = excerpt_of(self.text, excerpt_chars) or None
        self.sender = sender_address(request.sender)
        self.sender_domain = sender_domain(request.sender)
        self.employer_domain = employer_domain(self.sender_domain)
        self.marketing = is_marketing_sender(request.sender)
        self.content_hash = content_hash(request.source.value, self.sender_domain, self.subject, self.text or repr(sorted((request.payload or {}).items()))[:4000])
        self.dedupe_key = f"{request.source.value}:ref:{self.source_reference}" if self.source_reference else f"{request.source.value}:hash:{self.content_hash}"
        self.payload = _clean(request.payload)
        self.provenance = _clean(request.provenance)
        self.external_at = to_db(ensure_aware(request.external_at)) if request.external_at else None
        self.observed_at = to_db(ensure_aware(request.observed_at)) if request.observed_at else (now or db_now())
        self.hints = request.hints or AttributionHints()
        searchable = "\n".join(filter(None, [self.subject, self.text]))
        self.references = list(dict.fromkeys([r for r in ([self.hints.reference] if self.hints.reference else []) + extract_references(searchable)]))
        self.urls = list(dict.fromkeys(([self.hints.url] if self.hints.url else []) + extract_urls(searchable)))
        self.uuids = extract_uuids(searchable)

    def searchable_text(self) -> str:
        return "\n".join(filter(None, [self.subject, self.text]))


__all__ = [
    "DEFAULT_EXCERPT_CHARS",
    "NormalizedSignal",
    "content_hash",
    "email_to_ingest",
    "employer_domain",
    "excerpt_of",
    "extract_references",
    "extract_urls",
    "normalize_text",
    "redact_credentials",
    "sender_domain",
    "strip_html",
]
