"""Extract plain text from uploaded portrait files (PDF / DOCX / TXT).

Used by the "Add Vacancy" FSM wizard (S3) when an admin uploads a file instead
of typing the portrait.  All parsers are async-friendly wrappers (the underlying
libraries are sync; we keep the signatures async so the FSM handler can await
uniformly and we can offload to a thread later if needed).

Size guard: 5 MB hard cap, enforced in :func:`extract_text` before dispatch.
"""

from __future__ import annotations

import io

import structlog

logger = structlog.get_logger(__name__)

MAX_FILE_SIZE_BYTES = 5 * 1024 * 1024  # 5 MB

MIME_PDF = "application/pdf"
MIME_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
MIME_TXT = "text/plain"


class UnsupportedFileType(Exception):
    """Raised when the uploaded file's MIME type is not PDF/DOCX/TXT."""


class FileTooLarge(Exception):
    """Raised when the uploaded file exceeds MAX_FILE_SIZE_BYTES."""


async def extract_text_from_pdf(file_bytes: bytes) -> str:
    """Concatenate text from every page of a PDF."""
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(file_bytes))
    parts = [page.extract_text() or "" for page in reader.pages]
    return "\n".join(parts).strip()


async def extract_text_from_docx(file_bytes: bytes) -> str:
    """Concatenate non-empty paragraphs from a DOCX document."""
    from docx import Document

    doc = Document(io.BytesIO(file_bytes))
    parts = [p.text for p in doc.paragraphs if p.text and p.text.strip()]
    return "\n".join(parts).strip()


async def extract_text_from_txt(file_bytes: bytes) -> str:
    """Decode a plain-text file, UTF-8 first with a cp1251 fallback."""
    try:
        return file_bytes.decode("utf-8").strip()
    except UnicodeDecodeError:
        logger.info("file_parsers.txt_cp1251_fallback")
        return file_bytes.decode("cp1251", errors="replace").strip()


async def extract_text(mime: str, file_bytes: bytes) -> str:
    """Dispatch to the right parser by MIME type.

    Raises:
        FileTooLarge:        if len(file_bytes) > MAX_FILE_SIZE_BYTES.
        UnsupportedFileType: if *mime* is not PDF/DOCX/TXT.
    """
    if len(file_bytes) > MAX_FILE_SIZE_BYTES:
        raise FileTooLarge(
            f"File is {len(file_bytes)} bytes, limit is {MAX_FILE_SIZE_BYTES}"
        )

    if mime == MIME_PDF:
        return await extract_text_from_pdf(file_bytes)
    if mime == MIME_DOCX:
        return await extract_text_from_docx(file_bytes)
    if mime == MIME_TXT:
        return await extract_text_from_txt(file_bytes)

    raise UnsupportedFileType(f"Unsupported MIME type: {mime!r}")
