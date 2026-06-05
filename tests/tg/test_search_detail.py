"""Tests for hh_monitor.tg.search_detail — shared detail renderer."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy.ext.asyncio import AsyncSession

from hh_monitor.db.models import Event, Resume, Search
from hh_monitor.tg.search_detail import render_search_detail


def _make_detail_session(
    *,
    active: bool = True,
    archived_at: datetime | None = None,
    s45: int = 3,
    s60: int = 2,
    s70: int = 10,
    s80: int = 15,
    s90: int = 8,
    parser_row: MagicMock | None = None,
    reasons: list[MagicMock] | None = None,
) -> AsyncMock:
    search_row = MagicMock()
    search_row.position_name = "Андеррайтер"
    search_row.position_code = "underwriter"
    search_row.active = active
    search_row.archived_at = archived_at
    search_row.created_at = datetime(2026, 1, 1, tzinfo=UTC)

    counts_row = MagicMock()
    counts_row.total, counts_row.d7, counts_row.d30 = 50, 5, 20

    score_row = MagicMock()
    score_row.s45, score_row.s60, score_row.s70, score_row.s80, score_row.s90 = (
        s45, s60, s70, s80, s90,
    )

    llm_row = MagicMock()
    llm_row.enriched, llm_row.pending = 40, 5

    if parser_row is None:
        parser_row = MagicMock()
        parser_row.started_at = datetime(2026, 5, 27, 10, 0, tzinfo=UTC)
        parser_row.status = "ok"
        parser_row.resumes_seen = 100
        parser_row.snapshots_inserted = 3
        parser_row.error = None

    if reasons is None:
        reason = MagicMock()
        reason.reason_code = "relevant_exp"
        reason.cnt = 8
        reasons = [reason]

    results = []
    for row, method in [
        (search_row, "fetchone"),
        (counts_row, "fetchone"),
        (score_row, "fetchone"),
        (llm_row, "fetchone"),
        (parser_row, "fetchone"),
        (reasons, "fetchall"),
    ]:
        r = MagicMock()
        if method == "fetchone":
            r.fetchone.return_value = row
        else:
            r.fetchall.return_value = row
        results.append(r)

    mock_session = AsyncMock(spec=AsyncSession)
    mock_session.execute = AsyncMock(side_effect=results)
    return mock_session


async def test_render_returns_string_with_position_info() -> None:
    session = _make_detail_session()
    result = await render_search_detail(session, 1)
    assert isinstance(result, str)
    assert "Андеррайтер" in result
    assert "underwriter" in result
    assert "50" in result
    assert "Релевантный опыт" in result


async def test_render_returns_none_when_not_found() -> None:
    not_found = MagicMock()
    not_found.fetchone.return_value = None
    mock_session = AsyncMock(spec=AsyncSession)
    mock_session.execute = AsyncMock(return_value=not_found)
    result = await render_search_detail(mock_session, 999)
    assert result is None


async def test_render_includes_s45_bucket() -> None:
    session = _make_detail_session(s45=3)
    result = await render_search_detail(session, 1)
    assert result is not None
    assert "45-59: 3" in result


async def test_render_includes_all_score_buckets() -> None:
    session = _make_detail_session()
    result = await render_search_detail(session, 1)
    assert result is not None
    for label in ("45-59:", "60-69:", "70-79:", "80-89:", "90+:"):
        assert label in result


async def test_render_status_active() -> None:
    session = _make_detail_session(active=True, archived_at=None)
    result = await render_search_detail(session, 1)
    assert result is not None
    assert "🟢 Активный" in result


async def test_render_status_paused() -> None:
    session = _make_detail_session(active=False, archived_at=None)
    result = await render_search_detail(session, 1)
    assert result is not None
    assert "🟡 Приостановлен" in result


async def test_render_status_archived() -> None:
    session = _make_detail_session(
        active=False, archived_at=datetime(2026, 4, 1, tzinfo=UTC)
    )
    result = await render_search_detail(session, 1)
    assert result is not None
    assert "📦 Архив" in result


async def test_render_parser_none() -> None:
    results = []
    rows_and_methods = [
        (MagicMock(position_name="X", position_code="x", active=True, archived_at=None,
                   created_at=datetime(2026, 1, 1, tzinfo=UTC)), "fetchone"),
        (MagicMock(total=0, d7=0, d30=0), "fetchone"),
        (MagicMock(s45=0, s60=0, s70=0, s80=0, s90=0), "fetchone"),
        (MagicMock(enriched=0, pending=0), "fetchone"),
        (None, "fetchone"),
        ([], "fetchall"),
    ]
    for row, method in rows_and_methods:
        r = MagicMock()
        if method == "fetchone":
            r.fetchone.return_value = row
        else:
            r.fetchall.return_value = row
        results.append(r)
    mock_session = AsyncMock(spec=AsyncSession)
    mock_session.execute = AsyncMock(side_effect=results)

    result = await render_search_detail(mock_session, 1)
    assert result is not None
    assert "нет данных" in result


async def test_render_parser_with_error() -> None:
    parser_row = MagicMock()
    parser_row.started_at = datetime(2026, 5, 27, 10, 0, tzinfo=UTC)
    parser_row.status = "error"
    parser_row.resumes_seen = 0
    parser_row.snapshots_inserted = 0
    parser_row.error = "connection refused — timeout after 30s (retry 3 of 3)"
    session = _make_detail_session(parser_row=parser_row)
    result = await render_search_detail(session, 1)
    assert result is not None
    error_str = "connection refused — timeout after 30s (retry 3 of 3)"
    assert error_str[:40] in result


async def test_render_no_reasons() -> None:
    session = _make_detail_session(reasons=[])
    result = await render_search_detail(session, 1)
    assert result is not None
    assert "нет данных" in result


async def test_render_escapes_position_and_error_html() -> None:
    """position_name / position_code / parser.error are HTML-escaped (P3-2)."""
    parser_row = MagicMock()
    parser_row.started_at = datetime(2026, 5, 27, 10, 0, tzinfo=UTC)
    parser_row.status = "error"
    parser_row.resumes_seen = 0
    parser_row.snapshots_inserted = 0
    parser_row.error = "boom <script> & co"

    results = []
    rows_and_methods = [
        (
            MagicMock(
                position_name="A & B <Director>",
                position_code="d<i>r",
                active=True,
                archived_at=None,
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
            ),
            "fetchone",
        ),
        (MagicMock(total=0, d7=0, d30=0), "fetchone"),
        (MagicMock(s45=0, s60=0, s70=0, s80=0, s90=0), "fetchone"),
        (MagicMock(enriched=0, pending=0), "fetchone"),
        (parser_row, "fetchone"),
        ([], "fetchall"),
    ]
    for row, method in rows_and_methods:
        r = MagicMock()
        if method == "fetchone":
            r.fetchone.return_value = row
        else:
            r.fetchall.return_value = row
        results.append(r)
    mock_session = AsyncMock(spec=AsyncSession)
    mock_session.execute = AsyncMock(side_effect=results)

    result = await render_search_detail(mock_session, 1)
    assert result is not None
    assert "A &amp; B &lt;Director&gt;" in result
    assert "d&lt;i&gt;r" in result
    assert "boom &lt;script&gt; &amp; co" in result
    assert "<Director>" not in result
    assert "<script>" not in result


async def test_score_distribution_reads_event_score_total(db_session: AsyncSession) -> None:
    """_DETAIL_SCORE_SQL buckets the per-event snapshot (e.score_total), scoped to
    the search — not the resume's latest global score (P3-4)."""
    search = Search(
        position_code="p3_4_detail",
        position_name="Тест",
        hh_params={},
        portrait={},
        active=True,
    )
    db_session.add(search)
    await db_session.flush()
    search_id = search.id  # capture before commit expires the instance

    rid = "resume_detail_p3_4"
    db_session.add(Resume(hh_resume_id=rid, fit_score=70, llm_score=90, score_total=20))
    await db_session.flush()
    db_session.add(
        Event(
            hh_resume_id=rid,
            event_type="NEW",
            search_id=search_id,
            llm_enriched=True,
            score_total=85,  # 80-89 bucket; resume's 20 would land in no bucket
            llm_verdict="подходит",
        )
    )
    await db_session.commit()

    result = await render_search_detail(db_session, search_id)
    assert result is not None
    assert "80-89: 1" in result
    assert "45-59: 0" in result
