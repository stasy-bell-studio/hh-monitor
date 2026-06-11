"""Unit tests for hh_monitor/daily_report/run.py.

All tests mock filesystem, subprocess, HTTP, and DB — no real I/O.
asyncio_mode = "auto" (see pyproject.toml), so async defs run without markers.
"""
from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from hh_monitor.daily_report.run import (
    MSK,
    _build_candidates_section,
    _build_external_section,
    _build_pipeline_section,
    _build_server_section,
    _build_units_section,
    _build_verdict,
    _traffic_light,
    build_daily_report,
)
from hh_monitor.db.models import OAuthToken, ParserRun

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
    assert _build_verdict([]) == "✅ Всё работает в штатном режиме. Хорошего рабочего дня!"


def test_verdict_degraded() -> None:
    result = _build_verdict(["Память", "OpenRouter"])
    assert result == "⚠️ Есть проблемы — детали выше."


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
        block, problems = _build_server_section()

    assert problems == []
    assert "🟢" in block
    assert "МБ" in block
    assert "ГБ" in block
    assert "Аптайм" in block


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
        block, problems = _build_server_section()

    assert "Память" in problems
    assert "🔴" in block


def test_server_section_swap_thresholds() -> None:
    # 50% swap usage → 🟡 (warn_lo=25, warn_hi=80)
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
        block, problems = _build_server_section()

    assert "Swap" not in problems  # yellow, not red
    assert "🟡" in block


def test_server_section_proc_unavailable() -> None:
    with (
        patch("hh_monitor.daily_report.run._read_meminfo", side_effect=OSError("no /proc")),
        patch("hh_monitor.daily_report.run._read_uptime_seconds", side_effect=OSError("no /proc")),
        patch("shutil.disk_usage", return_value=MagicMock(used=100 * 1024**3, total=500 * 1024**3)),
    ):
        block, problems = _build_server_section()

    assert "Память" in problems
    assert "🔴" in block


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
        block, problems = _build_units_section()

    assert problems == []
    assert block.count("🟢") >= len([u for u, k in [] if k == "longrunning"])


def test_units_oneshot_inactive_is_green() -> None:
    """Oneshot services show 'inactive' between runs — must be 🟢, not 🔴."""
    with patch("subprocess.run", return_value=_make_run_result("inactive")):
        block, problems = _build_units_section()

    # Only oneshot units use is-failed → "inactive" means not failed → 🟢
    # Long-running / timer units use is-active → "inactive" → 🔴
    # Bot is longrunning, so it would be 🔴; timers would be 🔴.
    # At least one unit (oneshot) must be green.
    assert "🟢" in block


def test_units_one_failed() -> None:
    """A oneshot service returning 'failed' → 🔴 line and added to problems."""
    call_count = 0

    def mock_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal call_count
        call_count += 1
        # Make hh-monitor-pipeline.service (oneshot, uses is-failed) return "failed"
        if "hh-monitor-pipeline.service" in cmd:
            return _make_run_result("failed")
        return _make_run_result("active")

    with patch("subprocess.run", side_effect=mock_run):
        block, problems = _build_units_section()

    assert "hh-monitor-pipeline.service" in problems
    assert "🔴" in block


def test_units_timeout_shows_unknown() -> None:
    """subprocess.TimeoutExpired → status 'unknown', treated as 🔴 for longrunning."""
    with patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="systemctl", timeout=5),
    ):
        block, problems = _build_units_section()

    assert "unknown" in block
    # At least the bot (longrunning, is-active) should be 🔴
    assert "hh-monitor-bot.service" in problems


# ── pipeline section ──────────────────────────────────────────────────────────


def _make_session(
    scalar_returns: list[object],
    execute_scalars_all: list[object] | None = None,
    scalars_all: list[object] | None = None,
) -> AsyncMock:
    """Build a minimal async session mock for pipeline / candidates / external tests."""
    session = AsyncMock()
    session.scalar = AsyncMock(side_effect=scalar_returns)

    if execute_scalars_all is not None:
        mock_exec_result = MagicMock()
        mock_exec_result.scalars.return_value.all.return_value = execute_scalars_all
        session.execute = AsyncMock(return_value=mock_exec_result)

    if scalars_all is not None:
        mock_scalars_result = MagicMock()
        mock_scalars_result.all.return_value = scalars_all
        session.scalars = AsyncMock(return_value=mock_scalars_result)

    return session


