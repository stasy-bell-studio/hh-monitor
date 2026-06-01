"""Tests for hh_monitor.fit.rules — scoring v2 (Lesnitskaya etalon v1).

Scoring formula:
  1. Seven hard filters → score=0, breakdown["hard_reject_reason"] set.
  2. Six weighted criteria (max raw = 45).
  3. fit_score = round(total_raw / 45 * 100), clamped [0, 100].

Principles preserved from v1 tests:
  - Strong candidate → high score (≥ 70).
  - Weak candidate → low score (≤ 30).
  - Hard-stop candidate → score = 0, reason in breakdown.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

import pytest

from hh_monitor.fit.portrait import Filters, Portrait, RegionFilters, Weights
from hh_monitor.fit.rules import (
    _insurance_experience_months,
    _max_career_gap_months,
    _motor_experience_months,
    _parse_ym_months,
    compute,
)

# ── Helper factories ──────────────────────────────────────────────────────────


def _portrait(
    *,
    age_range: tuple[int, int] | None = None,
    higher_education_required: bool = False,
    primary_regions: list[str] | None = None,
    adjacent_regions: list[str] | None = None,
    stop_regions: list[str] | None = None,
    forbidden_industries: list[str] | None = None,
    max_career_gap_months: int = 0,
    min_total_months: int = 0,
    min_insurance_experience_months: int = 0,
    min_motor_experience_months: int = 0,
    motor_experience_preferred: bool = False,
    min_tenure_last_job_months: int = 0,
    bonus_companies: list[str] | None = None,
    preferred_education_fields: list[str] | None = None,
    forbidden_industry_mode: Literal["soft", "hard"] = "soft",
    role_match_mode: Literal["soft", "hard"] = "soft",
) -> Portrait:
    return Portrait(
        position_code="test",
        position_name="Test Position",
        higher_education_required=higher_education_required,
        max_career_gap_months=max_career_gap_months,
        min_total_months=min_total_months,
        min_insurance_experience_months=min_insurance_experience_months,
        min_motor_experience_months=min_motor_experience_months,
        motor_experience_preferred=motor_experience_preferred,
        min_tenure_last_job_months=min_tenure_last_job_months,
        bonus_companies=bonus_companies or [],
        preferred_education_fields=preferred_education_fields or [],
        forbidden_industries=forbidden_industries or [],
        forbidden_industry_mode=forbidden_industry_mode,
        role_match_mode=role_match_mode,
        filters=Filters(
            age_range=age_range,
            regions=RegionFilters(
                primary=primary_regions or [],
                adjacent=adjacent_regions or [],
                stop=stop_regions or [],
            ),
        ),
    )


def _etalon_portrait() -> Portrait:
    """Portrait matching branch_director.yaml defaults (for integration tests)."""
    from hh_monitor.fit.portrait import load_all_portraits

    return load_all_portraits()["branch_director"]


def _resume(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Baseline strong candidate payload (insurance director, SPb, higher edu)."""
    payload: dict[str, Any] = {
        "id": "test001",
        "age": 35,
        "area": {"id": "2", "name": "Санкт-Петербург"},
        "total_experience": {"months": 120},
        "education": {
            "level": {"id": "higher"},
            "primary": [{"name": "Финансовый факультет"}],
        },
        "experience": [
            {
                "company": "ВСК Страхование",
                "position": "Директор филиала",
                "start": "2019-01",
                "end": None,
                "description": (
                    "Руководство агентской сетью. Развитие агентского канала. "
                    "Продажи ОСАГО, КАСКО и ИФЛ. Управление командой 60 агентов."
                ),
            },
            {
                "company": "ООО Страховой брокер",
                "position": "Руководитель отдела продаж",
                "start": "2015-03",
                "end": "2018-12",
                "description": "Продажи страховых продуктов. Развитие агентской сети.",
            },
        ],
        "key_skills": ["ОСАГО", "КАСКО", "ИФЛ", "Страхование"],
    }
    if extra:
        payload.update(extra)
    return payload


# ── _parse_ym_months ──────────────────────────────────────────────────────────


def test_parse_ym_months_fixed_range() -> None:
    """2020-01 → 2023-05 = 40 months."""
    assert _parse_ym_months("2020-01", "2023-05") == 40


def test_parse_ym_months_invalid_start_returns_none() -> None:
    assert _parse_ym_months("invalid", "2023-05") is None


def test_parse_ym_months_open_ended(monkeypatch: pytest.MonkeyPatch) -> None:
    """end=None → current month (monkeypatched to 2024-01)."""
    monkeypatch.setattr("hh_monitor.fit.rules._today", lambda: date(2024, 1, 1))
    assert _parse_ym_months("2022-01", None) == 24  # 2 years


# ── _insurance_experience_months ─────────────────────────────────────────────


def test_insurance_months_company_name() -> None:
    """Company name containing 'страхов' counts as insurance experience."""
    exps = [{"company": "ООО Страховой брокер", "start": "2020-01", "end": "2022-01"}]
    assert _insurance_experience_months(exps) == 24


def test_insurance_months_osago_keyword() -> None:
    """Description with 'ОСАГО' counts as insurance experience."""
    exps = [
        {
            "company": "Авто Ltd",
            "position": "Менеджер",
            "start": "2021-01",
            "end": "2022-01",
            "description": "Продажи ОСАГО и КАСКО",
        }
    ]
    assert _insurance_experience_months(exps) == 12


def test_insurance_months_non_insurance_skipped() -> None:
    """Non-insurance entry is skipped."""
    exps = [
        {
            "company": "Банк ВТБ",
            "position": "Кредитный менеджер",
            "start": "2020-01",
            "end": "2022-01",
            "description": "Кредитование",
        }
    ]
    assert _insurance_experience_months(exps) == 0


def test_insurance_months_mixed_entries() -> None:
    """Only insurance-related entries are summed."""
    exps = [
        {"company": "ООО Страховщик", "start": "2020-01", "end": "2022-01"},  # 24
        {"company": "Банк МФО", "start": "2018-01", "end": "2020-01"},  # 0
    ]
    assert _insurance_experience_months(exps) == 24


# ── _motor_experience_months ──────────────────────────────────────────────────


