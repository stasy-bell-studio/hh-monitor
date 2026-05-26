"""Tests for hh_monitor.tg.reasons — STATUS_LABELS, PRESETS, keyboard builder."""

from __future__ import annotations

import pytest

from hh_monitor.db.enums import ScreeningStatus
from hh_monitor.tg.reasons import (
    CUSTOM_CODE,
    PRESETS,
    STATUS_LABELS,
    build_reason_keyboard,
    format_final_text,
)

ALL_STATUSES = list(ScreeningStatus)


def test_status_labels_covers_all_statuses() -> None:
    assert set(STATUS_LABELS.keys()) == set(ScreeningStatus)


def test_presets_covers_all_statuses() -> None:
    assert set(PRESETS.keys()) == set(ScreeningStatus)


@pytest.mark.parametrize("status", ALL_STATUSES)
def test_build_reason_keyboard_has_presets(status: ScreeningStatus) -> None:
    kb = build_reason_keyboard(42, status)
    cbs = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    for reason in PRESETS[status]:
        assert f"reason:42:{status.value}:{reason.code}" in cbs


@pytest.mark.parametrize("status", ALL_STATUSES)
def test_build_reason_keyboard_has_custom_button(status: ScreeningStatus) -> None:
    kb = build_reason_keyboard(42, status)
    cbs = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert f"reason:42:{status.value}:{CUSTOM_CODE}" in cbs


@pytest.mark.parametrize("status", ALL_STATUSES)
def test_build_reason_keyboard_has_back_button(status: ScreeningStatus) -> None:
    kb = build_reason_keyboard(42, status)
    cbs = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert "back:42" in cbs


@pytest.mark.parametrize("status", ALL_STATUSES)
def test_callback_data_fits_64_bytes_for_all_presets(status: ScreeningStatus) -> None:
    max_bigint = 9_223_372_036_854_775_807
    kb = build_reason_keyboard(max_bigint, status)
    for row in kb.inline_keyboard:
        for btn in row:
            if btn.callback_data:
                length = len(btn.callback_data.encode())
                assert length <= 64, (
                    f"callback_data too long ({length} bytes): {btn.callback_data!r}"
                )


def test_format_final_text_prepends_status_line() -> None:
    result = format_final_text(
        "Original card", ScreeningStatus.APPROVE, "Релевантный опыт", "lukin"
    )
    assert result.startswith("✅ Подходит: Релевантный опыт — @lukin")
    assert "Original card" in result


def test_format_final_text_handles_none_username() -> None:
    result = format_final_text("Card text", ScreeningStatus.REJECT, "Слабый опыт", None)
    assert "аноним" in result


def test_format_final_text_stop_list() -> None:
    result = format_final_text("Card", ScreeningStatus.STOP_LIST, "Конкурент", "bob")
    assert result.startswith("🚫 Стоп-лист: Конкурент — @bob")
