"""Document text extraction (Blueprint §6).

v1: PyMuPDF for PDFs (text-based). Scanned/textless PDFs are DETECTED and
flagged ``needs_ocr`` — the OCRmyPDF/Tesseract stage (§18) plugs in behind
the same interface later; v1 never silently returns empty text as success.

DOCX support arrives with python-docx in a later pass; unknown types are
reported, not guessed.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ExtractedText:
    text: str
    pages: int = 0
    needs_ocr: bool = False
    kind: str = "pdf"                    # pdf | unknown
    warnings: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return bool(self.text.strip()) and not self.needs_ocr


# A text-based tender PDF yields far more than this per page; below it we
# assume a scanned/image PDF and route to OCR instead of pretending.
MIN_CHARS_PER_PAGE = 25


def extract_text(data: bytes, *, filename: str = "") -> ExtractedText:
    name = filename.lower()
    if data[:5] == b"%PDF-" or name.endswith(".pdf"):
        return _extract_pdf(data)
    return ExtractedText(text="", kind="unknown",
                         warnings=[f"unsupported document type: {filename or 'unknown'}"])


def _extract_pdf(data: bytes) -> ExtractedText:
    import pymupdf  # deferred: keeps import cheap for non-document code paths

    warnings: list[str] = []
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:  # noqa: BLE001 — corrupt files are data, not bugs
        return ExtractedText(text="", warnings=[f"unreadable PDF: {exc}"])

    with doc:
        pages = doc.page_count
        parts = [page.get_text("text") for page in doc]

    text = "\n".join(parts)
    needs_ocr = pages > 0 and len(text.strip()) < MIN_CHARS_PER_PAGE * pages
    if needs_ocr:
        warnings.append(
            f"textless/scanned PDF ({len(text.strip())} chars over {pages} "
            "pages) — route to OCR stage"
        )
    return ExtractedText(text=text, pages=pages, needs_ocr=needs_ocr,
                         warnings=warnings)