def test_motor_months_kasko_detected() -> None:
    """Entry with 'КАСКО' in description counts as motor experience."""
    exps = [
        {
            "company": "СК Ресо",
            "position": "Андеррайтер",
            "start": "2021-01",
            "end": "2023-01",
            "description": "Оценка рисков КАСКО",
        }
    ]
    assert _motor_experience_months(exps) == 24


def test_motor_months_osago_in_company() -> None:
    """Entry with 'ОСАГО' in company name counts as motor experience."""
    exps = [{"company": "Центр ОСАГО", "start": "2020-06", "end": "2022-06"}]
    assert _motor_experience_months(exps) == 24


def test_motor_months_non_motor_skipped() -> None:
    """Entry without motor stems is not counted."""
    exps = [
        {
            "company": "ДМС Клиника",
            "position": "Специалист ДМС",
            "start": "2019-01",
            "end": "2022-01",
            "description": "Работа со страхованием жизни",
        }
    ]
    assert _motor_experience_months(exps) == 0


def test_motor_months_keyword_variants() -> None:
    """Stems автострахов, мтпл, моторн are detected."""
    exps_auto = [{"company": "ООО Автострахование", "start": "2020-01", "end": "2021-01"}]
    exps_mtpl = [
        {
            "company": "СК",
            "position": "Эксперт",
            "start": "2020-01",
            "end": "2021-01",
            "description": "Урегулирование убытков МТПЛ",
        }
    ]
    exps_motor = [
        {"company": "СК", "position": "Моторное страхование", "start": "2020-01", "end": "2021-01"}
    ]
    assert _motor_experience_months(exps_auto) == 12
    assert _motor_experience_months(exps_mtpl) == 12
    assert _motor_experience_months(exps_motor) == 12


# ── _max_career_gap_months ────────────────────────────────────────────────────


def test_career_gap_no_gap() -> None:
    """Consecutive entries with no overlap → gap = 0."""
    exps = [
        {"start": "2018-01", "end": "2020-01"},
        {"start": "2020-01", "end": "2022-06"},
    ]
    assert _max_career_gap_months(exps) == 0


def test_career_gap_detects_gap() -> None:
    """12-month gap between roles detected."""
    exps = [
        {"start": "2016-01", "end": "2018-01"},  # ends Jan 2018
        {"start": "2019-01", "end": "2022-01"},  # starts Jan 2019 → gap = 12
    ]
    assert _max_career_gap_months(exps) == 12


def test_career_gap_single_entry() -> None:
    """Single entry → no gap possible."""
    exps = [{"start": "2020-01", "end": "2022-01"}]
    assert _max_career_gap_months(exps) == 0


# ══ Hard filters ══════════════════════════════════════════════════════════════


def test_hard_reject_age_below_min() -> None:
    """Candidate younger than age_range[0] → hard rejected."""
    p = _portrait(age_range=(30, 50))
    score, bd = compute({"age": 25}, p)
    assert score == 0
    assert bd["hard_reject_reason"] == "age"


def test_hard_reject_age_above_max() -> None:
    """Candidate older than age_range[1] → hard rejected."""
    p = _portrait(age_range=(25, 45))
    score, bd = compute({"age": 55}, p)
    assert score == 0
    assert bd["hard_reject_reason"] == "age"


def test_no_age_in_payload_skips_age_filter() -> None:
    """If age is not in payload, age filter is not applied."""
    p = _portrait(age_range=(25, 45))
    score, bd = compute({}, p)
    assert bd.get("hard_reject_reason") != "age"


def test_hard_reject_education_missing_higher() -> None:
    """higher_education_required=True + no higher edu → hard rejected."""
    p = _portrait(higher_education_required=True)
    score, bd = compute({"education": {"level": {"id": "secondary"}}}, p)
    assert score == 0
    assert bd["hard_reject_reason"] == "education"


def test_no_higher_edu_required_passes() -> None:
    """higher_education_required=False → no education hard filter."""
    p = _portrait(higher_education_required=False)
    score, bd = compute({"education": {"level": {"id": "secondary"}}}, p)
    assert bd.get("hard_reject_reason") != "education"


def test_hard_reject_stop_region() -> None:
    """Candidate in stop region → hard rejected with stop_region reason."""
    p = _portrait(stop_regions=["Москва"])
    score, bd = compute({"area": {"id": "1", "name": "Москва"}}, p)
    assert score == 0
    assert bd["hard_reject_reason"] == "stop_region"


def test_stop_region_substring_match() -> None:
    """Stop region is matched as a substring of the area name."""
    p = _portrait(stop_regions=["Архангельская"])
    score, bd = compute({"area": {"name": "Архангельск, Архангельская область"}}, p)
    assert score == 0
    assert bd["hard_reject_reason"] == "stop_region"


def test_hard_reject_forbidden_industry_last_job() -> None:
    """Most recent job in forbidden industry (hard mode) → hard rejected."""
    p = _portrait(forbidden_industries=["банк"], forbidden_industry_mode="hard")
    payload = {
        "experience": [
            {
                "company": "Сбербанк",
                "position": "Менеджер",
                "start": "2022-01",
                "end": None,
                "description": "Кредитование",
            },
            {
                "company": "Росгосстрах",
                "position": "Агент",
                "start": "2018-01",
                "end": "2021-12",
                "description": "Страхование",
            },
        ]
    }
    score, bd = compute(payload, p)
    assert score == 0
    assert bd["hard_reject_reason"] == "forbidden_industry"


def test_forbidden_industry_older_job_does_not_reject() -> None:
    """Forbidden industry in an OLDER job (not the last) does NOT trigger hard reject."""
    p = _portrait(forbidden_industries=["банк"])
    payload = {
        "experience": [
            # Most recent: insurance company
            {
                "company": "ВСК Страхование",
                "position": "Директор",
                "start": "2022-01",
                "end": None,
                "description": "Страхование",
            },
            # Older: bank — should NOT trigger
            {
                "company": "Сбербанк",
                "position": "Менеджер",
                "start": "2018-01",
                "end": "2021-12",
                "description": "Кредитование",
            },
        ]
    }
    score, bd = compute(payload, p)
    assert bd.get("hard_reject_reason") != "forbidden_industry"


