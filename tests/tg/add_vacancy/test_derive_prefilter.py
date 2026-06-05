"""Tests for derive_prefilter in hh_monitor.tg.add_vacancy.llm."""

from __future__ import annotations

from hh_monitor.fit.portrait import Filters, Portrait, RegionFilters
from hh_monitor.tg.add_vacancy.llm import derive_prefilter


def _portrait(**kw: object) -> Portrait:
    return Portrait(position_code="test", position_name="Test", **kw)  # type: ignore[arg-type]


def test_derive_prefilter_soft_mode_empty_forbidden() -> None:
    p = _portrait(
        forbidden_industries=["банки"],
        forbidden_industry_mode="soft",
    )
    result = derive_prefilter(p)
    assert result.forbidden_industry_names == []


def test_derive_prefilter_hard_mode_populates_forbidden() -> None:
    p = _portrait(
        forbidden_industries=["банки", "ставки"],
        forbidden_industry_mode="hard",
    )
    result = derive_prefilter(p)
    assert result.forbidden_industry_names == ["банки", "ставки"]


def test_derive_prefilter_stop_regions_resolve() -> None:
    p = _portrait(
        filters=Filters(regions=RegionFilters(stop=["Москва"])),
    )
    result = derive_prefilter(p)
    assert 1 in result.area_ids_stop


def test_derive_prefilter_no_area_ids_require() -> None:
    p = _portrait(filters=Filters(regions=RegionFilters(primary=["Москва"])))
    result = derive_prefilter(p)
    assert result.area_ids_require == []


def test_derive_prefilter_unknown_stop_region_ignored() -> None:
    p = _portrait(
        filters=Filters(regions=RegionFilters(stop=["НесуществующийРегион"])),
    )
    result = derive_prefilter(p)
    assert result.area_ids_stop == []
