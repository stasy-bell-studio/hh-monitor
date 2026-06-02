from __future__ import annotations

import html
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from hh_monitor.db.models import Event, Resume, Search

_VERDICT_EMOJI: dict[str, str] = {
    "подходит": "🟢",
    "спорно": "🟡",
    "мимо": "🔴",
}


def _verdict_emoji(v: str | None) -> str:
    if v is None:
        return "🔴"
    return _VERDICT_EMOJI.get(v.lower().strip(), "🔴")


def _plural_years(n: int) -> str:
    n = abs(int(n))
    if 11 <= n % 100 <= 14:
        return "лет"
    last = n % 10
    if last == 1:
        return "год"
    if 2 <= last <= 4:
        return "года"
    return "лет"


def safe(value: Any, default: str = "") -> str:
    if value is None or value == "":
        return default
    return html.escape(str(value))


def _short(text: str | None, limit: int = 140) -> str:
    """Collapse whitespace, keep the first sentence, cap at limit chars (HTML-safe)."""
    if not text:
        return ""
    collapsed = " ".join(str(text).split())
    first_end = len(collapsed)
    for i, ch in enumerate(collapsed):
        if ch in ".!?":
            first_end = i + 1
            break
    candidate = collapsed[:first_end]
    if len(candidate) > limit:
        candidate = candidate[:limit].rstrip() + "…"
    return safe(candidate)


def _red_flags_text(raw: list[str] | str | None) -> str:
    """Normalise red flags (JSONB list on Resume, Text on Event) to one string."""
    if not raw:
        return ""
    if isinstance(raw, list):
        return ", ".join(str(f) for f in raw if f)
    return str(raw)


