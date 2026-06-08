"""Tests for the Edit Portrait FSM (entry wiring, edit→validate→save, persistence)."""

from __future__ import annotations

import copy
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml
from aiogram.enums import ChatType
from aiogram.types import Message
from sqlalchemy import select

from hh_monitor.db.models import Search
from hh_monitor.fit.portrait import Portrait
from hh_monitor.fit.portrait_loader import load_portrait_for_search
from hh_monitor.regions.expander import resolve_region_names
from hh_monitor.tg.edit_portrait import fields as fld
from hh_monitor.tg.edit_portrait import handlers as h
from hh_monitor.tg.edit_portrait.states import EditPortrait

_PORTRAIT: dict[str, Any] = {
    "position_code": "branch_director",
    "position_name": "Директор филиала",
    "filters": {"regions": {"primary": ["Самарская область"], "adjacent": [], "stop": []}},
    "must_have_keywords": ["страхование"],
}


class FakeFSM:
    """Minimal in-memory FSMContext stand-in (mirrors the add_vacancy tests)."""

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


def _cb(data: str = "", *, user_id: int = 100, private: bool = False) -> MagicMock:
    c = MagicMock()
    c.data = data
    c.from_user = MagicMock(id=user_id)
    c.answer = AsyncMock()
    c.message = MagicMock()
    c.message.__class__ = Message
    c.message.chat = MagicMock()
    c.message.chat.type = ChatType.PRIVATE if private else ChatType.SUPERGROUP
    c.message.answer = AsyncMock()
    return c


def _msg(text: str = "", *, user_id: int = 100) -> MagicMock:
    m = MagicMock()
    m.text = text
    m.from_user = MagicMock(id=user_id)
    m.answer = AsyncMock()
    m.chat = MagicMock()
    m.chat.type = ChatType.SUPERGROUP
    return m


def _factory_from(db_session: Any) -> Any:
    @asynccontextmanager
    async def _ctx() -> Any:
        yield db_session

    return MagicMock(side_effect=lambda: _ctx())


def _idx(path: tuple[str, ...]) -> int:
    return next(i for i, d in enumerate(fld.FIELDS) if d.path == path)


async def _seed_search(
    session: Any,
    *,
    code: str = "branch_director",
    portrait: dict[str, Any] | None = None,
    hh_params: dict[str, Any] | None = None,
) -> Search:
    s = Search(
        position_code=code,
        position_name="Директор филиала",
        hh_params=hh_params or {"text": "директор", "area": [3]},
        portrait=portrait or copy.deepcopy(_PORTRAIT),
    )
    session.add(s)
    await session.flush()
    return s


def _fsm_for(search_id: int, *, portrait: dict[str, Any], code: str = "branch_director") -> FakeFSM:
    return FakeFSM(
        data={
            "portrait_dict": portrait,
            "search_id": search_id,
            "position_code": code,
            "position_name": "Директор филиала",
        },
        state=EditPortrait.menu,
    )


@pytest.fixture(autouse=True)
def _admin_true() -> Any:
    with patch("hh_monitor.tg.edit_portrait.handlers.is_admin", return_value=True):
        yield


# ── Entry wiring ─────────────────────────────────────────────────────────────────


def test_active_card_has_edit_button() -> None:
    from hh_monitor.tg.commands import _search_action_keyboard

    keyboard = _search_action_keyboard(7, True)
    cbs = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert "adm:edit_portrait:7" in cbs


@pytest.mark.asyncio
async def test_entry_loads_portrait_into_fsm(db_session: Any) -> None:
    s = await _seed_search(db_session)
    fsm = FakeFSM()
    with (
        patch.object(h, "get_session_factory", return_value=_factory_from(db_session)),
        patch("hh_monitor.fit.portrait_loader.load_all_portraits", return_value={}),
    ):
        await h.start_edit_portrait(_cb(f"adm:edit_portrait:{s.id}"), fsm)  # type: ignore[arg-type]

    assert fsm.state == EditPortrait.menu
    data = await fsm.get_data()
    assert data["search_id"] == s.id
    assert data["portrait_dict"]["position_code"] == "branch_director"
    assert data["position_name"] == "Директор филиала"


