"""Unit tests for hh_monitor.llm_enrich.prompts helpers."""

from __future__ import annotations

from structlog.testing import capture_logs

from hh_monitor.llm_enrich.prompts import extract_llm_score


def test_extract_score_from_json_score() -> None:
    """JSON score field present and valid → used directly."""
    dossier = {"score": 65, "verdict": "какой-то текст"}
    assert extract_llm_score(dossier, "r1") == 65


def test_extract_score_clamps_out_of_range() -> None:
    """score field outside 0–100 is clamped."""
    assert extract_llm_score({"score": 150, "verdict": "рекомендую"}, "r_hi") == 100
    assert extract_llm_score({"score": -5, "verdict": "мимо"}, "r_lo") == 0


def test_extract_score_fallback_mimo() -> None:
    """No score field, verdict contains 'мимо' → 20."""
    dossier = {"verdict": "мимо"}
    assert extract_llm_score(dossier, "r2") == 20


def test_extract_score_fallback_ne_rekomenduyu() -> None:
    """No score, 'не рекомендую' in verdict → 20 (same as мимо)."""
    dossier = {"verdict": "Кандидат слаб. Не рекомендую к найму."}
    assert extract_llm_score(dossier, "r_nr") == 20


def test_extract_score_fallback_sporno() -> None:
    """No score, 'нужно интервью' → спорно → 50."""
    dossier = {"verdict": "Нужно интервью с проверкой рекомендаций."}
    assert extract_llm_score(dossier, "r_sp") == 50


def test_extract_score_fallback_rekomenduyu() -> None:
    """No score, 'рекомендую' → подходит → 80."""
    dossier = {"verdict": "Рекомендую на следующий этап."}
    assert extract_llm_score(dossier, "r_ok") == 80


def test_extract_score_fallback_unrecognized_warns() -> None:
    """No score, verdict has no known keywords → 0 + warning logged."""
    dossier = {"verdict": "Уникальный текст без каких-либо стандартных маркеров вердикта."}
    with capture_logs() as cap:
        result = extract_llm_score(dossier, "r3")
    assert result == 0
    events = [e["event"] for e in cap]
    assert "llm_enrich.score_parse_fallback" in events


def test_extract_score_verdict_class_field_takes_priority() -> None:
    """dossier.verdict_class field (if string) is used before verdict text."""
    dossier = {"verdict_class": "мимо", "verdict": "Рекомендую."}
    assert extract_llm_score(dossier, "r_vc") == 20
