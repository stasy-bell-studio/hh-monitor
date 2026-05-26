"""Tests for hh_monitor.llm_enrich.prompts — UNIVERSAL_CRITIC_PROMPT and helpers."""

from __future__ import annotations

import pytest

from hh_monitor.llm_enrich.prompts import (
    UNIVERSAL_CRITIC_PROMPT,
    build_full_prompt,
    derive_score_from_verdict,
    derive_verdict_class,
    parse_dossier,
)

# ── Part A constant ───────────────────────────────────────────────────────────


def test_universal_prompt_has_required_sections() -> None:
    """UNIVERSAL_CRITIC_PROMPT contains required role/tone markers and JSON field names."""
    p = UNIVERSAL_CRITIC_PROMPT.lower()
    assert "hr-директор" in p
    assert "без вежливости" in p
    assert "facts_confirmed" in p
    assert "red_flags" in p
    assert "verdict" in p
    assert "interview_questions" in p
    assert "weak_spots" in p


def test_build_full_prompt_with_empty_lens() -> None:
    """build_full_prompt('') returns only Part A (no СПЕЦИФИКА block)."""
    result = build_full_prompt("")
    assert result == UNIVERSAL_CRITIC_PROMPT
    assert "СПЕЦИФИКА" not in result


def test_build_full_prompt_with_lens() -> None:
    """build_full_prompt with non-empty lens concatenates both parts."""
    lens = "1. ЧТО ВЫИСКИВАТЬ — опыт ОСАГО\n2. КРАСНЫЕ ФЛАГИ — переход из банка"
    result = build_full_prompt(lens)
    assert UNIVERSAL_CRITIC_PROMPT in result
    assert "СПЕЦИФИКА ДАННОЙ ВАКАНСИИ:" in result
    assert lens in result


# ── parse_dossier ─────────────────────────────────────────────────────────────


def test_parse_dossier_valid_json() -> None:
    """parse_dossier extracts all 5 fields from valid JSON."""
    import json

    raw = json.dumps(
        {
            "facts_confirmed": "Работал директором с 2019 по 2023.",
            "weak_spots": "Нет цифр P&L.",
            "red_flags": "Gap с 2023 года не объяснён.",
            "interview_questions": ["Каковы KPI?", "Где работали с 2023?"],
            "verdict": "Рекомендую на интервью.",
        }
    )
    d = parse_dossier(raw)
    assert d["facts_confirmed"] == "Работал директором с 2019 по 2023."
    assert d["weak_spots"] == "Нет цифр P&L."
    assert isinstance(d["interview_questions"], list)
    assert len(d["interview_questions"]) == 2


def test_parse_dossier_invalid_json_fallback() -> None:
    """parse_dossier on non-JSON text: verdict=raw, all others None."""
    raw = "Это не JSON, просто текст."
    d = parse_dossier(raw)
    assert d["verdict"] == raw
    assert d["facts_confirmed"] is None
    assert d["weak_spots"] is None
    assert d["red_flags"] is None
    assert d["interview_questions"] is None


def test_parse_dossier_interview_questions_as_string() -> None:
    """interview_questions as numbered string is split into list[str]."""
    import json

    raw = json.dumps(
        {
            "facts_confirmed": "...",
            "weak_spots": "...",
            "red_flags": "...",
            "interview_questions": "1. Вопрос один 2. Вопрос два",
            "verdict": "Рекомендую.",
        }
    )
    d = parse_dossier(raw)
    iq = d["interview_questions"]
    assert isinstance(iq, list)
    assert len(iq) == 2
    assert "Вопрос один" in iq[0]
    assert "Вопрос два" in iq[1]


def test_parse_dossier_missing_fields_become_none() -> None:
    """JSON with fewer than 5 fields: missing fields default to None; real_role to ''."""
    import json

    raw = json.dumps({"verdict": "Не рекомендую."})
    d = parse_dossier(raw)
    assert d["verdict"] == "Не рекомендую."
    assert d["facts_confirmed"] is None
    assert d["weak_spots"] is None
    assert d["real_role"] == ""


def test_parse_dossier_missing_real_role_defaults_to_empty() -> None:
    """JSON with 5 dossier fields but no real_role → real_role == ''."""
    import json

    raw = json.dumps(
        {
            "facts_confirmed": "Факты.",
            "weak_spots": "Слабые.",
            "red_flags": "Флаги.",
            "interview_questions": ["Q?"],
            "verdict": "Рекомендую.",
        }
    )
    d = parse_dossier(raw)
    assert d["real_role"] == ""


def test_parse_dossier_null_real_role_coerced_to_empty() -> None:
    """JSON with real_role: null → coerced to empty string."""
    import json

    raw = json.dumps({"facts_confirmed": "Ф.", "verdict": "Рекомендую.", "real_role": None})
    d = parse_dossier(raw)
    assert d["real_role"] == ""


def test_parse_dossier_invalid_json_real_role_empty() -> None:
    """Non-JSON fallback always has real_role == ''."""
    d = parse_dossier("Это точно не JSON.")
    assert d["real_role"] == ""
    assert d["verdict"] == "Это точно не JSON."


def test_parse_dossier_extra_fields_ignored() -> None:
    """JSON with extra unknown fields: they are preserved but not causing errors."""
    import json

    raw = json.dumps(
        {
            "facts_confirmed": "Факты.",
            "weak_spots": "Слабые.",
            "red_flags": "Флаги.",
            "interview_questions": ["Q1"],
            "verdict": "Рекомендую.",
            "unknown_extra": "ignored",
        }
    )
    d = parse_dossier(raw)
    assert d["facts_confirmed"] == "Факты."
    # Extra field is still in dict (no stripping)
    assert "unknown_extra" in d


# ── Score/verdict derivation ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("verdict_text", "expected_score"),
    [
        ("Не рекомендую. Кандидат слабый.", 20),
        ("Нужно интервью для проверки.", 60),
        ("Рекомендую на следующий этап.", 80),
        ("Неоднозначная кандидатура.", 50),  # default
    ],
)
def test_derive_score_from_verdict(verdict_text: str, expected_score: int) -> None:
    assert derive_score_from_verdict(verdict_text) == expected_score


@pytest.mark.parametrize(
    ("verdict_text", "expected_class"),
    [
        ("Не рекомендую.", "мимо"),
        ("Нужно интервью.", "спорно"),
        ("Рекомендую.", "подходит"),
        ("Непонятно.", "спорно"),
    ],
)
def test_derive_verdict_class(verdict_text: str, expected_class: str) -> None:
    assert derive_verdict_class(verdict_text) == expected_class
