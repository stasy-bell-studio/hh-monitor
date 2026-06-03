"""Tests for hh_monitor.regions.expander — resolve_region_names."""

from __future__ import annotations

import pytest
import structlog.testing

from hh_monitor.regions.expander import PRIMARY_AREA_IDS_21VEK, resolve_region_names
from hh_monitor.regions.ru_areas import RU_AREAS

# ── 21Vek macro ──────────────────────────────────────────────────────────────────


def test_resolve_canonical_macro() -> None:
    ids, unknown = resolve_region_names(["все регионы 21 века"])
    assert ids == list(PRIMARY_AREA_IDS_21VEK)
    assert unknown == []


def test_resolve_paraphrased_21vek_macro() -> None:
    """LLM paraphrase with trailing "(27)" must still trigger the 21Vek macro."""
    ids, unknown = resolve_region_names(["Все регионы присутствия 21 Века (27)"])
    assert ids == list(PRIMARY_AREA_IDS_21VEK)
    assert unknown == []


def test_resolve_21vek_bare() -> None:
    ids, unknown = resolve_region_names(["21 Век"])
    assert ids == list(PRIMARY_AREA_IDS_21VEK)
    assert unknown == []


def test_resolve_21vek_latin() -> None:
    ids, unknown = resolve_region_names(["21vek"])
    assert ids == list(PRIMARY_AREA_IDS_21VEK)
    assert unknown == []


def test_resolve_dedupes_multiple_macros() -> None:
    ids, unknown = resolve_region_names(["21vek", "21 век", "все регионы 21 века"])
    assert ids == list(PRIMARY_AREA_IDS_21VEK)
    assert unknown == []


# ── Explicit RF region names ──────────────────────────────────────────────────────


def test_resolve_explicit_region_moskva() -> None:
    ids, unknown = resolve_region_names(["Москва"])
    assert ids == [1]
    assert unknown == []


def test_resolve_explicit_region_nizhegorodskaya() -> None:
    ids, unknown = resolve_region_names(["Нижегородская область"])
    assert ids == [1679]
    assert unknown == []


def test_resolve_explicit_region_krasnodarsky() -> None:
    ids, unknown = resolve_region_names(["Краснодарский край"])
    assert ids == [1438]
    assert unknown == []


# ── Normalisation ─────────────────────────────────────────────────────────────────


def test_resolve_case_insensitive() -> None:
    ids, unknown = resolve_region_names(["МОСКВА"])
    assert ids == [1]
    assert unknown == []


def test_resolve_trailing_number_stripped() -> None:
    """Trailing "(NN)" is stripped before lookup."""
    ids, unknown = resolve_region_names(["Москва (1)"])
    assert ids == [1]
    assert unknown == []


def test_resolve_whitespace_stripped() -> None:
    ids, unknown = resolve_region_names(["  Санкт-Петербург  "])
    assert ids == [2]
    assert unknown == []


# ── Stripped-type-word alias ──────────────────────────────────────────────────────


def test_resolve_stripped_oblast() -> None:
    """'нижегородская' (without 'область') should resolve via stripped alias."""
    ids, unknown = resolve_region_names(["нижегородская"])
    assert ids == [1679]
    assert unknown == []


def test_resolve_stripped_respublika() -> None:
    """'татарстан' (without 'республика') should resolve."""
    ids, unknown = resolve_region_names(["татарстан"])
    assert ids == [1624]
    assert unknown == []


# ── Spelling aliases ─────────────────────────────────────────────────────────────


def test_resolve_spb_alias() -> None:
    ids, unknown = resolve_region_names(["СПб"])
    assert ids == [2]
    assert unknown == []


def test_resolve_piter_alias() -> None:
    ids, unknown = resolve_region_names(["Питер"])
    assert ids == [2]
    assert unknown == []


def test_resolve_sevastopol_alias() -> None:
    """Севастополь is a federal city nested under Крым in HH's hierarchy
    (id=130, parent_id=2114, absent from /areas/113 top level).
    Must resolve by name, not only via the 21Vek macro."""
    ids, unknown = resolve_region_names(["Севастополь"])
    assert ids == [130]
    assert unknown == []


def test_resolve_krym_stripped() -> None:
    """'Крым' (without 'Республика') resolves via auto-stripped type-word alias."""
    ids, unknown = resolve_region_names(["Крым"])
    assert ids == [2114]
    assert unknown == []


# ── Unknown names ─────────────────────────────────────────────────────────────────


def test_resolve_unknown_name_in_unknown_list() -> None:
    ids, unknown = resolve_region_names(["НесуществующийГород"])
    assert ids == []
    assert unknown == ["НесуществующийГород"]


def test_resolve_unknown_preserves_original_casing() -> None:
    ids, unknown = resolve_region_names(["НесуществующийГород"])
    assert unknown == ["НесуществующийГород"]


def test_resolve_emits_no_logs() -> None:
    """resolve_region_names must never emit structlog events."""
    with structlog.testing.capture_logs() as captured:
        resolve_region_names(["НесуществующийГород", "21 Век", "Москва"])
    assert captured == []


def test_resolve_mixed_known_and_unknown() -> None:
    ids, unknown = resolve_region_names(["Москва", "НесуществующийГород", "Санкт-Петербург"])
    assert ids == [1, 2]
    assert unknown == ["НесуществующийГород"]


