"""Tests for hh_monitor.tg.file_parsers (AC5, AC6)."""

from __future__ import annotations

from pathlib import Path

import pytest

from hh_monitor.tg.file_parsers import (
    MAX_FILE_SIZE_BYTES,
    MIME_DOCX,
    MIME_PDF,
    MIME_TXT,
    FileTooLarge,
    UnsupportedFileType,
    extract_text,
    extract_text_from_txt,
)

_FILES = Path(__file__).parent / "fixtures" / "files"


def _read(name: str) -> bytes:
    return (_FILES / name).read_bytes()


@pytest.mark.asyncio
async def test_pdf_happy_path() -> None:
    text = await extract_text(MIME_PDF, _read("sample.pdf"))
    assert "Portrait PDF sample text" in text
    assert text.strip()


@pytest.mark.asyncio
async def test_docx_happy_path() -> None:
    text = await extract_text(MIME_DOCX, _read("sample.docx"))
    assert "Портрет кандидата DOCX" in text
    assert "Андеррайтер" in text


@pytest.mark.asyncio
async def test_txt_happy_path_utf8() -> None:
    text = await extract_text(MIME_TXT, _read("sample.txt"))
    assert "Senior Backend Python" in text


@pytest.mark.asyncio
async def test_txt_cp1251_fallback() -> None:
    raw = _read("sample_cp1251.txt")
    # sanity: these bytes are NOT valid utf-8
    with pytest.raises(UnicodeDecodeError):
        raw.decode("utf-8")
    text = await extract_text_from_txt(raw)
    assert "менеджер по продажам" in text


@pytest.mark.asyncio
async def test_unsupported_mime_raises() -> None:
    with pytest.raises(UnsupportedFileType):
        await extract_text("image/jpeg", b"\xff\xd8\xff\xe0jpegdata")


@pytest.mark.asyncio
async def test_file_too_large_raises() -> None:
    big = b"x" * (MAX_FILE_SIZE_BYTES + 1)
    with pytest.raises(FileTooLarge):
        await extract_text(MIME_TXT, big)


@pytest.mark.asyncio
async def test_size_check_runs_before_mime_dispatch() -> None:
    """Oversized file is rejected even if MIME is also unsupported (size first)."""
    big = b"x" * (MAX_FILE_SIZE_BYTES + 1)
    with pytest.raises(FileTooLarge):
        await extract_text("image/png", big)
