"""Unit tests for hh_monitor/daily_report/run.py.

All tests mock filesystem, subprocess, HTTP, and DB — no real I/O.
asyncio_mode = "auto" (see pyproject.toml), so async defs run without markers.
"""
from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.ext.asyncio import AsyncSession

from hh_monitor.daily_report.run import (
    MSK,
    _build_candidates_section,
    _build_external_section,
    _build_pipeline_section,
    _build_server_section,
    _build_units_section,
    _build_verdict,
    _check_telegram,
    _traffic_light,
    build_daily_report,
)
from hh_monitor.db.models import Event, NotificationSent, OAuthToken, ParserRun, Resume, Search

# ── pure helpers ──────────────────────────────────────────────────────────────


def test_traffic_light_green() -> None:
    assert _traffic_light(0) == "🟢"
    assert _traffic_light(69) == "🟢"


def test_traffic_light_yellow() -> None:
    assert _traffic_light(70) == "🟡"
    assert _traffic_light(90) == "🟡"


def test_traffic_light_red() -> None:
    assert _traffic_light(91) == "🔴"
    assert _traffic_light(100) == "🔴"


def test_traffic_light_custom_thresholds() -> None:
    assert _traffic_light(24, warn_lo=25, warn_hi=80) == "🟢"
    assert _traffic_light(25, warn_lo=25, warn_hi=80) == "🟡"
    assert _traffic_light(81, warn_lo=25, warn_hi=80) == "🔴"


def test_verdict_all_green() -> None:
    assert _build_verdict([]) == "✅ Всё работает в штатном режиме"


def test_verdict_degraded() -> None:
    result = _build_verdict(["Память", "LLM API"])
    assert result == "⚠️ Есть проблемы — детали ниже"


# ── server section ────────────────────────────────────────────────────────────


def test_server_section_green() -> None:
    mem = {
        "MemTotal": 8_000_000,
        "MemAvailable": 6_000_000,
        "SwapTotal": 2_000_000,
        "SwapFree": 2_000_000,
    }
    with (
        patch("hh_monitor.daily_report.run._read_meminfo", return_value=mem),
        patch("hh_monitor.daily_report.run._read_uptime_seconds", return_value=86400.0),
        patch("shutil.disk_usage", return_value=MagicMock(used=100 * 1024**3, total=500 * 1024**3)),
    ):
        block, problems, compact = _build_server_section()

    assert problems == []
    assert "🟢" in block
    assert "МБ" in block
    assert "ГБ" in block
    assert "Аптайм" in block
    assert compact == "🖥 Сервер 🟢"


def test_server_section_ram_red() -> None:
    mem = {
        "MemTotal": 8_000_000,
        "MemAvailable": 400_000,  # ~95% used
        "SwapTotal": 0,
        "SwapFree": 0,
    }
    with (
        patch("hh_monitor.daily_report.run._read_meminfo", return_value=mem),
        patch("hh_monitor.daily_report.run._read_uptime_seconds", return_value=3600.0),
        patch("shutil.disk_usage", return_value=MagicMock(used=100 * 1024**3, total=500 * 1024**3)),
    ):
        block, problems, compact = _build_server_section()

    assert "Память" in problems
    assert "🔴" in block
    assert compact == "🖥 Сервер 🔴"


def test_server_section_swap_thresholds() -> None:
    # 50% swap usage → 🟡 (warn_lo=25, warn_hi=80) — amber compact, no problem
    mem = {
        "MemTotal": 8_000_000,
        "MemAvailable": 6_000_000,
        "SwapTotal": 2_000_000,
        "SwapFree": 1_000_000,  # 50% used
    }
    with (
        patch("hh_monitor.daily_report.run._read_meminfo", return_value=mem),
        patch("hh_monitor.daily_report.run._read_uptime_seconds", return_value=3600.0),
        patch("shutil.disk_usage", return_value=MagicMock(used=100 * 1024**3, total=500 * 1024**3)),
    ):
        block, problems, compact = _build_server_section()

    assert "Swap" not in problems  # yellow, not red
    assert "🟡" in block
    assert compact == "🖥 Сервер 🟡"  # amber propagates to compact