def test_resolve_empty_input() -> None:
    ids, unknown = resolve_region_names([])
    assert ids == []
    assert unknown == []


# ── Guard: PRIMARY_AREA_IDS_21VEK traceable in RU_AREAS ──────────────────────────


def test_primary_area_ids_traceable_in_ru_areas() -> None:
    """All 21Vek macro IDs must be traceable in RU_AREAS.

    ID 130 (Севастополь) is a city nested under Республика Крым (parent_id=2114)
    in HH's area hierarchy — not a top-level federal subject, hence absent from
    RU_AREAS (which covers /areas/113 top-level entries only).
    It is covered separately via _SPELLING_ALIASES.
    """
    _KNOWN_CITY_IDS = {130}
    missing = set(PRIMARY_AREA_IDS_21VEK) - set(RU_AREAS.values()) - _KNOWN_CITY_IDS
    assert not missing, f"IDs in PRIMARY_AREA_IDS_21VEK not found in RU_AREAS: {missing}"


# ── City resolution (RU_CITIES) ───────────────────────────────────────────────────


def test_city_resolves_to_parent_subject(monkeypatch: pytest.MonkeyPatch) -> None:
    import hh_monitor.regions.expander as exp
    import hh_monitor.regions.ru_areas as ra

    monkeypatch.setattr(ra, "RU_CITIES", {"екатеринбург": 1261})
    monkeypatch.setattr(exp, "RU_CITIES", {"екатеринбург": 1261})
    ids, unknown = resolve_region_names(["Екатеринбург"])
    assert ids == [1261]
    assert unknown == []


def test_ambiguous_city_returned_as_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    import hh_monitor.regions.expander as exp
    import hh_monitor.regions.ru_areas as ra

    monkeypatch.setattr(ra, "RU_AMBIGUOUS_CITIES", {"спорный": (1, 2)})
    monkeypatch.setattr(exp, "RU_AMBIGUOUS_CITIES", {"спорный": (1, 2)})
    ids, unknown = resolve_region_names(["Спорный"])
    assert ids == []
    assert unknown == ["Спорный"]


def test_mixed_subject_and_city_deduplicated(monkeypatch: pytest.MonkeyPatch) -> None:
    """Subject 'Свердловская область' (→1261) + city 'Екатеринбург' (→1261 via RU_CITIES)
    must deduplicate to a single id."""
    import hh_monitor.regions.expander as exp
    import hh_monitor.regions.ru_areas as ra

    monkeypatch.setattr(ra, "RU_CITIES", {"екатеринбург": 1261})
    monkeypatch.setattr(exp, "RU_CITIES", {"екатеринбург": 1261})
    ids, unknown = resolve_region_names(["Свердловская область", "Екатеринбург"])
    assert ids == [1261]
    assert unknown == []


def test_city_normalization_symmetric(monkeypatch: pytest.MonkeyPatch) -> None:
    """RU_CITIES key 'курск' (stripped from 'Курск (42)  ') must resolve input 'Курск'."""
    import hh_monitor.regions.expander as exp
    import hh_monitor.regions.ru_areas as ra

    monkeypatch.setattr(ra, "RU_CITIES", {"курск": 1880})
    monkeypatch.setattr(exp, "RU_CITIES", {"курск": 1880})
    ids, unknown = resolve_region_names(["Курск"])
    assert ids == [1880]
    assert unknown == []


# ── AC1: live data — cities resolve to correct parent subjects ────────────────────


def test_ac1_cities_resolve_live() -> None:
    """Екатеринбург→1261, Тамбов→1905, Краснодар→1438, Оренбург→1563."""
    cases = ["Екатеринбург", "Тамбов", "Краснодар", "Оренбург"]
    ids, unknown = resolve_region_names(cases)
    assert unknown == []
    assert 1261 in ids  # Свердловская область
    assert 1905 in ids  # Тамбовская область
    assert 1438 in ids  # Краснодарский край
    assert 1563 in ids  # Оренбургская область


def test_subject_lookup_unaffected_by_ru_cities() -> None:
    """Existing subject 'Москва' must still resolve to 1 via RU_AREAS, not RU_CITIES."""
    ids, unknown = resolve_region_names(["Москва"])
    assert ids == [1]
    assert unknown == []


def test_all_21vek_ids_resolve_by_name() -> None:
    """Every ID in PRIMARY_AREA_IDS_21VEK must be reachable by typing its region name.

    This catches the class of bug where a region can only be addressed via the
    macro but returns ⚠️ unknown when typed explicitly (e.g. 'Севастополь').
    """
    reverse = {v: k for k, v in RU_AREAS.items()}
    # 130 (Севастополь) is in _SPELLING_ALIASES, not RU_AREAS; name is known.
    extra: dict[int, str] = {130: "Севастополь"}

    failures: list[str] = []
    for area_id in PRIMARY_AREA_IDS_21VEK:
        name = reverse.get(area_id) or extra.get(area_id)
        assert name is not None, f"No name found for area_id={area_id}"
        ids, unk = resolve_region_names([name.title()])
        if area_id not in ids:
            failures.append(f"area_id={area_id} name={name!r} resolved_ids={ids}")

    assert not failures, "Some 21Vek IDs do not resolve by name:\n" + "\n".join(failures)