def _extract_snapshot_fields(payload: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}

    area = payload.get("area")
    if isinstance(area, dict) and area.get("name"):
        out["region"] = str(area["name"])

    age = payload.get("age")
    if age is not None:
        out["age"] = str(age)

    exp = payload.get("total_experience")
    if isinstance(exp, dict):
        months = exp.get("months")
        if isinstance(months, int) and months > 0:
            out["experience_years"] = str(months // 12)

    salary = payload.get("salary")
    if isinstance(salary, dict) and salary.get("currency") == "RUR":
        amount = salary.get("amount")
        if amount is not None:
            out["salary"] = str(int(amount))

    education = payload.get("education")
    if isinstance(education, dict):
        level = education.get("level")
        if isinstance(level, dict) and level.get("name"):
            out["education"] = str(level["name"])

    return out


def build_card_html(
    resume: Resume,
    event: Event,
    search: Search,
    snapshot_payload: dict[str, Any] | None = None,
) -> str:
    snap = _extract_snapshot_fields(snapshot_payload) if snapshot_payload else {}

    verdict = resume.llm_verdict or event.llm_verdict
    real_role = resume.llm_real_role

    score_total = resume.score_total
    fit = resume.fit_score
    llm_s = resume.llm_score

    lines: list[str] = []

    # ── Line 1: anchor ────────────────────────────────────────────────────────
    emoji = _verdict_emoji(verdict)
    score_str = f"Рейтинг {score_total}/100" if score_total is not None else "Рейтинг —/100"
    lines.append(f"{emoji} <b>Кандидат на «{safe(search.position_name)}»</b> — {score_str}")

    # ── Line 2: secondary score breakdown ─────────────────────────────────────
    breakdown_parts: list[str] = []
    if fit is not None:
        breakdown_parts.append(f"соответствие портрету {fit}")
    if llm_s is not None:
        breakdown_parts.append(f"оценка ИИ {llm_s}")
    if breakdown_parts:
        lines.append(f"<i>{' · '.join(breakdown_parts)}</i>")

    # blank separator
    lines.append("")

    # ── Identity block ────────────────────────────────────────────────────────
    if real_role:
        lines.append(f"Реальная роль: {safe(real_role)}")

    geo_parts: list[str] = []
    if snap.get("region"):
        geo_parts.append(safe(snap["region"]))
    if snap.get("age"):
        age_n = int(snap["age"])
        geo_parts.append(f"{age_n} {_plural_years(age_n)}")
    if snap.get("experience_years"):
        exp_n = int(snap["experience_years"])
        geo_parts.append(f"опыт {exp_n} {_plural_years(exp_n)}")
    if snap.get("education"):
        geo_parts.append(safe(snap["education"]))
    if geo_parts:
        lines.append(" · ".join(geo_parts))

    if snap.get("salary"):
        salary_fmt = f"{int(snap['salary']):,} ₽".replace(",", " ")
        lines.append(f"ЗП: {salary_fmt}")

    # ── Dossier: strengths / weak spots / risks / conclusion ──────────────────
    red_text = _red_flags_text(resume.llm_red_flags or event.llm_red_flags)
    dossier: list[str] = []
    if event.llm_facts_confirmed:
        dossier.append(f"✅ Сильные стороны: {_short(event.llm_facts_confirmed)}")
    if event.llm_weak_spots:
        dossier.append(f"⚠️ Слабые места: {_short(event.llm_weak_spots)}")
    if red_text:
        dossier.append(f"🚩 Риски: {_short(red_text)}")
    if event.llm_verdict_text:
        dossier.append(f"🧭 Вывод: {_short(event.llm_verdict_text)}")

    if not dossier and resume.llm_comment:
        dossier.append(f"🧭 Вывод: {_short(resume.llm_comment)}")

    if dossier:
        if lines and lines[-1] != "":
            lines.append("")
        lines.extend(dossier)

    return "\n".join(lines).rstrip()


def build_inline_keyboard(event_id: int, resume_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Подходит",
                    callback_data=f"screen:{event_id}:approve",
                ),
                InlineKeyboardButton(
                    text="❌ Мимо",
                    callback_data=f"screen:{event_id}:reject",
                ),
                InlineKeyboardButton(
                    text="🤔 Спорно",
                    callback_data=f"screen:{event_id}:doubt",
                ),
                InlineKeyboardButton(
                    text="🚫 Стоп",
                    callback_data=f"screen:{event_id}:stop_list",
                ),
            ],
            [
                InlineKeyboardButton(text="🔗 hh.ru", url=resume_url),
                InlineKeyboardButton(
                    text="🔍 Подробнее",
                    callback_data=f"details:{event_id}",
                ),
            ],
        ]
    )


def build_detail_collapse_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🗑 Свернуть",
                    callback_data="detail_collapse",
                ),
            ],
        ]
    )


def build_detail_html(resume: Resume, event: Event, search: Search) -> str:
    """Full dossier (no truncation) shown as a separate reply on demand."""
    lines: list[str] = [
        f"🔍 <b>Подробный анализ — {safe(search.position_name)}</b>",
        "",
    ]

    sections: list[str] = []

    if event.llm_facts_confirmed:
        sections.append(f"✅ <b>Подтверждённые факты:</b>\n{safe(event.llm_facts_confirmed)}")
    if event.llm_weak_spots:
        sections.append(f"⚠️ <b>Слабые места:</b>\n{safe(event.llm_weak_spots)}")

    red_text = _red_flags_text(resume.llm_red_flags or event.llm_red_flags)
    if red_text:
        sections.append(f"🚩 <b>Риски:</b>\n{safe(red_text)}")

    questions = event.llm_interview_questions
    if questions:
        numbered = "\n".join(f"{i}. {safe(q)}" for i, q in enumerate(questions, start=1) if q)
        if numbered:
            sections.append(f"❓ <b>Вопросы на интервью:</b>\n{numbered}")

    if event.llm_verdict_text:
        sections.append(f"🧭 <b>Вердикт:</b>\n{safe(event.llm_verdict_text)}")

    if not sections:
        return f"{lines[0]}\n\nПодробных данных по этому кандидату нет (обогащено старой версией)."

    lines.append("\n\n".join(sections))
    return "\n".join(lines).rstrip()