def test_server_section_proc_unavailable() -> None:
    with (
        patch("hh_monitor.daily_report.run._read_meminfo", side_effect=OSError("no /proc")),
        patch("hh_monitor.daily_report.run._read_uptime_seconds", side_effect=OSError("no /proc")),
        patch("shutil.disk_usage", return_value=MagicMock(used=100 * 1024**3, total=500 * 1024**3)),
    ):
        block, problems, compact = _build_server_section()

    assert "Память" in problems
    assert "🔴" in block
    assert compact == "🖥 Сервер 🔴"


# ── units section ─────────────────────────────────────────────────────────────


def _make_run_result(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    r: subprocess.CompletedProcess[str] = subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=""
    )
    return r


def test_units_all_active() -> None:
    with patch(
        "subprocess.run",
        return_value=_make_run_result("active"),
    ):
        block, problems, compact = _build_units_section()

    assert problems == []
    assert "🟢" in compact


def test_units_oneshot_inactive_is_green() -> None:
    """Oneshot services show 'inactive' between runs — must be 🟢, not 🔴."""
    with patch("subprocess.run", return_value=_make_run_result("inactive")):
        block, problems, compact = _build_units_section()

    assert "🟢" in block


def test_units_one_failed() -> None:
    """A oneshot service returning 'failed' → 🔴 line and added to problems."""
    call_count = 0

    def mock_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal call_count
        call_count += 1
        if "hh-monitor-pipeline.service" in cmd:
            return _make_run_result("failed")
        return _make_run_result("active")

    with patch("subprocess.run", side_effect=mock_run):
        block, problems, compact = _build_units_section()

    assert "hh-monitor-pipeline.service" in problems
    assert "🔴" in block
    assert "🔴" in compact


def test_units_timeout_shows_unknown() -> None:
    """subprocess.TimeoutExpired → status 'unknown', treated as 🔴 for longrunning."""
    with patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="systemctl", timeout=5),
    ):
        block, problems, compact = _build_units_section()

    assert "unknown" in block
    assert "hh-monitor-bot.service" in problems


# ── pipeline section ──────────────────────────────────────────────────────────


def _make_session(
    scalar_returns: list[object],
    execute_scalar_one: object = None,
    scalars_all: list[object] | None = None,
) -> AsyncMock:
    """Build a minimal async session mock.

    scalar_returns: side_effect list for session.scalar
    execute_scalar_one: returned by execute(...).scalar_one_or_none() (for get_current_threshold)
    scalars_all: list returned by session.scalars(...).all() (for Search query)
    """
    session = AsyncMock()
    session.scalar = AsyncMock(side_effect=scalar_returns)

    mock_exec_result = MagicMock()
    mock_exec_result.scalar_one_or_none.return_value = execute_scalar_one
    session.execute = AsyncMock(return_value=mock_exec_result)

    if scalars_all is not None:
        mock_scalars_result = MagicMock()
        mock_scalars_result.all.return_value = scalars_all
        session.scalars = AsyncMock(return_value=mock_scalars_result)

    return session


async def test_pipeline_empty_db() -> None:
    """Zero runs ever: renders no-data line and adds Прогон to problems."""
    session = _make_session(
        scalar_returns=[None, 0],  # last_run=None, viewed_today=0
        scalars_all=[],
    )
    msk_now = datetime.now(MSK)
    block, problems = await _build_pipeline_section(session, msk_now)

    assert "Прогон" in problems
    assert "нет данных" in block


async def test_pipeline_no_runs_in_24h_but_last_run_exists() -> None:
    """last_run exists with ok status — no pipeline problem."""
    mock_run = MagicMock(spec=ParserRun)
    mock_run.id = 99
    mock_run.started_at = datetime(2026, 6, 9, 10, 0, tzinfo=UTC)
    mock_run.status = "ok"
    mock_run.resumes_seen = 50
    mock_run.resumes_viewed = 5
    mock_run.snapshots_skipped = 2

    session = _make_session(
        scalar_returns=[mock_run, 0],
        scalars_all=[],
    )
    msk_now = datetime.now(MSK)
    block, problems = await _build_pipeline_section(session, msk_now)

    assert "Прогон" not in problems
    assert "#99" in block
    # Billable = resumes_viewed + snapshots_skipped = 5+2 = 7
    assert "просмотрено 7" in block


