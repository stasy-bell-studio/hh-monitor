"""Unit tests for hh_monitor/parser/prefilter.py — one test per rule (AC1, AC7, AC8)."""

from __future__ import annotations

from typing import Any

from hh_monitor.fit.portrait import Filters, Portrait, PrefilterConfig
from hh_monitor.parser.prefilter import apply_prefilter

# ── Helpers ────────────────────────────────────────────────────────────────────


def _portrait(**kwargs: Any) -> Portrait:
    return Portrait(position_code="test", position_name="Test", **kwargs)


def _item(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "r001",
        "area": {"id": "78", "name": "Санкт-Петербург"},
        "age": 35,
        "total_experience": {"months": 60},
        "education": {"level": {"id": "higher", "name": "Высшее"}},
        "experience": [],
    }
    base.update(overrides)
    return base


def _exp(
    *,
    company: str = "Компания",
    position: str = "Менеджер",
    company_id: str = "",
    employer_id: str = "",
    industries: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "company": company,
        "position": position,
        "start": "2020-01",
        "end": None,
    }
    if company_id:
        entry["company_id"] = company_id
    if employer_id:
        entry["employer"] = {"id": employer_id}
    if industries is not None:
        entry["industries"] = industries
    return entry


# ── AC5: empty config passes everyone ─────────────────────────────────────────


def test_empty_config_passes_all() -> None:
    p = _portrait()
    assert apply_prefilter(_item(), p) == []


# ── area_id_not_allowed ────────────────────────────────────────────────────────


def test_area_id_not_allowed_rejects() -> None:
    p = _portrait(prefilter=PrefilterConfig(area_ids_require=[78]))
    assert "area_id_not_allowed" in apply_prefilter(_item(area={"id": "1"}), p)


def test_area_id_in_allow_list_passes() -> None:
    p = _portrait(prefilter=PrefilterConfig(area_ids_require=[78]))
    assert apply_prefilter(_item(area={"id": "78"}), p) == []


def test_area_id_absent_require_passes() -> None:
    p = _portrait(prefilter=PrefilterConfig(area_ids_require=[78]))
    assert apply_prefilter(_item(area=None), p) == []


# ── area_id_stopped ────────────────────────────────────────────────────────────


def test_area_id_stopped_rejects() -> None:
    p = _portrait(prefilter=PrefilterConfig(area_ids_stop=[1]))
    assert "area_id_stopped" in apply_prefilter(_item(area={"id": "1"}), p)


def test_area_id_not_in_stop_passes() -> None:
    p = _portrait(prefilter=PrefilterConfig(area_ids_stop=[1]))
    assert apply_prefilter(_item(area={"id": "78"}), p) == []


def test_area_id_absent_stop_passes() -> None:
    p = _portrait(prefilter=PrefilterConfig(area_ids_stop=[1]))
    assert apply_prefilter(_item(area=None), p) == []


# ── age ────────────────────────────────────────────────────────────────────────


def test_age_rejects() -> None:
    p = _portrait(filters=Filters(age_range=(25, 35)))
    assert "age" in apply_prefilter(_item(age=40), p)


def test_age_passes() -> None:
    p = _portrait(filters=Filters(age_range=(25, 35)))
    assert apply_prefilter(_item(age=30), p) == []


def test_age_absent_passes() -> None:
    p = _portrait(filters=Filters(age_range=(25, 35)))
    assert apply_prefilter(_item(age=None), p) == []


def test_age_no_config_passes() -> None:
    p = _portrait()
    assert apply_prefilter(_item(age=99), p) == []


# ── total_experience ───────────────────────────────────────────────────────────


def test_total_experience_rejects() -> None:
    p = _portrait(min_total_months=36)
    assert "total_experience" in apply_prefilter(_item(total_experience={"months": 12}), p)


def test_total_experience_passes() -> None:
    p = _portrait(min_total_months=36)
    assert apply_prefilter(_item(total_experience={"months": 48}), p) == []


def test_total_experience_absent_passes() -> None:
    p = _portrait(min_total_months=36)
    assert apply_prefilter(_item(total_experience=None), p) == []


def test_total_experience_fallback_from_experience() -> None:
    # total_experience absent; experience has valid dates summing to >36 months
    p = _portrait(min_total_months=36)
    item = _item(
        total_experience=None,
        experience=[{"company": "X", "position": "Y", "start": "2019-01", "end": "2023-01"}],
    )
    assert apply_prefilter(item, p) == []


def test_total_experience_fallback_unparseable_passes() -> None:
    # total_experience absent AND experience dates unparseable → None → passes (AC8)
    p = _portrait(min_total_months=36)
    item = _item(
        total_experience=None,
        experience=[{"company": "X", "position": "Y", "start": "bad", "end": "data"}],
    )
    assert apply_prefilter(item, p) == []


# ── education ─────────────────────────────────────────────────────────────────


