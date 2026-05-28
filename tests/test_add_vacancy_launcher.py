"""Tests for hh_monitor.tg.add_vacancy.launcher._run_initial_scan."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hh_monitor.db.models import Event, Resume, Search
from hh_monitor.tg.add_vacancy import launcher as L


def _factory_from(db_session: Any) -> Any:
    @asynccontextmanager
    async def _ctx() -> Any:
        yield db_session

    return MagicMock(side_effect=lambda: _ctx())


async def _seed_search(db_session: Any, code: str = "test-scan") -> int:
    search = Search(
        search_code=code,
        position_code=code,
        position_name="Сканируемая вакансия",
        hh_params={"text": "x"},
        portrait={"position_code": code, "position_name": "Сканируемая вакансия"},
        active=True,
    )
    db_session.add(search)
    await db_session.flush()
    return int(search.id)


@pytest.mark.asyncio
async def test_initial_scan_success_notifies(db_session: Any) -> None:
    search_id = await _seed_search(db_session)
    # one event so the count is non-zero
    db_session.add(Resume(hh_resume_id="r1"))
    await db_session.flush()
    db_session.add(Event(hh_resume_id="r1", event_type="NEW", search_id=search_id))
    await db_session.flush()

    notes: list[str] = []
    with (
        patch("hh_monitor.tg.client.get_session_factory", return_value=_factory_from(db_session)),
        patch(
            "hh_monitor.pipeline.run_all.run_all",
            new=AsyncMock(return_value={"total": 1, "succeeded": 1}),
        ),
        patch.object(L, "_notify_admin", new=AsyncMock(side_effect=lambda m: notes.append(m))),
    ):
        await L._run_initial_scan("test-scan", admin_user_id=42)

    assert notes, "expected an admin notification"
    assert "завершён" in notes[0]
    assert "1" in notes[0]


@pytest.mark.asyncio
async def test_notify_admin_no_bot_config_is_noop() -> None:
    """_notify_admin returns silently when bot/group are not configured."""
    from hh_monitor.config import settings as cfg

    with (
        patch.object(cfg, "telegram_bot_token", None),
        patch.object(cfg, "telegram_hr_group_id", 0),
    ):
        await L._notify_admin("anything")  # must not raise


@pytest.mark.asyncio
async def test_initial_scan_failure_notifies(db_session: Any) -> None:
    await _seed_search(db_session, code="boom-scan")

    notes: list[str] = []
    boom = AsyncMock(side_effect=RuntimeError("pipeline exploded"))
    with (
        patch("hh_monitor.tg.client.get_session_factory", return_value=_factory_from(db_session)),
        patch("hh_monitor.pipeline.run_all.run_all", new=boom),
        patch.object(L, "_notify_admin", new=AsyncMock(side_effect=lambda m: notes.append(m))),
    ):
        await L._run_initial_scan("boom-scan", admin_user_id=42)

    assert notes
    assert "упал" in notes[0]
    assert "RuntimeError" in notes[0]
