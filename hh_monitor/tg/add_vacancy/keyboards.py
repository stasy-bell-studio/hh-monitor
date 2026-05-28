"""Inline keyboards for the "Add Vacancy" FSM wizard.

Callback data scheme:
  add_vacancy:start          — entry button (lives in the admin panel)
  av:cancel                  — abort the wizard from any step
  av:mode:text / av:mode:file
  av:retry                   — retry LLM parse after a failure (S3)
  av:review:ok / av:review:more
  av:critic:ok / av:critic:rewrite
  av:launch:go / av:launch:draft
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

ENTRY_CALLBACK = "add_vacancy:start"

_CANCEL_BTN = InlineKeyboardButton(text="❌ Отмена", callback_data="av:cancel")


def kb_entry() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить вакансию", callback_data=ENTRY_CALLBACK)]
        ]
    )


def kb_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_CANCEL_BTN]])


def kb_input_mode() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📝 Описать текстом", callback_data="av:mode:text"),
                InlineKeyboardButton(text="📎 Загрузить файл", callback_data="av:mode:file"),
            ],
            [_CANCEL_BTN],
        ]
    )


def kb_retry() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Повторить", callback_data="av:retry")],
            [_CANCEL_BTN],
        ]
    )


def kb_review() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Всё верно", callback_data="av:review:ok"),
                InlineKeyboardButton(text="✏️ Дополнить", callback_data="av:review:more"),
            ],
            [_CANCEL_BTN],
        ]
    )


def kb_critic() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Принять", callback_data="av:critic:ok"),
                InlineKeyboardButton(text="✏️ Переписать", callback_data="av:critic:rewrite"),
            ],
            [_CANCEL_BTN],
        ]
    )


def kb_launch() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🚀 Запустить в поиск", callback_data="av:launch:go"),
                InlineKeyboardButton(
                    text="💾 Сохранить как черновик", callback_data="av:launch:draft"
                ),
            ],
            [_CANCEL_BTN],
        ]
    )
