"""FSM handlers for the "Edit Portrait" wizard (admin-topic only).

Entry: the «✏️ Редактировать портрет» button on the group /active card emits
``adm:edit_portrait:{search_id}``; the admin_router handler (tg/commands.py)
guards it and calls :func:`start_edit_portrait`.

Flow: load the search's portrait (YAML-first / DB-fallback) → show a section
menu built by introspecting the Portrait models → user edits any field with
full re-validation after each change → on save: re-derive prefilter + hh_params,
regenerate the critic prompt (shown read-only), and persist to the authoritative
source (YAML if YAML-backed, plus searches.portrait) in one transaction.

Access control mirrors add_vacancy: router-level admin/topic filters on messages,
inline ``_guard_callback`` on callbacks.
"""

from __future__ import annotations

from typing import Any

import structlog
import yaml
from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import BaseFilter, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from pydantic import ValidationError

from hh_monitor.config import settings
from hh_monitor.db.models import Search
from hh_monitor.fit import portrait as portrait_mod
from hh_monitor.fit.portrait import Portrait, load_all_portraits
from hh_monitor.fit.portrait_loader import load_portrait_for_search
from hh_monitor.llm_enrich.critic_lens_builder import build_deterministic_fallback
from hh_monitor.tg.add_vacancy.handlers import _render_review
from hh_monitor.tg.add_vacancy.llm import (
    derive_initial_hh_params,
    derive_prefilter,
    draft_critic_prompt,
)
from hh_monitor.tg.client import get_session_factory, is_admin
from hh_monitor.tg.edit_portrait import fields as fld
from hh_monitor.tg.edit_portrait import keyboards as kb
from hh_monitor.tg.edit_portrait.fields import FIELDS, SECTION_LABELS, FieldDesc
from hh_monitor.tg.edit_portrait.states import EditPortrait

logger = structlog.get_logger(__name__)

edit_portrait_router = Router(name="edit_portrait")

# Critic prompt is shown read-only; cap the displayed text well under the 4096
# Telegram limit (the full prompt is stored in searches.llm_critic_prompt).
_CRITIC_PREVIEW_MAX = 3800


# ── Access filters (sub-routers do NOT inherit parent router filters) ─────────────