async def test_pipeline_failed_last_run() -> None:
    """A last_run with status='failed' → Прогон added to problems."""
    mock_run = MagicMock(spec=ParserRun)
    mock_run.id = 5
    mock_run.started_at = datetime(2026, 6, 11, 7, 0, tzinfo=UTC)
    mock_run.status = "failed"
    mock_run.resumes_seen = 0
    mock_run.resumes_viewed = 0
    mock_run.snapshots_skipped = 0

    session = _make_session(
        scalar_returns=[mock_run, 0],
        scalars_all=[],
    )
    msk_now = datetime.now(MSK)
    block, problems = await _build_pipeline_section(session, msk_now)

    assert "Прогон" in problems
    assert "🔴" in block


async def test_pipeline_quota_amber() -> None:
    """430 billable views → 🟡 (>=_QUOTA_AMBER=400 but < 500)."""
    from hh_monitor.daily_report.run import _QUOTA_AMBER

    mock_run = MagicMock(spec=ParserRun)
    mock_run.id = 1
    mock_run.started_at = datetime.now(UTC)
    mock_run.status = "ok"
    mock_run.resumes_seen = 500
    mock_run.resumes_viewed = 420
    mock_run.snapshots_skipped = 10  # total billable = 430

    session = _make_session(
        scalar_returns=[mock_run, 430],  # SUM(resumes_viewed+snapshots_skipped)=430
        scalars_all=[],
    )
    msk_now = datetime.now(MSK)
    block, problems = await _build_pipeline_section(session, msk_now)

    assert "🟡" in block
    assert "Квота" not in problems
    assert _QUOTA_AMBER == 400  # sanity-check the constant


async def test_pipeline_quota_exhausted() -> None:
    """500 billable views → 🔴 + Квота in problems."""
    mock_run = MagicMock(spec=ParserRun)
    mock_run.id = 2
    mock_run.started_at = datetime.now(UTC)
    mock_run.status = "view_limit_exhausted"
    mock_run.resumes_seen = 800
    mock_run.resumes_viewed = 490
    mock_run.snapshots_skipped = 10  # total = 500

    session = _make_session(
        scalar_returns=[mock_run, 500],
        scalars_all=[],
    )
    msk_now = datetime.now(MSK)
    block, problems = await _build_pipeline_section(session, msk_now)

    assert "Квота" in problems
    assert "🔴" in block


async def test_pipeline_quota_over_budget_anomaly() -> None:
    """viewed > budget → 'израсходовано N из budget ⚠️' (no negative remainder)."""
    mock_run = MagicMock(spec=ParserRun)
    mock_run.id = 3
    mock_run.started_at = datetime.now(UTC)
    mock_run.status = "ok"
    mock_run.resumes_seen = 600
    mock_run.resumes_viewed = 510
    mock_run.snapshots_skipped = 10  # total = 520

    session = _make_session(
        scalar_returns=[mock_run, 520],
        scalars_all=[],
    )
    msk_now = datetime.now(MSK)
    block, problems = await _build_pipeline_section(session, msk_now)

    assert "Квота" in problems
    assert "израсходовано 520 из 500 ⚠️" in block


async def test_pipeline_quota_remaining_format() -> None:
    """Normal quota: format is 'осталось N из 500'."""
    mock_run = MagicMock(spec=ParserRun)
    mock_run.id = 4
    mock_run.started_at = datetime.now(UTC)
    mock_run.status = "ok"
    mock_run.resumes_seen = 300
    mock_run.resumes_viewed = 190
    mock_run.snapshots_skipped = 10  # total = 200

    session = _make_session(
        scalar_returns=[mock_run, 200],
        scalars_all=[],
    )
    msk_now = datetime.now(MSK)
    block, problems = await _build_pipeline_section(session, msk_now)

    assert "Квота" not in problems
    assert "осталось 300 из 500" in block  # remaining = 500 - 200


