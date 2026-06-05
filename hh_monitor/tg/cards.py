from __future__ import annotations

import ast
import html
import json
import re
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


SCORE_TIER_GREEN_MIN = 60
SCORE_TIER_BEST_MIN = 76


def score_badge(score: int | None) -> str:
    if score is None:
        return "⚪"
    if score >= SCORE_TIER_BEST_MIN:
        return "🟣"
    if score >= SCORE_TIER_GREEN_MIN:
        return "🟢"
    return "🟡"


def is_best_score(score: int | None) -> bool:
    return score is not None and score >= SCORE_TIER_BEST_MIN


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


def _coerce_display(v: object) -> str:
    """Repair already-stored dossier values — same recursive logic as run._coerce_text."""
    if v is None:
        return ""
    if isinstance(v, dict):
        return "\n".join(f"{k} — {_coerce_display(val)}" for k, val in v.items())
    if isinstance(v, list | tuple):
        parts = [_coerce_display(x) for x in v]
        parts = [p for p in parts if p]
        sep = "\n" if any(len(p) > 40 or "\n" in p for p in parts) else "; "
        return sep.join(parts)
    if isinstance(v, str):
        s = v.strip()
        if s and s[0] in ("{", "["):
            for loader in (json.loads, ast.literal_eval):
                try:
                    return _coerce_display(loader(s))
                except (ValueError, SyntaxError):
                    pass
        return s
    return str(v)


def _bullets(
    text: str | None,
    *,
    max_items: int = 3,
    item_limit: int = 160,
) -> str:
    """Render multi-point dossier text as bullet lines (HTML-escaped)."""
    if not text:
        return ""
    display = _coerce_display(text)
    raw_items = re.split(r"\n|•|; ", display)
    items = [x.strip() for x in raw_items if x.strip()][:max_items]
    lines = []
    for item in items:
        if len(item) > item_limit:
            item = item[:item_limit].rsplit(" ", 1)[0] + "…"
        lines.append(f"   • {safe(item)}")
    return "\n".join(lines)


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

    real_role = resume.llm_real_role

    score_total = resume.score_total
    fit = resume.fit_score
    llm_s = resume.llm_score

    lines: list[str] = []

    # ── Line 1: anchor ────────────────────────────────────────────────────────
    score_str = f"Рейтинг {score_total}/100" if score_total is not None else "Рейтинг —/100"
    if is_best_score(score_total):
        lines.append(
            f"🟣 <b>🏆 ЛУЧШИЙ · Кандидат на «{safe(search.position_name)}»</b> — {score_str}"
        )
    else:
        lines.append(
            f"{score_badge(score_total)} <b>Кандидат на «{safe(search.position_name)}»</b>"
            f" — {score_str}"
        )

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
        dossier.append(f"✅ Сильные стороны:\n{_bullets(event.llm_facts_confirmed)}")
    if event.llm_weak_spots:
        dossier.append(f"⚠️ Слабые места:\n{_bullets(event.llm_weak_spots)}")
    if red_text:
        dossier.append(f"🚩 Риски:\n{_bullets(red_text)}")
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
