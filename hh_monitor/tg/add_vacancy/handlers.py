"""FSM handlers for the "Add Vacancy" wizard (admin-topic only).

State machine (aiogram FSMContext + MemoryStorage):
  S1_name → S2_input_mode → S3_portrait_raw → S3b_insurance → S4_review → S6_launch

Access control: every handler is restricted to admin users inside the HR
supergroup admin topic.  Message handlers use router-level filters; callback
handlers guard inline.  Private-chat callbacks are silently ignored (defense in
depth — the entry button is never rendered in DM).
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog
from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import BaseFilter, Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import insert

from hh_monitor.config import settings
from hh_monitor.db.models import Search
from hh_monitor.fit.portrait import Portrait
from hh_monitor.llm_enrich.critic_lens_builder import build_deterministic_fallback
from hh_monitor.regions.expander import resolve_region_names
from hh_monitor.searches.codes import next_unique_search_code, slugify
from hh_monitor.tg.add_vacancy import keyboards as kb
from hh_monitor.tg.add_vacancy.llm import (
    compute_gaps,
    derive_initial_hh_params,
    derive_prefilter,
    draft_critic_prompt,
    generate_enrichment_recs,
    parse_to_portrait_dict,
)
from hh_monitor.tg.add_vacancy.states import AddVacancy
from hh_monitor.tg.client import get_session_factory, is_admin
from hh_monitor.tg.file_parsers import (
    FileTooLarge,
    UnsupportedFileType,
    extract_text,
)

logger = structlog.get_logger(__name__)

add_vacancy_router = Router(name="add_vacancy")

MAX_NAME_LEN = 200
MAX_RAW_TEXT_LEN = 10_000
_CONCAT_SEPARATOR = "\n\n--- Дополнение ---\n\n"

# Hold references to fire-and-forget background scans so the event loop does not
# garbage-collect the task before it finishes (see RUF006 / asyncio docs).
_background_tasks: set[asyncio.Task[Any]] = set()


# ── Access filters (sub-routers do NOT inherit parent router filters) ────────────


class _AdminTopicFilter(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        topic_id = settings.telegram_admin_topic_id
        if not topic_id:
            return False
        return getattr(message, "message_thread_id", None) == topic_id


class _AdminUserFilter(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        return message.from_user is not None and is_admin(message.from_user.id)


add_vacancy_router.message.filter(_AdminTopicFilter(), _AdminUserFilter())


def _guard_callback(callback: CallbackQuery) -> bool:
    """True if the callback comes from an admin inside a non-private chat."""
    if callback.from_user is None or not is_admin(callback.from_user.id):
        return False
    msg = callback.message
    return not (msg is not None and msg.chat.type == ChatType.PRIVATE)


async def _reset_to_panel(state: FSMContext, message: Message) -> None:
    await state.clear()
    await message.answer("❌ Отменено. Возврат в панель управления.")


# ── Entry: command /add or inline button add_vacancy:start ───────────────────────


@add_vacancy_router.message(Command("add"), _AdminTopicFilter(), _AdminUserFilter())
async def handle_add_command(message: Message, state: FSMContext) -> None:
    await _start_wizard(state, message)


@add_vacancy_router.callback_query(F.data == kb.ENTRY_CALLBACK)
async def handle_add_start_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not _guard_callback(callback):
        await callback.answer()
        return
    await callback.answer()
    if isinstance(callback.message, Message):
        await _start_wizard(state, callback.message)


async def _start_wizard(state: FSMContext, message: Message) -> None:
    await state.clear()
    await state.set_state(AddVacancy.S1_name)
    await message.answer(
        "Название позиции? (короткая строка, например: "
        "«Senior Backend Python» или «Андеррайтер моторных»)",
        reply_markup=kb.kb_cancel(),
    )


# ── Universal cancel ─────────────────────────────────────────────────────────────


@add_vacancy_router.callback_query(F.data == "av:cancel")
async def handle_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    if not _guard_callback(callback):
        await callback.answer()
        return
    await callback.answer()
    if isinstance(callback.message, Message):
        await _reset_to_panel(state, callback.message)


# ── S1: position name ────────────────────────────────────────────────────────────


@add_vacancy_router.message(StateFilter(AddVacancy.S1_name), F.text)
async def handle_s1_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name or len(name) > MAX_NAME_LEN:
        await message.answer(
            f"Название должно быть непустым и ≤ {MAX_NAME_LEN} символов. Попробуй ещё раз.",
            reply_markup=kb.kb_cancel(),
        )
        return
    await state.update_data(position_name=name, visited_review=False)
    await state.set_state(AddVacancy.S2_input_mode)
    await message.answer("Как опишешь портрет?", reply_markup=kb.kb_input_mode())


# ── S2: input mode ───────────────────────────────────────────────────────────────


@add_vacancy_router.callback_query(StateFilter(AddVacancy.S2_input_mode), F.data == "av:mode:text")
async def handle_s2_text(callback: CallbackQuery, state: FSMContext) -> None:
    if not _guard_callback(callback):
        await callback.answer()
        return
    await callback.answer()
    await state.update_data(input_mode="text")
    await state.set_state(AddVacancy.S3_portrait_raw)
    if isinstance(callback.message, Message):
        await callback.message.answer(
            "Опиши портрет кандидата в свободной форме: ключевые требования, опыт, "
            "индустрии, география, зарплатные ожидания, что отсекаем сразу. "
            "Чем подробнее — тем точнее поиск.",
            reply_markup=kb.kb_cancel(),
        )


@add_vacancy_router.callback_query(StateFilter(AddVacancy.S2_input_mode), F.data == "av:mode:file")
async def handle_s2_file(callback: CallbackQuery, state: FSMContext) -> None:
    if not _guard_callback(callback):
        await callback.answer()
        return
    await callback.answer()
    await state.update_data(input_mode="file")
    await state.set_state(AddVacancy.S3_portrait_raw)
    if isinstance(callback.message, Message):
        await callback.message.answer(
            "Прикрепи файл с портретом (PDF, DOCX или TXT, до 5 МБ).",
            reply_markup=kb.kb_cancel(),
        )


# ── S3: raw portrait (text or file) ──────────────────────────────────────────────


@add_vacancy_router.message(StateFilter(AddVacancy.S3_portrait_raw), F.text)
async def handle_s3_text(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if not raw:
        await message.answer("Пустой текст. Опиши портрет.", reply_markup=kb.kb_cancel())
        return
    if len(raw) > MAX_RAW_TEXT_LEN:
        await message.answer(
            f"Слишком длинно (> {MAX_RAW_TEXT_LEN} символов). Сократи.",
            reply_markup=kb.kb_cancel(),
        )
        return
    await _accumulate_and_parse(message, state, raw)


@add_vacancy_router.message(StateFilter(AddVacancy.S3_portrait_raw), F.document)
async def handle_s3_document(message: Message, state: FSMContext) -> None:
    doc = message.document
    if doc is None:
        return
    if doc.file_size and doc.file_size > 5 * 1024 * 1024:
        await message.answer(
            "Файл больше 5 МБ. Загрузи файл поменьше.", reply_markup=kb.kb_cancel()
        )
        return
    try:
        bot = message.bot
        assert bot is not None
        tg_file = await bot.get_file(doc.file_id)
        assert tg_file.file_path is not None
        buf = await bot.download_file(tg_file.file_path)
        file_bytes = buf.read() if buf is not None else b""
        text = await extract_text(doc.mime_type or "", file_bytes)
    except FileTooLarge:
        await message.answer(
            "Файл больше 5 МБ. Загрузи файл поменьше.", reply_markup=kb.kb_cancel()
        )
        return
    except UnsupportedFileType:
        await message.answer(
            "Неподдерживаемый формат. Нужен PDF, DOCX или TXT.", reply_markup=kb.kb_cancel()
        )
        return
    except Exception as exc:
        logger.error("add_vacancy.file_extract_failed", error=str(exc))
        await message.answer(
            "Не удалось прочитать файл. Попробуй другой или опиши текстом.",
            reply_markup=kb.kb_cancel(),
        )
        return

    if not text.strip():
        await message.answer(
            "В файле не нашлось текста. Попробуй другой файл.", reply_markup=kb.kb_cancel()
        )
        return
    await _accumulate_and_parse(message, state, text.strip())


@add_vacancy_router.callback_query(StateFilter(AddVacancy.S3_portrait_raw), F.data == "av:retry")
async def handle_s3_retry(callback: CallbackQuery, state: FSMContext) -> None:
    if not _guard_callback(callback):
        await callback.answer()
        return
    await callback.answer()
    data = await state.get_data()
    raw = data.get("portrait_raw", "")
    if isinstance(callback.message, Message) and raw:
        await _run_parse(callback.message, state, raw)


async def _accumulate_and_parse(message: Message, state: FSMContext, new_text: str) -> None:
    data = await state.get_data()
    prev = data.get("portrait_raw", "")
    raw = prev + _CONCAT_SEPARATOR + new_text if (data.get("visited_review") and prev) else new_text
    await state.update_data(portrait_raw=raw)
    await _run_parse(message, state, raw)


async def _run_parse(message: Message, state: FSMContext, raw: str) -> None:
    data = await state.get_data()
    position_name = data["position_name"]
    await message.answer("⏳ Анализирую портрет…")
    try:
        portrait_dict = await parse_to_portrait_dict(raw, position_name)
    except Exception as exc:
        logger.error("add_vacancy.parse_failed", error=str(exc))
        await message.answer(
            "Не удалось разобрать портрет. Попробуем ещё раз?",
            reply_markup=kb.kb_retry(),
        )
        return
    await state.update_data(portrait_dict=portrait_dict)

    if data.get("insurance_role_asked"):
        # Re-parse path (Дополнить): re-apply stored insurance override.
        is_insurance = bool(data.get("insurance_role_is_insurance", True))
        updated = _apply_insurance_override(portrait_dict, is_insurance)
        await state.update_data(portrait_dict=updated)
        await _enter_review(message, state)
    else:
        # First parse: ask whether this is an insurance role.
        await state.set_state(AddVacancy.S3b_insurance)
        await message.answer(
            "Это страховая роль (нужен профильный страховой опыт)?",
            reply_markup=kb.kb_insurance(),
        )


# ── S3b: insurance role question ─────────────────────────────────────────────────


@add_vacancy_router.callback_query(
    StateFilter(AddVacancy.S3b_insurance), F.data == "av:insurance:yes"
)
async def handle_s3b_insurance_yes(callback: CallbackQuery, state: FSMContext) -> None:
    if not _guard_callback(callback):
        await callback.answer()
        return
    await callback.answer()
    await state.update_data(insurance_role_asked=True, insurance_role_is_insurance=True)
    if isinstance(callback.message, Message):
        await _enter_review(callback.message, state)


@add_vacancy_router.callback_query(
    StateFilter(AddVacancy.S3b_insurance), F.data == "av:insurance:no"
)
async def handle_s3b_insurance_no(callback: CallbackQuery, state: FSMContext) -> None:
    if not _guard_callback(callback):
        await callback.answer()
        return
    await callback.answer()
    await state.update_data(insurance_role_asked=True, insurance_role_is_insurance=False)
    data = await state.get_data()
    portrait_dict = _apply_insurance_override(data["portrait_dict"], is_insurance=False)
    await state.update_data(portrait_dict=portrait_dict)
    if isinstance(callback.message, Message):
        await _enter_review(callback.message, state)


def _apply_insurance_override(portrait_dict: dict[str, Any], is_insurance: bool) -> dict[str, Any]:
    """Return portrait_dict with insurance fields zeroed when is_insurance=False."""
    if not is_insurance:
        portrait_dict = dict(portrait_dict)
        portrait_dict["domain_governor_mode"] = "off"
        portrait_dict["min_insurance_experience_months"] = 0
        portrait_dict["min_motor_experience_months"] = 0
        portrait_dict["motor_experience_preferred"] = False
        portrait_dict["insurance_experience_mode"] = "soft"
    return portrait_dict


async def _enter_review(message: Message, state: FSMContext) -> None:
    """Render the S4 review card.  Generates enrichment recs with insurance context."""
    data = await state.get_data()
    portrait_dict = data["portrait_dict"]
    portrait = Portrait.model_validate(portrait_dict)
    position_name = data["position_name"]
    is_insurance = bool(data.get("insurance_role_is_insurance", True))

    try:
        enrichment_recs = await generate_enrichment_recs(
            portrait, position_name, is_insurance=is_insurance
        )
    except Exception:
        enrichment_recs = ""

    _ids, unknown_regions = resolve_region_names(portrait.filters.regions.primary)
    kb_fn = kb.kb_review_with_unknown if unknown_regions else kb.kb_review
    await state.set_state(AddVacancy.S4_review)
    review_text = _render_review(
        portrait, unknown_regions, enrichment_recs=enrichment_recs, is_insurance=is_insurance
    )
    await message.answer(review_text, reply_markup=kb_fn())


def _render_review(
    portrait: Portrait,
    unknown: list[str] | None = None,
    *,
    enrichment_recs: str = "",
    is_insurance: bool = True,
) -> str:
    unknown = unknown or []
    regions = portrait.filters.regions
    salary = portrait.filters.salary_range
    salary_str = f"{salary[0]}–{salary[1]} ₽" if salary else "не указана"
    lines = [
        "Вот как я понял портрет:\n",
        f"📌 Позиция: {portrait.position_name}",
        f"📝 Описание: {(portrait.position_description or '—')[:300]}",
        f"🎯 Критерии: {', '.join(portrait.evaluation_focus) or '—'}",
        f"🔍 Синонимы: {', '.join(portrait.position_synonyms) or '—'}",
        f"📍 Регионы (целевые): {', '.join(regions.primary) or '—'}",
        f"📍 Регионы (соседние): {', '.join(regions.adjacent) or '—'}",
        f"🚫 Регионы (стоп): {', '.join(regions.stop) or '—'}",
        f"💰 Зарплата: {salary_str}",
        f"✅ Обязательные требования: {', '.join(portrait.must_have_keywords) or '—'}",
        f"⭐ Желательные требования: {', '.join(portrait.nice_to_have_keywords) or '—'}",
        f"🛑 Стоп-слова: {', '.join(portrait.stop_words) or '—'}",
        f"🚫 Запретные индустрии: {', '.join(portrait.forbidden_industries) or '—'}",
        f"📊 Опыт (страхование): {portrait.min_insurance_experience_months} мес.",
        f"📊 Опыт (моторное): {portrait.min_motor_experience_months} мес.",
        f"🎓 Высшее обязательно: {'да' if portrait.higher_education_required else 'нет'}",
    ]
    pf_parts: list[str] = []
    stop_ids, _ = resolve_region_names(portrait.filters.regions.stop)
    if stop_ids:
        pf_parts.append(f"стоп-регионов: {len(stop_ids)}")
    if portrait.forbidden_industry_mode == "hard" and portrait.forbidden_industries:
        names_preview = ", ".join(portrait.forbidden_industries[:3])
        suffix = "…" if len(portrait.forbidden_industries) > 3 else ""
        pf_parts.append(f"инд.: {names_preview}{suffix}")
    if pf_parts:
        lines.append(f"🔒 Префильтр: {'; '.join(pf_parts)}")
    if unknown:
        lines.append(f"\n⚠️ Не распознаны: {', '.join(unknown)}")
        lines.append("Исправь портрет или запусти поиск без фильтра региона.")
    if enrichment_recs:
        lines.append(f"\n{enrichment_recs}")
    elif gaps := compute_gaps(portrait, is_insurance=is_insurance):
        lines.append("\nЧто я мог не дозаполнить:")
        lines.extend(f"  • {g}" for g in gaps)
    lines.append("\nВсё верно?")
    return "\n".join(lines)


# ── S4: review ───────────────────────────────────────────────────────────────────


@add_vacancy_router.callback_query(StateFilter(AddVacancy.S4_review), F.data == "av:review:ok")
async def handle_s4_ok(callback: CallbackQuery, state: FSMContext) -> None:
    if not _guard_callback(callback):
        await callback.answer()
        return
    await callback.answer()
    if isinstance(callback.message, Message):
        msg = callback.message
        data = await state.get_data()
        portrait = Portrait.model_validate(data["portrait_dict"])
        position_name = data["position_name"]
        try:
            critic = await draft_critic_prompt(portrait, position_name)
        except Exception as exc:
            logger.warning("add_vacancy.critic_failed", error=str(exc))
            critic = build_deterministic_fallback(portrait, position_name)
        await state.update_data(llm_critic_prompt=critic)
        await _enter_launch(msg, state)


@add_vacancy_router.callback_query(StateFilter(AddVacancy.S4_review), F.data == "av:review:more")
async def handle_s4_more(callback: CallbackQuery, state: FSMContext) -> None:
    if not _guard_callback(callback):
        await callback.answer()
        return
    await callback.answer()
    await state.update_data(visited_review=True, input_mode="text")
    await state.set_state(AddVacancy.S3_portrait_raw)
    if isinstance(callback.message, Message):
        await callback.message.answer("Что добавить или уточнить?", reply_markup=kb.kb_cancel())


async def _enter_launch(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    portrait = Portrait.model_validate(data["portrait_dict"])
    salary = portrait.filters.salary_range
    summary = "\n".join(
        [
            "<b>Готово к запуску:</b>\n",
            f"📌 Позиция: {data['position_name']}",
            f"🔍 Синонимов: {len(portrait.position_synonyms)}",
            f"📍 Целевых регионов: {len(portrait.filters.regions.primary)}",
            f"✅ Обязательных требований: {len(portrait.must_have_keywords)}",
            f"🚫 Запретных индустрий: {len(portrait.forbidden_industries)}",
            f"💰 Зарплата: {f'{salary[0]}–{salary[1]} ₽' if salary else 'не указана'}",
            "🔢 Первичный скан: max_pages=2",
            "⏳ Следующий cron-слот эту вакансию пропустит на 30 минут.",
        ]
    )
    await state.set_state(AddVacancy.S6_launch)
    await message.answer(summary, reply_markup=kb.kb_launch())


# ── S6: launch / draft ───────────────────────────────────────────────────────────


async def _insert_search(data: dict[str, Any], *, active: bool, tg_user_id: int | None) -> str:
    portrait_dict = data["portrait_dict"]
    portrait = Portrait.model_validate(portrait_dict)
    portrait_dict["prefilter"] = derive_prefilter(portrait).model_dump()
    portrait = Portrait.model_validate(portrait_dict)
    base = slugify(data["position_name"])
    factory = get_session_factory()
    async with factory() as session:
        search_code = await next_unique_search_code(session, base)
        await session.execute(
            insert(Search).values(
                search_code=search_code,
                position_code=base,
                position_name=data["position_name"],
                hh_params=derive_initial_hh_params(portrait),
                portrait=portrait_dict,
                llm_critic_prompt=data.get("llm_critic_prompt", ""),
                active=active,
                created_by_tg_user_id=tg_user_id,
            )
        )
        await session.commit()
    return search_code


@add_vacancy_router.callback_query(StateFilter(AddVacancy.S6_launch), F.data == "av:launch:go")
async def handle_s6_launch(callback: CallbackQuery, state: FSMContext) -> None:
    if not _guard_callback(callback):
        await callback.answer()
        return
    await callback.answer()
    data = await state.get_data()
    tg_user_id = callback.from_user.id if callback.from_user else None
    search_code = await _insert_search(data, active=True, tg_user_id=tg_user_id)
    if isinstance(callback.message, Message):
        await callback.message.answer(
            "✅ Вакансия добавлена. Запускаю первичный скан (max_pages=2)…"
        )
    from hh_monitor.tg.add_vacancy.launcher import _run_initial_scan

    task = asyncio.create_task(_run_initial_scan(search_code, tg_user_id or 0))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    await state.clear()


@add_vacancy_router.callback_query(StateFilter(AddVacancy.S6_launch), F.data == "av:launch:draft")
async def handle_s6_draft(callback: CallbackQuery, state: FSMContext) -> None:
    if not _guard_callback(callback):
        await callback.answer()
        return
    await callback.answer()
    data = await state.get_data()
    tg_user_id = callback.from_user.id if callback.from_user else None
    await _insert_search(data, active=False, tg_user_id=tg_user_id)
    if isinstance(callback.message, Message):
        await callback.message.answer(
            "💾 Сохранено как черновик. Запустить можно позже из /active."
        )
    await state.clear()