def test_pipeline_quota_msk_boundary() -> None:
    """23:30 UTC (= 02:30 MSK next day) lands in the correct MSK day (AC4)."""
    msk_now = datetime(2026, 6, 11, 8, 30, tzinfo=MSK)
    msk_today_start = msk_now.replace(hour=0, minute=0, second=0, microsecond=0)
    # msk_today_start = 2026-06-11 00:00 MSK = 2026-06-10 21:00 UTC

    # A run at 23:30 UTC (June 10) = 02:30 MSK (June 11) → inside today's window
    run_in = datetime(2026, 6, 10, 23, 30, tzinfo=UTC)
    assert run_in >= msk_today_start

    # A run at 20:59 UTC (June 10) = 23:59 MSK (June 10) → outside today's window
    run_out = datetime(2026, 6, 10, 20, 59, tzinfo=UTC)
    assert run_out < msk_today_start


# ── candidates section ────────────────────────────────────────────────────────


async def test_candidates_empty_day() -> None:
    session = AsyncMock()
    mock_exec_result = MagicMock()
    mock_exec_result.scalar_one_or_none.return_value = None  # no DB override → settings default
    session.execute = AsyncMock(return_value=mock_exec_result)
    session.scalar = AsyncMock(side_effect=[0, 0])  # scored=0, notified=0
    msk_now = datetime.now(MSK)

    with patch("hh_monitor.daily_report.run.settings") as mock_settings:
        mock_settings.telegram_score_threshold = 70
        line = await _build_candidates_section(session, msk_now)

    assert "оценка ≥ 70" in line
    assert "уведомлений отправлено — 0" in line


async def test_candidates_nonzero() -> None:
    session = AsyncMock()
    mock_exec_result = MagicMock()
    mock_exec_result.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=mock_exec_result)
    session.scalar = AsyncMock(side_effect=[3, 2])  # scored=3, notified=2
    msk_now = datetime.now(MSK)

    with patch("hh_monitor.daily_report.run.settings") as mock_settings:
        mock_settings.telegram_score_threshold = 70
        line = await _build_candidates_section(session, msk_now)

    assert "— 3," in line
    assert "уведомлений отправлено — 2" in line


async def test_candidates_uses_live_threshold() -> None:
    """get_current_threshold returns DB value → label and query use it (AC8)."""
    session = AsyncMock()
    mock_exec_result = MagicMock()
    mock_exec_result.scalar_one_or_none.return_value = "80"  # DB override
    session.execute = AsyncMock(return_value=mock_exec_result)
    session.scalar = AsyncMock(side_effect=[5, 1])
    msk_now = datetime.now(MSK)

    with patch("hh_monitor.daily_report.run.settings") as mock_settings:
        mock_settings.telegram_score_threshold = 70  # settings default — must NOT be used
        line = await _build_candidates_section(session, msk_now)

    assert "оценка ≥ 80" in line  # DB value wins
    assert "оценка ≥ 70" not in line


async def test_candidates_notified_excludes_merged(db_session: AsyncSession) -> None:
    """AC7: 'уведомлений отправлено' counts delivered cards only — a merged-duplicate row
    (multi-field edit collapsed into the winner) is not counted as a second notification."""
    msk_now = datetime.now(MSK)
    search = Search(
        position_code="dr_pos", position_name="DR", hh_params={}, portrait={}, active=True
    )
    db_session.add(search)
    await db_session.flush()
    rid = "resume_dr_merged"
    db_session.add(
        Resume(hh_resume_id=rid, score_total=75, fit_score=60, llm_score=78, llm_verdict="подходит")
    )
    await db_session.flush()
    winner = Event(
        hh_resume_id=rid, event_type="UPDATED_POSITION", search_id=search.id,
        llm_enriched=True, score_total=75, details={"curr_snapshot_id": 1},
    )
    sibling = Event(
        hh_resume_id=rid, event_type="UPDATED_SALARY", search_id=search.id,
        llm_enriched=True, score_total=75, details={"curr_snapshot_id": 1},
    )
    db_session.add_all([winner, sibling])
    await db_session.flush()
    db_session.add(NotificationSent(event_id=winner.id, tg_message_id=10))
    db_session.add(
        NotificationSent(event_id=sibling.id, tg_message_id=10, merged_into_event_id=winner.id)
    )
    await db_session.flush()

    line = await _build_candidates_section(db_session, msk_now)
    # ONE person (one résumé) scored ≥ 70 across two events → counted once (distinct people);
    # only ONE delivered card (winner) — the merged sibling is excluded.
    assert "— 1," in line
    assert "уведомлений отправлено — 1" in line


