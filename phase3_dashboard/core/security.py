"""Input and output safety for the dashboard.

Two things are untrusted here: uploaded policy files (they can contain text
aimed at the AI, i.e. prompt injection) and model output (it can contain
Markdown that renders as links, images or tables). These helpers keep both in
check. The rest of the app never renders document or model text without them.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

MAX_UPLOAD_MB = 10
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
MAX_PDF_PAGES = 80
MAX_QUESTION_CHARS = 500


class UploadRejected(ValueError):
    """The uploaded file can't be used. The message is safe to show to the user."""


# ---------------------------------------------------------------- uploads

def safe_display_name(filename) -> str:
    """Drop any folder path and unusual characters from a user-supplied file name."""
    name = PurePosixPath(str(filename or "").replace("\\", "/")).name
    name = re.sub(r"[^A-Za-z0-9 ._()-]", "_", name).strip(" .")
    name = re.sub(r"_{2,}", "_", name)
    if len(name) > 120:
        stem, dot, ext = name.rpartition(".")
        name = f"{stem[:115 - len(ext)]}.{ext}" if dot else name[:120]
    return name or "uploaded.pdf"


def check_upload(filename, data: bytes) -> str:
    """Return a safe display name for the file, or raise UploadRejected."""
    name = safe_display_name(filename)
    if not name.lower().endswith(".pdf"):
        raise UploadRejected("Only PDF files can be analyzed.")
    if not data:
        raise UploadRejected("The file is empty.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise UploadRejected(f"The file is larger than {MAX_UPLOAD_MB} MB.")
    if b"%PDF-" not in data[:1024]:
        raise UploadRejected("The file doesn't look like a PDF: it has no PDF header.")
    return name


# ---------------------------------------------------------------- rendering

# Characters that start Markdown syntax anywhere in a line. Escaping [ ] and <
# is what blocks links, images, raw HTML and Streamlit directives like
# :red[...]; the rest stop tables (|), emphasis, code and LaTeX ($).
_MD_SPECIAL = re.compile(r"([\\`*_\[\]<>#|~$])")
# These only mean something at the start of a line: list items, setext
# headings and numbered lists ("- ", "+ ", "===", "1. ").
_LINE_START_MARK = re.compile(r"^(\s*)([-+=])(?=[\s=-]|$)", re.MULTILINE)
_LINE_START_NUMBER = re.compile(r"^(\s*\d+)([.)])(?=\s|$)", re.MULTILINE)


def _escape(value: str) -> str:
    value = _MD_SPECIAL.sub(r"\\\1", value)
    value = _LINE_START_MARK.sub(r"\1\\\2", value)
    return _LINE_START_NUMBER.sub(r"\1\\\2", value)


def escape_md(text) -> str:
    """Make untrusted text display literally in st.markdown, on one line."""
    if text is None:
        return ""
    return _escape(" ".join(str(text).split()))


def escape_md_block(text) -> str:
    """Like escape_md, but keeps the line breaks (for policy passages)."""
    if text is None:
        return ""
    lines = [" ".join(line.split()) for line in str(text).splitlines()]
    return "  \n".join(_escape(line) for line in lines)


# ---------------------------------------------------------------- prompt injection

# A cheap heuristic, not a guarantee. Matches are shown to the reviewer as a
# warning; they don't block anything. The agents themselves should still treat
# document text as data (for example, by wrapping it in tags in the prompt).
_INJECTION_PATTERNS = (
    (re.compile(r"\b(?:ignore|disregard|forget|override)\b.{0,40}?\b(?:previous|prior|above|earlier|all|any)\b"
                r".{0,20}?\b(?:instructions?|prompts?|rules|directions)\b", re.I | re.S),
     "tries to override instructions"),
    (re.compile(r"\b(?:system prompt|developer message|you are (?:now )?(?:an? |the )?(?:ai|assistant|language model|llm)"
                r"|note to (?:the )?(?:ai|llm|model|assistant)s?)\b", re.I),
     "addresses the AI directly"),
    (re.compile(r"\b(?:mark|classify|rate|label)\b.{0,50}?\bas\s+(?:fully\s+)?"
                r"(?:compliant|covered|full(?: coverage)?|approved|passing)\b", re.I | re.S),
     "tries to dictate the audit result"),
    (re.compile(r"\b(?:do not|don't|never)\s+(?:report|flag|mention|list)\b.{0,30}?\b(?:gaps?|findings?|issues?|risks?)\b",
                re.I | re.S),
     "tries to hide findings"),
    (re.compile(r"<\s*/?\s*(?:system|assistant|instructions?|prompt|tool)\s*>", re.I),
     "contains prompt-style tags"),
)


def scan_for_injection(text) -> list[str]:
    """Return the reasons a passage looks like it's giving instructions to an AI."""
    if not text:
        return []
    return [reason for pattern, reason in _INJECTION_PATTERNS if pattern.search(str(text))]


def scan_document(chunks, label_for) -> list[tuple[str, list[str]]]:
    """Scan every section; return (section label, reasons) for the flagged ones."""
    flagged = []
    for chunk in chunks:
        reasons = scan_for_injection(chunk.get("text"))
        if reasons:
            flagged.append((label_for(chunk), reasons))
    return flagged