def test_hard_reject_career_gap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Career gap exceeding max_career_gap_months → hard rejected."""
    monkeypatch.setattr("hh_monitor.fit.rules._today", lambda: date(2026, 1, 1))
    p = _portrait(max_career_gap_months=36)
    payload = {
        "experience": [
            {"start": "2015-01", "end": "2018-01"},  # ends Jan 2018
            {"start": "2022-01", "end": "2026-01"},  # starts Jan 2022 → gap=48 months
        ]
    }
    score, bd = compute(payload, p)
    assert score == 0
    assert bd["hard_reject_reason"] == "career_gap"


def test_career_gap_within_limit_passes() -> None:
    """Gap within limit does not trigger hard reject."""
    p = _portrait(max_career_gap_months=36)
    payload = {
        "experience": [
            {"start": "2016-01", "end": "2020-01"},
            {"start": "2021-01", "end": "2023-01"},  # gap=12 months
        ]
    }
    score, bd = compute(payload, p)
    assert bd.get("hard_reject_reason") != "career_gap"


def test_hard_reject_total_experience() -> None:
    """Total experience below minimum → hard rejected."""
    p = _portrait(min_total_months=60)
    score, bd = compute({"total_experience": {"months": 24}}, p)
    assert score == 0
    assert bd["hard_reject_reason"] == "total_experience"


def test_total_experience_at_minimum_passes() -> None:
    """Exactly at minimum → not hard rejected."""
    p = _portrait(min_total_months=60)
    score, bd = compute({"total_experience": {"months": 60}}, p)
    assert bd.get("hard_reject_reason") != "total_experience"


def test_total_experience_unknown_skips_filter() -> None:
    """Unknown total experience (None) → filter not applied (lenient)."""
    p = _portrait(min_total_months=60)
    score, bd = compute({}, p)
    # Missing data → we don't hard-reject (false negative > false positive)
    assert bd.get("hard_reject_reason") != "total_experience"


def test_hard_reject_insurance_experience() -> None:
    """Insurance experience below minimum → hard rejected."""
    p = _portrait(min_insurance_experience_months=36)
    payload = {
        "experience": [
            {
                "company": "Банк ВТБ",
                "position": "Кредитный менеджер",
                "start": "2018-01",
                "end": "2024-01",
                "description": "Кредитование",
            },
        ]
    }
    score, bd = compute(payload, p)
    assert score == 0
    assert bd["hard_reject_reason"] == "insurance_experience"


# ══ Scored criteria ═══════════════════════════════════════════════════════════


def _scored_portrait() -> Portrait:
    """Portrait for scored criteria tests — all hard filters disabled."""
    return Portrait(
        position_code="scored_test",
        position_name="Scored Test",
        bonus_companies=["Ресо-Гарантия", "ВСК"],
        preferred_education_fields=["финансы", "экономика", "страхование"],
        weights=Weights(
            agent_network_experience=10,
            osago_knowledge=9,
            target_region_primary=8,
            target_region_adjacent=4,
            ifl_experience=7,
            top4_competitor_experience=6,
            higher_specialized_education=5,
        ),
        filters=Filters(
            regions=RegionFilters(
                primary=["Самарская область"],
                adjacent=["Оренбургская область"],
                stop=[],
            )
        ),
    )


def test_agent_network_full_score() -> None:
    """'агентск' in experience description → full agent_network_experience weight."""
    p = _scored_portrait()
    payload = {
        "experience": [
            {
                "company": "X",
                "position": "Директор",
                "start": "2020-01",
                "end": None,
                "description": "Управление агентской сетью страховой компании.",
            }
        ]
    }
    _, bd = compute(payload, p)
    assert bd["agent_network_experience"] == 10


def test_agent_network_partial_score() -> None:
    """'агентов' (standalone) without 'агентск' → half the agent weight."""
    p = _scored_portrait()
    payload = {
        "experience": [
            {
                "company": "X",
                "position": "Директор",
                "start": "2020-01",
                "end": None,
                "description": "Работа с агентами, управление продажами.",
            }
        ]
    }
    _, bd = compute(payload, p)
    assert bd["agent_network_experience"] == 5


def test_agent_network_no_score() -> None:
    """No agent keywords → 0."""
    p = _scored_portrait()
    payload = {
        "experience": [
            {
                "company": "X",
                "position": "Кредитный менеджер",
                "start": "2020-01",
                "end": None,
                "description": "Работа с клиентами банка.",
            }
        ]
    }
    _, bd = compute(payload, p)
    assert bd["agent_network_experience"] == 0


def test_osago_knowledge_full() -> None:
    """'осаго' in text → full osago_knowledge weight."""
    p = _scored_portrait()
    payload = {
        "experience": [
            {
                "company": "X",
                "position": "Y",
                "start": "2020-01",
                "end": None,
                "description": "Продажи ОСАГО и КАСКО.",
            }
        ]
    }
    _, bd = compute(payload, p)
    assert bd["osago_knowledge"] == 9


def test_osago_knowledge_via_skills() -> None:
    """'каско' in key_skills → full osago_knowledge weight."""
    p = _scored_portrait()
    payload = {"key_skills": ["КАСКО", "Страхование"]}
    _, bd = compute(payload, p)
    assert bd["osago_knowledge"] == 9


def test_osago_knowledge_zero_without_keywords() -> None:
    p = _scored_portrait()
    _, bd = compute({"experience": []}, p)
    assert bd["osago_knowledge"] == 0


def test_region_primary_scores_primary_weight() -> None:
    """Primary region → target_region_primary weight."""
    p = _scored_portrait()
    _, bd = compute({"area": {"name": "Самара, Самарская область"}}, p)
    assert bd["region"] == 8


def test_region_adjacent_scores_adjacent_weight() -> None:
    """Adjacent region → target_region_adjacent weight."""
    p = _scored_portrait()
    _, bd = compute({"area": {"name": "Оренбург, Оренбургская область"}}, p)
    assert bd["region"] == 4


def test_region_max_not_additive() -> None:
    """Region score is max(primary, adjacent), not their sum."""
    # A portrait where candidate is in primary (shouldn't also count adjacent)
    p = Portrait(
        position_code="t",
        position_name="T",
        filters=Filters(
            regions=RegionFilters(
                primary=["Самара"],
                adjacent=["Самара"],  # both include Samara
                stop=[],
            )
        ),
        weights=Weights(target_region_primary=8, target_region_adjacent=4),
    )
    _, bd = compute({"area": {"name": "Самара"}}, p)
    assert bd["region"] == 8  # max, not 8+4


def test_region_no_match_zero() -> None:
    p = _scored_portrait()
    _, bd = compute({"area": {"name": "Новосибирск"}}, p)
    assert bd["region"] == 0


def test_ifl_experience_full() -> None:
    """'ИФЛ' keyword → full ifl_experience weight."""
    p = _scored_portrait()
    payload = {
        "experience": [
            {
                "company": "X",
                "position": "Y",
                "start": "2020-01",
                "end": None,
                "description": "Продажи ИФЛ и страхование имущества.",
            }
        ]
    }
    _, bd = compute(payload, p)
    assert bd["ifl_experience"] == 7


def test_ifl_long_form() -> None:
    """'имущество физических лиц' → ifl credit."""
    p = _scored_portrait()
    payload = {
        "experience": [
            {
                "company": "X",
                "position": "Y",
                "start": "2020-01",
                "end": None,
                "description": "Страхование имущества физических лиц.",
            }
        ]
    }
    _, bd = compute(payload, p)
    assert bd["ifl_experience"] == 7


def test_ifl_zero_without_keywords() -> None:
    p = _scored_portrait()
    _, bd = compute({}, p)
    assert bd["ifl_experience"] == 0


def test_top4_competitor_full() -> None:
    """Bonus company found in experience → full top4_competitor weight."""
    p = _scored_portrait()
    payload = {
        "experience": [
            {
                "company": "Ресо-Гарантия",
                "position": "Менеджер",
                "start": "2020-01",
                "end": None,
                "description": "Страхование",
            }
        ]
    }
    _, bd = compute(payload, p)
    assert bd["top4_competitor_experience"] == 6


def test_top4_competitor_case_insensitive() -> None:
    """Company name matching is case-insensitive."""
    p = _scored_portrait()
    payload = {
        "experience": [
            {
                "company": "вск страхование",
                "position": "Y",
                "start": "2020-01",
                "end": None,
                "description": "",
            }
        ]
    }
    _, bd = compute(payload, p)
    assert bd["top4_competitor_experience"] == 6


def test_top4_competitor_zero_for_other_company() -> None:
    p = _scored_portrait()
    payload = {
        "experience": [
            {
                "company": "ООО Рога и Копыта",
                "position": "Y",
                "start": "2020-01",
                "end": None,
                "description": "",
            }
        ]
    }
    _, bd = compute(payload, p)
    assert bd["top4_competitor_experience"] == 0


def test_higher_specialized_education_full() -> None:
    """Higher edu + matching specialization → full higher_specialized_education weight."""
    p = _scored_portrait()
    payload = {
        "education": {
            "level": {"id": "higher"},
            "primary": [{"name": "Финансовый факультет"}],
        }
    }
    _, bd = compute(payload, p)
    assert bd["higher_specialized_education"] == 5


def test_higher_edu_no_spec_match_zero() -> None:
    """Higher edu but non-matching specialization → 0."""
    p = _scored_portrait()
    payload = {
        "education": {
            "level": {"id": "higher"},
            "primary": [{"name": "Исторический факультет"}],
        }
    }
    _, bd = compute(payload, p)
    assert bd["higher_specialized_education"] == 0


def test_non_higher_edu_no_spec_credit() -> None:
    """Secondary education does not earn specialization credit."""
    p = _scored_portrait()
    payload = {
        "education": {
            "level": {"id": "secondary"},
            "primary": [{"name": "Финансовый колледж"}],
        }
    }
    _, bd = compute(payload, p)
    assert bd["higher_specialized_education"] == 0


# ══ Normalisation ══════════════════════════════════════════════════════════════


def test_perfect_candidate_scores_100() -> None:
    """A candidate matching all criteria scores 100 (or very close due to rounding)."""
    p = _scored_portrait()
    payload = {
        "area": {"name": "Самара, Самарская область"},  # primary (+8)
        "education": {
            "level": {"id": "higher"},
            "primary": [{"name": "Финансовый факультет"}],  # spec match (+5)
        },
        "experience": [
            {
                "company": "Ресо-Гарантия",  # top4 (+6)
                "position": "Директор",
                "start": "2018-01",
                "end": None,
                "description": (
                    "Руководство агентской сетью. "  # agent_network (+10)
                    "Продажи ОСАГО и КАСКО. "  # osago (+9)
                    "Страхование ИФЛ."  # ifl (+7)
                ),
            }
        ],
    }
    score, bd = compute(payload, p)
    # 10+9+8+7+6+5 = 45 raw → 100%
    assert score == 100
    assert bd["agent_network_experience"] == 10
    assert bd["osago_knowledge"] == 9
    assert bd["region"] == 8
    assert bd["ifl_experience"] == 7
    assert bd["top4_competitor_experience"] == 6
    assert bd["higher_specialized_education"] == 5


def test_empty_payload_scores_zero() -> None:
    """Empty payload passes all hard filters and scores 0 for all criteria."""
    p = _scored_portrait()
    score, bd = compute({}, p)
    assert score == 0
    assert "hard_reject_reason" not in bd
    assert bd["agent_network_experience"] == 0
    assert bd["osago_knowledge"] == 0
    assert bd["region"] == 0
    assert bd["ifl_experience"] == 0
    assert bd["top4_competitor_experience"] == 0
    assert bd["higher_specialized_education"] == 0


def test_score_clamped_at_100() -> None:
    p = _scored_portrait()
    score, _ = compute(_resume(), p)
    assert score <= 100


def test_score_clamped_at_0_minimum() -> None:
    p = _scored_portrait()
    score, _ = compute({}, p)
    assert score >= 0


# ══ Integration: strong vs. weak candidate ════════════════════════════════════


def test_strong_candidate_high_score() -> None:
    """Strong insurance director candidate (all criteria met) → score ≥ 70."""
    p = _etalon_portrait()
    # Patch age filter: our baseline resume age 35 is within [25, 45]
    payload = _resume()
    # Must also have insurance experience ≥ 36 months
    # _resume() has ВСК Страхование + Страховой брокер → insurance detected
    score, bd = compute(payload, p)
    assert "hard_reject_reason" not in bd, f"Unexpected hard reject: {bd.get('hard_reject_reason')}"
    assert score >= 50, f"Strong candidate scored only {score}"


def test_weak_candidate_low_score() -> None:
    """Non-insurance accountant with wrong region → low score."""
    p = _scored_portrait()
    payload = {
        "age": 35,
        "area": {"name": "Новосибирск"},
        "experience": [
            {
                "company": "ООО Ромашка",
                "position": "Бухгалтер",
                "start": "2020-01",
                "end": None,
                "description": "Бухгалтерский учёт.",
            }
        ],
        "education": {"level": {"id": "secondary"}},
    }
    score, bd = compute(payload, p)
    assert "hard_reject_reason" not in bd
    assert score <= 20


def test_hard_stop_candidate_scores_zero() -> None:
    """Candidate in stop region always scores 0 regardless of other criteria."""
    p = _portrait(stop_regions=["Архангельская"])
    payload = _resume({"area": {"name": "Архангельск, Архангельская область"}})
    score, bd = compute(payload, p)
    assert score == 0
    assert bd["hard_reject_reason"] == "stop_region"


# ══ Hard filter: current_role_mismatch ════════════════════════════════════════
#
# Filter is active only when portrait.position_synonyms is non-empty.
# Two pass paths: (a) synonym substring match, (b) group-A + group-B combo.


def _role_filter_portrait() -> Portrait:
    """Portrait with synonyms, hard role mode (tests the legacy hard-reject path)."""
    return Portrait(
        position_code="test_role",
        position_name="Директор филиала",
        role_match_mode="hard",
        position_synonyms=[
            "Руководитель филиала",
            "Региональный директор",
            "Управляющий представительства",
            "Директор представительства",
            "Менеджер филиала",
        ],
        filters=Filters(regions=RegionFilters(primary=[], adjacent=[], stop=[])),
    )


def _role_resume(position: str) -> dict[str, Any]:
    """Minimal resume payload where the most recent experience has *position* as title."""
    return {
        "id": "role_test",
        "experience": [
            {
                "company": "Страховая компания",
                "position": position,
                "start": "2020-01",
                "end": None,
                "description": "",
            }
        ],
    }


# ── Passes (should NOT be rejected) ──────────────────────────────────────────


def test_current_role_passes_position_name() -> None:
    """Latest position == portrait.position_name → passes (path a, exact match)."""
    p = _role_filter_portrait()
    _, bd = compute(_role_resume("Директор филиала"), p)
    assert bd.get("hard_reject_reason") not in ("current_role_mismatch", "current_role_unknown")


def test_current_role_passes_position_name_as_substring() -> None:
    """position_name is a substring of the title → passes (path a)."""
    p = _role_filter_portrait()
    _, bd = compute(_role_resume("Директор филиала по продажам в регионе"), p)
    assert bd.get("hard_reject_reason") not in ("current_role_mismatch", "current_role_unknown")


def test_current_role_passes_synonym_regional_director() -> None:
    """'Региональный директор' is a synonym → passes (path a)."""
    p = _role_filter_portrait()
    _, bd = compute(_role_resume("Региональный директор"), p)
    assert bd.get("hard_reject_reason") not in ("current_role_mismatch", "current_role_unknown")


def test_current_role_passes_combo_manager_branch() -> None:
    """'Управляющий филиалом' — управляющ(A) + филиал(B) → passes (path b)."""
    p = _role_filter_portrait()
    _, bd = compute(_role_resume("Управляющий филиалом"), p)
    assert bd.get("hard_reject_reason") not in ("current_role_mismatch", "current_role_unknown")


def test_current_role_passes_combo_director_representative() -> None:
    """'Директор представительства' — директор(A) + представительств(B) → passes."""
    p = _role_filter_portrait()
    _, bd = compute(_role_resume("Директор представительства"), p)
    assert bd.get("hard_reject_reason") not in ("current_role_mismatch", "current_role_unknown")


def test_current_role_passes_combo_head_office() -> None:
    """'Руководитель офиса продаж' — руководитель(A) + офис(B) → passes (path b)."""
    p = _role_filter_portrait()
    _, bd = compute(_role_resume("Руководитель офиса продаж"), p)
    assert bd.get("hard_reject_reason") not in ("current_role_mismatch", "current_role_unknown")


def test_current_role_passes_via_resume_title_fallback() -> None:
    """When experience is empty, resume.title is used for the check."""
    p = _role_filter_portrait()
    payload = {"id": "title_test", "title": "Директор филиала"}  # no experience key
    _, bd = compute(payload, p)
    assert bd.get("hard_reject_reason") not in ("current_role_mismatch", "current_role_unknown")


# ── Rejected — wrong current role ─────────────────────────────────────────────


def test_current_role_rejects_product_manager() -> None:
    """'Product Manager' — no Russian management+branch combo → mismatch."""
    p = _role_filter_portrait()
    score, bd = compute(_role_resume("Product Manager"), p)
    assert score == 0
    assert bd["hard_reject_reason"] == "current_role_mismatch"


def test_current_role_rejects_accountant() -> None:
    """'Главный бухгалтер' — classic false-positive; no A+B → mismatch."""
    p = _role_filter_portrait()
    score, bd = compute(_role_resume("Главный бухгалтер"), p)
    assert score == 0
    assert bd["hard_reject_reason"] == "current_role_mismatch"


def test_current_role_rejects_director_marketing() -> None:
    """'Директор по маркетингу' — директор(A) but no group-B word → mismatch."""
    p = _role_filter_portrait()
    score, bd = compute(_role_resume("Директор по маркетингу"), p)
    assert score == 0
    assert bd["hard_reject_reason"] == "current_role_mismatch"


def test_current_role_rejects_director_procurement() -> None:
    """'Директор по закупкам' — директор(A) but 'закупки' ∉ group B → mismatch."""
    p = _role_filter_portrait()
    score, bd = compute(_role_resume("Директор по закупкам"), p)
    assert score == 0
    assert bd["hard_reject_reason"] == "current_role_mismatch"


def test_current_role_rejects_operational_director() -> None:
    """'Операционный директор' — no group-B scope word → mismatch."""
    p = _role_filter_portrait()
    score, bd = compute(_role_resume("Операционный директор"), p)
    assert score == 0
    assert bd["hard_reject_reason"] == "current_role_mismatch"


def test_current_role_rejects_manager_no_branch_scope() -> None:
    """'Менеджер по работе с агентской сетью' — менеджер(A) but no group-B → mismatch."""
    p = _role_filter_portrait()
    score, bd = compute(_role_resume("Менеджер по работе с агентской сетью"), p)
    assert score == 0
    assert bd["hard_reject_reason"] == "current_role_mismatch"


def test_current_role_rejects_lawyer() -> None:
    """'Юрист' — neither A nor B → mismatch."""
    p = _role_filter_portrait()
    score, bd = compute(_role_resume("Юрист"), p)
    assert score == 0
    assert bd["hard_reject_reason"] == "current_role_mismatch"


def test_current_role_rejects_dispatcher() -> None:
    """'Диспетчер-логист' — no management or branch word → mismatch."""
    p = _role_filter_portrait()
    score, bd = compute(_role_resume("Диспетчер-логист"), p)
    assert score == 0
    assert bd["hard_reject_reason"] == "current_role_mismatch"


# ── Edge cases ────────────────────────────────────────────────────────────────


def test_current_role_unknown_when_no_experience_and_no_title() -> None:
    """No experience + no resume.title → current_role_unknown (portrait has synonyms)."""
    p = _role_filter_portrait()
    score, bd = compute({"id": "empty_resume"}, p)
    assert score == 0
    assert bd["hard_reject_reason"] == "current_role_unknown"


def test_current_role_filter_skipped_without_synonyms() -> None:
    """Portrait with empty position_synonyms → filter inactive, 'Юрист' passes through."""
    p = _portrait()  # position_synonyms=[] by default
    _, bd = compute(_role_resume("Юрист"), p)
    assert bd.get("hard_reject_reason") not in ("current_role_mismatch", "current_role_unknown")


def test_current_role_case_insensitive() -> None:
    """Matching is case-insensitive for both synonym and combo paths."""
    p = _role_filter_portrait()
    # All-caps — should still match "директор филиала" (position_name)
    _, bd = compute(_role_resume("ДИРЕКТОР ФИЛИАЛА"), p)
    assert bd.get("hard_reject_reason") not in ("current_role_mismatch", "current_role_unknown")


# ── Commit 9: hard_reject_reasons array ───────────────────────────────────────


def test_single_hard_reject_populates_reasons_array() -> None:
    """A single triggered filter → hard_reject_reasons is a one-element list."""
    # Portrait with only age filter active; resume fails only on age.
    p = _portrait(age_range=(30, 60))
    payload: dict[str, Any] = {"id": "r_age", "age": 20}
    score, bd = compute(payload, p)

    assert score == 0
    assert "hard_reject_reasons" in bd, "hard_reject_reasons key must be present on rejection"
    assert bd["hard_reject_reasons"] == ["age"], (
        f"Expected ['age'], got {bd['hard_reject_reasons']}"
    )


def test_multiple_hard_rejects_returns_all_reasons() -> None:
    """When multiple filters fire the reasons list contains all of them.

    Portrait with age_range + min_total_months both active; the resume
    fails both filters simultaneously.
    """
    p = _portrait(age_range=(30, 60), min_total_months=60)
    payload: dict[str, Any] = {
        "id": "r_multi",
        "age": 20,  # fails age filter (30–60)
        "total_experience": {"months": 10},  # fails total_experience filter (need ≥60)
    }
    score, bd = compute(payload, p)

    assert score == 0
    reasons: list[str] = bd.get("hard_reject_reasons", [])
    assert "age" in reasons, f"'age' missing from {reasons}"
    assert "total_experience" in reasons, f"'total_experience' missing from {reasons}"
    assert len(reasons) >= 2, f"Expected ≥2 reasons, got {reasons}"


def test_hard_reject_reason_is_backward_compat_alias() -> None:
    """breakdown['hard_reject_reason'] must equal hard_reject_reasons[0] (first fired)."""
    p = _portrait(age_range=(30, 60), min_total_months=60)
    payload: dict[str, Any] = {
        "id": "r_compat",
        "age": 20,
        "total_experience": {"months": 10},
    }
    _, bd = compute(payload, p)

    reasons = bd.get("hard_reject_reasons", [])
    assert reasons, "hard_reject_reasons must be non-empty"
    assert bd.get("hard_reject_reason") == reasons[0], (
        f"hard_reject_reason={bd.get('hard_reject_reason')!r} "
        f"!= hard_reject_reasons[0]={reasons[0]!r}"
    )


def test_passing_candidate_has_no_hard_reject_reasons_key() -> None:
    """A candidate that passes all hard filters must not have hard_reject_reasons in breakdown."""
    # Minimal portrait with no hard constraints active → any resume passes.
    p = _portrait()
    payload: dict[str, Any] = {"id": "r_pass"}
    score, bd = compute(payload, p)

    # Score may be 0 (no bonus points) but no hard rejection.
    assert "hard_reject_reasons" not in bd, (
        f"Passing candidate must not have hard_reject_reasons; got {bd}"
    )
    assert "hard_reject_reason" not in bd


# ── Hard filter 1i: motor_experience ─────────────────────────────────────────


def test_hard_reject_motor_experience_below() -> None:
    """Motor experience below minimum → hard rejected."""
    p = _portrait(min_motor_experience_months=24)
    payload = {
        "experience": [
            {
                "company": "СК Тест",
                "position": "Андеррайтер КАСКО",
                "start": "2022-01",
                "end": "2023-01",
            },
        ]
    }
    score, bd = compute(payload, p)
    assert score == 0
    assert bd["hard_reject_reason"] == "motor_experience"


def test_hard_reject_motor_experience_passes() -> None:
    """Motor experience above minimum → not hard rejected for this filter."""
    p = _portrait(min_motor_experience_months=24)
    payload = {
        "experience": [
            {
                "company": "СК Ресо",
                "position": "Андеррайтер",
                "start": "2020-01",
                "end": "2023-01",
                "description": "КАСКО и ОСАГО",
            },
        ]
    }
    _, bd = compute(payload, p)
    assert bd.get("hard_reject_reason") != "motor_experience"


def test_hard_reject_motor_experience_disabled() -> None:
    """min_motor_experience_months=0 → filter is skipped entirely."""
    p = _portrait(min_motor_experience_months=0)
    payload: dict[str, Any] = {}  # no experience at all
    _, bd = compute(payload, p)
    assert bd.get("hard_reject_reason") != "motor_experience"


# ── Hard filter 1j: last_job_tenure ──────────────────────────────────────────


def test_hard_reject_last_job_tenure_below() -> None:
    """Last job tenure below minimum → hard rejected."""
    p = _portrait(min_tenure_last_job_months=12)
    payload = {
        "experience": [
            {"company": "СК Тест", "position": "Андеррайтер", "start": "2024-01", "end": "2024-07"},
        ]
    }
    score, bd = compute(payload, p)
    assert score == 0
    assert bd["hard_reject_reason"] == "last_job_tenure"


def test_hard_reject_last_job_tenure_passes() -> None:
    """Last job tenure above minimum → not hard rejected for this filter."""
    p = _portrait(min_tenure_last_job_months=12)
    payload = {
        "experience": [
            {"company": "СК Тест", "position": "Андеррайтер", "start": "2022-01", "end": "2024-01"},
        ]
    }
    _, bd = compute(payload, p)
    assert bd.get("hard_reject_reason") != "last_job_tenure"


def test_hard_reject_last_job_tenure_disabled() -> None:
    """min_tenure_last_job_months=0 → filter is skipped entirely."""
    p = _portrait(min_tenure_last_job_months=0)
    payload = {
        "experience": [
            {"company": "СК Тест", "position": "Андеррайтер", "start": "2024-01", "end": "2024-02"},
        ]
    }
    _, bd = compute(payload, p)
    assert bd.get("hard_reject_reason") != "last_job_tenure"


def test_hard_reject_last_job_tenure_current_position() -> None:
    """Active position (end=None) uses current month for tenure calculation."""
    today = date.today()
    # Start 6 months ago: open-ended current role with only 6 months tenure
    start_month = today.month - 6
    start_year = today.year
    if start_month <= 0:
        start_month += 12
        start_year -= 1
    start_str = f"{start_year:04d}-{start_month:02d}"

    p = _portrait(min_tenure_last_job_months=12)
    payload = {
        "experience": [
            {"company": "СК Тест", "position": "Андеррайтер", "start": start_str, "end": None},
        ]
    }
    score, bd = compute(payload, p)
    assert score == 0
    assert bd["hard_reject_reason"] == "last_job_tenure"


# ── Path (c): current_role_mismatch title fallback ────────────────────────────


def _underwriter_portrait(role_match_mode: Literal["soft", "hard"] = "hard") -> Portrait:
    """Portrait with underwriter synonyms — mirrors underwriter_21vek subset."""
    return Portrait(
        position_code="uw_test",
        position_name="Андеррайтер (тест)",
        role_match_mode=role_match_mode,
        position_synonyms=[
            "Андеррайтер",
            "Ведущий андеррайтер",
            "Андеррайтер моторных видов",
            "Специалист по андеррайтингу",
        ],
        filters=Filters(regions=RegionFilters(primary=[], adjacent=[], stop=[])),
    )


def test_current_role_mismatch_title_fallback() -> None:
    """Path (c): experience position doesn't match but resume title does → NOT rejected."""
    p = _underwriter_portrait()
    payload = {
        "id": "uw_title_fallback",
        "title": "Андеррайтер моторных видов",
        "experience": [
            {
                "company": "СК Тест",
                "position": "Старший специалист аналитики",
                "start": "2020-01",
                "end": None,
                "description": "",
            }
        ],
    }
    _, bd = compute(payload, p)
    assert bd.get("hard_reject_reason") not in ("current_role_mismatch", "current_role_unknown")