# ── Telegram check ────────────────────────────────────────────────────────────


async def test_telegram_check_getme_ok() -> None:
    """Token configured, getMe returns 200 + ok=True → True."""
    with patch("hh_monitor.daily_report.run.settings") as mock_settings:
        mock_settings.telegram_bot_token = "abc:123"
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"ok": True, "result": {}}

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_response)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await _check_telegram()

    assert result is True
    # Verify the correct endpoint was used (token not tested directly — never log it)
    called_url = mock_client.get.call_args[0][0]
    assert "getMe" in called_url
    assert "abc:123" in called_url


async def test_telegram_check_getme_not_ok() -> None:
    """Token configured, getMe returns 200 but ok=False → False."""
    with patch("hh_monitor.daily_report.run.settings") as mock_settings:
        mock_settings.telegram_bot_token = "abc:123"
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"ok": False}

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_response)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await _check_telegram()

    assert result is False


async def test_telegram_check_no_token_302() -> None:
    """No token configured, HEAD returns 302 → True (2xx/3xx = available)."""
    with patch("hh_monitor.daily_report.run.settings") as mock_settings:
        mock_settings.telegram_bot_token = None
        mock_response = MagicMock()
        mock_response.status_code = 302

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.head = AsyncMock(return_value=mock_response)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await _check_telegram()

    assert result is True  # 302 < 400 → up


async def test_telegram_check_timeout() -> None:
    """Any exception → False (never raises)."""
    with patch("hh_monitor.daily_report.run.settings") as mock_settings:
        mock_settings.telegram_bot_token = None

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.head = AsyncMock(side_effect=Exception("timeout"))

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await _check_telegram()

    assert result is False


# ── external section ──────────────────────────────────────────────────────────


async def test_external_all_ok() -> None:
    mock_token = MagicMock(spec=OAuthToken)
    mock_token.expires_at = datetime.now(UTC) + timedelta(hours=100)

    session = AsyncMock()
    session.scalar = AsyncMock(return_value=mock_token)

    with (
        patch("hh_monitor.daily_report.run._check_url", return_value=True),
        patch("hh_monitor.daily_report.run._check_telegram", return_value=True),
    ):
        block, problems, compact = await _build_external_section(session)

    assert problems == []
    assert "🟢" in compact
    assert "HH OAuth" in compact


async def test_external_no_token() -> None:
    session = AsyncMock()
    session.scalar = AsyncMock(return_value=None)

    with (
        patch("hh_monitor.daily_report.run._check_url", return_value=True),
        patch("hh_monitor.daily_report.run._check_telegram", return_value=True),
    ):
        block, problems, compact = await _build_external_section(session)

    assert "HH OAuth" in problems
    assert "токен не найден" in block
    assert compact == "🌐 Сервисы 🔴"


async def test_external_token_expiring_soon() -> None:
    """Token with <24h TTL → 🔴."""
    mock_token = MagicMock(spec=OAuthToken)
    mock_token.expires_at = datetime.now(UTC) + timedelta(hours=10)

    session = AsyncMock()
    session.scalar = AsyncMock(return_value=mock_token)

    with (
        patch("hh_monitor.daily_report.run._check_url", return_value=True),
        patch("hh_monitor.daily_report.run._check_telegram", return_value=True),
    ):
        block, problems, compact = await _build_external_section(session)

    assert "HH OAuth" in problems
    assert "🔴" in block
    assert compact == "🌐 Сервисы 🔴"


async def test_external_token_amber() -> None:
    """Token with 24-72h TTL → 🟡, compact shows 🟡, no problem (AC4-amber)."""
    mock_token = MagicMock(spec=OAuthToken)
    mock_token.expires_at = datetime.now(UTC) + timedelta(hours=50)

    session = AsyncMock()
    session.scalar = AsyncMock(return_value=mock_token)

    with (
        patch("hh_monitor.daily_report.run._check_url", return_value=True),
        patch("hh_monitor.daily_report.run._check_telegram", return_value=True),
    ):
        block, problems, compact = await _build_external_section(session)

    assert "HH OAuth" not in problems
    assert "🟡" in block
    assert "🟡" in compact  # amber propagates to compact one-liner
    assert "HH OAuth" in compact