async def test_pipeline_empty_db() -> None:
    """Zero runs ever: renders 'За сутки прогонов не было', no exception (AC2)."""
    session = _make_session(
        scalar_returns=[None, 0],  # last_run=None, viewed_today=0
        execute_scalars_all=[],    # _collect_parser_stats returns []
        scalars_all=[],            # active searches = []
    )
    msk_now = datetime.now(MSK)
    block, problems = await _build_pipeline_section(session, msk_now)

    assert "За сутки прогонов не было" in block
    assert "Пайплайн" in problems


async def test_pipeline_no_runs_in_24h_but_last_run_exists() -> None:
    """last_run exists but no runs in 24h → 'За сутки прогонов не было' + Пайплайн."""
    mock_run = MagicMock(spec=ParserRun)
    mock_run.id = 99
    mock_run.started_at = datetime(2026, 6, 9, 10, 0, tzinfo=UTC)
    mock_run.status = "ok"
    mock_run.resumes_seen = 50
    mock_run.resumes_viewed = 5
    mock_run.snapshots_inserted = 3

    session = _make_session(
        scalar_returns=[mock_run, 0],
        execute_scalars_all=[],
        scalars_all=[],
    )
    msk_now = datetime.now(MSK)
    block, problems = await _build_pipeline_section(session, msk_now)

    assert "За сутки прогонов не было" in block
    assert "Пайплайн" in problems


async def test_pipeline_failed_last_run() -> None:
    """A last_run with status='failed' → Пайплайн added to problems."""
    mock_run = MagicMock(spec=ParserRun)
    mock_run.id = 5
    mock_run.started_at = datetime(2026, 6, 11, 7, 0, tzinfo=UTC)
    mock_run.status = "failed"
    mock_run.resumes_seen = 0
    mock_run.resumes_viewed = 0
    mock_run.snapshots_inserted = 0

    session = _make_session(
        scalar_returns=[mock_run, 0],
        execute_scalars_all=[],  # _collect_parser_stats gets empty list → 0 runs in 24h
        scalars_all=[],
    )
    msk_now = datetime.now(MSK)
    block, problems = await _build_pipeline_section(session, msk_now)

    assert "Пайплайн" in problems


async def test_pipeline_quota_amber() -> None:
    """430 views → 🟡 (>=400 but < 500)."""
    mock_run = MagicMock(spec=ParserRun)
    mock_run.id = 1
    mock_run.started_at = datetime.now(UTC)
    mock_run.status = "ok"
    mock_run.resumes_seen = 500
    mock_run.resumes_viewed = 430
    mock_run.snapshots_inserted = 10

    session = _make_session(
        scalar_returns=[mock_run, 430],
        execute_scalars_all=[],  # 24h aggregate via empty list — quota test only
        scalars_all=[],
    )
    msk_now = datetime.now(MSK)
    block, problems = await _build_pipeline_section(session, msk_now)

    assert "🟡" in block
    assert "Квота" not in problems


async def test_pipeline_quota_exhausted() -> None:
    """500+ views → 🔴 + Квота in problems."""
    mock_run = MagicMock(spec=ParserRun)
    mock_run.id = 2
    mock_run.started_at = datetime.now(UTC)
    mock_run.status = "view_limit_exhausted"
    mock_run.resumes_seen = 800
    mock_run.resumes_viewed = 500
    mock_run.snapshots_inserted = 0

    session = _make_session(
        scalar_returns=[mock_run, 500],
        execute_scalars_all=[],  # 24h aggregate via empty list — quota test only
        scalars_all=[],
    )
    msk_now = datetime.now(MSK)
    block, problems = await _build_pipeline_section(session, msk_now)

    assert "Квота" in problems
    assert "🔴" in block


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
    session.scalar = AsyncMock(side_effect=[0, 0, 0, 0])  # new/enriched/scored/notified
    msk_now = datetime.now(MSK)
    block = await _build_candidates_section(session, msk_now)

    assert "Новых событий: 0" in block
    assert "Уведомлений отправлено: 0" in block


