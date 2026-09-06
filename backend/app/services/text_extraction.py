# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Full-text extraction for user-uploaded artifacts.

The RAG chunker (`app.rag.chunking`) splits a document into many small,
embeddable chunks. For the Generate-or-Upload feature we instead need the
*whole* document as a single markdown-ish string to store in the artifact
row's `content` column — that's what every downstream consumer reads.

This helper reuses the chunker's per-format parsing (python-docx / pypdf +
OCR fallback / plain text) and concatenates the chunks back in document
order, re-introducing section headings where the parser captured them.
"""
from pathlib import Path

# Subset of rag.chunking.SUPPORTED_EXTENSIONS that makes sense as a
# human-authored document (excludes .xlsx/.xml/.xsd which are structured data).
ALLOWED_UPLOAD_EXTENSIONS = {".docx", ".pdf", ".md", ".txt"}


def extract_full_text(path: Path) -> str:
    """Return the full text of a .docx/.pdf/.md/.txt file as one string.

    Returns "" when nothing extractable was found (empty file, scanned PDF
    with no OCR text, etc.) — the caller decides how to surface that.
    """
    from app.rag.chunking import chunk_file

    chunks = chunk_file(path)
    parts: list[str] = []
    last_title = None
    for c in chunks:
        title = c.get("section_title")
        if title and title != last_title:
            parts.append(f"## {title}")
            last_title = title
        text = (c.get("content") or "").strip()
        if text:
            parts.append(text)
    return "\n\n".join(parts).strip()