async def test_external_oauth_amber_compact_line() -> None:
    """Amber OAuth: compact shows 🟡, full block NOT expanded, verdict stays green (AC4-amber)."""
    mock_token = MagicMock(spec=OAuthToken)
    mock_token.expires_at = datetime.now(UTC) + timedelta(hours=30)

    mock_run = MagicMock(spec=ParserRun)
    mock_run.id = 1
    mock_run.started_at = datetime.now(UTC)
    mock_run.status = "ok"
    mock_run.resumes_seen = 10
    mock_run.resumes_viewed = 2
    mock_run.snapshots_skipped = 0

    session = _make_session(
        scalar_returns=[mock_run, 2, 0, 0, mock_token],  # last_run, viewed, scored, notified, oauth
        execute_scalar_one=None,
        scalars_all=[],
    )

    mem = {"MemTotal": 8_000_000, "MemAvailable": 6_000_000, "SwapTotal": 0, "SwapFree": 0}
    with (
        patch("hh_monitor.daily_report.run._read_meminfo", return_value=mem),
        patch("hh_monitor.daily_report.run._read_uptime_seconds", return_value=3600.0),
        patch("shutil.disk_usage", return_value=MagicMock(used=50 * 1024**3, total=500 * 1024**3)),
        patch("subprocess.run", return_value=_make_run_result("active")),
        patch("hh_monitor.daily_report.run._check_url", return_value=True),
        patch("hh_monitor.daily_report.run._check_telegram", return_value=True),
    ):
        report = await build_daily_report(session)

    assert "🟡" in report          # amber visible in compact line
    assert "✅" in report           # verdict is still green (amber ≠ red)
    assert "⚠️" not in report       # no degraded verdict
    # Full external block should NOT be appended (only expands on 🔴)
    assert "<b>🌐 Внешние сервисы</b>" not in report


async def test_external_check_timeout_never_raises() -> None:
    """httpx timeout on _check_telegram → False; no exception propagated."""
    from hh_monitor.daily_report.run import _check_telegram as real_check_telegram

    with patch("hh_monitor.daily_report.run.settings") as mock_settings:
        mock_settings.telegram_bot_token = None

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.head = AsyncMock(side_effect=Exception("timeout"))

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await real_check_telegram()

    assert result is False


async def test_external_llm_down() -> None:
    """Unreachable LLM API → 🔴 compact, no crash."""
    mock_token = MagicMock(spec=OAuthToken)
    mock_token.expires_at = datetime.now(UTC) + timedelta(hours=100)

    session = AsyncMock()
    session.scalar = AsyncMock(return_value=mock_token)

    with (
        patch("hh_monitor.daily_report.run._check_url", return_value=False),
        patch("hh_monitor.daily_report.run._check_telegram", return_value=True),
    ):
        block, problems, compact = await _build_external_section(session)

    assert "LLM API" in problems
    assert "недоступен" in block
    assert compact == "🌐 Сервисы 🔴"


# ── gate (send_guard) ─────────────────────────────────────────────────────────


async def test_gate_closed_returns_false() -> None:
    """send_enabled=False → run_daily_report returns False (gate closed, AC6)."""
    from hh_monitor.daily_report.run import run_daily_report

    session = AsyncMock()
    bot = AsyncMock()

    with patch("hh_monitor.daily_report.run.send_enabled", return_value=False):
        result = await run_daily_report(session, bot)

    assert result is False


async def test_gate_open_returns_true() -> None:
    """send_enabled=True, send succeeds → run_daily_report returns True (AC6)."""
    from hh_monitor.daily_report.run import run_daily_report

    session = AsyncMock()
    bot = AsyncMock()

    with (
        patch("hh_monitor.daily_report.run.send_enabled", return_value=True),
        patch("hh_monitor.daily_report.run.build_daily_report", return_value="text"),
        patch("hh_monitor.daily_report.run._send_long_message", return_value=None),
        patch("hh_monitor.daily_report.run.settings") as mock_settings,
    ):
        mock_settings.telegram_admin_topic_id = 0
        mock_settings.telegram_hr_group_id = -100
        result = await run_daily_report(session, bot)

    assert result is True


# ── full report integration ───────────────────────────────────────────────────


