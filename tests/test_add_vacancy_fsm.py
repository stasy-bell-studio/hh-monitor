"""Tests for the Add Vacancy FSM handlers (AC2, AC3, AC4, AC7, AC8, AC11)."""

from __future__ import annotations

import io
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.enums import ChatType
from aiogram.types import Message
from sqlalchemy import select

from hh_monitor.db.models import Search
from hh_monitor.tg.add_vacancy import handlers as h
from hh_monitor.tg.add_vacancy.states import AddVacancy

_PORTRAIT_DICT = {
    "position_code": "test-role",
    "position_name": "Test Role",
    "must_have_keywords": ["python"],
    "position_synonyms": ["dev"],
}


class FakeFSM:
    """Minimal in-memory FSMContext stand-in."""

    def __init__(self, data: dict[str, Any] | None = None, state: Any = None) -> None:
        self._data: dict[str, Any] = dict(data or {})
        self.state = state
        self.cleared = False

    async def get_data(self) -> dict[str, Any]:
        return dict(self._data)

    async def update_data(self, **kw: Any) -> dict[str, Any]:
        self._data.update(kw)
        return dict(self._data)

    async def set_state(self, s: Any = None) -> None:
        self.state = s

    async def get_state(self) -> Any:
        return self.state

    async def clear(self) -> None:
        self._data = {}
        self.state = None
        self.cleared = True


def _msg(text: str = "", *, user_id: int = 100) -> MagicMock:
    m = MagicMock()
    m.text = text
    m.from_user = MagicMock(id=user_id)
    m.answer = AsyncMock()
    m.chat = MagicMock()
    m.chat.type = ChatType.SUPERGROUP
    m.document = None
    m.bot = MagicMock()
    return m


def _cb(data: str = "", *, user_id: int = 100, private: bool = False) -> MagicMock:
    c = MagicMock()
    c.data = data
    c.from_user = MagicMock(id=user_id)
    c.answer = AsyncMock()
    c.message = MagicMock()
    c.message.__class__ = Message  # make isinstance(c.message, Message) → True
    c.message.chat = MagicMock()
    c.message.chat.type = ChatType.PRIVATE if private else ChatType.SUPERGROUP
    c.message.answer = AsyncMock()
    return c


@pytest.fixture(autouse=True)
def _admin_true() -> Any:
    with patch("hh_monitor.tg.add_vacancy.handlers.is_admin", return_value=True):
        yield


# ── Entry: /add in group (regression CC-12) ─────────────────────────────────────


@pytest.mark.asyncio
async def test_group_add_command_starts_wizard() -> None:
    """/add in group admin topic must still reach the wizard (not DM redirect)."""
    fsm = FakeFSM()
    msg = _msg("/add")
    await h.handle_add_command(msg, fsm)  # type: ignore[arg-type]
    assert fsm.state == AddVacancy.S1_name
    msg.answer.assert_called_once()


# ── S1 ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_s1_happy_path() -> None:
    fsm = FakeFSM(state=AddVacancy.S1_name)
    msg = _msg("Senior Backend Python")
    await h.handle_s1_name(msg, fsm)  # type: ignore[arg-type]
    assert (await fsm.get_data())["position_name"] == "Senior Backend Python"
    assert fsm.state == AddVacancy.S2_input_mode


@pytest.mark.asyncio
async def test_s1_empty_rejected() -> None:
    fsm = FakeFSM(state=AddVacancy.S1_name)
    msg = _msg("   ")
    await h.handle_s1_name(msg, fsm)  # type: ignore[arg-type]
    assert fsm.state == AddVacancy.S1_name
    msg.answer.assert_awaited()


@pytest.mark.asyncio
async def test_s1_too_long_rejected() -> None:
    fsm = FakeFSM(state=AddVacancy.S1_name)
    await h.handle_s1_name(_msg("x" * 201), fsm)  # type: ignore[arg-type]
    assert fsm.state == AddVacancy.S1_name