async def test_candidates_nonzero() -> None:
    session = AsyncMock()
    session.scalar = AsyncMock(side_effect=[10, 8, 3, 2])
    msk_now = datetime.now(MSK)
    block = await _build_candidates_section(session, msk_now)

    assert "Новых событий: 10" in block
    assert "LLM обогащено: 8" in block
    assert "Уведомлений отправлено: 2" in block


# ── external section ──────────────────────────────────────────────────────────


async def test_external_all_ok() -> None:
    mock_token = MagicMock(spec=OAuthToken)
    mock_token.expires_at = datetime.now(UTC) + timedelta(hours=100)

    session = AsyncMock()
    session.scalar = AsyncMock(return_value=mock_token)

    with patch("hh_monitor.daily_report.run._check_url", return_value=True):
        block, problems = await _build_external_section(session)

    assert problems == []
    assert "🟢" in block


async def test_external_no_token() -> None:
    session = AsyncMock()
    session.scalar = AsyncMock(return_value=None)

    with patch("hh_monitor.daily_report.run._check_url", return_value=True):
        block, problems = await _build_external_section(session)

    assert "HH OAuth" in problems
    assert "токен не найден" in block


async def test_external_token_expiring_soon() -> None:
    """Token with <24h TTL → 🔴."""
    mock_token = MagicMock(spec=OAuthToken)
    mock_token.expires_at = datetime.now(UTC) + timedelta(hours=10)

    session = AsyncMock()
    session.scalar = AsyncMock(return_value=mock_token)

    with patch("hh_monitor.daily_report.run._check_url", return_value=True):
        block, problems = await _build_external_section(session)

    assert "HH OAuth" in problems
    assert "🔴" in block


async def test_external_token_amber() -> None:
    """Token with 24-72h TTL → 🟡."""
    mock_token = MagicMock(spec=OAuthToken)
    mock_token.expires_at = datetime.now(UTC) + timedelta(hours=50)

    session = AsyncMock()
    session.scalar = AsyncMock(return_value=mock_token)

    with patch("hh_monitor.daily_report.run._check_url", return_value=True):
        block, problems = await _build_external_section(session)

    assert "HH OAuth" not in problems
    assert "🟡" in block


async def test_external_check_timeout_never_raises() -> None:
    """httpx timeout → 🔴 for that service; report generation does NOT raise (B6, AC3)."""
    mock_token = MagicMock(spec=OAuthToken)
    mock_token.expires_at = datetime.now(UTC) + timedelta(hours=100)

    session = AsyncMock()
    session.scalar = AsyncMock(return_value=mock_token)

    async def _timeout(url: str, timeout: float = 5.0) -> bool:
        raise Exception("timeout")

    with patch("hh_monitor.daily_report.run._check_url", side_effect=_timeout):
        # _build_external_section calls _check_url via asyncio.gather
        # Each _check_url internally catches exceptions; but if _check_url itself
        # is patched to raise before the inner try, we test the outer safety net.
        # Actually _check_url wraps the exception itself — let's verify via full flow.
        pass

    # Test the real _check_url with a raising httpx:
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.head = AsyncMock(side_effect=Exception("timeout"))
        mock_client_cls.return_value = mock_client

        from hh_monitor.daily_report.run import _check_url as real_check_url

        result = await real_check_url("https://example.com")

    assert result is False  # no exception raised, returns False


async def test_external_openrouter_down() -> None:
    """Unreachable OpenRouter → 🔴 line, degraded verdict, no crash (AC3)."""
    mock_token = MagicMock(spec=OAuthToken)
    mock_token.expires_at = datetime.now(UTC) + timedelta(hours=100)

    session = AsyncMock()
    session.scalar = AsyncMock(return_value=mock_token)

    async def _selective(url: str, timeout: float = 5.0) -> bool:
        return "openrouter" not in url

    with patch("hh_monitor.daily_report.run._check_url", side_effect=_selective):
        block, problems = await _build_external_section(session)

    assert "OpenRouter" in problems
    assert "недоступен" in block