def _make_full_session(mock_token: object) -> AsyncMock:
    """Session mock for build_daily_report integration tests.

    Query order:
    1. _build_pipeline_section: scalar(last_run), scalar(viewed_today)
    2. _build_pipeline_section: scalars(active_searches)
    3. _build_candidates_section: execute(threshold), scalar(scored), scalar(notified)
    4. _build_external_section: scalar(oauth_token)
    """
    session = AsyncMock()
    # Scalars: last_run=None, viewed_today=0, scored=0, notified=0, oauth_token
    session.scalar = AsyncMock(side_effect=[None, 0, 0, 0, mock_token])
    # execute for get_current_threshold (returns None → fallback to settings)
    mock_exec = MagicMock()
    mock_exec.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=mock_exec)
    # scalars for active searches
    mock_scalars = MagicMock()
    mock_scalars.all.return_value = []
    session.scalars = AsyncMock(return_value=mock_scalars)
    return session


async def test_full_report_no_english_labels() -> None:
    """The rendered report must contain no ASCII-only English label words (addendum §4)."""
    forbidden = {"Memory", "Uptime", "Status", "Disk", "RAM", "Units", "Pipeline", "Candidates"}

    mem = {"MemTotal": 8_000_000, "MemAvailable": 6_000_000, "SwapTotal": 0, "SwapFree": 0}
    mock_token = MagicMock(spec=OAuthToken)
    mock_token.expires_at = datetime.now(UTC) + timedelta(hours=100)
    session = _make_full_session(mock_token)

    with (
        patch("hh_monitor.daily_report.run._read_meminfo", return_value=mem),
        patch("hh_monitor.daily_report.run._read_uptime_seconds", return_value=7200.0),
        patch("shutil.disk_usage", return_value=MagicMock(used=100 * 1024**3, total=500 * 1024**3)),
        patch("subprocess.run", return_value=_make_run_result("active")),
        patch("hh_monitor.daily_report.run._check_url", return_value=True),
        patch("hh_monitor.daily_report.run._check_telegram", return_value=True),
        patch("hh_monitor.daily_report.run.settings") as mock_settings,
    ):
        mock_settings.llm_base_url = "https://llm.21-vek.spb.ru/v1"
        mock_settings.telegram_score_threshold = 70
        report = await build_daily_report(session)

    for word in forbidden:
        assert word not in report, f"Forbidden English word '{word}' found in report"


async def test_full_report_no_snapshotword() -> None:
    """'снэпшот' must not appear in any form in the rendered report (AC7)."""
    mem = {"MemTotal": 8_000_000, "MemAvailable": 6_000_000, "SwapTotal": 0, "SwapFree": 0}
    mock_token = MagicMock(spec=OAuthToken)
    mock_token.expires_at = datetime.now(UTC) + timedelta(hours=100)
    session = _make_full_session(mock_token)

    with (
        patch("hh_monitor.daily_report.run._read_meminfo", return_value=mem),
        patch("hh_monitor.daily_report.run._read_uptime_seconds", return_value=7200.0),
        patch("shutil.disk_usage", return_value=MagicMock(used=100 * 1024**3, total=500 * 1024**3)),
        patch("subprocess.run", return_value=_make_run_result("active")),
        patch("hh_monitor.daily_report.run._check_url", return_value=True),
        patch("hh_monitor.daily_report.run._check_telegram", return_value=True),
        patch("hh_monitor.daily_report.run.settings") as mock_settings,
    ):
        mock_settings.llm_base_url = "https://llm.21-vek.spb.ru/v1"
        mock_settings.telegram_score_threshold = 70
        report = await build_daily_report(session)

    assert "снэпшот" not in report


