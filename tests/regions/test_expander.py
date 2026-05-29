"""Tests for hh_monitor.regions.expander — resolve_region_names."""

from __future__ import annotations

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
