"""Tests for hh_monitor.tg.cards — HTML rendering and inline keyboard."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from hh_monitor.db.models import Event, Resume, Search
from hh_monitor.tg.cards import build_card_html, build_inline_keyboard, safe

# ── Helpers ───────────────────────────────────────────────────────────────────


def _resume(
    hh_resume_id: str = "abc123",
    score_total: int = 75,
    fit_score: int = 70,
    llm_score: int = 80,
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
) -> Event:
    e = MagicMock(spec=Event)
    e.id = id_
    e.llm_verdict = llm_verdict
    e.llm_red_flags = llm_red_flags
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


# ── Tests: build_card_html() ──────────────────────────────────────────────────


def test_card_html_basic_render() -> None:
    html = build_card_html(_resume(), _event(), _search(), _snap())
    assert "Директор филиала" in html
    assert "75/100" in html
    assert "подходит" in html
    assert "Директор филиала" in html  # real_role
    assert "Минск" in html
    assert "Высшее" in html
    assert "https://hh.ru/resume/abc123" in html


def test_card_html_salary_shown_when_rur() -> None:
    html = build_card_html(_resume(), _event(), _search(), _snap(salary=150000))
    assert "150000" in html
    assert "₽" in html


def test_card_html_no_salary_line_when_absent() -> None:
    html = build_card_html(_resume(), _event(), _search(), _snap(salary=None))
    assert "₽" not in html


def test_card_html_no_snapshot_fields_skipped() -> None:
    html = build_card_html(_resume(), _event(), _search(), snapshot_payload=None)
    assert "Регион" not in html
    assert "ЗП" not in html
    assert "Образование" not in html
    # should still have required fields
    assert "75/100" in html
    assert "https://hh.ru/resume/abc123" in html


def test_card_html_red_flags_list() -> None:
    res = _resume(llm_red_flags=["частые смены", "нет опыта"])
    html = build_card_html(res, _event(), _search(), _snap())
    assert "частые смены" in html
    assert "нет опыта" in html
    assert "⚠️" in html


def test_card_html_red_flags_text_from_event() -> None:
    res = _resume(llm_red_flags=None)
    ev = _event(llm_red_flags="нет опыта вождения")
    html = build_card_html(res, ev, _search(), _snap())
    assert "нет опыта вождения" in html


def test_card_html_no_red_flags_no_warning() -> None:
    res = _resume(llm_red_flags=None)
    ev = _event(llm_red_flags=None)
    html = build_card_html(res, ev, _search(), _snap())
    assert "⚠️" not in html


def test_card_html_escapes_special_chars_in_position() -> None:
    srch = _search(position_name="Директор <ОП> & CEO")
    # position_name with injected tags should not break rendering
    html2 = build_card_html(_resume(), _event(), srch, _snap())
    assert "&lt;ОП&gt;" in html2
    assert "&amp;" in html2


def test_card_html_comment_hidden_when_verdict_mимо() -> None:
    res = _resume(llm_verdict="мимо", llm_comment="Не подходит совсем")
    html = build_card_html(res, _event(), _search(), _snap())
    assert "Не подходит совсем" not in html


def test_card_html_comment_shown_when_verdict_not_mимо() -> None:
    res = _resume(llm_verdict="подходит", llm_comment="Хороший опыт")
    html = build_card_html(res, _event(), _search(), _snap())
    assert "Хороший опыт" in html


# ── Tests: build_inline_keyboard() ───────────────────────────────────────────


def test_keyboard_has_three_action_buttons() -> None:
    kb = build_inline_keyboard(42, "https://hh.ru/resume/abc")
    row0 = kb.inline_keyboard[0]
    assert len(row0) == 3
    cbs = [b.callback_data for b in row0]
    assert "screen:42:approve" in cbs
    assert "screen:42:reject" in cbs
    assert "screen:42:doubt" in cbs


def test_keyboard_has_url_button() -> None:
    kb = build_inline_keyboard(42, "https://hh.ru/resume/abc")
    row1 = kb.inline_keyboard[1]
    assert len(row1) == 1
    assert row1[0].url == "https://hh.ru/resume/abc"


@pytest.mark.parametrize("status", ["approve", "reject", "doubt"])
def test_callback_data_fits_64_bytes(status: str) -> None:
    max_bigint = 9_223_372_036_854_775_807
    cb = f"screen:{max_bigint}:{status}"
    assert len(cb.encode()) <= 64, f"callback_data too long: {len(cb.encode())} bytes"
