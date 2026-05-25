"""Tests for hh_monitor.digest — query, xlsx export, pdf export.

AC4 coverage for commit 9.2:

  test_fetch_candidates_returns_matching_rows
    — Seeds Search + Resume + Snapshot + Event with a specific search_code,
      calls fetch_candidates, asserts the seeded candidate appears in results.

  test_export_xlsx_creates_valid_workbook
    — Builds CandidateRow objects in memory, calls export_xlsx, asserts the
      output file exists, has the correct header row, and the right number of
      data rows.

  test_export_pdf_creates_non_empty_file
    — Builds CandidateRow objects in memory, calls export_pdf, asserts the
      output file exists and is non-empty.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from hh_monitor.db.models import Event, Resume, Search, Snapshot
from hh_monitor.digest.query import CandidateRow, fetch_candidates

# ── Helpers ───────────────────────────────────────────────────────────────────


def _hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _make_candidate(
    rid: str = "abc00000000000001",
    score_total: int = 75,
    llm_verdict: str = "подходит",
) -> CandidateRow:
    return CandidateRow(
        hh_resume_id=rid,
        score_total=score_total,
        fit_score=60,
        llm_score=80,
        llm_verdict=llm_verdict,
        llm_comment="Хороший кандидат",
        llm_red_flags=["Нет опыта ОСАГО"],
        screening_status=None,
        payload={
            "id": rid,
            "title": "Директор филиала",
            "age": 38,
            "area": {"id": "2", "name": "Санкт-Петербург"},
            "total_experience": {"months": 96},
            "experience": [
                {
                    "company": "ВСК Страхование",
                    "position": "Директор филиала",
                    "start": "2019-01",
                    "end": None,
                }
            ],
        },
    )


# ── AC4a: query ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_candidates_returns_matching_rows(db_session: AsyncSession) -> None:
    """fetch_candidates returns the seeded candidate for the given search_code."""
    # Arrange — seed search with known search_code
    sc = Search(
        search_code="digest_test_sc",
        position_code="branch_director",
        position_name="Директор филиала",
        hh_params={"text": "директор"},
        portrait={"position_code": "branch_director", "position_name": "Директор филиала"},
    )
    db_session.add(sc)
    await db_session.flush()

    rid = "digest0000000001"
    db_session.add(Resume(hh_resume_id=rid, score_total=70, fit_score=55, llm_score=78))
    await db_session.flush()

    payload: dict[str, Any] = {
        "id": rid,
        "title": "Директор филиала",
        "total_experience": {"months": 60},
    }
    db_session.add(Snapshot(hh_resume_id=rid, payload=payload, content_hash=_hash(payload)))
    await db_session.flush()

    db_session.add(
        Event(
            hh_resume_id=rid,
            event_type="NEW",
            search_id=sc.id,
            details={"curr_snapshot_id": 1},
        )
    )
    await db_session.flush()

    # Act
    candidates = await fetch_candidates(
        db_session,
        search_code="digest_test_sc",
        min_score=60,
        include_screened=False,
    )

    # Assert
    assert len(candidates) >= 1, "Expected at least one candidate row"
    ids = [c.hh_resume_id for c in candidates]
    assert rid in ids, f"Seeded resume {rid!r} missing from results: {ids}"


@pytest.mark.asyncio
async def test_fetch_candidates_respects_min_score(db_session: AsyncSession) -> None:
    """fetch_candidates excludes resumes below min_score."""
    sc = Search(
        search_code="digest_test_minscore",
        position_code="branch_director",
        position_name="Директор филиала",
        hh_params={"text": "директор"},
        portrait={"position_code": "branch_director", "position_name": "Директор филиала"},
    )
    db_session.add(sc)
    await db_session.flush()

    rid = "digest0000000002"
    db_session.add(Resume(hh_resume_id=rid, score_total=45))  # below threshold
    await db_session.flush()

    payload: dict[str, Any] = {"id": rid, "title": "Менеджер"}
    db_session.add(Snapshot(hh_resume_id=rid, payload=payload, content_hash=_hash(payload)))
    await db_session.flush()

    db_session.add(Event(hh_resume_id=rid, event_type="NEW", search_id=sc.id, details={}))
    await db_session.flush()

    candidates = await fetch_candidates(
        db_session, search_code="digest_test_minscore", min_score=60
    )
    assert all(c.hh_resume_id != rid for c in candidates), "Resume below min_score must not appear"


# ── AC4b: xlsx export ────────────────────────────────────────────────────────


def test_export_xlsx_creates_valid_workbook(tmp_path: Path) -> None:
    """export_xlsx creates an .xlsx file with the correct headers and data rows."""
    from openpyxl import load_workbook

    from hh_monitor.digest.export_xlsx import _COLUMNS, export_xlsx

    candidates = [
        _make_candidate("r0000000000000001", 75),
        _make_candidate("r0000000000000002", 65),
    ]
    out = tmp_path / "test_digest.xlsx"

    export_xlsx(candidates, out)

    assert out.exists(), "xlsx file was not created"
    wb = load_workbook(str(out))
    ws = wb.active

    # Header row has correct number of columns
    headers = [ws.cell(row=1, column=i + 1).value for i in range(len(_COLUMNS))]
    expected_headers = [col[0] for col in _COLUMNS]
    assert headers == expected_headers, f"Headers mismatch: {headers}"

    # Two data rows (one per candidate)
    data_rows = [ws.cell(row=r, column=1).value for r in range(2, len(candidates) + 2)]
    assert len(data_rows) == 2, f"Expected 2 data rows, got {len(data_rows)}"
    assert data_rows[0] == 1  # rank column
    assert data_rows[1] == 2


# ── AC4c: pdf export ─────────────────────────────────────────────────────────


def test_export_pdf_creates_non_empty_file(tmp_path: Path) -> None:
    """export_pdf creates a non-empty .pdf file."""
    try:
        import weasyprint as _wp  # noqa: F401
    except OSError:
        pytest.skip("WeasyPrint system libs (pango/cairo) not available")

    from hh_monitor.digest.export_pdf import export_pdf

    candidates = [_make_candidate()]
    out = tmp_path / "test_digest.pdf"

    export_pdf(candidates, out, search_code="branch_director_21vek")

    assert out.exists(), "pdf file was not created"
    assert out.stat().st_size > 1024, (
        f"pdf file too small ({out.stat().st_size} bytes) — likely empty"
    )
