"""Tests for derive_initial_hh_params in hh_monitor.tg.add_vacancy.llm."""

from __future__ import annotations

from hh_monitor.fit.portrait import Filters, Portrait, RegionFilters
from hh_monitor.regions.expander import PRIMARY_AREA_IDS_21VEK
from hh_monitor.tg.add_vacancy.llm import derive_initial_hh_params


def _portrait(primary: list[str] | None = None) -> Portrait:
    kw: dict = {"position_code": "test_pos", "position_name": "Тест Менеджер"}
    if primary is not None:
        kw["filters"] = Filters(regions=RegionFilters(primary=primary))
    return Portrait(**kw)


def test_derive_no_regions_returns_text_only() -> None:
    result = derive_initial_hh_params(_portrait())
    assert result == {"text": "Тест Менеджер"}
    assert "area" not in result


def test_derive_with_21vek_macro_adds_area() -> None:
    result = derive_initial_hh_params(_portrait(["все регионы 21 века"]))
    assert result["text"] == "Тест Менеджер"
    assert result["area"] == list(PRIMARY_AREA_IDS_21VEK)


def test_derive_with_explicit_region_adds_area() -> None:
    """Explicit RF region name 'Москва' must resolve to area_id=1."""
    result = derive_initial_hh_params(_portrait(["Москва"]))
    assert result["text"] == "Тест Менеджер"
    assert result["area"] == [1]


def test_derive_with_nizhegorodskaya_adds_area() -> None:
    result = derive_initial_hh_params(_portrait(["Нижегородская область"]))
    assert result["text"] == "Тест Менеджер"
    assert result["area"] == [1679]


def test_derive_with_truly_unknown_region_text_only() -> None:
    result = derive_initial_hh_params(_portrait(["НесуществующийРегион"]))
    assert result == {"text": "Тест Менеджер"}
    assert "area" not in result


def test_derive_with_paraphrased_21vek_macro_adds_area() -> None:
    """LLM paraphrase 'Все регионы присутствия 21 Века (27)' must resolve."""
    result = derive_initial_hh_params(_portrait(["Все регионы присутствия 21 Века (27)"]))
    assert result["text"] == "Тест Менеджер"
    assert result["area"] == list(PRIMARY_AREA_IDS_21VEK)
