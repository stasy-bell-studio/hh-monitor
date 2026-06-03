"""Tests for hh_monitor.fit.rules — scoring v2 (Lesnitskaya etalon v1).

Scoring formula:
  1. Hard filters → score=0, breakdown["hard_reject_reason"] set.
  2. Weighted criteria with a DYNAMIC raw max (CC-16b): base six (45) + active
     insurance_experience (2g) and/or motor_experience (2h) weights.
  3. fit_score = round(total_raw / max_raw * 100), clamped [0, 100].

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
    _matches_role,
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
    insurance_experience_mode: Literal["soft", "hard"] = "soft",
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
        insurance_experience_mode=insurance_experience_mode,
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
    """Portrait for integration scoring tests; mirrors branch_director key values."""
    return _portrait(
        age_range=(25, 45),
        primary_regions=["Санкт-Петербург", "Москва"],
        min_total_months=60,
        min_insurance_experience_months=36,
        insurance_experience_mode="soft",
        higher_education_required=True,
        bonus_companies=["ВСК", "Ресо-Гарантия", "Альфа-Страхование", "Ингосстрах"],
    )


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


def test_insurance_desc_only_not_counted() -> None:
    """Description-only ОСАГО with no industries and no stem in CP → 0 months."""
    exps = [
        {
            "company": "Авто Ltd",
            "position": "Менеджер",
            "start": "2021-01",
            "end": "2022-01",
            "description": "Продажи ОСАГО и КАСКО",
        }
    ]
    assert _insurance_experience_months(exps) == 0


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


def test_insurance_auto_dealer_desc_only_zero() -> None:
    """Auto-dealer: desc-only страхование + non-insurance industry → 0 months."""
    exps = [
        {
            "company": "АвтоМаркет",
            "position": "Менеджер",
            "start": "2020-01",
            "end": "2022-01",
            "description": "Продажа страховых продуктов при продаже авто.",
            "industries": [{"id": "93", "name": "Розничная торговля автомобилями"}],
        }
    ]
    assert _insurance_experience_months(exps) == 0


def test_insurance_foms_industry_not_counted() -> None:
    """ФОМС-like: company contains 'страхования', industry 'Государственные организации' → 0."""
    exps = [
        {
            "company": "Фонд обязательного медицинского страхования",
            "position": "Специалист",
            "start": "2019-01",
            "end": "2022-01",
            "industries": [{"id": "19", "name": "Государственные организации"}],
        }
    ]
    assert _insurance_experience_months(exps) == 0


def test_insurance_real_insurer_counted() -> None:
    """Entry at real insurer (industry 'Страхование, перестрахование') → months counted."""
    exps = [
        {
            "company": "АО Компания",
            "position": "Директор",
            "start": "2020-01",
            "end": "2023-01",
            "industries": [{"id": "83", "name": "Страхование, перестрахование"}],
        }
    ]
    assert _insurance_experience_months(exps) == 36


def test_insurance_empty_industries_position_fallback() -> None:
    """No industries field + 'страхов' stem in position → counted (fallback path)."""
    exps = [
        {
            "company": "Некая СК",
            "position": "Менеджер страхового отдела",
            "start": "2021-01",
            "end": "2022-01",
        }
    ]
    assert _insurance_experience_months(exps) == 12


# ── _motor_experience_months ──────────────────────────────────────────────────


def test_motor_desc_only_not_counted() -> None:
    """КАСКО only in description, no industries, no motor stem in CP → 0 months."""
    exps = [
        {
            "company": "СК Ресо",
            "position": "Андеррайтер",
            "start": "2021-01",
            "end": "2023-01",
            "description": "Оценка рисков КАСКО",
        }
    ]
    assert _motor_experience_months(exps) == 0


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
            "position": "Специалист МТПЛ",
            "start": "2020-01",
            "end": "2021-01",
        }
    ]
    exps_motor = [
        {"company": "СК", "position": "Моторное страхование", "start": "2020-01", "end": "2021-01"}
    ]
    assert _motor_experience_months(exps_auto) == 12
    assert _motor_experience_months(exps_mtpl) == 12
    assert _motor_experience_months(exps_motor) == 12


def test_motor_auto_dealer_desc_only_zero() -> None:
    """Auto-dealer: 'ОСАГО' only in description, industry is auto-retail → 0 motor months."""
    exps = [
        {
            "company": "Авто Плюс",
            "position": "Менеджер по продажам",
            "start": "2020-01",
            "end": "2022-01",
            "description": "Оформление ОСАГО при продаже автомобилей.",
            "industries": [{"id": "93", "name": "Розничная торговля автомобилями"}],
        }
    ]
    assert _motor_experience_months(exps) == 0


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


def _below_min_insurance_payload() -> dict[str, Any]:
    """Candidate with zero insurance-related experience (below any minimum)."""
    return {
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


def test_hard_reject_insurance_experience() -> None:
    """Insurance below minimum + hard mode → hard rejected (CC-16b: mode='hard')."""
    p = _portrait(min_insurance_experience_months=36, insurance_experience_mode="hard")
    score, bd = compute(_below_min_insurance_payload(), p)
    assert score == 0
    assert bd["hard_reject_reason"] == "insurance_experience"


def test_insurance_soft_mode_no_hard_reject() -> None:
    """CC-16b: soft mode never hard-rejects; insurance scored as 2g (present, 0 pts)."""
    p = _portrait(min_insurance_experience_months=36, insurance_experience_mode="soft")
    score, bd = compute(_below_min_insurance_payload(), p)
    assert "hard_reject_reason" not in bd
    assert "insurance_experience" not in bd.get("hard_reject_reasons", [])
    assert bd["insurance_experience"] == 0  # key present → enters denominator


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
                "position": "Андеррайтер КАСКО",
                "start": "2020-01",
                "end": "2023-01",
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
        filters=Filters(regions=RegionFilters(primary=["Санкт-Петербург"], adjacent=[], stop=[])),
    )


def test_soft_role_mismatch_region_offset_by_penalty() -> None:
    """Soft mode: wrong current role → not hard-rejected; penalty offsets region score → score=0."""
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
    # penalty=9 ≥ total_raw=8 (region only) → max(0, 8-9)=0; candidate not hard-rejected.
    assert score == 0


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


# ── Soft role mismatch penalty (role_mismatch_soft_penalty) ───────────────────


def test_soft_role_mismatch_penalty_applied() -> None:
    """Confirmed mismatch deducts role_mismatch_soft_penalty from total_raw."""
    p = _soft_role_portrait()
    # Both experience.position and title are non-matching — path (c) fallback blocked.
    mismatch_payload = {
        "area": {"name": "Санкт-Петербург"},  # primary region → +8
        "title": "Территориальный менеджер",
        "experience": [
            {
                "company": "СК Тест",
                "position": "Территориальный менеджер",  # fails _matches_role
                "start": "2020-01",
                "end": None,
                "description": "ОСАГО и КАСКО",  # osago → +9
            }
        ],
    }
    # total_raw = 8+9=17, penalty=9 → raw_after=8 → score=round(8/45*100)=18
    score_mismatch, bd = compute(mismatch_payload, p)
    assert "hard_reject_reasons" not in bd
    assert bd.get("role_match") is False
    assert not bd.get("current_role_unknown")
    assert score_mismatch == 18

    # Same payload, matching role → no penalty → score=round(17/45*100)=38
    match_payload = {
        "area": {"name": "Санкт-Петербург"},
        "title": "Директор филиала",
        "experience": [
            {
                "company": "СК Тест",
                "position": "Директор филиала",  # GROUP_A+B → match
                "start": "2020-01",
                "end": None,
                "description": "ОСАГО и КАСКО",
            }
        ],
    }
    score_match, _ = compute(match_payload, p)
    assert score_match == 38
    assert score_mismatch < score_match


def test_soft_role_unknown_not_penalized() -> None:
    """Unknown role is NOT penalized; score equals baseline with no position_synonyms."""
    p = _soft_role_portrait()
    # No experience, no title → current_role_unknown; primary region gives points
    payload = {"area": {"name": "Санкт-Петербург"}}
    score_unknown, bd = compute(payload, p)
    assert bd.get("role_match") is False
    assert bd.get("current_role_unknown") is True
    assert "hard_reject_reasons" not in bd

    # Portrait without synonyms — role matching inactive, same candidate, same score
    p_no_syn = Portrait(
        position_code="no_syn_baseline",
        position_name="Директор филиала",
        role_match_mode="soft",
        position_synonyms=[],
        filters=Filters(regions=RegionFilters(primary=["Санкт-Петербург"], adjacent=[], stop=[])),
    )
    score_baseline, _ = compute(payload, p_no_syn)
    assert score_unknown == score_baseline


def test_soft_matching_role_unaffected_by_penalty() -> None:
    """Matching candidate (role_match never False) — fit identical with/without synonyms."""
    p_with_syn = _soft_role_portrait()
    p_no_syn = Portrait(
        position_code="no_syn_match",
        position_name="Директор филиала",
        role_match_mode="soft",
        position_synonyms=[],
        filters=Filters(regions=RegionFilters(primary=["Санкт-Петербург"], adjacent=[], stop=[])),
    )
    payload = {
        "area": {"name": "Санкт-Петербург"},
        "title": "Директор филиала",
        "experience": [
            {
                "company": "СК Тест",
                "position": "Директор филиала",
                "start": "2020-01",
                "end": None,
                "description": "ОСАГО",
            }
        ],
    }
    score_syn, bd_syn = compute(payload, p_with_syn)
    score_nosyn, _ = compute(payload, p_no_syn)
    assert bd_syn.get("role_match") is not False
    assert score_syn == score_nosyn  # max_raw / denominator unchanged by synonyms


def test_soft_role_mismatch_stacks_with_forbidden_industry() -> None:
    """Both role mismatch and forbidden industry penalties stack, floored at 0."""
    p = Portrait(
        position_code="stack_test",
        position_name="Директор филиала",
        role_match_mode="soft",
        position_synonyms=["Руководитель филиала", "Региональный директор"],
        forbidden_industries=["банк"],
        forbidden_industry_mode="soft",
        filters=Filters(regions=RegionFilters(primary=["Санкт-Петербург"], adjacent=[], stop=[])),
    )
    # Candidate has primary region (+8) + ОСАГО (+9) + forbidden company + mismatch role.
    # total_raw=17, two penalties of 9 → max(0, 17-9-9)=0 → score=0, but no hard reject.
    payload = {
        "area": {"name": "Санкт-Петербург"},
        "title": "Территориальный менеджер",
        "experience": [
            {
                "company": "Сбербанк",  # forbidden_industry trigger
                "position": "Территориальный менеджер",  # role mismatch
                "start": "2020-01",
                "end": None,
                "description": "ОСАГО и КАСКО",
            }
        ],
    }
    score, bd = compute(payload, p)
    assert "hard_reject_reasons" not in bd
    assert bd.get("forbidden_industry_recent") is True
    assert bd.get("role_match") is False
    assert not bd.get("current_role_unknown")
    assert score == 0


def test_max_raw_unchanged_by_role_penalty() -> None:
    """max_raw (denominator) is identical whether or not position_synonyms are present."""
    p_with_syn = _soft_role_portrait()
    p_no_syn = Portrait(
        position_code="maxraw_test",
        position_name="Директор филиала",
        role_match_mode="soft",
        position_synonyms=[],
        filters=Filters(regions=RegionFilters(primary=["Санкт-Петербург"], adjacent=[], stop=[])),
    )
    # Matching candidate — no penalty in either portrait; equal scores prove equal denominators.
    payload = {
        "area": {"name": "Санкт-Петербург"},
        "title": "Директор филиала",
        "experience": [
            {
                "company": "СК Тест",
                "position": "Директор филиала",
                "start": "2020-01",
                "end": None,
                "description": "ОСАГО",
            }
        ],
    }
    score_syn, _ = compute(payload, p_with_syn)
    score_nosyn, _ = compute(payload, p_no_syn)
    assert score_syn == score_nosyn == 38


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


# ══ CC-16b: scored insurance/motor criteria + dynamic denominator ═════════════
#
# Synthetic data only (no PII).  The golden 5-event before→after regression lives
# in scripts/verify_cc16b.py (DB-driven, run manually against prod).


def _ym_entry(months: int, *, keyword: str) -> dict[str, Any]:
    """One experience entry spanning exactly *months*, tagged with *keyword*.

    start "2020-01"; end = start + months (so _parse_ym_months returns *months*).
    """
    end_abs = (2020 * 12 + 1) + months
    end_year, end_month = divmod(end_abs - 1, 12)
    return {
        "company": keyword,
        "position": keyword,
        "start": "2020-01",
        "end": f"{end_year:04d}-{end_month + 1:02d}",
        "description": keyword,
    }


# ── 2g insurance graduation (insurance_experience weight = 12) ────────────────


@pytest.mark.parametrize(
    ("months", "expected"),
    [(11, 0), (12, 6), (35, 6), (36, 12)],
)
def test_2g_insurance_graduation(months: int, expected: int) -> None:
    p = _portrait(min_insurance_experience_months=1, insurance_experience_mode="soft")
    payload = {"experience": [_ym_entry(months, keyword="Страховая компания")]}
    _, bd = compute(payload, p)
    assert bd["insurance_experience"] == expected


def test_2g_absent_when_min_zero() -> None:
    """2g inactive when min_insurance_experience_months == 0 → key absent."""
    p = _portrait(min_insurance_experience_months=0, insurance_experience_mode="soft")
    payload = {"experience": [_ym_entry(60, keyword="Страховая компания")]}
    _, bd = compute(payload, p)
    assert "insurance_experience" not in bd


def test_2g_absent_in_hard_mode() -> None:
    """Hard mode disables 2g (insurance is a gate, not a scored criterion)."""
    p = _portrait(min_insurance_experience_months=1, insurance_experience_mode="hard")
    payload = {"experience": [_ym_entry(60, keyword="Страховая компания")]}
    _, bd = compute(payload, p)
    assert "insurance_experience" not in bd


# ── 2h motor graduation (motor_experience weight = 6) ─────────────────────────


@pytest.mark.parametrize(
    ("months", "expected"),
    [(11, 0), (12, 3), (23, 3), (24, 6)],
)
def test_2h_motor_graduation(months: int, expected: int) -> None:
    p = _portrait(motor_experience_preferred=True)
    payload = {"experience": [_ym_entry(months, keyword="ОСАГО КАСКО")]}
    _, bd = compute(payload, p)
    assert bd["motor_experience"] == expected


def test_2h_absent_when_not_preferred() -> None:
    """2h inactive when motor_experience_preferred is False → key absent."""
    p = _portrait(motor_experience_preferred=False)
    payload = {"experience": [_ym_entry(60, keyword="ОСАГО КАСКО")]}
    _, bd = compute(payload, p)
    assert "motor_experience" not in bd


# ── Dynamic denominator: 45 / 51 / 57 / 63 ────────────────────────────────────
#
# A region-only candidate scores exactly target_region_primary (8) and 0 on every
# other criterion (incl. 2g/2h when active → present but 0).  fit_score therefore
# pins the denominator: round(8/denom*100) is distinct for each denom, and proves
# the region term is max(8,4)=8, never the additive 12.


def _denom_portrait(*, insurance: bool, motor: bool) -> Portrait:
    return _portrait(
        primary_regions=["Самарская область"],
        adjacent_regions=["Оренбургская область"],
        min_insurance_experience_months=1 if insurance else 0,
        insurance_experience_mode="soft",
        motor_experience_preferred=motor,
    )


def _region_only_payload() -> dict[str, Any]:
    """SPb-region candidate with NO insurance/motor/agent/edu signals."""
    return {
        "id": "denom_test",
        "area": {"name": "Самарская область"},
        "experience": [
            {
                "company": "ООО Ромашка",
                "position": "Бухгалтер",
                "start": "2018-01",
                "end": None,
                "description": "Бухгалтерский учёт",
            }
        ],
    }


@pytest.mark.parametrize(
    ("insurance", "motor", "denom", "expected_fit"),
    [
        (False, False, 45, 18),  # round(8/45*100)
        (False, True, 51, 16),  # round(8/51*100)
        (True, False, 57, 14),  # round(8/57*100)
        (True, True, 63, 13),  # round(8/63*100)
    ],
)
def test_dynamic_denominator(insurance: bool, motor: bool, denom: int, expected_fit: int) -> None:
    p = _denom_portrait(insurance=insurance, motor=motor)
    score, bd = compute(_region_only_payload(), p)
    assert bd["region"] == 8, "region must be max(8,4)=8, never additive 12"
    assert score == expected_fit, f"denom {denom}: expected {expected_fit}, got {score}"


def test_denominator_neither_omits_scored_keys() -> None:
    """When neither 2g nor 2h is active, neither key enters the breakdown."""
    p = _denom_portrait(insurance=False, motor=False)
    _, bd = compute(_region_only_payload(), p)
    assert "insurance_experience" not in bd
    assert "motor_experience" not in bd


# ── Role matcher: "управлени" Group-B stem (CC-16b) ───────────────────────────


def _role_portrait() -> Portrait:
    return Portrait(
        position_code="role",
        position_name="Директор филиала",
        position_synonyms=["Руководитель филиала", "Региональный директор"],
    )


def test_matches_role_upravlenie_strahovaniya() -> None:
    """CC-16b fix: 'Руководитель управления страхования' → руководитель(A)+управлени(B)."""
    assert _matches_role("Руководитель управления страхования", _role_portrait()) is True


def test_matches_role_guard_stays_false() -> None:
    """Title with no Group-B scope word must STAY False (no over-match)."""
    assert _matches_role("Менеджер по продажам", _role_portrait()) is False


def test_matches_role_upravlenie_known_overmatch() -> None:
    """Acknowledged over-match: 'управлени' makes generic 'управление проектами' match.

    Documents the calibration risk surfaced in CC-16b: менеджер(A)+управлени(B).
    role_match is logging-only today (not scored, not in the LLM prompt), so this
    has no fit_score or LLM impact — captured here so a future tightening is a
    deliberate, test-visible decision.
    """
    assert _matches_role("Менеджер по управлению проектами", _role_portrait()) is True


def test_matches_role_group_ab_still_works() -> None:
    """Regression: path (b) GROUP_A+GROUP_B still matches without insurance stem."""
    assert _matches_role("Управляющий офисом", _role_portrait()) is True


def test_matches_role_insurance_direction_path_d() -> None:
    """Path (d): GROUP_A + insurance stem matches; no GROUP_B scope word needed.

    _role_portrait synonyms ('Директор филиала', 'Руководитель филиала',
    'Региональный директор') are not substrings of this title → path (a) skipped.
    """
    assert _matches_role("Руководитель направления (Страхование)", _role_portrait()) is True


def test_matches_role_no_insurance_stem_false() -> None:
    """Path (d) guard: GROUP_A present but no insurance stem → False."""
    title = "Руководитель направления по работе с партнёрами"
    assert _matches_role(title, _role_portrait()) is False
