"""Tests for the S3b insurance-role question (AC6)."""

from __future__ import annotations

from hh_monitor.fit.portrait import Portrait
from hh_monitor.tg.add_vacancy.handlers import _apply_insurance_override
from hh_monitor.tg.add_vacancy.llm import compute_gaps


def test_insurance_override_no_sets_governor_off() -> None:
    """AC6a: non-insurance answer → portrait has domain_governor_mode='off' and zeroed fields."""
    d: dict = {"position_code": "t", "position_name": "T"}
    result = _apply_insurance_override(d, is_insurance=False)
    p = Portrait.model_validate(result)
    assert p.domain_governor_mode == "off"
    assert p.min_insurance_experience_months == 0
    assert p.min_motor_experience_months == 0
    assert p.motor_experience_preferred is False
    assert p.insurance_experience_mode == "soft"


def test_insurance_override_yes_keeps_defaults() -> None:
    """AC6b: insurance answer → portrait keeps domain_governor_mode='cap'."""
    d: dict = {"position_code": "t", "position_name": "T"}
    result = _apply_insurance_override(d, is_insurance=True)
    p = Portrait.model_validate(result)
    assert p.domain_governor_mode == "cap"


def test_insurance_override_no_clears_llm_set_values() -> None:
    """AC6c: even if LLM set insurance months, non-insurance override zeros them."""
    d = {
        "position_code": "t",
        "position_name": "T",
        "min_insurance_experience_months": 24,
        "min_motor_experience_months": 12,
        "motor_experience_preferred": True,
    }
    result = _apply_insurance_override(d, is_insurance=False)
    p = Portrait.model_validate(result)
    assert p.min_insurance_experience_months == 0
    assert p.min_motor_experience_months == 0
    assert p.motor_experience_preferred is False


def test_compute_gaps_excludes_insurance_field_for_non_insurance_role() -> None:
    """AC6d: compute_gaps(is_insurance=False) must not include the insurance-experience label."""
    portrait = Portrait(
        position_code="t",
        position_name="T",
        evaluation_focus=["навык"],
        must_have_keywords=["опыт"],
    )
    insurance_label = "Опыт в страховании (мес.)"

    gaps_insurance = compute_gaps(portrait, is_insurance=True)
    gaps_non_insurance = compute_gaps(portrait, is_insurance=False)

    assert insurance_label in gaps_insurance
    assert insurance_label not in gaps_non_insurance
