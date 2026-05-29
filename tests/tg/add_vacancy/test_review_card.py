"""Tests for _render_review region-warning logic."""

from __future__ import annotations

from hh_monitor.fit.portrait import Filters, Portrait, RegionFilters
from hh_monitor.tg.add_vacancy.handlers import _render_review


def _portrait(primary: list[str] | None = None) -> Portrait:
    kw: dict = {"position_code": "test_pos", "position_name": "Тест Менеджер"}
    if primary is not None:
        kw["filters"] = Filters(regions=RegionFilters(primary=primary))
    return Portrait(**kw)


def test_render_review_shows_warning_for_unknown() -> None:
    portrait = _portrait(["Москва"])
    text = _render_review(portrait, unknown=["НесуществующийГород"])
    assert "⚠️ Не распознаны:" in text
    assert "НесуществующийГород" in text


def test_render_review_no_warning_when_unknown_empty() -> None:
    portrait = _portrait(["Москва"])
    text = _render_review(portrait, unknown=[])
    assert "⚠️" not in text


def test_render_review_no_warning_when_unknown_omitted() -> None:
    portrait = _portrait(["Москва"])
    text = _render_review(portrait)
    assert "⚠️" not in text


def test_render_review_multiple_unknowns_listed() -> None:
    portrait = _portrait()
    text = _render_review(portrait, unknown=["Город А", "Город Б"])
    assert "Город А" in text
    assert "Город Б" in text
