"""CV text extraction (§11.1 step 3).

PDF and DOCX in, plain text plus a page count out. Lives in the adapter layer
because it depends on `pdfplumber` and `python-docx`; the domain scores what
comes out of here without knowing where it came from.

The extraction is deliberately layout-aware in one respect only: it keeps line
breaks, because the parse-quality scorer reads column bleed out of line shape,
and evidence spans are more useful when they land on a single bullet.
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass

import pdfplumber
import structlog
from docx import Document as open_docx
from docx.document import Document as DocxDocument
from docx.table import Table
from docx.text.paragraph import Paragraph

log = structlog.get_logger(__name__)

PDF_MIME = "application/pdf"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
TEXT_MIME = "text/plain"
SUPPORTED_MIMES = frozenset({PDF_MIME, DOCX_MIME, TEXT_MIME})

MAX_BYTES = 10 * 1024 * 1024  # §11.1 step 2


class UnsupportedDocumentError(ValueError):
    """The upload is not a CV format we extract."""


class DocumentTooLargeError(ValueError):
    pass


class ExtractionFailedError(RuntimeError):
    """The file claimed a format it does not hold — a corrupt or mislabelled upload."""


@dataclass(frozen=True, slots=True)
class ExtractedDocument:
    text: str
    page_count: int
    mime: str
    sha256: str
    byte_size: int


def extract(data: bytes, mime: str, *, filename: str | None = None) -> ExtractedDocument:
    """Extract text from an uploaded CV.

    The MIME type is taken from the upload but not trusted blindly: a PDF that
    does not start with `%PDF` is rejected here rather than producing empty text
    that the quality scorer would then blame on the candidate's layout.
    """
    if len(data) > MAX_BYTES:
        raise DocumentTooLargeError(f"{len(data)} bytes exceeds the {MAX_BYTES} byte limit")
    if mime not in SUPPORTED_MIMES:
        raise UnsupportedDocumentError(f"unsupported document type: {mime}")

    digest = hashlib.sha256(data).hexdigest()

    if mime == PDF_MIME:
        text, pages = _extract_pdf(data)
    elif mime == DOCX_MIME:
        text, pages = _extract_docx(data)
    else:
        text = data.decode("utf-8", errors="replace")
        pages = max(1, text.count("\f") + 1)

    return ExtractedDocument(
        text=text.strip(),
        page_count=pages,
        mime=mime,
        sha256=digest,
        byte_size=len(data),
    )


def _extract_pdf(data: bytes) -> tuple[str, int]:
    if not data.startswith(b"%PDF"):
        raise ExtractionFailedError("file is not a PDF despite its content type")
    pages: list[str] = []
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for page in pdf.pages:
                # `layout=False` keeps reading order rather than reconstructing
                # visual position; column bleed then shows up as interleaved
                # lines, which is exactly the signal the quality scorer needs.
                pages.append(page.extract_text() or "")
    except ExtractionFailedError:
        raise
    except Exception as exc:  # pdfplumber raises a wide variety on damaged files
        raise ExtractionFailedError(f"could not read PDF: {type(exc).__name__}: {exc}") from exc
    return "\n\n".join(pages), max(len(pages), 1)


def _extract_docx(data: bytes) -> tuple[str, int]:
    if not data.startswith(b"PK"):  # DOCX is a zip container
        raise ExtractionFailedError("file is not a DOCX despite its content type")
    try:
        document = open_docx(io.BytesIO(data))
    except Exception as exc:
        raise ExtractionFailedError(f"could not read DOCX: {type(exc).__name__}: {exc}") from exc

    parts: list[str] = []
    for block in _iter_block_items(document):
        if isinstance(block, Paragraph):
            if block.text.strip():
                parts.append(block.text)
        else:
            for row in block.rows:
                cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
                if cells:
                    # Tab-separated so the quality scorer can see the table.
                    parts.append("\t".join(cells))

    text = "\n".join(parts)
    # DOCX carries no page count; estimate from content for the yield metric.
    estimated_pages = max(1, round(len(text) / 3000))
    return text, estimated_pages


def _iter_block_items(document: DocxDocument) -> list[Paragraph | Table]:
    """Paragraphs and tables in document order.

    python-docx exposes them as separate collections, which loses ordering — and
    ordering is what makes a CV's experience section readable.
    """
    from docx.oxml.table import CT_Tbl
    from docx.oxml.text.paragraph import CT_P

    body = document.element.body
    items: list[Paragraph | Table] = []
    for child in body.iterchildren():
        if isinstance(child, CT_P):
            items.append(Paragraph(child, document))
        elif isinstance(child, CT_Tbl):
            items.append(Table(child, document))
    return items
