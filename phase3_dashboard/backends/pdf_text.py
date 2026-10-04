"""Reads an uploaded PDF in memory, the same way phase1_ingestion/parse_pdfs.py does.

Nothing is written to disk. Limits on size, page count and encryption keep a
bad or hostile file from tying up the app.
"""

from __future__ import annotations

from ..core.security import MAX_PDF_PAGES, UploadRejected


def parse_pdf_pages(source_name: str, data: bytes) -> list[dict]:
    """Return [{"source", "page", "text"}] with one-based page numbers."""
    try:
        import pymupdf
    except ImportError as exc:
        raise UploadRejected("PDF support needs PyMuPDF. Install it with: pip install pymupdf") from exc

    try:
        document = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:  # noqa: BLE001 - PyMuPDF raises several types for bad files
        raise UploadRejected("This file couldn't be opened as a PDF.") from exc

    with document:
        if document.needs_pass:
            raise UploadRejected("Password-protected PDFs can't be analyzed.")
        if document.page_count == 0:
            raise UploadRejected("The PDF has no pages.")
        if document.page_count > MAX_PDF_PAGES:
            raise UploadRejected(f"The PDF has more than {MAX_PDF_PAGES} pages. Split it and upload the parts.")

        pages = []
        for number, page in enumerate(document, start=1):
            text = page.get_text("text", sort=True).strip()
            if not text:
                raise UploadRejected(
                    f"Page {number} has no selectable text. It may be a scanned image that needs OCR first."
                )
            pages.append({"source": source_name, "page": number, "text": text})
    return pages
