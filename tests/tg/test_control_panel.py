"""Tests for hh_monitor.tg.control_panel — DM control panel handlers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import ReplyKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from hh_monitor.tg.control_panel import (
    _CP_THRESHOLD_FSM_TTL,
    _cp_threshold_fsm,
    _CpThresholdFsmState,
    _IsCpThresholdReply,
    _main_menu_keyboard,
    handle_cp_archive_request,
    handle_cp_cancel_archive,
    handle_cp_confirm_archive,
    handle_cp_detail,
    handle_cp_resume,
    handle_cp_stop,
    handle_cp_threshold_button,
    handle_cp_threshold_reply,
    handle_dm_active,
    handle_dm_help,
    handle_dm_settings,
    handle_dm_start,
    handle_dm_stats,
)
from tests.tg.conftest import (
    make_callback as _cb,
)
from tests.tg.conftest import (
    make_message as _msg,
)
from tests.tg.conftest import (
    session_factory_from as _sf,
)

# ── Router registration order ─────────────────────────────────────────────────


def test_cp_router_registered_before_admin() -> None:
    """cp_router must be included before admin_router and screening router."""
    import inspect

    from hh_monitor.tg.client import register_tg_routers

    src = inspect.getsource(register_tg_routers)
    cp_call = "include_router(cp_router)"
    admin_call = "include_router(admin_router)"
    assert cp_call in src, f"'{cp_call}' not found in register_tg_routers"
    assert admin_call in src, f"'{admin_call}' not found in register_tg_routers"
    assert src.index(cp_call) < src.index(admin_call), (
        "cp_router must be registered before admin_router"
    )


# ── /start ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_sends_reply_keyboard() -> None:
    msg = _msg("/start")
    await handle_dm_start(msg)  # type: ignore[arg-type]
    msg.answer.assert_called_once()
    keyboard = msg.answer.call_args[1]["reply_markup"]
    assert isinstance(keyboard, ReplyKeyboardMarkup)


@pytest.mark.asyncio
async def test_start_keyboard_has_four_buttons() -> None:
    keyboard = _main_menu_keyboard()
    all_texts = [btn.text for row in keyboard.keyboard for btn in row]
    assert "🆕 Добавить вакансию" not in all_texts, "add-vacancy must be hidden in DM (CC-12)"
    assert "📋 Активные вакансии" in all_texts
    assert "📊 Статистика" in all_texts
    assert "⚙️ Настройки" in all_texts
    assert "❓ Помощь" in all_texts
    assert len(all_texts) == 4


@pytest.mark.asyncio
async def test_start_keyboard_is_persistent() -> None:
    keyboard = _main_menu_keyboard()
    assert keyboard.persistent is True
    assert keyboard.resize_keyboard is True


# ── helpers ───────────────────────────────────────────────────────────────────


def _make_active_session(rows: list[tuple[object, ...]]) -> AsyncMock:
    """Session whose first execute().fetchall() returns mock rows."""
    mock_rows = []
    for r in rows:
        row = MagicMock()
        row.id, row.position_name, row.position_code, row.active = r[0], r[1], r[2], r[3]
        row.total, row.week7, row.avg_score = r[4], r[5], r[6]
        mock_rows.append(row)
    mock_result = MagicMock()
    mock_result.fetchall.return_value = mock_rows
    mock_session = AsyncMock(spec=AsyncSession)
    mock_session.execute = AsyncMock(return_value=mock_result)
    return mock_session


# ── 📋 Активные вакансии ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cp_active_sends_one_message_per_search() -> None:
    msg = _msg("📋 Активные вакансии")
    rows = [
        (1, "Директор филиала", "branch_director", True, 10, 3, 72),
        (2, "Бухгалтер", "accountant", False, 5, 0, 65),
    ]
    mock_session = _make_active_session(rows)
    with patch(
        "hh_monitor.tg.control_panel.get_session_factory",
        return_value=_sf(mock_session),
    ):
        await handle_dm_active(msg)  # type: ignore[arg-type]
    assert msg.answer.call_count == 2


@pytest.mark.asyncio
async def test_cp_active_empty_shows_no_searches_message() -> None:
    msg = _msg("📋 Активные вакансии")
    mock_session = _make_active_session([])
    with patch(
        "hh_monitor.tg.control_panel.get_session_factory",
        return_value=_sf(mock_session),
    ):
        await handle_dm_active(msg)  # type: ignore[arg-type]
    msg.answer.assert_called_once()
    assert "Нет активных" in msg.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_cp_active_card_active_search_has_cp_stop_button() -> None:
    msg = _msg("📋 Активные вакансии")
    rows = [(1, "Директор", "dir", True, 5, 1, 70)]
    mock_session = _make_active_session(rows)
    with patch(
        "hh_monitor.tg.control_panel.get_session_factory",
        return_value=_sf(mock_session),
    ):
        await handle_dm_active(msg)  # type: ignore[arg-type]
    keyboard = msg.answer.call_args[1]["reply_markup"]
    all_data = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert "cp:stop:1" in all_data
    assert "cp:resume:1" not in all_data
    assert "adm:stop:1" not in all_data


@pytest.mark.asyncio
async def test_cp_active_card_paused_search_has_cp_resume_button() -> None:
    msg = _msg("📋 Активные вакансии")
    rows = [(2, "Бухгалтер", "acc", False, 5, 0, 60)]
    mock_session = _make_active_session(rows)
    with patch(
        "hh_monitor.tg.control_panel.get_session_factory",
        return_value=_sf(mock_session),
    ):
        await handle_dm_active(msg)  # type: ignore[arg-type]
    keyboard = msg.answer.call_args[1]["reply_markup"]
    all_data = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert "cp:resume:2" in all_data
    assert "cp:stop:2" not in all_data


# ── 📊 Статистика ─────────────────────────────────────────────────────────────


def _make_stats_session() -> AsyncMock:
    """Session whose execute side_effect returns different mocks for each query."""
    periods_row = MagicMock()
    periods_row.h24, periods_row.d7, periods_row.d30 = 3, 10, 42

    pos_row = MagicMock()
    pos_row.position_code, pos_row.h24, pos_row.d7, pos_row.d30 = "underwriter", 1, 5, 20

    parser_row = MagicMock()
    parser_row.success, parser_row.failures, parser_row.quota = 7, 1, 0

    reason_row = MagicMock()
    reason_row.reason_code, reason_row.cnt = "relevant_exp", 5

    results = []
    for row, method in [
        (periods_row, "fetchone"),
        (pos_row, "fetchall"),
        (parser_row, "fetchone"),
        (reason_row, "fetchall"),
    ]:
        r = MagicMock()
        if method == "fetchone":
            r.fetchone.return_value = row
        else:
            r.fetchall.return_value = [row]
        results.append(r)

    # get_current_threshold also calls execute; add a threshold row
    threshold_result = MagicMock()
    threshold_result.scalar_one_or_none.return_value = "75"
    results.append(threshold_result)

    mock_session = AsyncMock(spec=AsyncSession)
    mock_session.execute = AsyncMock(side_effect=results)
    return mock_session


@pytest.mark.asyncio
async def test_stats_renders_periods_and_parser_runs() -> None:
    msg = _msg("📊 Статистика")
    mock_session = _make_stats_session()
    with patch(
        "hh_monitor.tg.control_panel.get_session_factory",
        return_value=_sf(mock_session),
    ):
        await handle_dm_stats(msg)  # type: ignore[arg-type]
    msg.answer.assert_called_once()
    text_out: str = msg.answer.call_args[0][0]
    assert "24ч: 3" in text_out
    assert "7д: 10" in text_out
    assert "успешно=7" in text_out
    assert "ошибки=1" in text_out


@pytest.mark.asyncio
async def test_stats_renders_top_reasons_and_threshold() -> None:
    msg = _msg("📊 Статистика")
    mock_session = _make_stats_session()
    with patch(
        "hh_monitor.tg.control_panel.get_session_factory",
        return_value=_sf(mock_session),
    ):
        await handle_dm_stats(msg)  # type: ignore[arg-type]
    text_out: str = msg.answer.call_args[0][0]
    assert "Релевантный опыт" in text_out
    assert "75" in text_out


# ── ⚙️ Настройки ─────────────────────────────────────────────────────────────


def _make_threshold_session(threshold: str = "70") -> AsyncMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = threshold
    mock_session = AsyncMock(spec=AsyncSession)
    mock_session.execute = AsyncMock(return_value=result)
    return mock_session


@pytest.mark.asyncio
async def test_settings_shows_threshold_value() -> None:
    msg = _msg("⚙️ Настройки")
    mock_session = _make_threshold_session("80")
    with (
        patch(
            "hh_monitor.tg.control_panel.get_session_factory",
            return_value=_sf(mock_session),
        ),
        patch("hh_monitor.tg.control_panel.settings") as mock_cfg,
    ):
        mock_cfg.telegram_admin_user_ids = "111"
        mock_cfg.weekly_digest_cron = "0 9 * * 1"
        mock_cfg.weekly_digest_tz = "Europe/Moscow"
        await handle_dm_settings(msg)  # type: ignore[arg-type]
    text_out: str = msg.answer.call_args[0][0]
    assert "80" in text_out


@pytest.mark.asyncio
async def test_settings_admin_sees_change_threshold_button() -> None:
    msg = _msg("⚙️ Настройки", user_id=42)
    mock_session = _make_threshold_session()
    with (
        patch(
            "hh_monitor.tg.control_panel.get_session_factory",
            return_value=_sf(mock_session),
        ),
        patch("hh_monitor.tg.control_panel.is_admin", return_value=True),
        patch("hh_monitor.tg.control_panel.settings") as mock_cfg,
    ):
        mock_cfg.telegram_admin_user_ids = "42"
        mock_cfg.weekly_digest_cron = "0 9 * * 1"
        mock_cfg.weekly_digest_tz = "UTC"
        await handle_dm_settings(msg)  # type: ignore[arg-type]
    keyboard = msg.answer.call_args[1].get("reply_markup")
    assert keyboard is not None
    all_data = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert "cp:threshold" in all_data


@pytest.mark.asyncio
async def test_settings_non_admin_no_inline_keyboard() -> None:
    msg = _msg("⚙️ Настройки", user_id=999)
    mock_session = _make_threshold_session()
    with (
        patch(
            "hh_monitor.tg.control_panel.get_session_factory",
            return_value=_sf(mock_session),
        ),
        patch("hh_monitor.tg.control_panel.is_admin", return_value=False),
        patch("hh_monitor.tg.control_panel.settings") as mock_cfg,
    ):
        mock_cfg.telegram_admin_user_ids = "42"
        mock_cfg.weekly_digest_cron = "0 9 * * 1"
        mock_cfg.weekly_digest_tz = "UTC"
        await handle_dm_settings(msg)  # type: ignore[arg-type]
    keyboard = msg.answer.call_args[1].get("reply_markup")
    assert keyboard is None


# ── ❓ Помощь ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_help_lists_all_sections() -> None:
    msg = _msg("❓ Помощь")
    await handle_dm_help(msg)  # type: ignore[arg-type]
    text_out: str = msg.answer.call_args[0][0]
    for section in ["Добавить вакансию", "Активные вакансии", "Статистика", "Настройки"]:
        assert section in text_out


# ── cp:detail ─────────────────────────────────────────────────────────────────


def _make_detail_session() -> AsyncMock:
    search_row = MagicMock()
    search_row.position_name = "Андеррайтер"
    search_row.position_code = "underwriter"
    search_row.active = True
    search_row.archived_at = None
    search_row.created_at = datetime(2026, 1, 1, tzinfo=UTC)

    counts_row = MagicMock()
    counts_row.total, counts_row.d7, counts_row.d30 = 50, 5, 20

    score_row = MagicMock()
    score_row.s45, score_row.s60, score_row.s70, score_row.s80, score_row.s90 = 3, 2, 10, 15, 8

    llm_row = MagicMock()
    llm_row.enriched, llm_row.pending = 40, 5

    parser_row = MagicMock()
    parser_row.started_at = datetime(2026, 5, 27, 10, 0, tzinfo=UTC)
    parser_row.status = "ok"
    parser_row.resumes_seen = 100
    parser_row.snapshots_inserted = 3
    parser_row.error = None

    reason_row = MagicMock()
    reason_row.reason_code = "relevant_exp"
    reason_row.cnt = 8

    results = []
    for row, method in [
        (search_row, "fetchone"),
        (counts_row, "fetchone"),
        (score_row, "fetchone"),
        (llm_row, "fetchone"),
        (parser_row, "fetchone"),
        (reason_row, "fetchall"),
    ]:
        r = MagicMock()
        if method == "fetchone":
            r.fetchone.return_value = row
        else:
            r.fetchall.return_value = [row]
        results.append(r)

    mock_session = AsyncMock(spec=AsyncSession)
    mock_session.execute = AsyncMock(side_effect=results)
    return mock_session


@pytest.mark.asyncio
async def test_cp_detail_renders_search_info() -> None:
    cb = _cb("cp:detail:1")
    mock_session = _make_detail_session()
    with patch(
        "hh_monitor.tg.control_panel.get_session_factory",
        return_value=_sf(mock_session),
    ):
        await handle_cp_detail(cb)  # type: ignore[arg-type]
    cb.message.answer.assert_called_once()
    text_out: str = cb.message.answer.call_args[0][0]
    assert "Андеррайтер" in text_out
    assert "underwriter" in text_out
    assert "50" in text_out  # total
    assert "Релевантный опыт" in text_out


@pytest.mark.asyncio
async def test_cp_detail_not_found() -> None:
    cb = _cb("cp:detail:999")
    not_found_result = MagicMock()
    not_found_result.fetchone.return_value = None
    mock_session = AsyncMock(spec=AsyncSession)
    mock_session.execute = AsyncMock(return_value=not_found_result)
    with patch(
        "hh_monitor.tg.control_panel.get_session_factory",
        return_value=_sf(mock_session),
    ):
        await handle_cp_detail(cb)  # type: ignore[arg-type]
    cb.answer.assert_called_once()
    args, kwargs = cb.answer.call_args
    assert kwargs.get("show_alert") is True
    assert "не найден" in args[0]


# ── cp:stop ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cp_stop_non_admin_denied() -> None:
    cb = _cb("cp:stop:1", user_id=999)
    with patch("hh_monitor.tg.control_panel.is_admin", return_value=False):
        await handle_cp_stop(cb)  # type: ignore[arg-type]
    cb.answer.assert_called_once()
    args, kwargs = cb.answer.call_args
    assert kwargs.get("show_alert") is True
    assert "Только администраторы" in args[0]


@pytest.mark.asyncio
async def test_cp_stop_admin_updates_and_rerenders() -> None:
    cb = _cb("cp:stop:1", user_id=42)

    update_result = MagicMock()
    update_result.fetchall.return_value = [MagicMock()]

    fetch_row = MagicMock()
    fetch_row.id = 1
    fetch_row.position_name = "Директор"
    fetch_row.position_code = "dir"
    fetch_row.active = False
    fetch_row.total, fetch_row.week7, fetch_row.avg_score = 5, 1, 70
    fetch_result = MagicMock()
    fetch_result.fetchone.return_value = fetch_row

    mock_session = AsyncMock(spec=AsyncSession)
    mock_session.execute = AsyncMock(side_effect=[update_result, fetch_result])
    mock_session.commit = AsyncMock()

    with (
        patch("hh_monitor.tg.control_panel.is_admin", return_value=True),
        patch(
            "hh_monitor.tg.control_panel.get_session_factory",
            return_value=_sf(mock_session),
        ),
    ):
        await handle_cp_stop(cb)  # type: ignore[arg-type]

    cb.message.edit_text.assert_called_once()
    cb.answer.assert_called_once()


@pytest.mark.asyncio
async def test_cp_stop_stale_state_alerts() -> None:
    cb = _cb("cp:stop:1", user_id=42)
    empty_result = MagicMock()
    empty_result.fetchall.return_value = []
    mock_session = AsyncMock(spec=AsyncSession)
    mock_session.execute = AsyncMock(return_value=empty_result)
    mock_session.commit = AsyncMock()
    with (
        patch("hh_monitor.tg.control_panel.is_admin", return_value=True),
        patch(
            "hh_monitor.tg.control_panel.get_session_factory",
            return_value=_sf(mock_session),
        ),
    ):
        await handle_cp_stop(cb)  # type: ignore[arg-type]
    args, kwargs = cb.answer.call_args
    assert kwargs.get("show_alert") is True


# ── cp:resume ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cp_resume_non_admin_denied() -> None:
    cb = _cb("cp:resume:1", user_id=999)
    with patch("hh_monitor.tg.control_panel.is_admin", return_value=False):
        await handle_cp_resume(cb)  # type: ignore[arg-type]
    args, kwargs = cb.answer.call_args
    assert kwargs.get("show_alert") is True
    assert "Только администраторы" in args[0]


@pytest.mark.asyncio
async def test_cp_resume_admin_updates_and_rerenders() -> None:
    cb = _cb("cp:resume:2", user_id=42)

    update_result = MagicMock()
    update_result.fetchall.return_value = [MagicMock()]

    fetch_row = MagicMock()
    fetch_row.id = 2
    fetch_row.position_name = "Бухгалтер"
    fetch_row.position_code = "acc"
    fetch_row.active = True
    fetch_row.total, fetch_row.week7, fetch_row.avg_score = 3, 0, 65
    fetch_result = MagicMock()
    fetch_result.fetchone.return_value = fetch_row

    mock_session = AsyncMock(spec=AsyncSession)
    mock_session.execute = AsyncMock(side_effect=[update_result, fetch_result])
    mock_session.commit = AsyncMock()

    with (
        patch("hh_monitor.tg.control_panel.is_admin", return_value=True),
        patch(
            "hh_monitor.tg.control_panel.get_session_factory",
            return_value=_sf(mock_session),
        ),
    ):
        await handle_cp_resume(cb)  # type: ignore[arg-type]

    cb.message.edit_text.assert_called_once()
    cb.answer.assert_called_once()


# ── cp:archive flow ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cp_archive_non_admin_denied() -> None:
    cb = _cb("cp:archive:1", user_id=999)
    with patch("hh_monitor.tg.control_panel.is_admin", return_value=False):
        await handle_cp_archive_request(cb)  # type: ignore[arg-type]
    args, kwargs = cb.answer.call_args
    assert kwargs.get("show_alert") is True
    assert "Только администраторы" in args[0]


@pytest.mark.asyncio
async def test_cp_archive_request_shows_confirmation() -> None:
    cb = _cb("cp:archive:1", user_id=42)
    name_result = MagicMock()
    name_row = MagicMock()
    name_row.position_name = "Директор"
    name_result.fetchone.return_value = name_row
    mock_session = AsyncMock(spec=AsyncSession)
    mock_session.execute = AsyncMock(return_value=name_result)
    with (
        patch("hh_monitor.tg.control_panel.is_admin", return_value=True),
        patch(
            "hh_monitor.tg.control_panel.get_session_factory",
            return_value=_sf(mock_session),
        ),
    ):
        await handle_cp_archive_request(cb)  # type: ignore[arg-type]
    cb.message.edit_text.assert_called_once()
    edit_text: str = cb.message.edit_text.call_args[0][0]
    assert "Директор" in edit_text
    assert "безвозвратное" in edit_text
    keyboard = cb.message.edit_text.call_args[1]["reply_markup"]
    all_data = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert "cp:yes_arch:1" in all_data
    assert "cp:no_arch:1" in all_data


@pytest.mark.asyncio
async def test_cp_confirm_archive_updates_db() -> None:
    cb = _cb("cp:yes_arch:1", user_id=42)
    arch_result = MagicMock()
    arch_row = MagicMock()
    arch_row.position_name = "Директор"
    arch_result.fetchone.return_value = arch_row
    mock_session = AsyncMock(spec=AsyncSession)
    mock_session.execute = AsyncMock(return_value=arch_result)
    mock_session.commit = AsyncMock()
    with (
        patch("hh_monitor.tg.control_panel.is_admin", return_value=True),
        patch(
            "hh_monitor.tg.control_panel.get_session_factory",
            return_value=_sf(mock_session),
        ),
    ):
        await handle_cp_confirm_archive(cb)  # type: ignore[arg-type]
    mock_session.commit.assert_called_once()
    cb.message.edit_text.assert_called_once()
    edit_text: str = cb.message.edit_text.call_args[0][0]
    assert "архивирован" in edit_text


@pytest.mark.asyncio
async def test_cp_cancel_archive_rerenders_card() -> None:
    cb = _cb("cp:no_arch:1", user_id=42)
    fetch_row = MagicMock()
    fetch_row.id = 1
    fetch_row.position_name = "Директор"
    fetch_row.position_code = "dir"
    fetch_row.active = True
    fetch_row.total, fetch_row.week7, fetch_row.avg_score = 5, 1, 70
    fetch_result = MagicMock()
    fetch_result.fetchone.return_value = fetch_row
    mock_session = AsyncMock(spec=AsyncSession)
    mock_session.execute = AsyncMock(return_value=fetch_result)
    with (
        patch("hh_monitor.tg.control_panel.is_admin", return_value=True),
        patch(
            "hh_monitor.tg.control_panel.get_session_factory",
            return_value=_sf(mock_session),
        ),
    ):
        await handle_cp_cancel_archive(cb)  # type: ignore[arg-type]
    cb.message.edit_text.assert_called_once()
    keyboard = cb.message.edit_text.call_args[1]["reply_markup"]
    all_data = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert "cp:stop:1" in all_data


# ── cp:threshold FSM ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cp_threshold_button_non_admin_denied() -> None:
    cb = _cb("cp:threshold", user_id=999)
    with patch("hh_monitor.tg.control_panel.is_admin", return_value=False):
        await handle_cp_threshold_button(cb)  # type: ignore[arg-type]
    args, kwargs = cb.answer.call_args
    assert kwargs.get("show_alert") is True
    assert "Только администраторы" in args[0]


@pytest.mark.asyncio
async def test_cp_threshold_button_sends_forcereply() -> None:
    cb = _cb("cp:threshold", user_id=42)
    _cp_threshold_fsm.clear()
    with patch("hh_monitor.tg.control_panel.is_admin", return_value=True):
        await handle_cp_threshold_button(cb)  # type: ignore[arg-type]
    cb.message.answer.assert_called_once()
    assert 42 in _cp_threshold_fsm
    _cp_threshold_fsm.clear()


@pytest.mark.asyncio
async def test_cp_threshold_reply_valid_updates_config() -> None:
    msg = _msg("75", user_id=42, reply_to_id=888)
    _cp_threshold_fsm[42] = _CpThresholdFsmState(
        prompt_message_id=888,
        chat_id=1234,
        created_at=datetime.now(UTC),
    )

    threshold_result = MagicMock()
    threshold_result.scalar_one_or_none.return_value = "70"
    upsert_result = MagicMock()
    mock_session = AsyncMock(spec=AsyncSession)
    mock_session.execute = AsyncMock(side_effect=[threshold_result, upsert_result])
    mock_session.commit = AsyncMock()

    with patch(
        "hh_monitor.tg.control_panel.get_session_factory",
        return_value=_sf(mock_session),
    ):
        await handle_cp_threshold_reply(msg)  # type: ignore[arg-type]

    msg.reply.assert_called_once()
    assert "75" in msg.reply.call_args[0][0]
    assert 42 not in _cp_threshold_fsm


@pytest.mark.asyncio
async def test_cp_threshold_reply_invalid_keeps_state() -> None:
    msg = _msg("abc", user_id=42, reply_to_id=888)
    state = _CpThresholdFsmState(
        prompt_message_id=888,
        chat_id=1234,
        created_at=datetime.now(UTC),
    )
    _cp_threshold_fsm[42] = state

    await handle_cp_threshold_reply(msg)  # type: ignore[arg-type]

    msg.reply.assert_called_once()
    assert "60 до 100" in msg.reply.call_args[0][0]
    assert 42 in _cp_threshold_fsm
    _cp_threshold_fsm.clear()


@pytest.mark.asyncio
async def test_cp_threshold_reply_out_of_range_keeps_state() -> None:
    msg = _msg("50", user_id=42, reply_to_id=888)
    _cp_threshold_fsm[42] = _CpThresholdFsmState(
        prompt_message_id=888,
        chat_id=1234,
        created_at=datetime.now(UTC),
    )

    await handle_cp_threshold_reply(msg)  # type: ignore[arg-type]

    assert 42 in _cp_threshold_fsm
    _cp_threshold_fsm.clear()


@pytest.mark.asyncio
async def test_cp_threshold_reply_ttl_expired() -> None:
    msg = _msg("75", user_id=42, reply_to_id=888)
    expired_time = datetime.now(UTC) - _CP_THRESHOLD_FSM_TTL - timedelta(seconds=1)
    _cp_threshold_fsm[42] = _CpThresholdFsmState(
        prompt_message_id=888,
        chat_id=1234,
        created_at=expired_time,
    )

    await handle_cp_threshold_reply(msg)  # type: ignore[arg-type]

    msg.reply.assert_called_once()
    assert "истекла" in msg.reply.call_args[0][0]
    assert 42 not in _cp_threshold_fsm


@pytest.mark.asyncio
async def test_is_cp_threshold_reply_filter() -> None:
    msg_with_reply = _msg("75", user_id=42, reply_to_id=888)
    _cp_threshold_fsm[42] = _CpThresholdFsmState(
        prompt_message_id=888,
        chat_id=1234,
        created_at=datetime.now(UTC),
    )
    result = await _IsCpThresholdReply()(msg_with_reply)  # type: ignore[arg-type]
    assert result is True

    msg_no_reply = _msg("75", user_id=42)
    result2 = await _IsCpThresholdReply()(msg_no_reply)  # type: ignore[arg-type]
    assert result2 is False

    _cp_threshold_fsm.clear()


@pytest.mark.asyncio
async def test_ttl_constant_matches_commands() -> None:
    """Verify DM panel uses the same FSM TTL as the HR group panel."""
    from hh_monitor.tg.commands import _THRESHOLD_FSM_TTL as adm_ttl

    assert adm_ttl == _CP_THRESHOLD_FSM_TTL
