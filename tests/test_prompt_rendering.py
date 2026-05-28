"""Tests for hh_monitor.llm_enrich.prompt — template rendering and normalization.

Covers _normalize_resume_payload, _render_user_template, and the public
build_messages / build_system_prompt API.
"""

from __future__ import annotations

from typing import Any

from hh_monitor.fit.portrait import GlobalContext, Portrait
from hh_monitor.llm_enrich.prompt import (
    _normalize_resume_payload,
    _render_user_template,
    build_messages,
    build_system_prompt,
)

# ── Helpers ───────────────────────────────────────────────────────────────────


def _portrait(
    *,
    evaluation_focus: list[str] | None = None,
    target_companies_override: list[str] | None = None,
    stop_companies_override: list[str] | None = None,
    position_description: str = "Тестовая позиция.",
) -> Portrait:
    return Portrait(
        position_code="test",
        position_name="Test Position",
        must_have_keywords=["страхование"],
        nice_to_have_keywords=["MBA"],
        stop_words=["студент"],
        min_total_months=12,
        preferred_total_months=36,
        position_description=position_description,
        evaluation_focus=evaluation_focus or [],
        target_companies_override=target_companies_override or [],
        stop_companies_override=stop_companies_override or [],
    )


def _global_ctx(
    *,
    target_companies: list[str] | None = None,
    stop_companies: list[str] | None = None,
    market_context: str = "",
) -> GlobalContext:
    return GlobalContext(
        target_companies=target_companies or [],
        stop_companies=stop_companies or [],
        market_context=market_context,
    )


