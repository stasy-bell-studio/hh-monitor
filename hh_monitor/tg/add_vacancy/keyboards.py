"""Inline keyboards for the "Add Vacancy" FSM wizard.

Callback data scheme:
  add_vacancy:start              — entry button (lives in the admin panel)
  av:cancel                      — abort the wizard from any step
  av:mode:text / av:mode:file
  av:retry                       — retry LLM parse after a failure (S3)
  av:insurance:yes / av:insurance:no
  av:fresh:{days}                — freshness period choice (S3c); 0 = no filter
  av:review:ok / av:review:more
  av:launch:go / av:launch:draft
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

ENTRY_CALLBACK = "add_vacancy:start"

_CANCEL_BTN = InlineKeyboardButton(text="❌ Отмена", callback_data="av:cancel")

# Single source of truth for the resume-freshness period options (days, button label).
# 21 (3 недели) is the recommended/default option. 0 = no period filter.
FRESHNESS_OPTIONS: tuple[tuple[int, str], ...] = (
    (7, "1 неделя"),
    (14, "2 недели"),
    (21, "3 недели (реком.)"),
    (30, "Месяц"),
    (0, "Без ограничения"),
)


def format_freshness(days: int) -> str:
    """Human-readable rendering of resume_freshness_days for review/launch cards."""
    mapping = {7: "1 неделя", 14: "2 недели", 21: "3 недели", 30: "месяц", 0: "без ограничения"}
    return mapping.get(days, f"{days} дн.")


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


def kb_insurance() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да, страховая", callback_data="av:insurance:yes"),
                InlineKeyboardButton(
                    text="➡️ Нет, другая роль", callback_data="av:insurance:no"
                ),
            ],
            [_CANCEL_BTN],
        ]
    )


def kb_freshness() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=label, callback_data=f"av:fresh:{days}")]
        for days, label in FRESHNESS_OPTIONS
    ]
    rows.append([_CANCEL_BTN])
    return InlineKeyboardMarkup(inline_keyboard=rows)


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


def kb_review_with_unknown() -> InlineKeyboardMarkup:
    """Review keyboard variant shown when portrait contains unresolved region names."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✏️ Исправить портрет", callback_data="av:review:more"),
                InlineKeyboardButton(text="⚠️ Запустить без региона", callback_data="av:review:ok"),
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
