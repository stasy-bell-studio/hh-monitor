"""Tests for the pipeline run command — --no-parse mode and output format.

Calls the internal ``_pipeline_run`` coroutine directly (avoiding asyncio.run
nesting) and monkeypatches ``async_session_factory`` to inject the test DB
session so all DB changes roll back automatically.
"""

import hashlib
import json
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from hh_monitor.cli import _pipeline_run
from hh_monitor.db.models import Resume, Search, Snapshot

# ── helpers ───────────────────────────────────────────────────────────────────

_PORTRAIT: dict[str, Any] = {
    "position_code": "test_pos",
    "position_name": "Test Position",
    "title_keywords": ["менеджер"],
    "experience_keywords": [],
    "min_total_months": 0,
    "preferred_total_months": 24,
    "preferred_areas": [],
}


def _hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


async def _seed(session: AsyncSession) -> tuple[int, str]:
    """Insert one Search + one Resume + one Snapshot. Returns (search_id, resume_id)."""
    s = Search(
        position_code="test_pos",
        position_name="Test Position",
        hh_params={"text": "менеджер"},
        portrait=_PORTRAIT,
    )
    session.add(s)
    await session.flush()
    search_id: int = s.id

    resume_id = "abcdef1234567890"
    session.add(Resume(hh_resume_id=resume_id))
    await session.flush()

    payload = {"id": resume_id, "title": "Менеджер по продажам", "total_experience": {"months": 36}}
    session.add(Snapshot(hh_resume_id=resume_id, payload=payload, content_hash=_hash(payload)))
    await session.flush()

    return search_id, resume_id


def _make_session_factory(session: AsyncSession):  # type: ignore[no-untyped-def]
    """Return a callable that behaves like ``async_session_factory()``."""

    @asynccontextmanager
    async def _cm() -> Any:
        yield session

    def _factory() -> Any:
        return _cm()

    return _factory


# ── Test 1: --no-parse skips run_parser entirely ─────────────────────────────


@pytest.mark.asyncio
async def test_no_parse_does_not_call_run_parser(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With --no-parse, run_parser must never be called."""
    search_id, _ = await _seed(db_session)

    # Redirect the CLI's session factory to our test session.
    monkeypatch.setattr("hh_monitor.cli.async_session_factory", _make_session_factory(db_session))

    # Track whether run_parser is called.
    run_parser_mock = AsyncMock()
    monkeypatch.setattr("hh_monitor.parser.run.run_parser", run_parser_mock)

    await _pipeline_run(
        search_id=search_id,
        portrait_path=None,
        top=5,
        max_pages=5,
        no_parse=True,
    )

    run_parser_mock.assert_not_called()

    captured = capsys.readouterr()
    assert "no-parse" in captured.out.lower()


# ── Test 2: output format — hh.ru link present, Name column absent ────────────


@pytest.mark.asyncio
async def test_pipeline_output_format(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Top-N table must contain hh.ru/resume/ URLs and must NOT contain a Name column."""
    search_id, resume_id = await _seed(db_session)

    monkeypatch.setattr("hh_monitor.cli.async_session_factory", _make_session_factory(db_session))
    monkeypatch.setattr("hh_monitor.parser.run.run_parser", AsyncMock())

    await _pipeline_run(
        search_id=search_id,
        portrait_path=None,
        top=5,
        max_pages=5,
        no_parse=True,
    )

    captured = capsys.readouterr()
    assert "hh.ru/resume/" in captured.out
    assert f"hh.ru/resume/{resume_id}" in captured.out
    # The header must not contain a 'Name' column.
    header_line = next(
        (line for line in captured.out.splitlines() if "Title" in line and "#" in line),
        None,
    )
    assert header_line is not None, "header line not found in output"
    assert "Name" not in header_line