def test_current_role_mismatch_no_title_no_experience_match() -> None:
    """Neither experience position nor resume title matches synonyms → rejected."""
    p = _underwriter_portrait()
    payload = {
        "id": "uw_no_match",
        "title": "Кредитный менеджер",
        "experience": [
            {
                "company": "Банк ВТБ",
                "position": "Кредитный аналитик",
                "start": "2020-01",
                "end": None,
                "description": "",
            }
        ],
    }
    score, bd = compute(payload, p)
    assert score == 0
    assert bd["hard_reject_reason"] == "current_role_mismatch"


# ── motor_experience_preferred: soft vs hard ──────────────────────────────────


def test_motor_experience_preferred_skipped() -> None:
    """motor_experience_preferred=True: insufficient motor months → NOT hard rejected."""
    p = _portrait(min_motor_experience_months=24, motor_experience_preferred=True)
    payload: dict[str, Any] = {
        "experience": [
            {
                "company": "СК Тест",
                "position": "Андеррайтер",
                "start": "2023-01",
                "end": None,
                "description": "Работа в страховании",
            }
        ]
    }
    _, bd = compute(payload, p)
    assert bd.get("hard_reject_reason") != "motor_experience"


def test_motor_experience_required_still_hard() -> None:
    """motor_experience_preferred=False (default): insufficient motor months → hard rejected."""
    p = _portrait(min_motor_experience_months=24, motor_experience_preferred=False)
    payload: dict[str, Any] = {
        "experience": [
            {
                "company": "СК Тест",
                "position": "Андеррайтер",
                "start": "2023-01",
                "end": None,
                "description": "Работа в страховании",
            }
        ]
    }
    score, bd = compute(payload, p)
    assert score == 0
    assert bd["hard_reject_reason"] == "motor_experience"


