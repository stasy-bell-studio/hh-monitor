"""Tests for hh_monitor.tg.cards — HTML rendering and inline keyboard."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from hh_monitor.db.models import Event, Resume, Search
from hh_monitor.tg.cards import (
    _bullets,
    _coerce_display,
    _plural_years,
    build_card_html,
    build_detail_collapse_keyboard,
    build_detail_html,
    build_inline_keyboard,
    build_update_summary,
    is_best_score,
    safe,
    score_badge,
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


# ── Tests: build_update_summary() + «Обновлено» card line ─────────────────────


def test_update_summary_aggregates_changes() -> None:
    """A multi-field edit yields one before→after line per changed field, order preserved."""
    s = build_update_summary(
        [
            ("UPDATED_POSITION", {"before": "Директор", "after": "Гендир"}),
            ("UPDATED_SALARY", {"before": 80000, "after": 90000}),
        ]
    )
    assert s == "Директор → Гендир\n80000 → 90000"


def test_update_summary_dedups_identical() -> None:
    s = build_update_summary(
        [
            ("UPDATED_POSITION", {"before": "A", "after": "B"}),
            ("UPDATED_SALARY", {"before": "A", "after": "B"}),
        ]
    )
    assert s == "A → B"


def test_update_summary_new_only_is_none() -> None:
    """AC6: a brand-new résumé has no meaningful 'updated' line."""
    assert build_update_summary([("NEW", {"curr_snapshot_id": 1})]) is None


def test_update_summary_reactivated_label() -> None:
    assert build_update_summary([("REACTIVATED", {"curr_snapshot_id": 1})]) == "Возобновлено"


def test_update_summary_skips_new_in_mixed_group() -> None:
    s = build_update_summary(
        [
            ("NEW", {"curr_snapshot_id": 1}),
            ("UPDATED_SALARY", {"before": 1, "after": 2}),
        ]
    )
    assert s == "1 → 2"


def test_card_renders_update_block() -> None:
    """AC2: the winner card carries the «Обновлено» block with every change."""
    html = build_card_html(
        _resume(),
        _event(),
        _search(),
        _snap(),
        update_summary="Директор → Гендир\n80000 → 90000",
    )
    assert "✏️ Обновлено:" in html
    assert "Директор → Гендир" in html
    assert "80000 → 90000" in html


def test_card_no_update_block_by_default() -> None:
    """AC6: a single-event card (no update_summary) is unchanged — no «Обновлено» line."""
    html = build_card_html(_resume(), _event(), _search(), _snap())
    assert "Обновлено" not in html


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


def test_card_anchor_score_green_emoji() -> None:
    html = build_card_html(_resume(score_total=75), _event(), _search(), _snap())
    assert html.splitlines()[0].startswith("🟢")


def test_card_anchor_score_yellow_emoji() -> None:
    html = build_card_html(_resume(score_total=59), _event(), _search(), _snap())
    assert html.splitlines()[0].startswith("🟡")


def test_card_anchor_score_best_emoji() -> None:
    html = build_card_html(_resume(score_total=76), _event(), _search(), _snap())
    assert html.splitlines()[0].startswith("🟣")
    assert "🏆 ЛУЧШИЙ" in html.splitlines()[0]


def test_card_anchor_score_none_emoji() -> None:
    html = build_card_html(_resume(score_total=None), _event(), _search(), _snap())
    assert html.splitlines()[0].startswith("⚪")


@pytest.mark.parametrize(
    "score,expected",
    [
        (None, "⚪"),
        (0, "🟡"),
        (59, "🟡"),
        (60, "🟢"),
        (75, "🟢"),
        (76, "🟣"),
        (100, "🟣"),
    ],
)
def test_score_badge_boundaries(score: int | None, expected: str) -> None:
    assert score_badge(score) == expected


@pytest.mark.parametrize(
    "score,expected",
    [
        (None, False),
        (75, False),
        (76, True),
    ],
)
def test_is_best_score_boundaries(score: int | None, expected: bool) -> None:
    assert is_best_score(score) == expected


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
    html = build_card_html(_resume(llm_real_role="Директор филиала"), _event(), _search(), _snap())
    assert "Реальная роль: Директор филиала" in html


def test_card_education_folded_into_geo_line() -> None:
    html = build_card_html(_resume(), _event(), _search(), _snap(education="Высшее"))
    assert "<b>Образование:</b>" not in html
    assert "Высшее" in html
    # education appears in the combined bare geo line (no long label anymore)
    assert "Регион · возраст · опыт · образование" not in html


def test_card_geo_line_contains_all_present_parts() -> None:
    html = build_card_html(
        _resume(),
        _event(),
        _search(),
        _snap(region="Минск", age=35, exp_months=96, education="Высшее"),
    )
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
    assert "🚩 Риски:" in html
    assert "нет лицензии" in html
    assert "🧭 Вывод: Не подходит совсем" in html


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
    assert "✅ Сильные стороны:" in html
    assert "10 лет в рознице, рост выручки 30%." in html
    assert "⚠️ Слабые места:" in html
    assert "Нет опыта в логистике." in html
    assert "🚩 Риски:" in html
    assert "Частые смены работодателей." in html
    assert "🧭 Вывод: Сильный кандидат, рекомендуем на собеседование." in html


def test_card_dossier_omits_empty_blocks() -> None:
    res = _resume(llm_verdict="подходит", llm_comment=None, llm_red_flags=None)
    ev = _event(llm_facts_confirmed="Есть управленческий опыт.")
    html = build_card_html(res, ev, _search(), _snap())
    assert "✅ Сильные стороны:" in html
    assert "Есть управленческий опыт." in html
    assert "Слабые места" not in html
    assert "🚩 Риски" not in html
    assert "Вывод:" not in html


def test_card_dossier_long_facts_truncated() -> None:
    long_facts = "слово " * 60  # ~360 chars, no sentence break
    res = _resume(llm_verdict="подходит", llm_red_flags=None)
    ev = _event(llm_facts_confirmed=long_facts)
    html = build_card_html(res, ev, _search(), _snap())
    assert "…" in html
    # Label line is just the header; bullet line holds the content (≤ item_limit + prefix)
    bullet_line = next(ln for ln in html.splitlines() if "•" in ln)
    assert len(bullet_line) <= len("   • ") + 160 + len("…")


def test_card_dossier_legacy_event_falls_back_to_comment() -> None:
    # Pre-9.3 event: no dossier fields → fall back to resume.llm_comment as Вывод.
    res = _resume(llm_verdict="подходит", llm_comment="Старый комментарий", llm_red_flags=None)
    ev = _event()  # all dossier fields None
    html = build_card_html(res, ev, _search(), _snap())
    assert "🧭 Вывод: Старый комментарий" in html
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
    assert len(row1) == 2
    assert row1[0].url == "https://hh.ru/resume/abc"


@pytest.mark.parametrize("status", ["approve", "reject", "doubt", "stop_list"])
def test_callback_data_fits_64_bytes(status: str) -> None:
    max_bigint = 9_223_372_036_854_775_807
    cb = f"screen:{max_bigint}:{status}"
    assert len(cb.encode()) <= 64, f"callback_data too long: {len(cb.encode())} bytes"


# ── Tests: "Подробнее" button + build_detail_html ─────────────────────────────


def test_keyboard_has_details_button() -> None:
    kb = build_inline_keyboard(42, "https://hh.ru/resume/abc")
    row1 = kb.inline_keyboard[1]
    cbs = [b.callback_data for b in row1]
    assert "details:42" in cbs
    labels = [b.text for b in row1]
    assert "🔍 Подробнее" in labels


def test_details_callback_data_fits_64_bytes() -> None:
    max_bigint = 9_223_372_036_854_775_807
    cb = f"details:{max_bigint}"
    assert len(cb.encode()) <= 64


def test_detail_collapse_keyboard_single_button() -> None:
    kb = build_detail_collapse_keyboard()
    assert len(kb.inline_keyboard) == 1
    assert len(kb.inline_keyboard[0]) == 1
    btn = kb.inline_keyboard[0][0]
    assert btn.text == "🗑 Свернуть"
    assert btn.callback_data == "detail_collapse"


def test_detail_html_all_sections() -> None:
    res = _resume(llm_red_flags=None)
    ev = _event(
        llm_facts_confirmed="10 лет управленческого опыта.",
        llm_weak_spots="Нет опыта в FMCG.",
        llm_red_flags="Частые смены работодателей.",
        llm_verdict_text="Сильный кандидат.",
        llm_interview_questions=["Почему ушли с прошлого места?", "Опыт с прибылью?"],
    )
    html = build_detail_html(res, ev, _search("Директор филиала"))
    assert "🔍 <b>Подробный анализ — Директор филиала</b>" in html
    assert "✅ <b>Подтверждённые факты:</b>\n10 лет управленческого опыта." in html
    assert "⚠️ <b>Слабые места:</b>\nНет опыта в FMCG." in html
    assert "🚩 <b>Риски:</b>\nЧастые смены работодателей." in html
    assert "❓ <b>Вопросы на интервью:</b>" in html
    assert "1. Почему ушли с прошлого места?" in html
    assert "2. Опыт с прибылью?" in html
    assert "🧭 <b>Вердикт:</b>\nСильный кандидат." in html


def test_detail_html_no_truncation() -> None:
    long_facts = "слово " * 60  # ~360 chars
    res = _resume(llm_red_flags=None)
    ev = _event(llm_facts_confirmed=long_facts)
    html = build_detail_html(res, ev, _search())
    assert "…" not in html
    assert long_facts.strip() in html


def test_detail_html_omits_empty_sections() -> None:
    res = _resume(llm_red_flags=None)
    ev = _event(llm_verdict_text="Только вердикт.")
    html = build_detail_html(res, ev, _search())
    assert "🧭 <b>Вердикт:</b>" in html
    assert "Подтверждённые факты" not in html
    assert "Слабые места" not in html
    assert "Вопросы на интервью" not in html


def test_detail_html_empty_dossier_fallback() -> None:
    res = _resume(llm_red_flags=None)
    ev = _event()  # all dossier fields None
    html = build_detail_html(res, ev, _search())
    assert "Подробных данных по этому кандидату нет (обогащено старой версией)." in html


def test_detail_html_escapes_html() -> None:
    res = _resume(llm_red_flags=None)
    ev = _event(llm_facts_confirmed="<script>alert(1)</script>")
    html = build_detail_html(res, ev, _search())
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


# ── _coerce_display ───────────────────────────────────────────────────────────


def test_coerce_display_plain_string() -> None:
    assert _coerce_display("text") == "text"


def test_coerce_display_none() -> None:
    assert _coerce_display(None) == ""


def test_coerce_display_dict_no_braces() -> None:
    result = _coerce_display({"a": "b"})
    assert "{" not in result
    assert "a" in result and "b" in result


def test_coerce_display_list() -> None:
    result = _coerce_display(["item1", "item2"])
    assert "item1" in result and "item2" in result
    assert "{" not in result


def test_coerce_display_stringified_dict_repr() -> None:
    result = _coerce_display("{'key': 'val'}")
    assert "{" not in result
    assert "key" in result


def test_coerce_display_stringified_json() -> None:
    result = _coerce_display('{"key": "val"}')
    assert "{" not in result
    assert "key" in result


def test_coerce_display_malformed_brace_string_passthrough() -> None:
    result = _coerce_display("{not valid")
    assert result == "{not valid"


# ── _bullets ──────────────────────────────────────────────────────────────────


def test_bullets_none_returns_empty() -> None:
    assert _bullets(None) == ""


def test_bullets_empty_string_returns_empty() -> None:
    assert _bullets("") == ""


def test_bullets_basic_newline_split() -> None:
    out = _bullets("item1\nitem2\nitem3")
    assert "•" in out
    assert "item1" in out and "item2" in out


def test_bullets_max_items_default_three() -> None:
    out = _bullets("a\nb\nc\nd")
    assert out.count("•") == 3


def test_bullets_respects_custom_max_items() -> None:
    out = _bullets("a\nb\nc\nd", max_items=2)
    assert out.count("•") == 2


def test_bullets_truncates_long_items() -> None:
    long_item = "слово " * 40  # >> 160 chars
    out = _bullets(long_item)
    assert "…" in out


def test_bullets_no_ellipsis_for_short_items() -> None:
    out = _bullets("короткий пункт")
    assert "…" not in out


def test_bullets_html_escapes_content() -> None:
    out = _bullets("<b>bold</b>")
    assert "<b>" not in out
    assert "&lt;b&gt;" in out


def test_bullets_semicolon_split() -> None:
    out = _bullets("item1; item2; item3")
    assert out.count("•") == 3


def test_bullets_repairs_dict_repr() -> None:
    out = _bullets("{'факт': 'значение'}")
    assert "{" not in out
    assert "факт" in out


def test_bullets_card_renders_bullets_not_short() -> None:
    res = _resume(llm_red_flags=None)
    ev = _event(llm_facts_confirmed="СОГАЗ 2022\nКоманда 350 агентов\nP&L 1 млрд руб.")
    card = build_card_html(res, ev, _search())
    assert "•" in card
    assert "СОГАЗ 2022" in card
