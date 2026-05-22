"""100% coverage target for hh_monitor.fit.rules."""

import json
from datetime import date
from pathlib import Path

import pytest

from hh_monitor.fit.portrait import Portrait, load_portrait
from hh_monitor.fit.rules import _parse_ym_months, compute

_F = Path(__file__).parent / "fixtures" / "resumes"
_P = Path(__file__).parent.parent / "portraits"


def _load(name: str) -> dict:  # type: ignore[type-arg]
    return json.loads((_F / name).read_text())


PORTRAIT = load_portrait(_P / "branch_director.json")

MINIMAL_PORTRAIT = Portrait(
    position_code="test",
    position_name="Test",
    title_keywords=[],
    experience_keywords=[],
    min_total_months=0,
    preferred_total_months=0,
)

# Portrait with explicit thresholds for total_experience tests.
EXP_PORTRAIT = Portrait(
    position_code="exp_test",
    position_name="Exp Test",
    title_keywords=[],
    experience_keywords=[],
    min_total_months=24,
    preferred_total_months=60,
)


# ── Relevant candidate (a_v2: director, 108 months, 220k, SPb, higher) ────


def test_relevant_candidate_high_score() -> None:
    score, bd = compute(_load("candidate_a_v2.json"), PORTRAIT)
    assert score > 70
    assert bd["title_match"] == 25
    assert bd["experience_keywords"] == 15
    assert bd["total_experience"] == 20  # 108 >= 84
    assert bd["salary_fit"] == 10  # 220k <= 350k, RUR
    assert bd["education"] == 5  # higher
    assert bd["area"] == 10  # СПб
    assert bd["age"] == 5  # 38 in [28,55]


# ── Irrelevant candidate (b_v1: accountant, 36 months, 80k, Moscow) ───────


def test_irrelevant_candidate_low_score() -> None:
    score, bd = compute(_load("candidate_b_v1.json"), PORTRAIT)
    assert score < 20
    assert bd["title_match"] == 0  # "бухгалтер" not in keywords
    assert bd["experience_keywords"] == 0  # no insurance/sales keywords
    assert bd["total_experience"] == -10  # 36 < 60
    assert bd["area"] == 0  # Москва not in [СПб]


# ── Partial candidate (a_v1: sales head, 96 months, 180k, SPb) ───────────


def test_partial_match_mid_score() -> None:
    score, bd = compute(_load("candidate_a_v1.json"), PORTRAIT)
    assert bd["title_match"] == 25  # "руководитель отдела" matches
    assert bd["total_experience"] == 20  # 96 >= 84 (preferred_total_months)
    assert bd["area"] == 10


# ── Empty payload: no crashes, only deterministic rules present ───────────


def test_empty_payload_does_not_raise() -> None:
    score, bd = compute({}, PORTRAIT)
    assert 0 <= score <= 100
    # Rules that always produce a value regardless of payload content:
    assert "title_match" in bd
    assert "experience_keywords" in bd
    assert "education" in bd
    assert "area" in bd
    assert "age" in bd
    # total_experience and salary_fit are skipped when payload lacks the data.
    assert "total_experience" not in bd
    assert "salary_fit" not in bd


# ── Salary: above max → penalty ───────────────────────────────────────────


def test_salary_above_max_penalty() -> None:
    payload = {**_load("candidate_a_v2.json"), "salary": {"amount": 400000, "currency": "RUR"}}
    _, bd = compute(payload, PORTRAIT)
    assert bd["salary_fit"] == -15


# ── Salary: absent → rule skipped ────────────────────────────────────────


def test_no_salary_skips_rule() -> None:
    payload = {k: v for k, v in _load("candidate_a_v2.json").items() if k != "salary"}
    _, bd = compute(payload, PORTRAIT)
    assert "salary_fit" not in bd


# ── Salary: non-RUR currency → rule skipped ──────────────────────────────


def test_salary_usd_skips_rule() -> None:
    payload = {**_load("candidate_a_v2.json"), "salary": {"amount": 5000, "currency": "USD"}}
    _, bd = compute(payload, PORTRAIT)
    assert "salary_fit" not in bd


def test_salary_currency_none_skips_rule() -> None:
    payload = {**_load("candidate_a_v2.json"), "salary": {"amount": 200000, "currency": None}}
    _, bd = compute(payload, PORTRAIT)
    assert "salary_fit" not in bd


def test_salary_none_skips_rule() -> None:
    payload = {**_load("candidate_a_v2.json"), "salary": None}
    _, bd = compute(payload, PORTRAIT)
    assert "salary_fit" not in bd


# ── Salary: RUR within budget → +10, over budget → -15 ──────────────────


def test_salary_rur_within_budget() -> None:
    payload = {"salary": {"amount": 180000, "currency": "RUR"}}
    _, bd = compute(payload, PORTRAIT)
    assert bd["salary_fit"] == 10


