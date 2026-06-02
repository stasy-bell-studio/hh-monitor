"""Tests for hh_monitor.tg.cards — HTML rendering and inline keyboard."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from hh_monitor.db.models import Event, Resume, Search
from hh_monitor.tg.cards import (
    _plural_years,
    build_card_html,
    build_inline_keyboard,
    safe,
)

# ── Helpers ───────────────────────────────────────────────────────────────────


def _resume(
    hh_resume_id: str = "abc123",
    score_total: int | None = 75,
    fit_score: int | None = 70,
    llm_score: int | None = 80,
    llm_verdict: str | None = "подходит",
    llm_real_role: str | None = "Директор филиала",
    llm_comment: str | None = "Хороший опыт",
    llm_red_flags: list[str] | None = None,
) -> Resume:
    r = MagicMock(spec=Resume)
    r.hh_resume_id = hh_resume_id
    r.score_total = score_total
    r.fit_score = fit_score
    r.llm_score = llm_score
    r.llm_verdict = llm_verdict
    r.llm_real_role = llm_real_role
    r.llm_comment = llm_comment
    r.llm_red_flags = llm_red_flags
    return r


def _event(
    id_: int = 1,
    llm_verdict: str | None = None,
    llm_red_flags: str | None = None,
    llm_facts_confirmed: str | None = None,
    llm_weak_spots: str | None = None,
    llm_verdict_text: str | None = None,
    llm_interview_questions: list[str] | None = None,
) -> Event:
    e = MagicMock(spec=Event)
    e.id = id_
    e.llm_verdict = llm_verdict
    e.llm_red_flags = llm_red_flags
    e.llm_facts_confirmed = llm_facts_confirmed
    e.llm_weak_spots = llm_weak_spots
    e.llm_verdict_text = llm_verdict_text
    e.llm_interview_questions = llm_interview_questions
    return e


def _search(position_name: str = "Директор филиала") -> Search:
    s = MagicMock(spec=Search)
    s.position_name = position_name
    return s


def _snap(
    region: str = "Минск",
    age: int = 35,
    exp_months: int = 96,
    salary: int | None = None,
    education: str | None = "Высшее",
) -> dict[str, object]:
    payload: dict[str, object] = {
        "area": {"id": "4", "name": region},
        "age": age,
        "total_experience": {"months": exp_months},
        "education": {"level": {"id": "higher", "name": education}} if education else None,
    }
    if salary is not None:
        payload["salary"] = {"amount": salary, "currency": "RUR"}
    return payload


# ── Tests: safe() ─────────────────────────────────────────────────────────────


def test_safe_escapes_html() -> None:
    assert safe("<b>test</b>") == "&lt;b&gt;test&lt;/b&gt;"
    assert safe("a & b") == "a &amp; b"
    assert safe('say "hi"') == "say &quot;hi&quot;"


def test_safe_none_returns_default() -> None:
    assert safe(None) == ""
    assert safe(None, default="N/A") == "N/A"


def test_safe_empty_string_returns_default() -> None:
    assert safe("") == ""


# ── Tests: _plural_years() declension ─────────────────────────────────────────


@pytest.mark.parametrize(
    "n,expected",
    [
        (1, "год"),
        (2, "года"),
        (3, "года"),
        (4, "года"),
        (5, "лет"),
        (11, "лет"),
        (12, "лет"),
        (14, "лет"),
        (21, "год"),
        (22, "года"),
        (34, "года"),
        (111, "лет"),
    ],
)
def test_plural_years(n: int, expected: str) -> None:
    assert _plural_years(n) == expected


def test_card_age_declension_34_goda() -> None:
    html = build_card_html(_resume(), _event(), _search(), _snap(age=34))
    assert "34 года" in html


def test_card_experience_declension_8_let() -> None:
    html = build_card_html(_resume(), _event(), _search(), _snap(exp_months=96))
    assert "опыт 8 лет" in html


def test_card_salary_thousands_separator() -> None:
    html = build_card_html(_resume(), _event(), _search(), _snap(salary=120000))
    assert "120 000 ₽" in html


def test_card_edge_no_trailing_blank_line() -> None:
    res = _resume(
        score_total=None,
        fit_score=None,
        llm_score=40,
        llm_verdict=None,
        llm_real_role=None,
        llm_comment=None,
        llm_red_flags=None,
    )
    ev = _event(llm_verdict=None, llm_red_flags=None)
    html = build_card_html(res, ev, _search(), snapshot_payload=None)
    assert not html.endswith("\n")
    assert not html.endswith("\n\n")
    assert html == html.rstrip()


# ── Tests: build_card_html() — anchor line ────────────────────────────────────


def test_card_anchor_contains_rating() -> None:
    html = build_card_html(_resume(score_total=75), _event(), _search(), _snap())
    first_line = html.splitlines()[0]
    assert "Рейтинг 75/100" in first_line


def test_card_anchor_score_none_shows_dash() -> None:
    html = build_card_html(_resume(score_total=None), _event(), _search(), _snap())
    first_line = html.splitlines()[0]
    assert "Рейтинг —/100" in first_line


def test_card_anchor_verdict_подходит_emoji() -> None:
    html = build_card_html(_resume(llm_verdict="подходит"), _event(), _search(), _snap())
    assert html.splitlines()[0].startswith("🟢")


def test_card_anchor_verdict_спорно_emoji() -> None:
    html = build_card_html(_resume(llm_verdict="спорно"), _event(), _search(), _snap())
    assert html.splitlines()[0].startswith("🟡")


def test_card_anchor_verdict_мимо_emoji() -> None:
    html = build_card_html(_resume(llm_verdict="мимо"), _event(), _search(), _snap())
    assert html.splitlines()[0].startswith("🔴")


def test_card_anchor_verdict_none_is_red() -> None:
    res = _resume(llm_verdict=None)
    ev = _event(llm_verdict=None)
    html = build_card_html(res, ev, _search(), _snap())
    assert html.splitlines()[0].startswith("🔴")


def test_card_anchor_contains_position_name() -> None:
    html = build_card_html(_resume(), _event(), _search("Ведущий аналитик"), _snap())
    assert "Ведущий аналитик" in html.splitlines()[0]


# ── Tests: secondary score breakdown line ─────────────────────────────────────


def test_card_secondary_line_both_present() -> None:
    html = build_card_html(_resume(fit_score=70, llm_score=80), _event(), _search(), _snap())
    assert "соответствие портрету 70 · оценка ИИ 80" in html


def test_card_secondary_line_only_fit() -> None:
    html = build_card_html(_resume(fit_score=70, llm_score=None), _event(), _search(), _snap())
    assert "соответствие портрету 70" in html
    assert "·" not in html.splitlines()[1]


def test_card_secondary_line_only_llm() -> None:
    html = build_card_html(_resume(fit_score=None, llm_score=80), _event(), _search(), _snap())
    assert "оценка ИИ 80" in html
    assert "·" not in html.splitlines()[1]


def test_card_secondary_line_both_none_omitted() -> None:
    html = build_card_html(_resume(fit_score=None, llm_score=None), _event(), _search(), _snap())
    assert "соответствие портрету" not in html
    assert "оценка ИИ" not in html


def test_card_secondary_not_in_anchor() -> None:
    html = build_card_html(_resume(fit_score=70, llm_score=80), _event(), _search(), _snap())
    assert "соответствие" not in html.splitlines()[0]
    assert "оценка" not in html.splitlines()[0]


# ── Tests: no old English labels ─────────────────────────────────────────────


def test_card_no_score_label_line() -> None:
    html = build_card_html(_resume(), _event(), _search(), _snap())
    assert "<b>Score:</b>" not in html


def test_card_no_latin_fit_label() -> None:
    html = build_card_html(_resume(), _event(), _search(), _snap())
    # "fit" as standalone label (Latin) must not appear
    assert "fit " not in html and " fit" not in html.lower().replace("соответствие портрету", "")


def test_card_no_latin_llm_label() -> None:
    html = build_card_html(_resume(), _event(), _search(), _snap())
    assert "LLM " not in html


def test_card_no_red_flags_label() -> None:
    res = _resume(llm_red_flags=["частые смены"])
    html = build_card_html(res, _event(), _search(), _snap())
    assert "Red flags" not in html
    assert "Риски" in html


# ── Tests: facts block ────────────────────────────────────────────────────────


def test_card_no_должность_label_for_verdict() -> None:
    html = build_card_html(_resume(), _event(), _search(), _snap())
    assert "<b>Должность:</b>" not in html


def test_card_no_verdict_label_line() -> None:
    # Verdict is encoded by the anchor emoji; no "Вердикт:" label line anymore.
    html = build_card_html(_resume(), _event(), _search(), _snap())
    assert "<b>Вердикт:</b>" not in html


def test_card_real_role_line_present() -> None:
    html = build_card_html(
        _resume(llm_real_role="Директор филиала"), _event(), _search(), _snap()
    )
    assert "Реальная роль: Директор филиала" in html


def test_card_education_folded_into_geo_line() -> None:
    html = build_card_html(_resume(), _event(), _search(), _snap(education="Высшее"))
    assert "<b>Образование:</b>" not in html
    assert "Высшее" in html
    # education appears in the combined bare geo line (no long label anymore)
    assert "Регион · возраст · опыт · образование" not in html


def test_card_geo_line_contains_all_present_parts() -> None:
    html = build_card_html(_resume(), _event(), _search(), _snap(
        region="Минск", age=35, exp_months=96, education="Высшее"
    ))
    assert "Минск" in html
    assert "35 лет" in html
    assert "опыт 8 лет" in html
    assert "Высшее" in html


def test_card_salary_shown_when_rur() -> None:
    html = build_card_html(_resume(), _event(), _search(), _snap(salary=150000))
    assert "150 000" in html
    assert "₽" in html


def test_card_no_salary_line_when_absent() -> None:
    html = build_card_html(_resume(), _event(), _search(), _snap(salary=None))
    assert "₽" not in html


def test_card_no_snapshot_fields_skipped() -> None:
    html = build_card_html(_resume(), _event(), _search(), snapshot_payload=None)
    assert "Регион" not in html
    assert "ЗП" not in html
    assert "Образование" not in html
    assert "Рейтинг 75/100" in html


# ── Tests: risks block ────────────────────────────────────────────────────────


def test_card_risks_list() -> None:
    res = _resume(llm_red_flags=["частые смены", "нет опыта"])
    html = build_card_html(res, _event(), _search(), _snap())
    assert "частые смены" in html
    assert "нет опыта" in html
    assert "🚩" in html
    assert "Риски" in html


def test_card_risks_from_event() -> None:
    res = _resume(llm_red_flags=None)
    ev = _event(llm_red_flags="нет опыта вождения")
    html = build_card_html(res, ev, _search(), _snap())
    assert "нет опыта вождения" in html


def test_card_no_risks_no_flag_emoji() -> None:
    res = _resume(llm_red_flags=None)
    ev = _event(llm_red_flags=None)
    html = build_card_html(res, ev, _search(), _snap())
    assert "🚩" not in html


def test_card_risks_appear_after_geo_line() -> None:
    res = _resume(llm_red_flags=["риск1"])
    html = build_card_html(res, _event(), _search(), _snap())
    lines = html.splitlines()
    geo_idx = next(i for i, ln in enumerate(lines) if "Минск" in ln)
    risk_idx = next(i for i, ln in enumerate(lines) if "Риски" in ln)
    assert risk_idx > geo_idx


# ── Tests: comment ────────────────────────────────────────────────────────────


def test_card_conclusion_visible_when_verdict_мимо() -> None:
    # Reason is now always shown via "Вывод:" — no hide-on-мимо rule.
    res = _resume(llm_verdict="мимо", llm_comment=None)
    ev = _event(llm_verdict_text="Не подходит совсем", llm_red_flags="нет лицензии")
    html = build_card_html(res, ev, _search(), _snap())
    assert "🚩 Риски: нет лицензии" in html
    assert "Вывод: Не подходит совсем" in html


def test_card_comment_shown_when_verdict_not_мимо() -> None:
    res = _resume(llm_verdict="подходит", llm_comment="Хороший опыт")
    html = build_card_html(res, _event(), _search(), _snap())
    assert "Хороший опыт" in html


# ── Tests: dossier block (strengths / weak spots / risks / conclusion) ────────


def test_card_dossier_full_card_shows_four_blocks() -> None:
    res = _resume(llm_verdict="подходит", llm_red_flags=None)
    ev = _event(
        llm_facts_confirmed="10 лет в рознице, рост выручки 30%.",
        llm_weak_spots="Нет опыта в логистике.",
        llm_red_flags="Частые смены работодателей.",
        llm_verdict_text="Сильный кандидат, рекомендуем на собеседование.",
    )
    html = build_card_html(res, ev, _search(), _snap())
    assert "✅ Сильные стороны: 10 лет в рознице, рост выручки 30%." in html
    assert "⚠️ Слабые места: Нет опыта в логистике." in html
    assert "🚩 Риски: Частые смены работодателей." in html
    assert "Вывод: Сильный кандидат, рекомендуем на собеседование." in html


def test_card_dossier_omits_empty_blocks() -> None:
    res = _resume(llm_verdict="подходит", llm_comment=None, llm_red_flags=None)
    ev = _event(llm_facts_confirmed="Есть управленческий опыт.")
    html = build_card_html(res, ev, _search(), _snap())
    assert "✅ Сильные стороны: Есть управленческий опыт." in html
    assert "Слабые места" not in html
    assert "🚩 Риски" not in html
    assert "Вывод:" not in html


def test_card_dossier_long_facts_truncated() -> None:
    long_facts = "слово " * 60  # ~360 chars, no sentence break
    res = _resume(llm_verdict="подходит", llm_red_flags=None)
    ev = _event(llm_facts_confirmed=long_facts)
    html = build_card_html(res, ev, _search(), _snap())
    assert "…" in html
    facts_line = next(ln for ln in html.splitlines() if "Сильные стороны" in ln)
    assert len(facts_line) < len("✅ Сильные стороны: ") + 160


def test_card_dossier_legacy_event_falls_back_to_comment() -> None:
    # Pre-9.3 event: no dossier fields → fall back to resume.llm_comment as Вывод.
    res = _resume(llm_verdict="подходит", llm_comment="Старый комментарий", llm_red_flags=None)
    ev = _event()  # all dossier fields None
    html = build_card_html(res, ev, _search(), _snap())
    assert "Вывод: Старый комментарий" in html
    assert "Сильные стороны" not in html


# ── Tests: no body link ───────────────────────────────────────────────────────


def test_card_no_body_link() -> None:
    html = build_card_html(_resume(), _event(), _search(), _snap())
    assert "Открыть на hh.ru" not in html
    assert "<a href=" not in html


# ── Tests: HTML safety ────────────────────────────────────────────────────────


def test_card_escapes_special_chars_in_position() -> None:
    srch = _search(position_name="Директор <ОП> & CEO")
    html = build_card_html(_resume(), _event(), srch, _snap())
    assert "&lt;ОП&gt;" in html
    assert "&amp;" in html


# ── Tests: build_inline_keyboard() ───────────────────────────────────────────


def test_keyboard_has_four_action_buttons() -> None:
    kb = build_inline_keyboard(42, "https://hh.ru/resume/abc")
    row0 = kb.inline_keyboard[0]
    assert len(row0) == 4
    cbs = [b.callback_data for b in row0]
    assert "screen:42:approve" in cbs
    assert "screen:42:reject" in cbs
    assert "screen:42:doubt" in cbs
    assert "screen:42:stop_list" in cbs


def test_keyboard_has_url_button() -> None:
    kb = build_inline_keyboard(42, "https://hh.ru/resume/abc")
    row1 = kb.inline_keyboard[1]
    assert len(row1) == 1
    assert row1[0].url == "https://hh.ru/resume/abc"


@pytest.mark.parametrize("status", ["approve", "reject", "doubt", "stop_list"])
def test_callback_data_fits_64_bytes(status: str) -> None:
    max_bigint = 9_223_372_036_854_775_807
    cb = f"screen:{max_bigint}:{status}"
    assert len(cb.encode()) <= 64, f"callback_data too long: {len(cb.encode())} bytes"
