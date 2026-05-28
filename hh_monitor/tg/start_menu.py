"""DM /start inline menu and /help command handler for hh-monitor bot."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from hh_monitor.tg.add_vacancy.states import AddVacancy

start_menu_router = Router()
start_menu_router.message.filter(F.chat.type == ChatType.PRIVATE)

_HELP_TEXT = (
    "<b>hh-monitor — команды:</b>\n\n"
    "/start — главное меню\n"
    "/add_vacancy — добавить вакансию (FSM-визард)\n"
    "/list — список активных поисков\n"
    "/help — эта справка\n\n"
    "Кнопки внизу под полем ввода работают так же, как команды."
)


def _main_inline_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="➕ Добавить вакансию", callback_data="ux0:menu:add_vacancy"
                ),
                InlineKeyboardButton(text="📋 Мои поиски", callback_data="ux0:menu:list"),
            ],
            [
                InlineKeyboardButton(
                    text="⏸ Остановить поиск", callback_data="ux0:menu:stop"
                ),
                InlineKeyboardButton(text="❓ Помощь", callback_data="ux0:menu:help"),
            ],
        ]
    )


def _interrupt_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Да, прервать", callback_data="ux0:fsm_interrupt:yes"
                ),
                InlineKeyboardButton(
                    text="↩ Нет, продолжить", callback_data="ux0:fsm_interrupt:no"
                ),
            ]
        ]
    )


# ── /start ────────────────────────────────────────────────────────────────────


@start_menu_router.message(Command("start"), StateFilter(AddVacancy))
async def handle_start_in_fsm(message: Message, state: FSMContext) -> None:
    await message.answer(
        "Сейчас ты в процессе добавления вакансии.\nПрервать текущий ввод?",
        reply_markup=_interrupt_keyboard(),
    )


@start_menu_router.message(Command("start"))
async def handle_start(message: Message) -> None:
    await message.answer(
        "Привет, я hh-monitor бот. Что делаем?",
        reply_markup=_main_inline_keyboard(),
    )


# ── /help ─────────────────────────────────────────────────────────────────────


@start_menu_router.message(Command("help"))
async def handle_help_dm(message: Message) -> None:
    await message.answer(_HELP_TEXT)


# ── /add_vacancy ──────────────────────────────────────────────────────────────


@start_menu_router.message(Command("add_vacancy"))
async def handle_add_vacancy_cmd(message: Message, state: FSMContext) -> None:
    from hh_monitor.tg.add_vacancy.handlers import _start_wizard

    await _start_wizard(state, message)


# ── /list ─────────────────────────────────────────────────────────────────────


@start_menu_router.message(Command("list"))
async def handle_list_cmd(message: Message) -> None:
    from hh_monitor.tg.control_panel import handle_dm_active

    await handle_dm_active(message)


# ── FSM interrupt callbacks ───────────────────────────────────────────────────


@start_menu_router.callback_query(F.data == "ux0:fsm_interrupt:yes")
async def handle_interrupt_yes(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if isinstance(callback.message, Message):
        await callback.message.edit_text("Ввод прерван.")
        await callback.message.answer(
            "Привет, я hh-monitor бот. Что делаем?",
            reply_markup=_main_inline_keyboard(),
        )
    await callback.answer()


@start_menu_router.callback_query(F.data == "ux0:fsm_interrupt:no")
async def handle_interrupt_no(callback: CallbackQuery) -> None:
    if isinstance(callback.message, Message):
        await callback.message.edit_text("ОК, продолжаем.")
    await callback.answer()


# ── Main menu inline callbacks ────────────────────────────────────────────────


@start_menu_router.callback_query(F.data == "ux0:menu:add_vacancy")
async def handle_menu_add_vacancy(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    from hh_monitor.tg.add_vacancy.handlers import _start_wizard

    if isinstance(callback.message, Message):
        await _start_wizard(state, callback.message)


@start_menu_router.callback_query(F.data == "ux0:menu:list")
async def handle_menu_list(callback: CallbackQuery) -> None:
    await callback.answer()
    from hh_monitor.tg.control_panel import handle_dm_active

    if isinstance(callback.message, Message):
        await handle_dm_active(callback.message)


@start_menu_router.callback_query(F.data == "ux0:menu:stop")
async def handle_menu_stop(callback: CallbackQuery) -> None:
    await callback.answer()
    from hh_monitor.tg.control_panel import handle_dm_active

    if isinstance(callback.message, Message):
        await handle_dm_active(callback.message)


@start_menu_router.callback_query(F.data == "ux0:menu:help")
async def handle_menu_help(callback: CallbackQuery) -> None:
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.answer(_HELP_TEXT)