def test_salary_rur_over_budget() -> None:
    payload = {"salary": {"amount": 500000, "currency": "RUR"}}
    _, bd = compute(payload, PORTRAIT)
    assert bd["salary_fit"] == -15


# ── max_salary=None in portrait → always +10 when RUR ────────────────────


def test_no_max_salary_in_portrait() -> None:
    p = Portrait(
        position_code="x",
        position_name="x",
        title_keywords=[],
        experience_keywords=[],
        min_total_months=0,
        preferred_total_months=0,
        max_salary=None,
    )
    payload = {"salary": {"amount": 999999, "currency": "RUR"}}
    _, bd = compute(payload, p)
    assert bd["salary_fit"] == 10


# ── age_range=None → age always 0 ────────────────────────────────────────


def test_no_age_range_gives_zero() -> None:
    p = Portrait(
        position_code="x",
        position_name="x",
        title_keywords=[],
        experience_keywords=[],
        min_total_months=0,
        preferred_total_months=0,
        age_range=None,
    )
    _, bd = compute({"age": 35}, p)
    assert bd["age"] == 0


# ── age outside range → 0 ────────────────────────────────────────────────


def test_age_outside_range_gives_zero() -> None:
    payload = {**_load("candidate_a_v2.json"), "age": 65}
    _, bd = compute(payload, PORTRAIT)
    assert bd["age"] == 0


# ── score clamped at 100 ──────────────────────────────────────────────────


def test_score_clamped_at_100() -> None:
    score, _ = compute(_load("candidate_a_v2.json"), PORTRAIT)
    assert score <= 100


# ── score clamped at 0 ────────────────────────────────────────────────────


def test_score_clamped_at_0() -> None:
    payload = {
        "title": "нерелевантная должность xyz",
        "salary": {"amount": 999999, "currency": "RUR"},
        "total_experience": {"months": 1},
        "area": {"id": "3", "name": "Новосибирск"},
    }
    score, _ = compute(payload, PORTRAIT)
    assert score >= 0


# ── education not in preferred → 0 ───────────────────────────────────────


def test_wrong_education_gives_zero() -> None:
    payload = {**_load("candidate_b_v1.json")}  # secondary_vocational
    _, bd = compute(payload, PORTRAIT)
    assert bd["education"] == 0


# ── preferred_education_levels empty → always 0 ──────────────────────────


def test_empty_edu_list_gives_zero() -> None:
    _, bd = compute(_load("candidate_a_v2.json"), MINIMAL_PORTRAIT)
    assert bd["education"] == 0


# ══ Area rule ═════════════════════════════════════════════════════════════════


def test_area_exact_match() -> None:
    """Exact string match still works after the substring refactor."""
    payload = {"area": {"id": "2", "name": "Санкт-Петербург"}}
    _, bd = compute(payload, PORTRAIT)
    assert bd["area"] == 10


def test_area_substring_match_with_region() -> None:
    """Portrait 'Санкт-Петербург' is a substring of 'Санкт-Петербург и область' → +10."""
    payload = {"area": {"id": "2", "name": "Санкт-Петербург и область"}}
    _, bd = compute(payload, PORTRAIT)
    assert bd["area"] == 10


def test_area_no_match() -> None:
    payload = {"area": {"id": "1", "name": "Москва"}}
    _, bd = compute(payload, PORTRAIT)
    assert bd["area"] == 0


def test_area_none_gives_zero_no_crash() -> None:
    payload = {"area": None}
    _, bd = compute(payload, PORTRAIT)
    assert bd["area"] == 0


def test_area_key_missing_gives_zero() -> None:
    _, bd = compute({}, PORTRAIT)
    assert bd["area"] == 0


def test_area_string_instead_of_dict_gives_zero_no_crash() -> None:
    """Unexpected payload type must not raise."""
    payload = {"area": "СПб"}
    _, bd = compute(payload, PORTRAIT)
    assert bd["area"] == 0


# ══ Total experience rule ═════════════════════════════════════════════════════


def test_total_experience_between_min_and_preferred() -> None:
    # EXP_PORTRAIT: min=24, preferred=60; months=48 → in [24,60) → +10
    payload = {"total_experience": {"months": 48}}
    _, bd = compute(payload, EXP_PORTRAIT)
    assert bd["total_experience"] == 10


def test_total_experience_above_preferred() -> None:
    payload = {"total_experience": {"months": 120}}
    _, bd = compute(payload, EXP_PORTRAIT)
    assert bd["total_experience"] == 20


def test_total_experience_below_min() -> None:
    payload = {"total_experience": {"months": 12}}
    _, bd = compute(payload, EXP_PORTRAIT)
    assert bd["total_experience"] == -10


