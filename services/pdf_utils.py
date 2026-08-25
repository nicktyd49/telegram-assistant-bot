"""PDF text extraction for policy documents / PDF receipts."""
from __future__ import annotations

import io

import pdfplumber

# Below this many characters of extracted text, assume the PDF is a scanned
# image (no real text layer) rather than a genuinely short document.
MIN_TEXT_CHARS = 40


def extract_text(pdf_bytes: bytes, max_pages: int = 40) -> str:
    """Extracts text from a PDF's pages, joined with page-break markers.

    Returns an empty string if nothing could be extracted (e.g. a scanned
    document with no text layer) — callers should treat that as "couldn't
    read this PDF" rather than "this PDF is empty".
    """
    text_parts: list[str] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for i, page in enumerate(pdf.pages[:max_pages]):
            page_text = page.extract_text() or ""
            if page_text.strip():
                text_parts.append(page_text.strip())

    full_text = "\n\n".join(text_parts)
    if len(full_text.strip()) < MIN_TEXT_CHARS:
        return ""
    return full_text