async def test_full_report_header_format() -> None:
    """Report starts with '☀️ hh-monitor: статус на DD.MM.YYYY'."""
    mem = {"MemTotal": 8_000_000, "MemAvailable": 6_000_000, "SwapTotal": 0, "SwapFree": 0}
    mock_token = MagicMock(spec=OAuthToken)
    mock_token.expires_at = datetime.now(UTC) + timedelta(hours=100)
    session = _make_full_session(mock_token)

    with (
        patch("hh_monitor.daily_report.run._read_meminfo", return_value=mem),
        patch("hh_monitor.daily_report.run._read_uptime_seconds", return_value=3600.0),
        patch("shutil.disk_usage", return_value=MagicMock(used=50 * 1024**3, total=500 * 1024**3)),
        patch("subprocess.run", return_value=_make_run_result("active")),
        patch("hh_monitor.daily_report.run._check_url", return_value=True),
        patch("hh_monitor.daily_report.run._check_telegram", return_value=True),
        patch("hh_monitor.daily_report.run.settings") as mock_settings,
    ):
        mock_settings.llm_base_url = "https://llm.21-vek.spb.ru/v1"
        mock_settings.telegram_score_threshold = 70
        report = await build_daily_report(session)

    import re

    assert re.search(r"☀️ hh-monitor: статус на \d{2}\.\d{2}\.\d{4}", report)


async def test_full_report_failed_unit_yields_degraded_verdict() -> None:
    """A failed unit → ⚠️ verdict, no exception."""
    mem = {"MemTotal": 8_000_000, "MemAvailable": 6_000_000, "SwapTotal": 0, "SwapFree": 0}
    mock_token = MagicMock(spec=OAuthToken)
    mock_token.expires_at = datetime.now(UTC) + timedelta(hours=100)
    session = _make_full_session(mock_token)

    def mock_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "hh-monitor-bot.service" in cmd:
            return _make_run_result("failed")
        return _make_run_result("active")

    with (
        patch("hh_monitor.daily_report.run._read_meminfo", return_value=mem),
        patch("hh_monitor.daily_report.run._read_uptime_seconds", return_value=3600.0),
        patch("shutil.disk_usage", return_value=MagicMock(used=50 * 1024**3, total=500 * 1024**3)),
        patch("subprocess.run", side_effect=mock_run),
        patch("hh_monitor.daily_report.run._check_url", return_value=True),
        patch("hh_monitor.daily_report.run._check_telegram", return_value=True),
        patch("hh_monitor.daily_report.run.settings") as mock_settings,
    ):
        mock_settings.llm_base_url = "https://llm.21-vek.spb.ru/v1"
        mock_settings.telegram_score_threshold = 70
        report = await build_daily_report(session)

    assert "⚠️ Есть проблемы — детали ниже" in report


async def test_full_report_failed_run_yields_degraded_verdict() -> None:
    """Failed last parser run → ⚠️ verdict, Прогон in compact block."""
    mem = {"MemTotal": 8_000_000, "MemAvailable": 6_000_000, "SwapTotal": 0, "SwapFree": 0}
    mock_token = MagicMock(spec=OAuthToken)
    mock_token.expires_at = datetime.now(UTC) + timedelta(hours=100)

    mock_run = MagicMock(spec=ParserRun)
    mock_run.id = 10
    mock_run.started_at = datetime(2026, 6, 11, 7, 0, tzinfo=UTC)
    mock_run.status = "failed"
    mock_run.resumes_seen = 0
    mock_run.resumes_viewed = 0
    mock_run.snapshots_skipped = 0

    session = AsyncMock()
    session.scalar = AsyncMock(side_effect=[mock_run, 0, 0, 0, mock_token])
    mock_exec = MagicMock()
    mock_exec.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=mock_exec)
    mock_scalars = MagicMock()
    mock_scalars.all.return_value = []
    session.scalars = AsyncMock(return_value=mock_scalars)

    with (
        patch("hh_monitor.daily_report.run._read_meminfo", return_value=mem),
        patch("hh_monitor.daily_report.run._read_uptime_seconds", return_value=3600.0),
        patch("shutil.disk_usage", return_value=MagicMock(used=50 * 1024**3, total=500 * 1024**3)),
        patch("subprocess.run", return_value=_make_run_result("active")),
        patch("hh_monitor.daily_report.run._check_url", return_value=True),
        patch("hh_monitor.daily_report.run._check_telegram", return_value=True),
        patch("hh_monitor.daily_report.run.settings") as mock_settings,
    ):
        mock_settings.llm_base_url = "https://llm.21-vek.spb.ru/v1"
        mock_settings.telegram_score_threshold = 70
        report = await build_daily_report(session)

    assert "⚠️ Есть проблемы — детали ниже" in report
    assert "🔴" in report  # failed status visible
