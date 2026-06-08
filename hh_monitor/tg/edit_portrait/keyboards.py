"""Inline keyboards for the "Edit Portrait" FSM.

Callback scheme (entry button lives in the /active card as adm:edit_portrait:{id}):
  ep:sections                — back to the section menu
  ep:sec:{section}           — open a section's field list
  ep:fld:{idx}               — pick field #idx (FIELDS index)
  ep:bool:{idx}:{0|1}        — set a bool field
  ep:lit:{idx}:{value}       — set a Literal field
  ep:fresh:{idx}:{days}      — set resume_freshness_days via period buttons
  ep:save                    — validate + persist + regen critic
  ep:cancel                  — abort the editor
  ep:done                    — close the read-only critic message
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from hh_monitor.tg.add_vacancy.keyboards import FRESHNESS_OPTIONS
from hh_monitor.tg.edit_portrait.fields import (
    FIELDS,
    SECTION_LABELS,
    SECTION_ORDER,
    FieldDesc,
    fields_in,
)

_CANCEL_BTN = InlineKeyboardButton(text="❌ Отмена", callback_data="ep:cancel")
_BACK_BTN = InlineKeyboardButton(text="⬅️ К разделам", callback_data="ep:sections")


def kb_sections() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=SECTION_LABELS[s], callback_data=f"ep:sec:{s}")]
        for s in SECTION_ORDER
    ]
    rows.append([InlineKeyboardButton(text="✅ Сохранить", callback_data="ep:save")])
    rows.append([_CANCEL_BTN])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_section(section: str) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(text=desc.label, callback_data=f"ep:fld:{idx}")
        for idx, desc in fields_in(section)
    ]
    # Two columns to keep long sections (weights) compact.
    rows: list[list[InlineKeyboardButton]] = [
        buttons[i : i + 2] for i in range(0, len(buttons), 2)
    ]
    rows.append([_BACK_BTN])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_bool(idx: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да", callback_data=f"ep:bool:{idx}:1"),
                InlineKeyboardButton(text="❌ Нет", callback_data=f"ep:bool:{idx}:0"),
            ],
            [_BACK_BTN],
        ]
    )


def kb_literal(idx: int, desc: FieldDesc) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=choice, callback_data=f"ep:lit:{idx}:{choice}")]
        for choice in desc.choices
    ]
    rows.append([_BACK_BTN])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_freshness(idx: int) -> InlineKeyboardMarkup:
    """Period buttons for resume_freshness_days (mirrors the add-vacancy wizard options)."""
    rows = [
        [InlineKeyboardButton(text=label, callback_data=f"ep:fresh:{idx}:{days}")]
        for days, label in FRESHNESS_OPTIONS
    ]
    rows.append([_BACK_BTN])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_cancel_field() -> InlineKeyboardMarkup:
    """Shown while waiting for a typed value — lets the user bail to the menu."""
    return InlineKeyboardMarkup(inline_keyboard=[[_BACK_BTN]])


def kb_critic_done() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="✅ Готово", callback_data="ep:done")]]
    )


__all__ = [
    "FIELDS",
    "kb_bool",
    "kb_cancel_field",
    "kb_critic_done",
    "kb_freshness",
    "kb_literal",
    "kb_section",
    "kb_sections",
]