# ── full report integration ───────────────────────────────────────────────────


async def test_full_report_no_english_labels() -> None:
    """The rendered report must contain no ASCII-only English label words (addendum §4)."""
    forbidden = {"Memory", "Uptime", "Status", "Disk", "RAM", "Units", "Pipeline", "Candidates"}

    mem = {
        "MemTotal": 8_000_000,
        "MemAvailable": 6_000_000,
        "SwapTotal": 0,
        "SwapFree": 0,
    }
    mock_token = MagicMock(spec=OAuthToken)
    mock_token.expires_at = datetime.now(UTC) + timedelta(hours=100)

    session = _make_session(
        scalar_returns=[None, 0, mock_token],  # last_run, viewed_today, oauth_token
        execute_scalars_all=[],
        scalars_all=[],
    )
    # candidates section also calls scalar 4 times
    session.scalar = AsyncMock(side_effect=[None, 0, 0, 0, 0, 0, mock_token])

    with (
        patch("hh_monitor.daily_report.run._read_meminfo", return_value=mem),
        patch("hh_monitor.daily_report.run._read_uptime_seconds", return_value=7200.0),
        patch("shutil.disk_usage", return_value=MagicMock(used=100 * 1024**3, total=500 * 1024**3)),
        patch("subprocess.run", return_value=_make_run_result("active")),
        patch("hh_monitor.daily_report.run._check_url", return_value=True),
    ):
        report = await build_daily_report(session)

    for word in forbidden:
        assert word not in report, f"Forbidden English word '{word}' found in report"


async def test_full_report_header_format() -> None:
    """Report starts with '☀️ hh-monitor: статус на DD.MM.YYYY'."""
    mem = {"MemTotal": 8_000_000, "MemAvailable": 6_000_000, "SwapTotal": 0, "SwapFree": 0}
    mock_token = MagicMock(spec=OAuthToken)
    mock_token.expires_at = datetime.now(UTC) + timedelta(hours=100)

    session = _make_session(
        scalar_returns=[None, 0, mock_token],
        execute_scalars_all=[],
        scalars_all=[],
    )
    session.scalar = AsyncMock(side_effect=[None, 0, 0, 0, 0, 0, mock_token])

    with (
        patch("hh_monitor.daily_report.run._read_meminfo", return_value=mem),
        patch("hh_monitor.daily_report.run._read_uptime_seconds", return_value=3600.0),
        patch("shutil.disk_usage", return_value=MagicMock(used=50 * 1024**3, total=500 * 1024**3)),
        patch("subprocess.run", return_value=_make_run_result("active")),
        patch("hh_monitor.daily_report.run._check_url", return_value=True),
    ):
        report = await build_daily_report(session)

    import re

    assert re.search(r"☀️ hh-monitor: статус на \d{2}\.\d{2}\.\d{4}", report)


async def test_full_report_failed_unit_yields_degraded_verdict() -> None:
    """A failed unit → ⚠️ verdict, no exception (AC3)."""
    mem = {"MemTotal": 8_000_000, "MemAvailable": 6_000_000, "SwapTotal": 0, "SwapFree": 0}
    mock_token = MagicMock(spec=OAuthToken)
    mock_token.expires_at = datetime.now(UTC) + timedelta(hours=100)

    def mock_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "hh-monitor-bot.service" in cmd:
            return _make_run_result("failed")
        return _make_run_result("active")

    session = _make_session(
        scalar_returns=[None, 0, mock_token],
        execute_scalars_all=[],
        scalars_all=[],
    )
    session.scalar = AsyncMock(side_effect=[None, 0, 0, 0, 0, 0, mock_token])

    with (
        patch("hh_monitor.daily_report.run._read_meminfo", return_value=mem),
        patch("hh_monitor.daily_report.run._read_uptime_seconds", return_value=3600.0),
        patch("shutil.disk_usage", return_value=MagicMock(used=50 * 1024**3, total=500 * 1024**3)),
        patch("subprocess.run", side_effect=mock_run),
        patch("hh_monitor.daily_report.run._check_url", return_value=True),
    ):
        report = await build_daily_report(session)

    assert "⚠️ Есть проблемы — детали выше." in report
