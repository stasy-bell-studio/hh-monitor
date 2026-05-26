from __future__ import annotations

from dataclasses import dataclass

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from hh_monitor.db.enums import ScreeningStatus

CUSTOM_CODE = "custom"

STATUS_LABELS: dict[ScreeningStatus, str] = {
    ScreeningStatus.APPROVE: "✅ Подходит",
    ScreeningStatus.REJECT: "❌ Мимо",
    ScreeningStatus.DOUBT: "🤔 Спорно",
    ScreeningStatus.STOP_LIST: "🚫 Стоп-лист",
}


@dataclass(frozen=True)
class Reason:
    code: str
    text: str


PRESETS: dict[ScreeningStatus, list[Reason]] = {
    ScreeningStatus.APPROVE: [
        Reason("relevant_exp", "Релевантный опыт"),
        Reason("exact_role", "Точная должность"),
        Reason("right_region", "Нужный регион"),
        Reason("ok_expectations", "Адекватные ожидания"),
    ],
    ScreeningStatus.REJECT: [
        Reason("weak_exp", "Слабый опыт"),
        Reason("wrong_region", "Не тот регион"),
        Reason("high_expectations", "Завышенные ожидания"),
        Reason("stop_industry", "Стоп-индустрия"),
    ],
    ScreeningStatus.DOUBT: [
        Reason("need_discuss", "Нужно обсудить"),
        Reason("borderline_exp", "Пограничный опыт"),
        Reason("non_standard", "Нестандартный профиль"),
    ],
    ScreeningStatus.STOP_LIST: [
        Reason("competitor", "Конкурент"),
        Reason("bad_history", "Прошлый плохой опыт"),
        Reason("portrait_mismatch", "Несовместимость по портрету"),
    ],
}


def build_reason_keyboard(event_id: int, status: ScreeningStatus) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    prefix = f"reason:{event_id}:{status.value}"
    for reason in PRESETS[status]:
        rows.append(
            [
                InlineKeyboardButton(
                    text=reason.text,
                    callback_data=f"{prefix}:{reason.code}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="✍️ Своя",
                callback_data=f"{prefix}:{CUSTOM_CODE}",
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                text="← Назад",
                callback_data=f"back:{event_id}",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def format_final_text(
    original_text: str,
    status: ScreeningStatus,
    reason_text: str,
    username: str | None,
) -> str:
    label = STATUS_LABELS[status]
    uname = f"@{username}" if username else "аноним"
    return f"{label}: {reason_text} — {uname}\n\n{original_text}"
