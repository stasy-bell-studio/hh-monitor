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
    red_flags_raw: list[str] | str | None = resume.llm_red_flags or event.llm_red_flags

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

    # ── Facts block ───────────────────────────────────────────────────────────
    if verdict:
        verdict_line = safe(verdict)
        if real_role:
            verdict_line += f" — {safe(real_role)}"
        lines.append(f"<b>Вердикт:</b> {verdict_line}")

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
        lines.append(
            f"<b>Регион · возраст · опыт · образование:</b> {' · '.join(geo_parts)}"
        )

    if snap.get("salary"):
        salary_fmt = f"{int(snap['salary']):,} ₽".replace(",", " ")
        lines.append(f"<b>ЗП:</b> {salary_fmt}")

    # ── Risks ─────────────────────────────────────────────────────────────────
    if red_flags_raw:
        if isinstance(red_flags_raw, list):
            flags_text = ", ".join(safe(f) for f in red_flags_raw if f)
        else:
            flags_text = safe(red_flags_raw)
        if flags_text:
            lines.append(f"⚠️ <b>Риски:</b> {flags_text}")

    # ── Comment (hidden for мимо) ─────────────────────────────────────────────
    comment = resume.llm_comment
    if comment and verdict and verdict.lower() != "мимо":
        lines.append(f"<i>{safe(comment)}</i>")

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
            ],
        ]
    )