@pytest.mark.asyncio
async def test_non_admin_entry_rejected() -> None:
    from hh_monitor.tg import commands as cmd

    fsm = FakeFSM()
    cb = _cb("adm:edit_portrait:1")
    with patch("hh_monitor.tg.commands.is_admin", return_value=False):
        await cmd.handle_edit_portrait(cb, fsm)  # type: ignore[arg-type]

    cb.answer.assert_awaited_with("Нет прав", show_alert=True)
    assert fsm.state is None


# ── Edit → validate → save (DB-backed, nested field) ─────────────────────────────


@pytest.mark.asyncio
async def test_edit_regions_primary_and_save_db_backed(db_session: Any) -> None:
    s = await _seed_search(db_session)
    sid = s.id
    fsm = _fsm_for(sid, portrait=copy.deepcopy(_PORTRAIT))

    idx = _idx(("filters", "regions", "primary"))
    await h.handle_pick_field(_cb(f"ep:fld:{idx}"), fsm)  # type: ignore[arg-type]
    assert fsm.state == EditPortrait.awaiting_value

    await h.handle_value(_msg("Москва, Московская область"), fsm)  # type: ignore[arg-type]
    data = await fsm.get_data()
    assert data["portrait_dict"]["filters"]["regions"]["primary"] == [
        "Москва",
        "Московская область",
    ]
    assert fsm.state == EditPortrait.menu

    cb = _cb("ep:save")
    with (
        patch.object(h, "get_session_factory", return_value=_factory_from(db_session)),
        patch.object(h, "draft_critic_prompt", new=AsyncMock(return_value="ЛИНЗА критика")),
        patch.object(h, "load_all_portraits", return_value={}),
    ):
        await h.handle_save(cb, fsm)  # type: ignore[arg-type]

    row = (await db_session.execute(select(Search).where(Search.id == sid))).scalars().one()
    assert row.portrait["filters"]["regions"]["primary"] == ["Москва", "Московская область"]
    assert row.llm_critic_prompt == "ЛИНЗА критика"
    assert fsm.cleared is True

    # The loader (DB-backed) must return the edited value.
    with patch("hh_monitor.fit.portrait_loader.load_all_portraits", return_value={}):
        loaded = load_portrait_for_search(row)
    assert loaded.filters.regions.primary == ["Москва", "Московская область"]

    # Read-only critic prompt shown.
    texts = [c.args[0] for c in cb.message.answer.call_args_list if c.args]
    assert any("Промпт-критик" in t and "ЛИНЗА критика" in t for t in texts)


# ── Authoritative-source write (YAML-backed) ─────────────────────────────────────