# ── S2 ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_s2_text_mode_routes() -> None:
    fsm = FakeFSM(data={"position_name": "X"}, state=AddVacancy.S2_input_mode)
    await h.handle_s2_text(_cb("av:mode:text"), fsm)  # type: ignore[arg-type]
    assert (await fsm.get_data())["input_mode"] == "text"
    assert fsm.state == AddVacancy.S3_portrait_raw


@pytest.mark.asyncio
async def test_s2_file_mode_routes() -> None:
    fsm = FakeFSM(data={"position_name": "X"}, state=AddVacancy.S2_input_mode)
    await h.handle_s2_file(_cb("av:mode:file"), fsm)  # type: ignore[arg-type]
    assert (await fsm.get_data())["input_mode"] == "file"
    assert fsm.state == AddVacancy.S3_portrait_raw


# ── S3 ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_s3_text_parses_and_advances() -> None:
    fsm = FakeFSM(
        data={"position_name": "Test Role", "visited_review": False},
        state=AddVacancy.S3_portrait_raw,
    )
    with patch.object(
        h, "parse_to_portrait_dict", new=AsyncMock(return_value=_PORTRAIT_DICT)
    ) as mock_parse:
        await h.handle_s3_text(_msg("требования: python, 5 лет"), fsm)  # type: ignore[arg-type]
    mock_parse.assert_awaited_once()
    assert (await fsm.get_data())["portrait_dict"] == _PORTRAIT_DICT
    assert fsm.state == AddVacancy.S4_review


@pytest.mark.asyncio
async def test_s3_file_pdf_path() -> None:
    fsm = FakeFSM(data={"position_name": "Test Role"}, state=AddVacancy.S3_portrait_raw)
    msg = _msg()
    msg.document = MagicMock(file_size=1000, mime_type="application/pdf", file_id="fid")
    msg.bot.get_file = AsyncMock(return_value=MagicMock(file_path="path"))
    msg.bot.download_file = AsyncMock(return_value=io.BytesIO(b"pdfbytes"))
    with (
        patch.object(h, "extract_text", new=AsyncMock(return_value="извлечённый текст")),
        patch.object(h, "parse_to_portrait_dict", new=AsyncMock(return_value=_PORTRAIT_DICT)) as mp,
    ):
        await h.handle_s3_document(msg, fsm)  # type: ignore[arg-type]
    mp.assert_awaited_once()
    assert fsm.state == AddVacancy.S4_review


@pytest.mark.asyncio
async def test_s3_file_txt_path() -> None:
    fsm = FakeFSM(data={"position_name": "Test Role"}, state=AddVacancy.S3_portrait_raw)
    msg = _msg()
    msg.document = MagicMock(file_size=50, mime_type="text/plain", file_id="fid")
    msg.bot.get_file = AsyncMock(return_value=MagicMock(file_path="path"))
    msg.bot.download_file = AsyncMock(return_value=io.BytesIO("портрет".encode()))
    with patch.object(
        h, "parse_to_portrait_dict", new=AsyncMock(return_value=_PORTRAIT_DICT)
    ) as mp:
        await h.handle_s3_document(msg, fsm)  # type: ignore[arg-type]
    mp.assert_awaited_once()
    assert fsm.state == AddVacancy.S4_review


@pytest.mark.asyncio
async def test_s3_file_too_large_rejected() -> None:
    fsm = FakeFSM(data={"position_name": "X"}, state=AddVacancy.S3_portrait_raw)
    msg = _msg()
    msg.document = MagicMock(file_size=6 * 1024 * 1024, mime_type="application/pdf", file_id="f")
    with patch.object(h, "parse_to_portrait_dict", new=AsyncMock()) as mp:
        await h.handle_s3_document(msg, fsm)  # type: ignore[arg-type]
    mp.assert_not_awaited()
    assert fsm.state == AddVacancy.S3_portrait_raw


