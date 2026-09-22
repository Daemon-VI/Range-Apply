"""The renderer contract.

    can_render(render_input) -> bool
    render(render_input)     -> RenderResult (bytes + page count + warnings)

A renderer receives a :class:`RenderInput` whose :class:`DocumentModel` is
the preparation's validated content; it decides fonts, spacing, wrapping,
page breaks and nothing else. Its ``name`` and ``version`` are part of the
artifact identity: a layout change is a new artifact version, never a
silent rewrite of an existing file.
"""

from typing import Protocol, runtime_checkable

from app.documents.models import DocumentFormat, RenderInput, RenderResult


class RenderError(RuntimeError):
    pass


@runtime_checkable
class Renderer(Protocol):
    name: str
    version: str
    format: DocumentFormat

    def can_render(self, render_input: RenderInput) -> bool: ...

    def render(self, render_input: RenderInput) -> RenderResult: ...


#: Typographic characters replaced when the active font lacks Unicode coverage.
ASCII_FALLBACK = {
    "—": "-",
    "–": "-",
    "…": "...",
    "’": "'",
    "‘": "'",
    "“": '"',
    "”": '"',
    "•": "-",
    " ": " ",
    "✗": "x",
    "✓": "v",
    "✱": "*",
}


def ascii_safe(text: str) -> str:
    for char, replacement in ASCII_FALLBACK.items():
        text = text.replace(char, replacement)
    return text.encode("latin-1", "replace").decode("latin-1")
