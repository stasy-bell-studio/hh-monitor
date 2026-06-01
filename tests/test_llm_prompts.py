"""Unit tests for hh_monitor.llm_enrich.prompts helpers."""

from __future__ import annotations

from structlog.testing import capture_logs

from hh_monitor.llm_enrich.prompts import derive_verdict_class, extract_llm_score


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


def test_extract_score_string_coercion() -> None:
    """score returned as string '65' is coerced to int 65."""
    dossier = {"score": "65", "verdict": "рекомендую"}
    assert extract_llm_score(dossier, "r_str") == 65


def test_extract_score_verdict_class_sporno() -> None:
    """No score, verdict_class='спорно' → 50."""
    dossier = {"verdict_class": "спорно", "verdict": "нечто нейтральное"}
    assert extract_llm_score(dossier, "r_sp2") == 50


def test_extract_score_no_verdict_class_nujno_intervyu() -> None:
    """No score, no verdict_class, verdict text 'нужно интервью' → 50."""
    dossier = {"verdict": "Нужно интервью для проверки фактов."}
    assert extract_llm_score(dossier, "r_nvc") == 50


# ── F2: unrecognised verdict fallback is non-sendable ────────────────────────


def test_unrecognised_verdict_fallbacks_agree() -> None:
    """Neither numeric score nor any verdict keyword → both functions agree: non-sendable."""
    # extract_llm_score short-circuits to 0 when no _VERDICT_KEYWORDS found.
    # derive_verdict_class falls through to "мимо" (non-sendable) for the same input.
    dossier = {"verdict": "Непонятно."}
    assert extract_llm_score(dossier, "r_unknown") == 0
    assert derive_verdict_class("Непонятно.") == "мимо"


def test_genuine_sporno_still_maps_to_sporno() -> None:
    """Genuine 'спорно' keyword in text still maps to 'спорно' (recognised path unchanged)."""
    assert derive_verdict_class("Спорно, требуется дополнительная проверка.") == "спорно"