@pytest.mark.asyncio
async def test_s3_file_wrong_mime_rejected() -> None:
    fsm = FakeFSM(data={"position_name": "X"}, state=AddVacancy.S3_portrait_raw)
    msg = _msg()
    msg.document = MagicMock(file_size=100, mime_type="image/jpeg", file_id="f")
    msg.bot.get_file = AsyncMock(return_value=MagicMock(file_path="path"))
    msg.bot.download_file = AsyncMock(return_value=io.BytesIO(b"\xff\xd8jpeg"))
    with patch.object(h, "parse_to_portrait_dict", new=AsyncMock()) as mp:
        await h.handle_s3_document(msg, fsm)  # type: ignore[arg-type]
    mp.assert_not_awaited()
    assert fsm.state == AddVacancy.S3_portrait_raw
    msg.answer.assert_awaited()


# ── S4 ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_s4_confirm_to_s5() -> None:
    fsm = FakeFSM(
        data={"position_name": "Test Role", "portrait_dict": _PORTRAIT_DICT},
        state=AddVacancy.S4_review,
    )
    with patch.object(h, "draft_critic_prompt", new=AsyncMock(return_value="линза " * 30)):
        await h.handle_s4_ok(_cb("av:review:ok"), fsm)  # type: ignore[arg-type]
    assert fsm.state == AddVacancy.S5_critic_prompt
    assert (await fsm.get_data())["llm_critic_prompt"]


@pytest.mark.asyncio
async def test_s4_more_back_to_s3_concatenates() -> None:
    """AC7: 'дополнить' concatenates new text after previous with separator."""
    fsm = FakeFSM(
        data={
            "position_name": "Test Role",
            "portrait_dict": _PORTRAIT_DICT,
            "portrait_raw": "первый текст",
        },
        state=AddVacancy.S4_review,
    )
    await h.handle_s4_more(_cb("av:review:more"), fsm)  # type: ignore[arg-type]
    assert fsm.state == AddVacancy.S3_portrait_raw
    assert (await fsm.get_data())["visited_review"] is True

    captured: dict[str, Any] = {}

    async def fake_parse(raw: str, name: str) -> dict[str, Any]:
        captured["raw"] = raw
        return _PORTRAIT_DICT

    with patch.object(h, "parse_to_portrait_dict", new=fake_parse):
        await h.handle_s3_text(_msg("второй текст"), fsm)  # type: ignore[arg-type]
    assert captured["raw"] == "первый текст\n\n--- Дополнение ---\n\nвторой текст"


# ── S5 ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_s5_accept_to_s6() -> None:
    fsm = FakeFSM(
        data={
            "position_name": "Test Role",
            "portrait_dict": _PORTRAIT_DICT,
            "llm_critic_prompt": "x",
        },
        state=AddVacancy.S5_critic_prompt,
    )
    await h.handle_s5_ok(_cb("av:critic:ok"), fsm)  # type: ignore[arg-type]
    assert fsm.state == AddVacancy.S6_launch


@pytest.mark.asyncio
async def test_s5_rewrite_recalls_draft_with_feedback() -> None:
    """AC8: rewrite feedback is passed to draft_critic_prompt."""
    fsm = FakeFSM(
        data={"position_name": "Test Role", "portrait_dict": _PORTRAIT_DICT},
        state=AddVacancy.S5_critic_prompt,
    )
    await h.handle_s5_rewrite(_cb("av:critic:rewrite"), fsm)  # type: ignore[arg-type]
    assert (await fsm.get_data())["awaiting"] == "critic_feedback"

    with patch.object(
        h, "draft_critic_prompt", new=AsyncMock(return_value="новая линза " * 20)
    ) as mp:
        await h.handle_s5_feedback(_msg("больше цифр"), fsm)  # type: ignore[arg-type]
    mp.assert_awaited_once()
    assert mp.call_args.kwargs["user_feedback"] == "больше цифр"


# ── S6 (DB-backed) ───────────────────────────────────────────────────────────────


def _factory_from(db_session: Any) -> Any:
    @asynccontextmanager
    async def _ctx() -> Any:
        yield db_session

    return MagicMock(side_effect=lambda: _ctx())