def test_education_rejects() -> None:
    p = _portrait(higher_education_required=True)
    item = _item(education={"level": {"id": "secondary", "name": "Среднее"}})
    assert "education" in apply_prefilter(item, p)


def test_education_higher_passes() -> None:
    p = _portrait(higher_education_required=True)
    assert apply_prefilter(_item(), p) == []  # default item has "higher"


def test_education_absent_passes() -> None:
    p = _portrait(higher_education_required=True)
    assert apply_prefilter(_item(education=None), p) == []


def test_education_no_config_passes() -> None:
    p = _portrait(higher_education_required=False)
    item = _item(education={"level": {"id": "secondary"}})
    assert apply_prefilter(item, p) == []


# ── required_industry_missing ─────────────────────────────────────────────────


def test_required_industry_missing_rejects() -> None:
    p = _portrait(prefilter=PrefilterConfig(required_industry_ids=["43"]))
    item = _item(experience=[_exp(industries=[{"id": "10.5", "name": "Торговля"}])])
    assert "required_industry_missing" in apply_prefilter(item, p)


def test_required_industry_id_exact_passes() -> None:
    p = _portrait(prefilter=PrefilterConfig(required_industry_ids=["43.646"]))
    item = _item(experience=[_exp(industries=[{"id": "43.646", "name": "Страхование"}])])
    assert apply_prefilter(item, p) == []


def test_required_industry_id_prefix_passes() -> None:
    # AC7: "43" should match "43.646"
    p = _portrait(prefilter=PrefilterConfig(required_industry_ids=["43"]))
    item = _item(experience=[_exp(industries=[{"id": "43.646", "name": "Страхование"}])])
    assert apply_prefilter(item, p) == []


def test_required_industry_id_prefix_no_false_match() -> None:
    # "43" must NOT match "430.1" (different industry tree)
    p = _portrait(prefilter=PrefilterConfig(required_industry_ids=["43"]))
    item = _item(experience=[_exp(industries=[{"id": "430.1", "name": "Другое"}])])
    assert "required_industry_missing" in apply_prefilter(item, p)


def test_required_industry_name_stem_passes() -> None:
    # AC7: fallback by industry name stem ("страхование" contains "страхов")
    p = _portrait(prefilter=PrefilterConfig(required_industry_ids=["99"]))
    item = _item(experience=[_exp(industries=[{"id": "99.1", "name": "Страхование имущества"}])])
    assert apply_prefilter(item, p) == []


def test_required_industry_no_industry_cp_stem_passes() -> None:
    # AC7: no industries field → fallback to company+position stem (mirrors R4)
    p = _portrait(prefilter=PrefilterConfig(required_industry_ids=["43"]))
    item = _item(experience=[_exp(company="Страховая компания Х", position="Директор")])
    assert apply_prefilter(item, p) == []


def test_required_industry_empty_config_passes() -> None:
    p = _portrait()
    item = _item(experience=[_exp(industries=[])])
    assert apply_prefilter(item, p) == []


# ── stop_employer ──────────────────────────────────────────────────────────────


def test_stop_employer_rejects() -> None:
    p = _portrait(prefilter=PrefilterConfig(stop_employer_ids=["emp123"]))
    item = _item(experience=[_exp(employer_id="emp123")])
    assert "stop_employer" in apply_prefilter(item, p)


def test_stop_employer_no_match_passes() -> None:
    p = _portrait(prefilter=PrefilterConfig(stop_employer_ids=["emp123"]))
    item = _item(experience=[_exp(employer_id="emp999")])
    assert apply_prefilter(item, p) == []


# ── stop_company (names + ids) ─────────────────────────────────────────────────


def test_stop_company_name_rejects() -> None:
    # AC9: substring match on company name ("Капитал Лайф")
    p = _portrait(prefilter=PrefilterConfig(stop_company_names=["Капитал Лайф"]))
    item = _item(experience=[_exp(company="Капитал Лайф Страхование Жизни")])
    assert "stop_company" in apply_prefilter(item, p)


def test_stop_company_name_case_insensitive_rejects() -> None:
    p = _portrait(prefilter=PrefilterConfig(stop_company_names=["капитал лайф"]))
    item = _item(experience=[_exp(company="Капитал Лайф Страхование Жизни")])
    assert "stop_company" in apply_prefilter(item, p)


def test_stop_company_id_rejects() -> None:
    p = _portrait(prefilter=PrefilterConfig(stop_company_ids=["co456"]))
    item = _item(experience=[_exp(company_id="co456")])
    assert "stop_company" in apply_prefilter(item, p)


def test_stop_company_no_match_passes() -> None:
    p = _portrait(prefilter=PrefilterConfig(stop_company_names=["Капитал Лайф"]))
    item = _item(experience=[_exp(company="Ингосстрах")])
    assert apply_prefilter(item, p) == []


def test_stop_company_empty_experience_passes() -> None:
    p = _portrait(prefilter=PrefilterConfig(stop_company_names=["Капитал Лайф"]))
    assert apply_prefilter(_item(experience=[]), p) == []
