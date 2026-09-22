"""Domain models for document artifacts."""

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class DocumentFormat(str, Enum):
    PDF = "PDF"
    DOCX = "DOCX"


class DocumentStatus(str, Enum):
    ACTIVE = "ACTIVE"
    INVALIDATED = "INVALIDATED"


class DocumentValidation(str, Enum):
    PASSED = "PASSED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    FAILED = "FAILED"


MAGIC = {DocumentFormat.PDF: b"%PDF-", DocumentFormat.DOCX: b"PK\x03\x04"}
EXTENSION = {DocumentFormat.PDF: "pdf", DocumentFormat.DOCX: "docx"}


class ContactLink(BaseModel):
    label: str
    url: str


class Contact(BaseModel):
    """Identity header copied from the candidate profile (never generated)."""

    name: str = ""
    email: Optional[str] = None
    phone: Optional[str] = None
    location: Optional[str] = None
    links: list[ContactLink] = Field(default_factory=list)


class Section(BaseModel):
    key: str
    title: str
    #: (text, bulleted) in preparation order.
    items: list[tuple[str, bool]] = Field(default_factory=list)


class DocumentModel(BaseModel):
    """What a renderer lays out: the preparation's content, nothing more.

    ``paragraphs`` is used by cover letters (ordered prose), ``sections`` by
    resumes. ``subject`` / ``date_line`` are job facts or configuration, never
    invented.
    """

    artifact_type: str
    contact: Contact
    title: Optional[str] = None
    subject: Optional[str] = None
    date_line: Optional[str] = None
    sections: list[Section] = Field(default_factory=list)
    paragraphs: list[str] = Field(default_factory=list)
    signature: Optional[str] = None

    def expected_lines(self) -> list[str]:
        """Every line of candidate content the rendered file must contain."""
        lines: list[str] = []
        if self.contact.name:
            lines.append(self.contact.name)
        for value in (self.contact.email, self.contact.phone, self.contact.location):
            if value:
                lines.append(value)
        for link in self.contact.links:
            lines.append(link.url)
        for value in (self.subject, self.date_line):
            if value:
                lines.append(value)
        for section in self.sections:
            lines.append(section.title)
            lines.extend(text for text, _ in section.items)
        lines.extend(self.paragraphs)
        if self.signature:
            lines.append(self.signature)
        return [line for line in lines if line and line.strip()]


class RenderInput(BaseModel):
    tenant_id: str
    preparation_id: str
    preparation_version: int
    preparation_fingerprint: str
    evidence_fingerprint: Optional[str] = None
    artifact_type: str
    format: DocumentFormat
    model: DocumentModel
    #: Layout options that participate in the artifact identity.
    options: dict[str, Any] = Field(default_factory=dict)


class RenderResult(BaseModel):
    data: bytes
    page_count: int = 0
    warnings: list[str] = Field(default_factory=list)


class ValidationOutcome(BaseModel):
    status: DocumentValidation
    page_count: int = 0
    issues: list[str] = Field(default_factory=list)
    missing_lines: list[str] = Field(default_factory=list)
    extracted_chars: int = 0


class DocumentArtifact(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    tenant_id: str
    candidate_opportunity_id: str
    preparation_id: str
    preparation_version: int
    artifact_type: str
    format: DocumentFormat
    version: int
    status: DocumentStatus
    validation_status: DocumentValidation
    validation_report: dict[str, Any] = Field(default_factory=dict)
    content_hash: str
    input_fingerprint: str
    evidence_fingerprint: Optional[str] = None
    renderer: str
    renderer_version: str
    byte_size: int
    page_count: int = 0
    relative_path: str
    created_at: Optional[datetime] = None
    invalidated_at: Optional[datetime] = None
    invalidated_reason: Optional[str] = None


class RenderRequest(BaseModel):
    preparation_id: str
    artifact_type: str = Field(default="RESUME", pattern="^(RESUME|COVER_LETTER)$")
    format: DocumentFormat = DocumentFormat.PDF
    force: bool = False


class RenderReport(BaseModel):
    """Result of ensuring a preparation's documents exist."""

    preparation_id: str
    artifacts: list[DocumentArtifact] = Field(default_factory=list)
    created: int = 0
    reused: int = 0
    problems: list[str] = Field(default_factory=list)