@pytest.mark.asyncio
async def test_s6_launch_inserts_active_and_schedules_scan(db_session: Any) -> None:
    """AC3: launch → active=TRUE row + exactly one create_task with the search_code."""
    fsm = FakeFSM(
        data={
            "position_name": "Директор филиала",
            "portrait_dict": _PORTRAIT_DICT,
            "llm_critic_prompt": "L",
        },
        state=AddVacancy.S6_launch,
    )
    created: list[Any] = []

    def _fake_create_task(coro: Any) -> Any:
        created.append(coro)
        return MagicMock()  # task object with add_done_callback

    with (
        patch.object(h, "get_session_factory", return_value=_factory_from(db_session)),
        patch(
            "hh_monitor.tg.add_vacancy.launcher._run_initial_scan",
            new=MagicMock(return_value=None),
        ),
        patch.object(h.asyncio, "create_task", side_effect=_fake_create_task),
    ):
        await h.handle_s6_launch(_cb("av:launch:go", user_id=555), fsm)  # type: ignore[arg-type]

    rows = (
        (await db_session.execute(select(Search).where(Search.position_code == "direktor-filiala")))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].active is True
    assert rows[0].created_by_tg_user_id == 555
    assert len(created) == 1
    assert fsm.cleared is True


@pytest.mark.asyncio
async def test_s6_draft_inserts_inactive_no_scan(db_session: Any) -> None:
    """AC4: draft → active=FALSE row, no scan scheduled."""
    fsm = FakeFSM(
        data={
            "position_name": "Андеррайтер",
            "portrait_dict": _PORTRAIT_DICT,
            "llm_critic_prompt": "L",
        },
        state=AddVacancy.S6_launch,
    )
    created: list[Any] = []
    with (
        patch.object(h, "get_session_factory", return_value=_factory_from(db_session)),
        patch.object(h.asyncio, "create_task", side_effect=lambda coro: created.append(coro)),
    ):
        await h.handle_s6_draft(_cb("av:launch:draft"), fsm)  # type: ignore[arg-type]

    rows = (
        (await db_session.execute(select(Search).where(Search.position_code == "anderrayter")))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].active is False
    assert created == []
    assert fsm.cleared is True


@pytest.mark.asyncio
async def test_s6_cancel_no_insert(db_session: Any) -> None:
    fsm = FakeFSM(
        data={"position_name": "Ghost", "portrait_dict": _PORTRAIT_DICT},
        state=AddVacancy.S6_launch,
    )
    with patch.object(h, "get_session_factory", return_value=_factory_from(db_session)):
        await h.handle_cancel(_cb("av:cancel"), fsm)  # type: ignore[arg-type]
    rows = (
        (await db_session.execute(select(Search).where(Search.position_code == "ghost")))
        .scalars()
        .all()
    )
    assert rows == []
    assert fsm.cleared is True


# ── AC11: non-admin ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_non_admin_button_ignored() -> None:
    fsm = FakeFSM()
    with patch("hh_monitor.tg.add_vacancy.handlers.is_admin", return_value=False):
        await h.handle_add_start_callback(_cb("add_vacancy:start", user_id=999), fsm)  # type: ignore[arg-type]
    # No FSM started
    assert fsm.state is None


@pytest.mark.asyncio
async def test_private_chat_callback_ignored() -> None:
    """AC18c: callbacks in private chat are not honoured even for admins."""
    fsm = FakeFSM()
    cb = _cb("add_vacancy:start", user_id=100, private=True)
    await h.handle_add_start_callback(cb, fsm)  # type: ignore[arg-type]
    assert fsm.state is None
    cb.answer.assert_awaited()


@pytest.mark.asyncio
async def test_entry_callback_starts_wizard() -> None:
    fsm = FakeFSM()
    await h.handle_add_start_callback(_cb("add_vacancy:start"), fsm)  # type: ignore[arg-type]
    assert fsm.state == AddVacancy.S1_name


@pytest.mark.asyncio
async def test_add_command_starts_wizard() -> None:
    fsm = FakeFSM()
    await h.handle_add_command(_msg("/add"), fsm)  # type: ignore[arg-type]
    assert fsm.state == AddVacancy.S1_name


