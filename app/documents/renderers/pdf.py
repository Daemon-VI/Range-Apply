"""PDF renderer (fpdf2): clean single-column layout, selectable text.

Deterministic by construction: fixed metadata (no wall-clock creation
date), fixed fonts, fixed layout options. The same input, renderer version
and options give byte-identical output.
"""

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fpdf import FPDF
from fpdf.enums import XPos, YPos

from app.config import settings
from app.documents.models import DocumentFormat, DocumentModel, RenderInput, RenderResult
from app.documents.renderers.base import ASCII_FALLBACK, Renderer, RenderError, ascii_safe

logger = logging.getLogger(__name__)

_FIXED_DATE = datetime(2000, 1, 1, tzinfo=timezone.utc)
_FONT_CANDIDATES = (
    r"C:\Windows\Fonts\arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/Library/Fonts/Arial.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
)


def find_font(explicit: Optional[str] = None) -> Optional[Path]:
    for candidate in ([explicit] if explicit else []) + list(_FONT_CANDIDATES):
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    return None


class _Document(FPDF):
    def __init__(self, unicode_font: Optional[Path]):
        super().__init__(orientation="P", unit="mm", format="A4")
        self.unicode = unicode_font is not None
        self.family = "Helvetica"
        if unicode_font is not None:
            self.add_font("body", "", str(unicode_font))
            self.add_font("body", "B", str(unicode_font))
            self.family = "body"
        self.set_auto_page_break(auto=True, margin=16)
        self.set_margins(18, 16, 18)
        self.paginate = False

    def t(self, text: str) -> str:
        return text if self.unicode else ascii_safe(text)

    def footer(self) -> None:
        if not self.paginate:
            return
        self.set_y(-12)
        self.set_font(self.family, "", 8)
        self.set_text_color(120, 120, 120)
        self.cell(0, 6, self.t(f"Page {self.page_no()} of {{nb}}"), align="C")
        self.set_text_color(0, 0, 0)


def needs_unicode_font(model: DocumentModel) -> bool:
    """True when the content has characters the core Latin-1 font cannot show.

    Typographic punctuation (dashes, quotes, ellipsis) is substituted, not
    counted; anything else outside Latin-1 (accents beyond it, scripts) needs
    the embedded TrueType font so no glyph is lost.
    """
    for line in model.expected_lines():
        try:
            ascii_safe(line).encode("latin-1")
        except UnicodeEncodeError:
            return True
        if any(ord(ch) > 255 and ch not in ASCII_FALLBACK for ch in line):
            return True
    return False


class PdfRenderer(Renderer):
    name = "fpdf2"
    version = "pdf-v1"
    format = DocumentFormat.PDF

    def __init__(self, font_path: Optional[str] = None, always_embed: bool = False):
        self.font_path = find_font(font_path or settings.documents_font_path)
        #: Embedding a TrueType font costs ~1-2 s per document (subsetting), so
        #: it is used only when the content needs it, or when asked.
        self.always_embed = always_embed or bool(settings.documents_font_path)

    def can_render(self, render_input: RenderInput) -> bool:
        return render_input.format is DocumentFormat.PDF and bool(render_input.model.expected_lines())

    def font_for(self, model: DocumentModel) -> Optional[Path]:
        if self.font_path is not None and (self.always_embed or needs_unicode_font(model)):
            return self.font_path
        return None

    def render(self, render_input: RenderInput) -> RenderResult:
        model = render_input.model
        if not model.expected_lines():
            raise RenderError("nothing to render: the preparation has no content")
        font = self.font_for(model)
        if font is None and needs_unicode_font(model):
            raise RenderError("content needs a Unicode font and none is available (set DOCUMENTS_FONT_PATH)")
        pdf = _Document(font)
        pdf.set_creation_date(_FIXED_DATE)
        pdf.set_title(pdf.t(f"{model.contact.name} - {'Resume' if model.artifact_type == 'RESUME' else 'Cover letter'}".strip(" -")))
        pdf.set_author(pdf.t(model.contact.name))
        pdf.set_producer("CareerOS document renderer pdf-v1")
        pdf.set_creator("CareerOS")
        pdf.alias_nb_pages()
        pdf.add_page()
        self._header(pdf, model)
        if model.artifact_type == "RESUME":
            self._resume(pdf, model)
        else:
            self._letter(pdf, model)
        pages = pdf.pages_count
        if pages > 1:
            # Re-render with page numbers now that we know there are several pages.
            pdf = _Document(font)
            pdf.paginate = True
            pdf.set_creation_date(_FIXED_DATE)
            pdf.set_title(pdf.t(f"{model.contact.name} - {'Resume' if model.artifact_type == 'RESUME' else 'Cover letter'}".strip(" -")))
            pdf.set_author(pdf.t(model.contact.name))
            pdf.set_producer("CareerOS document renderer pdf-v1")
            pdf.set_creator("CareerOS")
            pdf.alias_nb_pages()
            pdf.add_page()
            self._header(pdf, model)
            if model.artifact_type == "RESUME":
                self._resume(pdf, model)
            else:
                self._letter(pdf, model)
            pages = pdf.pages_count
        data = bytes(pdf.output())
        return RenderResult(data=data, page_count=pages)

    # ---------------------------------------------------------------- pieces

    def _header(self, pdf: _Document, model: DocumentModel) -> None:
        contact = model.contact
        if contact.name:
            pdf.set_font(pdf.family, "B", 17)
            pdf.cell(0, 9, pdf.t(contact.name), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_font(pdf.family, "", 9.5)
        bits = [b for b in (contact.email, contact.phone, contact.location) if b]
        if bits:
            pdf.cell(0, 5.5, pdf.t("  |  ".join(bits)), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        if contact.links:
            pdf.set_text_color(20, 60, 140)
            for index, link in enumerate(contact.links):
                label = pdf.t(link.url)
                width = pdf.get_string_width(label) + 1
                pdf.cell(width, 5.5, label, link=link.url)
                if index < len(contact.links) - 1:
                    pdf.cell(pdf.get_string_width("  |  "), 5.5, pdf.t("  |  "))
            pdf.ln(5.5)
            pdf.set_text_color(0, 0, 0)
        pdf.ln(2)
        pdf.set_draw_color(160, 160, 160)
        pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
        pdf.ln(3)

    def _resume(self, pdf: _Document, model: DocumentModel) -> None:
        for section in model.sections:
            pdf.set_font(pdf.family, "B", 11)
            pdf.cell(0, 7, pdf.t(section.title.upper()), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.set_font(pdf.family, "", 10)
            for text, bulleted in section.items:
                if bulleted:
                    x = pdf.get_x()
                    pdf.cell(5, 5.4, pdf.t("\u2022") if pdf.unicode else "-")
                    pdf.multi_cell(0, 5.4, pdf.t(text), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                    pdf.set_x(x)
                else:
                    pdf.multi_cell(0, 5.4, pdf.t(text), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.ln(2.5)

    def _letter(self, pdf: _Document, model: DocumentModel) -> None:
        pdf.set_font(pdf.family, "", 10.5)
        if model.date_line:
            pdf.cell(0, 6, pdf.t(model.date_line), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.ln(2)
        if model.subject:
            pdf.set_font(pdf.family, "B", 10.5)
            pdf.multi_cell(0, 6, pdf.t(model.subject), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.set_font(pdf.family, "", 10.5)
            pdf.ln(2)
        for paragraph in model.paragraphs:
            pdf.multi_cell(0, 5.8, pdf.t(paragraph), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.ln(3)
        if model.signature:
            pdf.multi_cell(0, 5.8, pdf.t(model.signature), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