class _AdminTopicFilter(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        topic_id = settings.telegram_admin_topic_id
        if not topic_id:
            return False
        return getattr(message, "message_thread_id", None) == topic_id


class _AdminUserFilter(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        return message.from_user is not None and is_admin(message.from_user.id)


edit_portrait_router.message.filter(_AdminTopicFilter(), _AdminUserFilter())


def _guard_callback(callback: CallbackQuery) -> bool:
    """True if the callback comes from an admin inside a non-private chat."""
    if callback.from_user is None or not is_admin(callback.from_user.id):
        return False
    msg = callback.message
    return not (msg is not None and msg.chat.type == ChatType.PRIVATE)


def _fmt_validation_error(exc: ValidationError) -> str:
    err = exc.errors()[0]
    loc = ".".join(str(p) for p in err.get("loc", ()))
    msg = err.get("msg", "недопустимое значение")
    return f"Недопустимое значение ({loc}): {msg}."


# ── Rendering helpers ─────────────────────────────────────────────────────────────


async def _show_menu(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    portrait = Portrait.model_validate(data["portrait_dict"])
    text = "✏️ <b>Редактирование портрета</b>\n\n" + _render_review(portrait)
    await message.answer(text[:4096], reply_markup=kb.kb_sections())


async def _show_section(message: Message, section: str) -> None:
    await message.answer(
        f"{SECTION_LABELS.get(section, section)} — выбери поле:",
        reply_markup=kb.kb_section(section),
    )


async def _apply(state: FSMContext, desc: FieldDesc, value: Any) -> str | None:
    """Validate the whole portrait with *value* applied; persist to FSM if valid."""
    data = await state.get_data()
    candidate = fld.with_value(data["portrait_dict"], desc.path, value)
    try:
        Portrait.model_validate(candidate)
    except ValidationError as exc:
        return _fmt_validation_error(exc)
    await state.update_data(portrait_dict=candidate)
    return None


# ── Entry (called from tg/commands.py admin_router handler) ───────────────────────


async def start_edit_portrait(callback: CallbackQuery, state: FSMContext) -> None:
    search_id = int((callback.data or "").split(":", 2)[2])
    factory = get_session_factory()
    async with factory() as session:
        search = await session.get(Search, search_id)
        if search is None:
            await callback.answer("Поиск не найден", show_alert=True)
            return
        try:
            portrait = load_portrait_for_search(search)
        except ValueError:
            await callback.answer("У поиска нет портрета", show_alert=True)
            return
        position_code = search.position_code
        position_name = search.position_name

    await state.clear()
    await state.set_state(EditPortrait.menu)
    await state.update_data(
        portrait_dict=portrait.model_dump(mode="json"),
        search_id=search_id,
        position_code=position_code,
        position_name=position_name,
    )
    await callback.answer()
    if isinstance(callback.message, Message):
        await _show_menu(callback.message, state)


# ── Menu navigation ──────────────────────────────────────────────────────────────


@edit_portrait_router.callback_query(F.data == "ep:sections")
async def handle_sections(callback: CallbackQuery, state: FSMContext) -> None:
    if not _guard_callback(callback):
        await callback.answer()
        return
    await callback.answer()
    await state.set_state(EditPortrait.menu)
    if isinstance(callback.message, Message):
        await _show_menu(callback.message, state)


@edit_portrait_router.callback_query(F.data.startswith("ep:sec:"))
async def handle_open_section(callback: CallbackQuery) -> None:
    if not _guard_callback(callback):
        await callback.answer()
        return
    await callback.answer()
    section = (callback.data or "").split(":", 2)[2]
    if isinstance(callback.message, Message):
        await _show_section(callback.message, section)


@edit_portrait_router.callback_query(F.data.startswith("ep:fld:"))
async def handle_pick_field(callback: CallbackQuery, state: FSMContext) -> None:
    if not _guard_callback(callback):
        await callback.answer()
        return
    await callback.answer()
    idx = int((callback.data or "").split(":")[2])
    desc = FIELDS[idx]
    data = await state.get_data()
    current = fld.get_value(data["portrait_dict"], desc.path)
    if not isinstance(callback.message, Message):
        return
    head = f"<b>{desc.label}</b>\nТекущее: {fld.format_value(desc, current)}"
    if desc.path == ("resume_freshness_days",):
        await callback.message.answer(head, reply_markup=kb.kb_freshness(idx))
        return
    if desc.kind == "bool":
        await callback.message.answer(head, reply_markup=kb.kb_bool(idx))
        return
    if desc.kind == "literal":
        await callback.message.answer(head, reply_markup=kb.kb_literal(idx, desc))
        return
    await state.set_state(EditPortrait.awaiting_value)
    await state.update_data(cur_field=idx)
    await callback.message.answer(
        f"{head}\n\nПришли новое значение — {fld.input_hint(desc)}.",
        reply_markup=kb.kb_cancel_field(),
    )


@edit_portrait_router.message(StateFilter(EditPortrait.awaiting_value))
async def handle_value(message: Message, state: FSMContext) -> None:
    if message.text is None:
        await message.answer("Пришли значение текстом.", reply_markup=kb.kb_cancel_field())
        return
    data = await state.get_data()
    idx = data.get("cur_field")
    if idx is None:
        await state.set_state(EditPortrait.menu)
        await _show_menu(message, state)
        return
    desc = FIELDS[int(idx)]
    try:
        value = fld.parse_value(desc, message.text)
    except fld.FieldParseError as exc:
        await message.answer(f"⚠️ {exc} Попробуй ещё раз.", reply_markup=kb.kb_cancel_field())
        return
    err = await _apply(state, desc, value)
    if err is not None:
        await message.answer(f"⚠️ {err} Попробуй ещё раз.", reply_markup=kb.kb_cancel_field())
        return
    await state.set_state(EditPortrait.menu)
    await message.answer(f"✅ «{desc.label}» обновлено.")
    await _show_section(message, desc.section)


@edit_portrait_router.callback_query(F.data.startswith("ep:bool:"))
async def handle_set_bool(callback: CallbackQuery, state: FSMContext) -> None:
    if not _guard_callback(callback):
        await callback.answer()
        return
    parts = (callback.data or "").split(":")
    idx = int(parts[2])
    desc = FIELDS[idx]
    err = await _apply(state, desc, parts[3] == "1")
    await callback.answer("Обновлено" if err is None else "Ошибка")
    if isinstance(callback.message, Message):
        if err is not None:
            await callback.message.answer(f"⚠️ {err}")
        await _show_section(callback.message, desc.section)


@edit_portrait_router.callback_query(F.data.startswith("ep:fresh:"))
async def handle_set_freshness(callback: CallbackQuery, state: FSMContext) -> None:
    if not _guard_callback(callback):
        await callback.answer()
        return
    parts = (callback.data or "").split(":")
    idx = int(parts[2])
    desc = FIELDS[idx]
    err = await _apply(state, desc, int(parts[3]))
    await callback.answer("Обновлено" if err is None else "Ошибка")
    if isinstance(callback.message, Message):
        if err is not None:
            await callback.message.answer(f"⚠️ {err}")
        await _show_section(callback.message, desc.section)


@edit_portrait_router.callback_query(F.data.startswith("ep:lit:"))
async def handle_set_literal(callback: CallbackQuery, state: FSMContext) -> None:
    if not _guard_callback(callback):
        await callback.answer()
        return
    parts = (callback.data or "").split(":")
    idx = int(parts[2])
    desc = FIELDS[idx]
    err = await _apply(state, desc, parts[3])
    await callback.answer("Обновлено" if err is None else "Ошибка")
    if isinstance(callback.message, Message):
        if err is not None:
            await callback.message.answer(f"⚠️ {err}")
        await _show_section(callback.message, desc.section)


# ── Cancel / done ────────────────────────────────────────────────────────────────


@edit_portrait_router.callback_query(F.data == "ep:cancel")
async def handle_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    if not _guard_callback(callback):
        await callback.answer()
        return
    await callback.answer()
    await state.clear()
    if isinstance(callback.message, Message):
        await callback.message.answer("❌ Редактирование отменено. Изменения не сохранены.")


@edit_portrait_router.callback_query(F.data == "ep:done")
async def handle_done(callback: CallbackQuery) -> None:
    if not _guard_callback(callback):
        await callback.answer()
        return
    await callback.answer("Готово")


# ── Save ─────────────────────────────────────────────────────────────────────────


def _write_portrait_yaml(position_code: str, portrait_dict: dict[str, Any]) -> None:
    """Persist a YAML-backed portrait, matching scripts/import_portraits_csv.py."""
    path = portrait_mod._PORTRAITS_DIR / f"{position_code}.yaml"
    with path.open("w", encoding="utf-8") as f:
        yaml.dump(
            portrait_dict, f, allow_unicode=True, default_flow_style=False, sort_keys=False
        )


@edit_portrait_router.callback_query(F.data == "ep:save")
async def handle_save(callback: CallbackQuery, state: FSMContext) -> None:
    if not _guard_callback(callback):
        await callback.answer()
        return
    await callback.answer()
    if not isinstance(callback.message, Message):
        return

    data = await state.get_data()
    position_name: str = data["position_name"]
    position_code: str = data["position_code"]
    search_id = int(data["search_id"])

    try:
        portrait = Portrait.model_validate(data["portrait_dict"])
    except ValidationError as exc:
        await callback.message.answer(f"⚠️ {_fmt_validation_error(exc)}")
        return

    # Re-derive the prefilter (lives nested inside the portrait dict).
    portrait_dict = portrait.model_dump(mode="json")
    portrait_dict["prefilter"] = derive_prefilter(portrait).model_dump()
    portrait = Portrait.model_validate(portrait_dict)

    # Regenerate the critic prompt (read-only review below).
    try:
        critic = await draft_critic_prompt(portrait, position_name)
    except Exception as exc:  # never block save on an LLM failure
        logger.warning("edit_portrait.critic_failed", error=str(exc))
        critic = build_deterministic_fallback(portrait, position_name)

    # Re-derive hh.ru params; region edits must both ADD and REMOVE area ids.
    new_params = derive_initial_hh_params(portrait)
    yaml_backed = position_code in load_all_portraits()

    factory = get_session_factory()
    async with factory() as session:
        search = await session.get(Search, search_id)
        if search is None:
            await callback.message.answer("⚠️ Поиск исчез, изменения не сохранены.")
            return
        hh_params = {**search.hh_params, **new_params}
        if "area" not in new_params:
            hh_params.pop("area", None)
        if yaml_backed:
            _write_portrait_yaml(position_code, portrait_dict)
        search.portrait = portrait_dict
        search.llm_critic_prompt = critic
        search.hh_params = hh_params
        await session.commit()

    logger.info("edit_portrait.saved", position_code=position_code, search_id=search_id)
    await state.clear()
    await callback.message.answer(
        "✅ Портрет сохранён. Изменения применятся на следующем плановом прогоне."
    )
    await callback.message.answer(
        "<b>Промпт-критик пересоздан (только для просмотра):</b>\n\n"
        + critic[:_CRITIC_PREVIEW_MAX],
        reply_markup=kb.kb_critic_done(),
    )