@pytest.mark.asyncio
async def test_cancel_clears_state() -> None:
    fsm = FakeFSM(data={"position_name": "X"}, state=AddVacancy.S3_portrait_raw)
    cb = _cb("av:cancel")
    await h.handle_cancel(cb, fsm)  # type: ignore[arg-type]
    assert fsm.cleared is True
    cb.message.answer.assert_awaited()


@pytest.mark.asyncio
async def test_s3_empty_text_rejected() -> None:
    fsm = FakeFSM(data={"position_name": "X"}, state=AddVacancy.S3_portrait_raw)
    msg = _msg("   ")
    with patch.object(h, "parse_to_portrait_dict", new=AsyncMock()) as mp:
        await h.handle_s3_text(msg, fsm)  # type: ignore[arg-type]
    mp.assert_not_awaited()
    assert fsm.state == AddVacancy.S3_portrait_raw


@pytest.mark.asyncio
async def test_s3_parse_failure_offers_retry() -> None:
    fsm = FakeFSM(
        data={"position_name": "Role", "visited_review": False},
        state=AddVacancy.S3_portrait_raw,
    )
    msg = _msg("текст портрета")
    with patch.object(h, "parse_to_portrait_dict", new=AsyncMock(side_effect=ValueError("bad"))):
        await h.handle_s3_text(msg, fsm)  # type: ignore[arg-type]
    # Stays in S3 so the user can retry / cancel
    assert fsm.state == AddVacancy.S3_portrait_raw


@pytest.mark.asyncio
async def test_admin_filter_classes() -> None:
    """AC11 support: filter classes reject non-admins / wrong topic."""
    from hh_monitor.config import settings as cfg

    with patch.object(cfg, "telegram_admin_topic_id", 7):
        topic_ok = _msg()
        topic_ok.message_thread_id = 7
        assert await h._AdminTopicFilter()(topic_ok) is True
        topic_bad = _msg()
        topic_bad.message_thread_id = 99
        assert await h._AdminTopicFilter()(topic_bad) is False

    with patch("hh_monitor.tg.add_vacancy.handlers.is_admin", return_value=False):
        assert await h._AdminUserFilter()(_msg(user_id=1)) is False


# ── CC-4a AC5: callback.answer() before slow downstream calls ─────────────────


@pytest.mark.asyncio
async def test_callback_answered_before_slow_call_s4_ok() -> None:
    """AC5: handle_s4_ok answers the callback BEFORE the blocking LLM call."""
    call_order: list[str] = []

    async def track_answer(*a: Any, **kw: Any) -> None:
        call_order.append("answer")

    async def track_critic(*a: Any, **kw: Any) -> None:
        call_order.append("critic")

    cb = _cb("av:review:ok")
    cb.answer = AsyncMock(side_effect=track_answer)

    fsm = FakeFSM(data={"portrait_dict": _PORTRAIT_DICT, "position_name": "Test Role"})
    with patch("hh_monitor.tg.add_vacancy.handlers._enter_critic", side_effect=track_critic):
        await h.handle_s4_ok(cb, fsm)  # type: ignore[arg-type]

    assert "answer" in call_order and "critic" in call_order
    assert call_order.index("answer") < call_order.index("critic")


@pytest.mark.asyncio
async def test_callback_answered_before_slow_call_s3_retry() -> None:
    """AC5: handle_s3_retry answers the callback BEFORE the blocking LLM parse."""
    call_order: list[str] = []

    async def track_answer(*a: Any, **kw: Any) -> None:
        call_order.append("answer")

    async def track_parse(*a: Any, **kw: Any) -> None:
        call_order.append("parse")

    cb = _cb("av:retry")
    cb.answer = AsyncMock(side_effect=track_answer)

    fsm = FakeFSM(data={"portrait_raw": "текст", "position_name": "Test Role"})
    with patch("hh_monitor.tg.add_vacancy.handlers._run_parse", side_effect=track_parse):
        await h.handle_s3_retry(cb, fsm)  # type: ignore[arg-type]

    assert "answer" in call_order and "parse" in call_order
    assert call_order.index("answer") < call_order.index("parse")