# ══ CC-15: Soft role & industry signals ══════════════════════════════════════


def _soft_role_portrait() -> Portrait:
    """Portrait with synonyms and role_match_mode='soft' (default)."""
    return Portrait(
        position_code="soft_role_test",
        position_name="Директор филиала",
        role_match_mode="soft",
        position_synonyms=["Руководитель филиала", "Региональный директор"],
        filters=Filters(
            regions=RegionFilters(primary=["Санкт-Петербург"], adjacent=[], stop=[])
        ),
    )


def test_soft_role_mismatch_score_positive() -> None:
    """Soft mode: wrong current role but has region points → score > 0, not rejected."""
    p = _soft_role_portrait()
    payload = {
        "id": "soft_role_1",
        "area": {"name": "Санкт-Петербург"},  # primary region → +8
        "experience": [
            {
                "company": "СК Тест",
                "position": "Территориальный менеджер",  # no branch keyword → mismatch
                "start": "2020-01",
                "end": None,
                "description": "",
            }
        ],
    }
    score, bd = compute(payload, p)
    assert "hard_reject_reasons" not in bd
    assert "hard_reject_reason" not in bd
    assert score > 0


def test_soft_role_mismatch_breakdown_recorded() -> None:
    """Soft mode: role mismatch sets breakdown['role_match'] = False."""
    p = _soft_role_portrait()
    payload = _role_resume("Территориальный менеджер")
    _, bd = compute(payload, p)
    assert bd.get("role_match") is False