@pytest.mark.asyncio
async def test_save_yaml_backed_writes_file_and_loader_returns_edit(
    db_session: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = await _seed_search(db_session, code="yaml_role")
    pd = copy.deepcopy(_PORTRAIT)
    pd["position_code"] = "yaml_role"
    pd["must_have_keywords"] = ["осаго"]
    sid = s.id
    fsm = _fsm_for(sid, portrait=pd, code="yaml_role")

    monkeypatch.setattr("hh_monitor.fit.portrait._PORTRAITS_DIR", tmp_path)
    yaml_registry = {"yaml_role": Portrait.model_validate(pd)}
    with (
        patch.object(h, "get_session_factory", return_value=_factory_from(db_session)),
        patch.object(h, "draft_critic_prompt", new=AsyncMock(return_value="L")),
        patch.object(h, "load_all_portraits", return_value=yaml_registry),
    ):
        await h.handle_save(_cb("ep:save"), fsm)  # type: ignore[arg-type]

    yaml_file = tmp_path / "yaml_role.yaml"
    assert yaml_file.exists()
    written = yaml.safe_load(yaml_file.read_text(encoding="utf-8"))
    assert written["must_have_keywords"] == ["осаго"]

    # DB kept in sync, and the YAML-first loader returns the edit.
    row = (await db_session.execute(select(Search).where(Search.id == sid))).scalars().one()
    assert row.portrait["must_have_keywords"] == ["осаго"]
    with patch(
        "hh_monitor.fit.portrait_loader.load_all_portraits",
        return_value={"yaml_role": Portrait.model_validate(written)},
    ):
        loaded = load_portrait_for_search(row)
    assert loaded.must_have_keywords == ["осаго"]


# ── Prefilter + hh_params re-derivation (split by field) ──────────────────────────


@pytest.mark.asyncio
async def test_save_region_primary_updates_hh_params_area(db_session: Any) -> None:
    s = await _seed_search(
        db_session, hh_params={"text": "директор", "area": [3], "experience": "between3And6"}
    )
    pd = copy.deepcopy(_PORTRAIT)
    pd["filters"]["regions"]["primary"] = ["Москва"]
    sid = s.id
    fsm = _fsm_for(sid, portrait=pd)

    with (
        patch.object(h, "get_session_factory", return_value=_factory_from(db_session)),
        patch.object(h, "draft_critic_prompt", new=AsyncMock(return_value="L")),
        patch.object(h, "load_all_portraits", return_value={}),
    ):
        await h.handle_save(_cb("ep:save"), fsm)  # type: ignore[arg-type]

    row = (await db_session.execute(select(Search).where(Search.id == sid))).scalars().one()
    expected_area, _ = resolve_region_names(["Москва"])
    assert expected_area  # sanity: Москва resolves
    assert row.hh_params["area"] == expected_area
    assert row.hh_params["experience"] == "between3And6"  # other keys preserved


@pytest.mark.asyncio
async def test_save_region_primary_cleared_removes_area(db_session: Any) -> None:
    s = await _seed_search(
        db_session, hh_params={"text": "директор", "area": [3], "period": 30}
    )
    pd = copy.deepcopy(_PORTRAIT)
    pd["filters"]["regions"]["primary"] = []
    sid = s.id
    fsm = _fsm_for(sid, portrait=pd)

    with (
        patch.object(h, "get_session_factory", return_value=_factory_from(db_session)),
        patch.object(h, "draft_critic_prompt", new=AsyncMock(return_value="L")),
        patch.object(h, "load_all_portraits", return_value={}),
    ):
        await h.handle_save(_cb("ep:save"), fsm)  # type: ignore[arg-type]

    row = (await db_session.execute(select(Search).where(Search.id == sid))).scalars().one()
    assert "area" not in row.hh_params  # stale area dropped on full clear
    assert row.hh_params["period"] == 30  # unrelated keys preserved by the merge


@pytest.mark.asyncio
async def test_save_region_stop_updates_prefilter(db_session: Any) -> None:
    s = await _seed_search(db_session)
    pd = copy.deepcopy(_PORTRAIT)
    pd["filters"]["regions"]["stop"] = ["Москва"]
    sid = s.id
    fsm = _fsm_for(sid, portrait=pd)

    with (
        patch.object(h, "get_session_factory", return_value=_factory_from(db_session)),
        patch.object(h, "draft_critic_prompt", new=AsyncMock(return_value="L")),
        patch.object(h, "load_all_portraits", return_value={}),
    ):
        await h.handle_save(_cb("ep:save"), fsm)  # type: ignore[arg-type]

    row = (await db_session.execute(select(Search).where(Search.id == sid))).scalars().one()
    stop_ids, _ = resolve_region_names(["Москва"])
    assert stop_ids
    assert row.portrait["prefilter"]["area_ids_stop"] == stop_ids
    assert row.portrait["prefilter"]["area_ids_require"] == []  # empty by design


# ── Invalid input handling ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_invalid_int_keeps_fsm_and_does_not_mutate() -> None:
    pd = copy.deepcopy(_PORTRAIT)
    fsm = _fsm_for(1, portrait=pd)
    idx = _idx(("min_insurance_experience_months",))
    await fsm.update_data(cur_field=idx)
    await fsm.set_state(EditPortrait.awaiting_value)

    msg = _msg("не число")
    await h.handle_value(msg, fsm)  # type: ignore[arg-type]

    assert fsm.state == EditPortrait.awaiting_value
    data = await fsm.get_data()
    assert "min_insurance_experience_months" not in data["portrait_dict"]
    msg.answer.assert_awaited()


@pytest.mark.asyncio
async def test_invalid_literal_value_rejected() -> None:
    pd = copy.deepcopy(_PORTRAIT)
    fsm = _fsm_for(1, portrait=pd)
    idx = _idx(("role_match_mode",))

    cb = _cb(f"ep:lit:{idx}:bogus")
    await h.handle_set_literal(cb, fsm)  # type: ignore[arg-type]

    data = await fsm.get_data()
    assert data["portrait_dict"].get("role_match_mode") != "bogus"
    cb.answer.assert_awaited_with("Ошибка")
