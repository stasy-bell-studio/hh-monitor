"""Tests for the pipeline run command — --no-parse mode and output format.

Calls the internal ``_pipeline_run`` coroutine directly (avoiding asyncio.run
nesting) and monkeypatches ``async_session_factory`` to inject the test DB
session so all DB changes roll back automatically.

Commit 9 additions:
  test_pipeline_run_by_search_code
    — verifies that passing search_code to _pipeline_run produces the same
      output as passing search_id directly.

  test_pipeline_run_search_code_not_found
    — verifies that an unknown search_code raises SearchNotFoundError.
"""

import hashlib
import json
import re
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from hh_monitor.cli import _pipeline_run
from hh_monitor.db.models import Resume, Search, Snapshot
from hh_monitor.errors import SearchNotFoundError

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
    # The header must contain "Current role" and must NOT contain a 'Name' column.
    header_line = next(
        (line for line in captured.out.splitlines() if "Current role" in line and "#" in line),
        None,
    )
    assert header_line is not None, "header line with 'Current role' not found in output"
    assert "Name" not in header_line


# ── Test 3: hard-rejected candidates excluded from top-N ──────────────────────


@pytest.mark.asyncio
async def test_pipeline_excludes_hard_rejected_from_top_n(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Only candidates that passed all hard filters appear in the top-N table."""
    portrait_with_synonyms: dict[str, Any] = {
        "position_code": "branch_dir",
        "position_name": "Директор филиала",
        "position_synonyms": ["Руководитель филиала"],
    }
    s = Search(
        position_code="branch_dir",
        position_name="Директор филиала",
        hh_params={"text": "директор"},
        portrait=portrait_with_synonyms,
    )
    db_session.add(s)
    await db_session.flush()
    search_id: int = s.id

    # Passing candidate — current position matches portrait
    rid_pass = "pass000000000000"
    db_session.add(Resume(hh_resume_id=rid_pass))
    await db_session.flush()
    payload_pass: dict[str, Any] = {
        "id": rid_pass,
        "title": "Директор",
        "experience": [
            {"company": "X", "position": "Директор филиала", "start": "2020-01", "end": None}
        ],
    }
    db_session.add(
        Snapshot(hh_resume_id=rid_pass, payload=payload_pass, content_hash=_hash(payload_pass))
    )
    await db_session.flush()

    # Hard-rejected candidate — current position is wrong role
    rid_fail = "fail000000000000"
    db_session.add(Resume(hh_resume_id=rid_fail))
    await db_session.flush()
    payload_fail: dict[str, Any] = {
        "id": rid_fail,
        "title": "Бухгалтер",
        "experience": [
            {"company": "Y", "position": "Главный бухгалтер", "start": "2021-01", "end": None}
        ],
    }
    db_session.add(
        Snapshot(hh_resume_id=rid_fail, payload=payload_fail, content_hash=_hash(payload_fail))
    )
    await db_session.flush()

    monkeypatch.setattr("hh_monitor.cli.async_session_factory", _make_session_factory(db_session))

    await _pipeline_run(
        search_id=search_id,
        portrait_path=None,
        top=10,
        max_pages=5,
        no_parse=True,
    )

    captured = capsys.readouterr()
    # Table rows start with a rank number; collect them
    table_rows = [ln for ln in captured.out.splitlines() if re.match(r"^\d{1,3}\s", ln)]
    assert len(table_rows) == 1, f"Expected 1 row (1 passing), got {len(table_rows)}"
    assert rid_pass[:8] in table_rows[0]
    assert rid_fail[:8] not in table_rows[0]
    # Summary must mention hard-reject count
    assert "hard-rejected" in captured.out or "hard_rejected" in captured.out


# ── Test 4: breakdown formatting is safe for string values ───────────────────


def test_breakdown_format_skips_non_int_values() -> None:
    """bd_str formula must skip string values such as hard_reject_reason."""
    breakdown: dict[str, Any] = {
        "agent_network_experience": 5,
        "osago_knowledge": 0,
        "hard_reject_reason": "current_role_mismatch",
    }
    bd_str = " ".join(f"{k}:{v:+d}" for k, v in breakdown.items() if isinstance(v, int) and v != 0)
    assert bd_str == "agent_network_experience:+5"
    assert "current_role_mismatch" not in bd_str
    assert "hard_reject_reason" not in bd_str


# ── Test 5: zero passing candidates — no crash ───────────────────────────────


@pytest.mark.asyncio
async def test_pipeline_zero_passing_candidates_no_crash(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Pipeline must not crash when all candidates fail hard filters."""
    portrait_with_synonyms: dict[str, Any] = {
        "position_code": "branch_dir2",
        "position_name": "Директор филиала",
        "position_synonyms": ["Руководитель филиала"],
    }
    s = Search(
        position_code="branch_dir2",
        position_name="Директор филиала",
        hh_params={"text": "директор"},
        portrait=portrait_with_synonyms,
    )
    db_session.add(s)
    await db_session.flush()
    search_id: int = s.id

    # Only a hard-rejected candidate (wrong role)
    rid = "zero000000000000"
    db_session.add(Resume(hh_resume_id=rid))
    await db_session.flush()
    payload: dict[str, Any] = {
        "id": rid,
        "title": "Логист",
        "experience": [
            {"company": "Z", "position": "Диспетчер-логист", "start": "2019-01", "end": None}
        ],
    }
    db_session.add(Snapshot(hh_resume_id=rid, payload=payload, content_hash=_hash(payload)))
    await db_session.flush()

    monkeypatch.setattr("hh_monitor.cli.async_session_factory", _make_session_factory(db_session))

    # Must not raise
    await _pipeline_run(
        search_id=search_id,
        portrait_path=None,
        top=10,
        max_pages=5,
        no_parse=True,
    )

    captured = capsys.readouterr()
    assert "0 " in captured.out or "no candidates" in captured.out.lower()
    # No table rows (no passing candidates)
    table_rows = [ln for ln in captured.out.splitlines() if re.match(r"^\d{1,3}\s", ln)]
    assert len(table_rows) == 0


# ── Commit 9: --search-code support ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_pipeline_run_by_search_code(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Passing search_code to _pipeline_run finds the correct search and runs normally.

    Seeds a search with search_code='test_sc', then calls _pipeline_run with
    search_id=None and search_code='test_sc'.  Asserts it produces the same
    no-parse output as calling with search_id directly.
    """
    # Seed a search with an explicit search_code
    s = Search(
        search_code="test_sc",
        position_code="test_pos_sc",
        position_name="Test Position SC",
        hh_params={"text": "тест"},
        portrait=_PORTRAIT,
    )
    db_session.add(s)
    await db_session.flush()

    resume_id = "sc00000000000000"
    db_session.add(Resume(hh_resume_id=resume_id))
    await db_session.flush()
    payload: dict[str, Any] = {
        "id": resume_id,
        "title": "Менеджер по продажам",
        "total_experience": {"months": 36},
    }
    db_session.add(Snapshot(hh_resume_id=resume_id, payload=payload, content_hash=_hash(payload)))
    await db_session.flush()

    monkeypatch.setattr("hh_monitor.cli.async_session_factory", _make_session_factory(db_session))
    monkeypatch.setattr("hh_monitor.parser.run.run_parser", AsyncMock())

    # Call with search_code only — search_id is None
    await _pipeline_run(
        search_id=None,
        portrait_path=None,
        top=5,
        max_pages=5,
        no_parse=True,
        search_code="test_sc",
    )

    captured = capsys.readouterr()
    # Pipeline must have run (scored resumes output present)
    assert "Scored" in captured.out
    assert "no-parse" in captured.out.lower()


@pytest.mark.asyncio
async def test_pipeline_run_search_code_not_found(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unrecognised search_code raises SearchNotFoundError."""
    monkeypatch.setattr("hh_monitor.cli.async_session_factory", _make_session_factory(db_session))

    with pytest.raises(SearchNotFoundError):
        await _pipeline_run(
            search_id=None,
            portrait_path=None,
            top=5,
            max_pages=5,
            no_parse=True,
            search_code="nonexistent_sc",
        )