def test_soft_role_unknown_score_positive() -> None:
    """Soft mode: no title/experience available → not skipped, breakdown recorded."""
    p = _soft_role_portrait()
    # Primary region gives points so score > 0
    payload = {"id": "soft_unknown", "area": {"name": "Санкт-Петербург"}}
    score, bd = compute(payload, p)
    assert "hard_reject_reasons" not in bd
    assert bd.get("role_match") is False
    assert bd.get("current_role_unknown") is True
    assert score > 0


def test_soft_forbidden_industry_penalty_applied() -> None:
    """Soft mode: forbidden industry reduces score by penalty but does not zero it."""
    p = Portrait(
        position_code="soft_ind_test",
        position_name="Директор",
        forbidden_industries=["банк"],
        forbidden_industry_mode="soft",
        filters=Filters(regions=RegionFilters(primary=["Санкт-Петербург"], adjacent=[], stop=[])),
    )
    # Candidate has ОСАГО (+9) and primary region (+8) = 17 raw → ~38 score without penalty.
    # Penalty deducts 9 raw → 8 raw → ~18 score.
    payload = {
        "area": {"name": "Санкт-Петербург"},
        "experience": [
            {
                "company": "Сбербанк",  # triggers forbidden_industry
                "position": "Менеджер",
                "start": "2022-01",
                "end": None,
                "description": "ОСАГО и КАСКО",
            }
        ],
    }
    score_with_penalty, bd = compute(payload, p)
    assert "hard_reject_reasons" not in bd
    assert bd.get("forbidden_industry_recent") is True
    assert score_with_penalty > 0
    # Confirm penalty actually reduced the score vs the same candidate without industry flag
    p_no_industry = Portrait(
        position_code="soft_ind_test",
        position_name="Директор",
        forbidden_industries=[],
        filters=Filters(regions=RegionFilters(primary=["Санкт-Петербург"], adjacent=[], stop=[])),
    )
    score_no_penalty, _ = compute(payload, p_no_industry)
    assert score_with_penalty < score_no_penalty


