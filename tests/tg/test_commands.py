"""Tests for hh_monitor.tg.commands — admin panel handlers, guards, FSM."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import Update
from sqlalchemy.ext.asyncio import AsyncSession

from hh_monitor.tg.commands import (
    _AdminTopicFilter,
    _AdminUserFilter,
    _threshold_fsm,
    _ThresholdFsmState,
    handle_active,
    handle_admin_help,
    handle_archive,
    handle_archive_request,
    handle_cancel_archive,
    handle_close,
    handle_confirm_archive,
    handle_detail,
    handle_resume,
    handle_settings,
    handle_stats,
    handle_stop,
    handle_threshold_button,
    handle_threshold_reply,
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

# ── Filter unit tests ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_admin_topic_filter_accepts_correct_topic() -> None:
    msg = _msg("/active", message_thread_id=7)
    with patch("hh_monitor.tg.commands.settings") as mock_settings:
        mock_settings.telegram_admin_topic_id = 7
        result = await _AdminTopicFilter()(msg)  # type: ignore[arg-type]
    assert result is True


@pytest.mark.asyncio
async def test_admin_topic_filter_rejects_wrong_topic() -> None:
    msg = _msg("/active", message_thread_id=9)
    with patch("hh_monitor.tg.commands.settings") as mock_settings:
        mock_settings.telegram_admin_topic_id = 7
        result = await _AdminTopicFilter()(msg)  # type: ignore[arg-type]
    assert result is False


@pytest.mark.asyncio
async def test_admin_topic_filter_rejects_no_topic_configured() -> None:
    msg = _msg("/active", message_thread_id=7)
    with patch("hh_monitor.tg.commands.settings") as mock_settings:
        mock_settings.telegram_admin_topic_id = 0
        result = await _AdminTopicFilter()(msg)  # type: ignore[arg-type]
    assert result is False


@pytest.mark.asyncio
async def test_admin_topic_filter_rejects_none_thread_id() -> None:
    msg = _msg("/active", message_thread_id=None)
    with patch("hh_monitor.tg.commands.settings") as mock_settings:
        mock_settings.telegram_admin_topic_id = 7
        result = await _AdminTopicFilter()(msg)  # type: ignore[arg-type]
    assert result is False


@pytest.mark.asyncio
async def test_admin_user_filter_accepts_admin() -> None:
    msg = _msg("/active", user_id=42)
    with patch("hh_monitor.tg.commands.is_admin", return_value=True):
        result = await _AdminUserFilter()(msg)  # type: ignore[arg-type]
    assert result is True


@pytest.mark.asyncio
async def test_admin_user_filter_rejects_non_admin() -> None:
    msg = _msg("/active", user_id=999)
    with patch("hh_monitor.tg.commands.is_admin", return_value=False):
        result = await _AdminUserFilter()(msg)  # type: ignore[arg-type]
    assert result is False


@pytest.mark.asyncio
async def test_admin_user_filter_rejects_no_user() -> None:
    msg = _msg("/active")
    msg.from_user = None
    result = await _AdminUserFilter()(msg)  # type: ignore[arg-type]
    assert result is False


# ── Callback guard ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_callback_non_admin_gets_no_access() -> None:
    cb = _cb("adm:close", user_id=999)
    with patch("hh_monitor.tg.commands.is_admin", return_value=False):
        await handle_close(cb)  # type: ignore[arg-type]
    cb.answer.assert_called_once()
    args, kwargs = cb.answer.call_args
    assert kwargs.get("show_alert") is True
    assert "Нет прав" in args[0]


# ── /help ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_admin_help_replies_with_commands() -> None:
    msg = _msg("/help")
    await handle_admin_help(msg)  # type: ignore[arg-type]
    msg.answer.assert_called_once()
    text_out: str = msg.answer.call_args[0][0]
    for cmd in ["/active", "/archive", "/stats", "/settings", "/help"]:
        assert cmd in text_out


# ── /active ───────────────────────────────────────────────────────────────────


def _active_rows(rows: list[tuple[object, ...]]) -> MagicMock:
    """Build mock session whose SELECT returns the given rows as RowProxy-like mocks."""
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


@pytest.mark.asyncio
async def test_active_sends_one_message_per_search() -> None:
    msg = _msg("/active")
    rows = [
        (1, "Директор филиала", "branch_director", True, 10, 3, 72),
        (2, "Бухгалтер", "accountant", False, 5, 0, 65),
    ]
    mock_session = _active_rows(rows)

    with patch(
        "hh_monitor.tg.commands.get_session_factory",
        return_value=_sf(mock_session),
    ):
        await handle_active(msg)  # type: ignore[arg-type]

    assert msg.answer.call_count == 2


@pytest.mark.asyncio
async def test_active_empty_shows_no_searches_message() -> None:
    msg = _msg("/active")
    mock_session = _active_rows([])

    with patch(
        "hh_monitor.tg.commands.get_session_factory",
        return_value=_sf(mock_session),
    ):
        await handle_active(msg)  # type: ignore[arg-type]

    msg.answer.assert_called_once()
    assert "Нет активных" in msg.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_active_card_active_search_has_pause_button() -> None:
    msg = _msg("/active")
    rows = [(1, "Директор", "dir", True, 5, 1, 70)]
    mock_session = _active_rows(rows)

    with patch(
        "hh_monitor.tg.commands.get_session_factory",
        return_value=_sf(mock_session),
    ):
        await handle_active(msg)  # type: ignore[arg-type]

    keyboard = msg.answer.call_args[1]["reply_markup"]
    # Collect all callback_data values
    all_data = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert "adm:stop:1" in all_data
    assert "adm:resume:1" not in all_data


@pytest.mark.asyncio
async def test_active_card_paused_search_has_resume_button() -> None:
    msg = _msg("/active")
    rows = [(2, "Бухгалтер", "acc", False, 5, 0, 60)]
    mock_session = _active_rows(rows)

    with patch(
        "hh_monitor.tg.commands.get_session_factory",
        return_value=_sf(mock_session),
    ):
        await handle_active(msg)  # type: ignore[arg-type]

    keyboard = msg.answer.call_args[1]["reply_markup"]
    all_data = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert "adm:resume:2" in all_data
    assert "adm:stop:2" not in all_data


# ── /archive ──────────────────────────────────────────────────────────────────


def _archive_rows(rows: list[tuple[object, ...]]) -> MagicMock:
    mock_rows = []
    for r in rows:
        row = MagicMock()
        row.id, row.position_name, row.position_code = r[0], r[1], r[2]
        row.archived_at, row.total = r[3], r[4]
        mock_rows.append(row)

    mock_result = MagicMock()
    mock_result.fetchall.return_value = mock_rows

    mock_session = AsyncMock(spec=AsyncSession)
    mock_session.execute = AsyncMock(return_value=mock_result)
    return mock_session


@pytest.mark.asyncio
async def test_archive_sends_archived_searches() -> None:
    msg = _msg("/archive")
    archived_at = datetime(2026, 5, 10, tzinfo=UTC)
    rows = [(5, "Старый поиск", "old_pos", archived_at, 20)]
    mock_session = _archive_rows(rows)

    with patch(
        "hh_monitor.tg.commands.get_session_factory",
        return_value=_sf(mock_session),
    ):
        await handle_archive(msg)  # type: ignore[arg-type]

    msg.answer.assert_called_once()
    text_out: str = msg.answer.call_args[0][0]
    assert "Старый поиск" in text_out
    assert "10.05.2026" in text_out


@pytest.mark.asyncio
async def test_archive_empty() -> None:
    msg = _msg("/archive")
    mock_session = _archive_rows([])

    with patch(
        "hh_monitor.tg.commands.get_session_factory",
        return_value=_sf(mock_session),
    ):
        await handle_archive(msg)  # type: ignore[arg-type]

    msg.answer.assert_called_once()
    assert "Нет архивных" in msg.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_archive_card_has_only_detail_and_close() -> None:
    msg = _msg("/archive")
    archived_at = datetime(2026, 5, 10, tzinfo=UTC)
    rows = [(5, "Старый поиск", "old_pos", archived_at, 20)]
    mock_session = _archive_rows(rows)

    with patch(
        "hh_monitor.tg.commands.get_session_factory",
        return_value=_sf(mock_session),
    ):
        await handle_archive(msg)  # type: ignore[arg-type]

    keyboard = msg.answer.call_args[1]["reply_markup"]
    all_data = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert "adm:detail:5" in all_data
    assert "adm:close" in all_data
    # No stop/resume/archive buttons
    assert not any(d.startswith("adm:stop") or d.startswith("adm:resume") for d in all_data)


# ── /stats ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stats_renders_periods_and_histogram() -> None:
    msg = _msg("/stats")

    # Mock all SQL calls in sequence
    periods_row = MagicMock()
    periods_row.h24, periods_row.d7, periods_row.d30 = 5, 30, 100

    histogram_rows = []
    for bucket, cnt in [("0-19", 2), ("60-79", 10), ("80-100", 5)]:
        r = MagicMock()
        r.bucket, r.cnt = bucket, cnt
        histogram_rows.append(r)

    pos_row = MagicMock()
    pos_row.position_code, pos_row.cnt = "branch_director", 15

    reason_row = MagicMock()
    reason_row.reason_code, reason_row.cnt = "relevant_exp", 8

    # Execute returns different results per call
    call_results = [
        MagicMock(**{"fetchone.return_value": periods_row}),  # periods
        MagicMock(**{"fetchall.return_value": [pos_row]}),    # top positions
        MagicMock(**{"fetchall.return_value": histogram_rows}),  # histogram
        MagicMock(**{"fetchall.return_value": [reason_row]}),   # reasons
    ]

    # get_current_threshold is called via sender.get_current_threshold
    call_count = 0

    async def _execute(*args: object, **kwargs: object) -> MagicMock:
        nonlocal call_count
        result = call_results[call_count % len(call_results)]
        call_count += 1
        return result

    mock_session = AsyncMock(spec=AsyncSession)
    mock_session.execute = _execute  # type: ignore[method-assign]

    with (
        patch("hh_monitor.tg.commands.get_session_factory", return_value=_sf(mock_session)),
        patch("hh_monitor.tg.commands.get_current_threshold", return_value=65),
    ):
        await handle_stats(msg)  # type: ignore[arg-type]

    msg.answer.assert_called_once()
    text_out: str = msg.answer.call_args[0][0]
    assert "24ч: 5" in text_out
    assert "7д: 30" in text_out
    assert "65" in text_out  # threshold
    assert "branch_director" in text_out
    assert "relevant_exp" in text_out


# ── /settings ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_settings_shows_threshold_and_cron() -> None:
    msg = _msg("/settings")
    mock_session = AsyncMock(spec=AsyncSession)

    with (
        patch("hh_monitor.tg.commands.get_session_factory", return_value=_sf(mock_session)),
        patch("hh_monitor.tg.commands.get_current_threshold", return_value=70),
    ):
        await handle_settings(msg)  # type: ignore[arg-type]

    msg.answer.assert_called_once()
    text_out: str = msg.answer.call_args[0][0]
    assert "70" in text_out
    # Has the threshold change button
    keyboard = msg.answer.call_args[1]["reply_markup"]
    all_data = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert "adm:threshold" in all_data


# ── Stop / Resume callbacks ───────────────────────────────────────────────────


def _update_result(rows: list[tuple[object, ...]]) -> MagicMock:
    r = MagicMock()
    r.fetchall.return_value = rows
    return r


def _fetch_row_mock(
    search_id: int = 1,
    is_active: bool = False,
) -> MagicMock:
    row = MagicMock()
    row.id, row.position_name, row.position_code = search_id, "Директор", "dir"
    row.active, row.total, row.week7, row.avg_score = is_active, 5, 1, 70
    return row


@pytest.mark.asyncio
async def test_stop_callback_updates_and_rerenders() -> None:
    cb = _cb("adm:stop:1")

    # First execute: UPDATE RETURNING id (success)
    # Second execute: SELECT for re-render
    update_result = _update_result([(1,)])
    fetch_result = MagicMock()
    fetch_result.fetchone.return_value = _fetch_row_mock(1, is_active=False)

    side_effects = [update_result, fetch_result]
    idx = 0

    async def _execute(*args: object, **kwargs: object) -> MagicMock:
        nonlocal idx
        r = side_effects[idx % len(side_effects)]
        idx += 1
        return r

    mock_session = AsyncMock(spec=AsyncSession)
    mock_session.execute = _execute  # type: ignore[method-assign]
    mock_session.commit = AsyncMock()

    with (
        patch("hh_monitor.tg.commands.get_session_factory", return_value=_sf(mock_session)),
        patch("hh_monitor.tg.commands.is_admin", return_value=True),
    ):
        await handle_stop(cb)  # type: ignore[arg-type]

    cb.message.edit_text.assert_called_once()
    cb.answer.assert_called_once()
    # Rendered card should have resume button now
    keyboard = cb.message.edit_text.call_args[1]["reply_markup"]
    all_data = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert "adm:resume:1" in all_data


@pytest.mark.asyncio
async def test_stop_callback_fail_soft_stale_state() -> None:
    cb = _cb("adm:stop:1")

    update_result = _update_result([])  # no rows → already archived/modified

    mock_session = AsyncMock(spec=AsyncSession)
    mock_session.execute = AsyncMock(return_value=update_result)
    mock_session.commit = AsyncMock()

    with (
        patch("hh_monitor.tg.commands.get_session_factory", return_value=_sf(mock_session)),
        patch("hh_monitor.tg.commands.is_admin", return_value=True),
    ):
        await handle_stop(cb)  # type: ignore[arg-type]

    cb.answer.assert_called_once()
    args, kwargs = cb.answer.call_args
    assert kwargs.get("show_alert") is True
    assert "обнови /active" in args[0]


@pytest.mark.asyncio
async def test_resume_callback_updates_and_rerenders() -> None:
    cb = _cb("adm:resume:2")

    update_result = _update_result([(2,)])
    fetch_result = MagicMock()
    fetch_result.fetchone.return_value = _fetch_row_mock(2, is_active=True)

    side_effects = [update_result, fetch_result]
    idx = 0

    async def _execute(*args: object, **kwargs: object) -> MagicMock:
        nonlocal idx
        r = side_effects[idx % len(side_effects)]
        idx += 1
        return r

    mock_session = AsyncMock(spec=AsyncSession)
    mock_session.execute = _execute  # type: ignore[method-assign]
    mock_session.commit = AsyncMock()

    with (
        patch("hh_monitor.tg.commands.get_session_factory", return_value=_sf(mock_session)),
        patch("hh_monitor.tg.commands.is_admin", return_value=True),
    ):
        await handle_resume(cb)  # type: ignore[arg-type]

    cb.message.edit_text.assert_called_once()
    keyboard = cb.message.edit_text.call_args[1]["reply_markup"]
    all_data = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert "adm:stop:2" in all_data


# ── Archive confirmation flow ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_archive_request_shows_confirmation() -> None:
    cb = _cb("adm:archive:3")

    name_row = MagicMock()
    name_row.position_name = "Директор"
    mock_result = MagicMock()
    mock_result.fetchone.return_value = name_row

    mock_session = AsyncMock(spec=AsyncSession)
    mock_session.execute = AsyncMock(return_value=mock_result)

    with (
        patch("hh_monitor.tg.commands.get_session_factory", return_value=_sf(mock_session)),
        patch("hh_monitor.tg.commands.is_admin", return_value=True),
    ):
        await handle_archive_request(cb)  # type: ignore[arg-type]

    cb.message.edit_text.assert_called_once()
    text_out: str = cb.message.edit_text.call_args[0][0]
    assert "Директор" in text_out
    assert "безвозвратное" in text_out
    keyboard = cb.message.edit_text.call_args[1]["reply_markup"]
    all_data = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert "adm:yes_arch:3" in all_data
    assert "adm:no_arch:3" in all_data


@pytest.mark.asyncio
async def test_confirm_archive_updates_db() -> None:
    cb = _cb("adm:yes_arch:3")

    name_row = MagicMock()
    name_row.position_name = "Директор"
    update_result = MagicMock()
    update_result.fetchone.return_value = name_row

    mock_session = AsyncMock(spec=AsyncSession)
    mock_session.execute = AsyncMock(return_value=update_result)
    mock_session.commit = AsyncMock()

    with (
        patch("hh_monitor.tg.commands.get_session_factory", return_value=_sf(mock_session)),
        patch("hh_monitor.tg.commands.is_admin", return_value=True),
    ):
        await handle_confirm_archive(cb)  # type: ignore[arg-type]

    cb.message.edit_text.assert_called_once()
    text_out: str = cb.message.edit_text.call_args[0][0]
    assert "архивирован" in text_out.lower()
    # reply_markup=None to remove keyboard
    assert cb.message.edit_text.call_args[1].get("reply_markup") is None


@pytest.mark.asyncio
async def test_confirm_archive_fail_soft_stale_state() -> None:
    cb = _cb("adm:yes_arch:3")

    update_result = MagicMock()
    update_result.fetchone.return_value = None  # already archived

    mock_session = AsyncMock(spec=AsyncSession)
    mock_session.execute = AsyncMock(return_value=update_result)
    mock_session.commit = AsyncMock()

    with (
        patch("hh_monitor.tg.commands.get_session_factory", return_value=_sf(mock_session)),
        patch("hh_monitor.tg.commands.is_admin", return_value=True),
    ):
        await handle_confirm_archive(cb)  # type: ignore[arg-type]

    cb.answer.assert_called_once()
    args, kwargs = cb.answer.call_args
    assert kwargs.get("show_alert") is True
    assert "обнови /active" in args[0]


@pytest.mark.asyncio
async def test_cancel_archive_rerenders_card() -> None:
    cb = _cb("adm:no_arch:3")

    fetch_result = MagicMock()
    fetch_result.fetchone.return_value = _fetch_row_mock(3, is_active=True)

    mock_session = AsyncMock(spec=AsyncSession)
    mock_session.execute = AsyncMock(return_value=fetch_result)

    with (
        patch("hh_monitor.tg.commands.get_session_factory", return_value=_sf(mock_session)),
        patch("hh_monitor.tg.commands.is_admin", return_value=True),
    ):
        await handle_cancel_archive(cb)  # type: ignore[arg-type]

    cb.message.edit_text.assert_called_once()
    text_out: str = cb.message.edit_text.call_args[0][0]
    assert "Директор" in text_out


# ── Detail stub ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_detail_shows_stub_alert() -> None:
    cb = _cb("adm:detail:1")

    with patch("hh_monitor.tg.commands.is_admin", return_value=True):
        await handle_detail(cb)  # type: ignore[arg-type]

    cb.answer.assert_called_once()
    args, kwargs = cb.answer.call_args
    assert kwargs.get("show_alert") is True
    assert "Сессия 9" in args[0]


# ── Close callback ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_close_deletes_message() -> None:
    cb = _cb("adm:close")
    cb.message.chat.id = 1234
    cb.message.message_id = 999

    with patch("hh_monitor.tg.commands.is_admin", return_value=True):
        await handle_close(cb)  # type: ignore[arg-type]

    cb.bot.delete_message.assert_called_once_with(chat_id=1234, message_id=999)
    cb.answer.assert_called_once()


# ── Threshold FSM ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_threshold_button_sends_forcereply() -> None:
    cb = _cb("adm:threshold", user_id=42)

    with patch("hh_monitor.tg.commands.is_admin", return_value=True):
        await handle_threshold_button(cb)  # type: ignore[arg-type]

    assert 42 in _threshold_fsm
    cb.message.answer.assert_called_once()
    assert "0-100" in cb.message.answer.call_args[0][0]
    _threshold_fsm.clear()


@pytest.mark.asyncio
async def test_threshold_reply_valid_updates_config() -> None:
    user_id = 55
    _threshold_fsm[user_id] = _ThresholdFsmState(
        prompt_message_id=888,
        chat_id=1234,
        created_at=datetime.now(UTC),
    )
    msg = _msg("75", user_id=user_id, reply_to_id=888)

    mock_session = AsyncMock(spec=AsyncSession)

    with (
        patch("hh_monitor.tg.commands.get_session_factory", return_value=_sf(mock_session)),
        patch("hh_monitor.tg.commands.get_current_threshold", return_value=60),
        patch(
            "hh_monitor.tg.commands.upsert_app_config", new_callable=AsyncMock
        ) as mock_upsert,
    ):
        await handle_threshold_reply(msg)  # type: ignore[arg-type]

    assert user_id not in _threshold_fsm
    msg.reply.assert_called_once()
    reply_text: str = msg.reply.call_args[0][0]
    assert "60" in reply_text and "75" in reply_text
    mock_upsert.assert_called_once()


@pytest.mark.asyncio
async def test_threshold_reply_invalid_shows_error_and_keeps_state() -> None:
    user_id = 56
    _threshold_fsm[user_id] = _ThresholdFsmState(
        prompt_message_id=888,
        chat_id=1234,
        created_at=datetime.now(UTC),
    )
    msg = _msg("abc", user_id=user_id, reply_to_id=888)

    await handle_threshold_reply(msg)  # type: ignore[arg-type]

    # State preserved so user can retry
    assert user_id in _threshold_fsm
    msg.reply.assert_called_once()
    assert "0 до 100" in msg.reply.call_args[0][0]
    _threshold_fsm.clear()


@pytest.mark.asyncio
async def test_threshold_reply_out_of_range_shows_error() -> None:
    user_id = 57
    _threshold_fsm[user_id] = _ThresholdFsmState(
        prompt_message_id=888,
        chat_id=1234,
        created_at=datetime.now(UTC),
    )
    msg = _msg("150", user_id=user_id, reply_to_id=888)

    await handle_threshold_reply(msg)  # type: ignore[arg-type]

    assert user_id in _threshold_fsm
    assert "0 до 100" in msg.reply.call_args[0][0]
    _threshold_fsm.clear()


@pytest.mark.asyncio
async def test_threshold_reply_ttl_expired() -> None:
    user_id = 58
    _threshold_fsm[user_id] = _ThresholdFsmState(
        prompt_message_id=888,
        chat_id=1234,
        created_at=datetime.now(UTC) - timedelta(seconds=400),  # expired
    )
    msg = _msg("75", user_id=user_id, reply_to_id=888)

    await handle_threshold_reply(msg)  # type: ignore[arg-type]

    assert user_id not in _threshold_fsm
    msg.reply.assert_called_once()
    assert "истекла" in msg.reply.call_args[0][0]


# ── Router registration order regression tests ────────────────────────────────


def test_admin_router_registered_first() -> None:
    """Regression: swapping include_router order breaks admin /help routing.

    Inspects the source of register_tg_routers to confirm admin_router is
    included before router. No side effects on global router state.
    """
    import inspect

    from hh_monitor.tg.client import register_tg_routers

    src = inspect.getsource(register_tg_routers)
    admin_call = "include_router(admin_router)"
    general_call = "include_router(router)"

    assert admin_call in src, f"'{admin_call}' not found in register_tg_routers"
    assert general_call in src, f"'{general_call}' not found in register_tg_routers"
    assert src.index(admin_call) < src.index(general_call), (
        "admin_router must be included before router so /help in ADMIN_TOPIC "
        "hits the new handler, not the old one in router"
    )


@pytest.mark.asyncio
async def test_admin_router_wins_help_in_admin_topic() -> None:
    """admin_router first + AdminTopicFilter: /help in admin topic hits admin handler."""
    from unittest.mock import patch as _patch

    from aiogram import Dispatcher
    from aiogram.types import Message

    from hh_monitor.tg.client import register_tg_routers

    _ADMIN_TOPIC_ID = 7

    dp = Dispatcher()
    register_tg_routers(dp)

    bot = AsyncMock()
    bot.id = 42

    update = Update.model_validate({
        "update_id": 1,
        "message": {
            "message_id": 1,
            "from": {"id": 100, "is_bot": False, "first_name": "Admin"},
            "chat": {"id": -1001, "type": "supergroup", "title": "HR"},
            "message_thread_id": _ADMIN_TOPIC_ID,
            "date": 1234567890,
            "text": "/help",
            "entities": [{"type": "bot_command", "offset": 0, "length": 5}],
        },
    })

    with (
        patch("hh_monitor.tg.commands.settings") as ms,
        patch("hh_monitor.tg.commands.is_admin", return_value=True),
        _patch.object(Message, "answer", new_callable=AsyncMock) as mock_answer,
    ):
        ms.telegram_admin_topic_id = _ADMIN_TOPIC_ID
        await dp.feed_update(bot, update)

    mock_answer.assert_called_once()
    sent_text: str = mock_answer.call_args[0][0]
    assert "/active" in sent_text, "Expected admin help card"
    assert "/threshold" not in sent_text, "Old help card must not appear"