def test_total_experience_none_skips_rule() -> None:
    payload = {"total_experience": None}
    _, bd = compute(payload, EXP_PORTRAIT)
    assert "total_experience" not in bd


def test_total_experience_not_dict_skips_rule() -> None:
    """Scalar instead of dict must not raise and must skip the rule."""
    payload = {"total_experience": 48}
    _, bd = compute(payload, EXP_PORTRAIT)
    assert "total_experience" not in bd


def test_total_experience_empty_dict_skips_rule() -> None:
    payload = {"total_experience": {}}
    _, bd = compute(payload, EXP_PORTRAIT)
    assert "total_experience" not in bd


# ══ start/end fallback for total_experience ═══════════════════════════════════


def test_parse_ym_months_fixed_range() -> None:
    """2020-01 → 2023-05 = 40 months."""
    assert _parse_ym_months("2020-01", "2023-05") == 40


def test_parse_ym_months_invalid_start_returns_none() -> None:
    assert _parse_ym_months("invalid", "2023-05") is None


def test_experience_fallback_from_start_end() -> None:
    """total_experience absent → compute from experience[].start/end."""
    payload = {
        "experience": [
            {"start": "2020-01", "end": "2023-05"},  # 40 months
        ]
    }
    _, bd = compute(payload, EXP_PORTRAIT)
    # 40 >= 24 (min) but < 60 (preferred) → +10
    assert bd["total_experience"] == 10


def test_experience_fallback_open_ended(monkeypatch: pytest.MonkeyPatch) -> None:
    """end=None treated as current month (monkeypatched to 2024-01)."""
    monkeypatch.setattr("hh_monitor.fit.rules._today", lambda: date(2024, 1, 1))
    payload = {
        "experience": [
            {"start": "2020-01", "end": None},  # 2020-01 → 2024-01 = 48 months
        ]
    }
    _, bd = compute(payload, EXP_PORTRAIT)
    assert bd["total_experience"] == 10  # 48 in [24, 60)


def test_experience_fallback_invalid_start_skips_entry() -> None:
    """Bad start string skips that entry; if no valid entries → rule skipped."""
    payload = {
        "experience": [
            {"start": "not-a-date", "end": "2023-05"},
        ]
    }
    _, bd = compute(payload, EXP_PORTRAIT)
    assert "total_experience" not in bd


# ── Geography: primary / adjacent / stop regions (filters.regions.*) ──────────


def _geo_portrait(
    primary: list[str],
    adjacent: list[str],
    stop: list[str],
) -> Portrait:
    """Build a minimal portrait with the given region filters."""
    from hh_monitor.fit.portrait import Filters, RegionFilters

    return Portrait(
        position_code="geo_test",
        position_name="Geo Test",
        filters=Filters(regions=RegionFilters(primary=primary, adjacent=adjacent, stop=stop)),
    )


def _area_payload(area_name: str) -> dict:  # type: ignore[type-arg]
    return {"area": {"id": "78", "name": area_name, "url": ""}}


def test_region_primary_match_scores_full_weight() -> None:
    """Candidate in a primary region receives the full region weight (+10)."""
    portrait = _geo_portrait(primary=["Самарская область"], adjacent=[], stop=[])
    score, bd = compute(_area_payload("Самара, Самарская область"), portrait)
    assert bd["area"] == 10


def test_region_adjacent_match_scores_half_weight() -> None:
    """Candidate in an adjacent region receives region_weight // 2 (+5)."""
    portrait = _geo_portrait(
        primary=["Самарская область"],
        adjacent=["Оренбургская область"],
        stop=[],
    )
    score, bd = compute(_area_payload("Оренбург, Оренбургская область"), portrait)
    assert bd["area"] == 5  # 10 // 2


def test_region_stop_sets_huge_negative_and_clamps_to_zero() -> None:
    """Candidate in a stop region gets breakdown area = -(10**6), score clamps to 0."""
    portrait = _geo_portrait(
        primary=["Самарская область"],
        adjacent=[],
        stop=["Москва"],
    )
    score, bd = compute(_area_payload("Москва"), portrait)
    assert bd["area"] < 0
    assert score == 0


def test_region_stop_detected_by_caller_sentinel() -> None:
    """Callers detect stop-region via breakdown.get('area', 0) < 0."""
    portrait = _geo_portrait(primary=[], adjacent=[], stop=["Питер"])
    _, bd = compute(_area_payload("Санкт-Петербург (Питер)"), portrait)
    assert bd.get("area", 0) < 0


def test_region_no_match_gives_zero() -> None:
    """Candidate outside all known regions scores 0 for area (not negative)."""
    portrait = _geo_portrait(
        primary=["Самарская область"],
        adjacent=["Оренбургская область"],
        stop=["Москва"],
    )
    score, bd = compute(_area_payload("Новосибирск, Новосибирская область"), portrait)
    assert bd["area"] == 0
    assert score >= 0