def test_soft_forbidden_industry_breakdown_recorded() -> None:
    """Soft mode (default): forbidden_industry_recent is set, no hard rejection."""
    p = _portrait(forbidden_industries=["банк"])  # forbidden_industry_mode="soft" by default
    payload = {
        "experience": [
            {
                "company": "Сбербанк",
                "position": "Менеджер",
                "start": "2022-01",
                "end": None,
                "description": "Кредитование",
            }
        ]
    }
    _, bd = compute(payload, p)
    assert bd.get("forbidden_industry_recent") is True
    assert "hard_reject_reasons" not in bd


def test_hard_mode_role_still_rejects() -> None:
    """role_match_mode='hard': role mismatch → score=0, hard_reject_reason set."""
    p = Portrait(
        position_code="hard_role_test",
        position_name="Директор филиала",
        role_match_mode="hard",
        position_synonyms=["Руководитель филиала"],
        filters=Filters(regions=RegionFilters(primary=[], adjacent=[], stop=[])),
    )
    score, bd = compute(_role_resume("Территориальный менеджер"), p)
    assert score == 0
    assert bd["hard_reject_reason"] == "current_role_mismatch"


def test_hard_mode_forbidden_industry_still_rejects() -> None:
    """forbidden_industry_mode='hard': forbidden industry → score=0, reason set."""
    p = _portrait(forbidden_industries=["банк"], forbidden_industry_mode="hard")
    payload = {
        "experience": [
            {
                "company": "Сбербанк",
                "position": "Менеджер",
                "start": "2022-01",
                "end": None,
                "description": "Кредитование",
            }
        ]
    }
    score, bd = compute(payload, p)
    assert score == 0
    assert bd["hard_reject_reason"] == "forbidden_industry"


# ── _ROLE_GROUP_B extended stems ──────────────────────────────────────────────


def test_group_b_matches_obosoblennoye_podrazdelenie() -> None:
    """'Директор обособленного подразделения' → Group A + Group B → role match."""
    p = _role_filter_portrait()  # hard mode so a mismatch would produce score=0
    _, bd = compute(_role_resume("Директор обособленного подразделения"), p)
    assert bd.get("hard_reject_reason") not in ("current_role_mismatch", "current_role_unknown")


def test_group_b_matches_dopolnitelnoye_podrazdelenie() -> None:
    """'Руководитель дополнительного подразделения' → Group A + Group B → role match."""
    p = _role_filter_portrait()
    _, bd = compute(_role_resume("Руководитель дополнительного подразделения"), p)
    assert bd.get("hard_reject_reason") not in ("current_role_mismatch", "current_role_unknown")


def test_group_b_matches_podrazdelenie_alone() -> None:
    """'Начальник подразделения' → начальник(A) + подразделени(B) → role match."""
    p = _role_filter_portrait()
    _, bd = compute(_role_resume("Начальник подразделения"), p)
    assert bd.get("hard_reject_reason") not in ("current_role_mismatch", "current_role_unknown")
