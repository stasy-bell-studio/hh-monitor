"""Tests for the resume-freshness keyboard and label helper (S3c)."""

from __future__ import annotations

from hh_monitor.tg.add_vacancy.keyboards import (
    FRESHNESS_OPTIONS,
    format_freshness,
    kb_freshness,
)


def test_kb_freshness_has_five_period_buttons() -> None:
    kb = kb_freshness()
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    period_cbs = [c for c in cbs if c is not None and c.startswith("av:fresh:")]
    assert period_cbs == ["av:fresh:7", "av:fresh:14", "av:fresh:21", "av:fresh:30", "av:fresh:0"]
    assert "av:cancel" in cbs


def test_freshness_options_recommended_label() -> None:
    labels = dict((d, lbl) for d, lbl in FRESHNESS_OPTIONS)
    assert "реком." in labels[21]


def test_format_freshness() -> None:
    assert format_freshness(7) == "1 неделя"
    assert format_freshness(14) == "2 недели"
    assert format_freshness(21) == "3 недели"
    assert format_freshness(30) == "месяц"
    assert format_freshness(0) == "без ограничения"
    assert format_freshness(45) == "45 дн."
