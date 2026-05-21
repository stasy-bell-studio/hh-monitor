"""100% coverage target for hh_monitor.fit.rules."""

import json
from pathlib import Path

from hh_monitor.fit.portrait import Portrait, load_portrait
from hh_monitor.fit.rules import compute

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


# ── Relevant candidate (a_v2: director, 108 months, 220k, SPb, higher) ────


def test_relevant_candidate_high_score() -> None:
    score, bd = compute(_load("candidate_a_v2.json"), PORTRAIT)
    assert score > 70
    assert bd["title_match"] == 25
    assert bd["experience_keywords"] == 15
    assert bd["total_experience"] == 20  # 108 >= 84
    assert bd["salary_fit"] == 10  # 220k <= 350k
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


# ── Empty payload: no crashes ─────────────────────────────────────────────


def test_empty_payload_does_not_raise() -> None:
    score, bd = compute({}, PORTRAIT)
    assert 0 <= score <= 100
    assert set(bd.keys()) == {
        "title_match",
        "experience_keywords",
        "total_experience",
        "salary_fit",
        "education",
        "area",
        "age",
    }


# ── Salary above max → penalty ────────────────────────────────────────────


def test_salary_above_max_penalty() -> None:
    payload = {**_load("candidate_a_v2.json"), "salary": {"amount": 400000, "currency": "RUR"}}
    _, bd = compute(payload, PORTRAIT)
    assert bd["salary_fit"] == -15


# ── Salary absent → +10 (unknown, assume ok) ─────────────────────────────


def test_no_salary_gives_positive() -> None:
    payload = {k: v for k, v in _load("candidate_a_v2.json").items() if k != "salary"}
    _, bd = compute(payload, PORTRAIT)
    assert bd["salary_fit"] == 10


# ── max_salary=None in portrait → always +10 ─────────────────────────────


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