def _resume(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    base: dict[str, Any] = {
        "hh_resume_id": "RES001",
        "title": "Директор филиала",
        "age": 38,
        "area": "Самара",
        "salary": "150 000 RUR",
        "total_experience_months": 60,
        "education": "Высшее",
        "experience": [
            {
                "company": "СОГАЗ",
                "position": "Директор",
                "start": "2019-01",
                "end": "2024-01",
                "months": 60,
                "description": "Управление филиалом",
            }
        ],
        "key_skills": ["страхование", "КАСКО"],
        "about": "Опытный руководитель",
    }
    if extra:
        base.update(extra)
    return base


# ── build_system_prompt ───────────────────────────────────────────────────────


def test_system_prompt_no_market_context() -> None:
    """Without market context, SYSTEM_PROMPT is returned as-is."""
    from hh_monitor.llm_enrich.prompt import SYSTEM_PROMPT

    ctx = _global_ctx()
    assert build_system_prompt(ctx) == SYSTEM_PROMPT


def test_system_prompt_with_market_context() -> None:
    """Market context is appended after a separator."""
    ctx = _global_ctx(market_context="Контекст рынка страхования.")
    result = build_system_prompt(ctx)
    assert "Контекст рынка страхования." in result
    assert "senior HR-партнёр" in result  # base persona still present


def test_system_prompt_contains_key_principles() -> None:
    """System prompt must contain the core evaluation principles."""
    result = build_system_prompt(_global_ctx())
    assert "ОЦЕНИВАЙ ПО СУЩЕСТВУ" in result
    assert "RED FLAGS" in result


# ── Template: evaluation_focus ────────────────────────────────────────────────


def test_empty_evaluation_focus_shows_auto_criteria_instruction() -> None:
    """When evaluation_focus is empty, template shows the auto-derive instruction."""
    portrait = _portrait(evaluation_focus=[])
    result = _render_user_template(portrait=portrait, resume=_resume(), global_ctx=_global_ctx())
    assert "КРИТЕРИИ ОЦЕНКИ" in result


def test_filled_evaluation_focus_shows_numbered_list() -> None:
    """When evaluation_focus is set, template renders a numbered list."""
    focus = ["Управление агентской сетью", "P&L-опыт", "Экспертиза B2C"]
    portrait = _portrait(evaluation_focus=focus)
    result = _render_user_template(portrait=portrait, resume=_resume(), global_ctx=_global_ctx())
    assert "ФОКУСНЫЕ ВОПРОСЫ" in result
    assert "1. Управление агентской сетью" in result
    assert "3. Экспертиза B2C" in result
    # Auto-derive instruction must NOT appear
    assert "КРИТЕРИИ ОЦЕНКИ" not in result


# ── Template: company overrides ───────────────────────────────────────────────


def test_target_companies_from_global_ctx() -> None:
    """When no override, global target companies appear in the template."""
    portrait = _portrait()
    ctx = _global_ctx(target_companies=["Ингосстрах", "СОГАЗ"])
    result = _render_user_template(portrait=portrait, resume=_resume(), global_ctx=ctx)
    assert "Ингосстрах" in result
    assert "СОГАЗ" in result


def test_target_companies_override_replaces_global() -> None:
    """Portrait-level override replaces (not merges) global target companies."""
    portrait = _portrait(target_companies_override=["ВСК"])
    ctx = _global_ctx(target_companies=["Ингосстрах", "Ресо-Гарантия"])
    result = _render_user_template(portrait=portrait, resume=_resume(), global_ctx=ctx)
    assert "ВСК" in result
    assert "Ингосстрах" not in result  # global should be replaced, not merged
    assert "Ресо-Гарантия" not in result


def test_stop_companies_override_unions_with_global() -> None:
    """Portrait-level stop_companies_override unions with global stop_companies (Bug-3b fix)."""
    portrait = _portrait(stop_companies_override=["Плохой Банк"])
    ctx = _global_ctx(stop_companies=["Капитал Лайф"])
    result = _render_user_template(portrait=portrait, resume=_resume(), global_ctx=ctx)
    assert "Плохой Банк" in result
    assert "Капитал Лайф" in result


def test_no_target_companies_section_when_both_empty() -> None:
    """If both global and override target_companies are empty, section is absent."""
    portrait = _portrait(target_companies_override=[])
    ctx = _global_ctx(target_companies=[])
    result = _render_user_template(portrait=portrait, resume=_resume(), global_ctx=ctx)
    assert "ЦЕЛЕВЫЕ КОМПАНИИ" not in result


# ── Template: resume fields ───────────────────────────────────────────────────


def test_resume_title_in_output() -> None:
    portrait = _portrait()
    result = _render_user_template(portrait=portrait, resume=_resume(), global_ctx=_global_ctx())
    assert "Директор филиала" in result


def test_resume_age_in_output() -> None:
    portrait = _portrait()
    result = _render_user_template(portrait=portrait, resume=_resume(), global_ctx=_global_ctx())
    assert "38" in result


def test_resume_experience_company_in_output() -> None:
    portrait = _portrait()
    result = _render_user_template(portrait=portrait, resume=_resume(), global_ctx=_global_ctx())
    assert "СОГАЗ" in result


def test_resume_key_skills_in_output() -> None:
    portrait = _portrait()
    result = _render_user_template(portrait=portrait, resume=_resume(), global_ctx=_global_ctx())
    assert "страхование" in result
    assert "КАСКО" in result


def test_resume_hh_id_in_output() -> None:
    portrait = _portrait()
    result = _render_user_template(portrait=portrait, resume=_resume(), global_ctx=_global_ctx())
    assert "RES001" in result


# ── _normalize_resume_payload ─────────────────────────────────────────────────


def test_normalize_salary_rur() -> None:
    payload = {"salary": {"amount": 150000, "currency": "RUR"}}
    result = _normalize_resume_payload(payload)
    assert result["salary"] is not None
    assert "150" in str(result["salary"])


def test_normalize_salary_missing_returns_none() -> None:
    result = _normalize_resume_payload({})
    assert result["salary"] is None


def test_normalize_area_dict() -> None:
    payload = {"area": {"name": "Самара"}}
    result = _normalize_resume_payload(payload)
    assert result["area"] == "Самара"


def test_normalize_area_string() -> None:
    """area can be a plain string (not wrapped in a dict)."""
    payload = {"area": "Самара"}
    result = _normalize_resume_payload(payload)
    assert result["area"] == "Самара"


def test_normalize_experience_months() -> None:
    payload = {
        "experience": [
            {"company": "ООО Тест", "position": "Директор", "start": "2020-01", "end": "2023-01"}
        ]
    }
    result = _normalize_resume_payload(payload)
    assert len(result["experience"]) == 1
    assert result["experience"][0]["months"] == 36


def test_normalize_experience_current_job_end_is_none() -> None:
    payload = {
        "experience": [
            {"company": "ООО Тест", "position": "Директор", "start": "2020-01", "end": None}
        ]
    }
    result = _normalize_resume_payload(payload)
    assert result["experience"][0]["end"] is None


def test_normalize_total_experience_from_payload() -> None:
    payload = {"total_experience": {"months": 72}}
    result = _normalize_resume_payload(payload)
    assert result["total_experience_months"] == 72


def test_normalize_key_skills_list_of_dicts() -> None:
    """hh.ru returns key_skills as list of {"name": "..."} dicts."""
    payload = {"key_skills": [{"name": "Python"}, {"name": "SQL"}]}
    result = _normalize_resume_payload(payload)
    assert result["key_skills"] == ["Python", "SQL"]


def test_normalize_key_skills_list_of_strings() -> None:
    """Also accepts plain strings in the key_skills list."""
    payload = {"key_skills": ["Python", "SQL"]}
    result = _normalize_resume_payload(payload)
    assert result["key_skills"] == ["Python", "SQL"]


def test_normalize_photo_key_stripped() -> None:
    """photo key is not propagated — not included in normalized output."""
    payload = {"photo": {"url": "https://img.example.com/photo.jpg"}}
    result = _normalize_resume_payload(payload)
    assert "photo" not in result


def test_normalize_actions_key_stripped() -> None:
    """actions key is not propagated — not included in normalized output."""
    payload = {"actions": {"negotiate": True}}
    result = _normalize_resume_payload(payload)
    assert "actions" not in result


def test_normalize_empty_payload_safe() -> None:
    """Empty payload produces valid dict with safe defaults (no KeyError)."""
    result = _normalize_resume_payload({})
    assert result["hh_resume_id"] == ""
    assert result["title"] == ""
    assert result["experience"] == []
    assert result["key_skills"] == []
    assert result["total_experience_months"] == 0


# ── build_messages integration ────────────────────────────────────────────────


def test_build_messages_passes_raw_payload() -> None:
    """build_messages normalises the raw payload internally."""
    portrait = _portrait()
    ctx = _global_ctx()
    raw_payload = {
        "id": "RES999",
        "title": "Директор",
        "salary": {"amount": 200000, "currency": "RUR"},
        "experience": [],
    }
    messages = build_messages(portrait, raw_payload, ctx)
    # User message should contain the resume ID
    assert "RES999" in messages[1]["content"]


def test_build_messages_json_schema_in_user_message() -> None:
    """The JSON response schema is present in the user message."""
    portrait = _portrait()
    ctx = _global_ctx()
    messages = build_messages(portrait, {}, ctx)
    user_content = messages[1]["content"]
    # The template instructs LLM to output JSON with these keys
    assert '"score"' in user_content
    assert '"verdict"' in user_content
    assert '"match_breakdown"' in user_content
