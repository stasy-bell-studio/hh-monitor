from __future__ import annotations

import html
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from hh_monitor.db.models import Event, Resume, Search


def safe(value: Any, default: str = "") -> str:  # noqa: ANN401
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
    resume_url = f"https://hh.ru/resume/{resume.hh_resume_id}"

    verdict = resume.llm_verdict or event.llm_verdict
    real_role = resume.llm_real_role
    red_flags_raw: list[str] | str | None = resume.llm_red_flags or event.llm_red_flags

    lines: list[str] = []

    lines.append(f"<b>Должность:</b> {safe(search.position_name)}")

    score_total = resume.score_total
    fit = resume.fit_score
    llm_s = resume.llm_score
    score_parts = []
    if fit is not None:
        score_parts.append(f"fit {fit}")
    if llm_s is not None:
        score_parts.append(f"LLM {llm_s}")
    score_str = f"{score_total}/100"
    if score_parts:
        score_str += f" ({', '.join(score_parts)})"
    lines.append(f"<b>Score:</b> {score_str}")

    if verdict:
        verdict_line = safe(verdict)
        if real_role:
            verdict_line += f" — {safe(real_role)}"
        lines.append(f"<b>Вердикт:</b> {verdict_line}")

    geo_parts = []
    if snap.get("region"):
        geo_parts.append(safe(snap["region"]))
    if snap.get("age"):
        geo_parts.append(f"{safe(snap['age'])} лет")
    if snap.get("experience_years"):
        geo_parts.append(f"опыт {safe(snap['experience_years'])} лет")
    if geo_parts:
        lines.append(f"<b>Регион / возраст / опыт:</b> {' / '.join(geo_parts)}")

    if snap.get("salary"):
        lines.append(f"<b>ЗП:</b> {safe(snap['salary'])} ₽")

    if snap.get("education"):
        lines.append(f"<b>Образование:</b> {safe(snap['education'])}")

    if red_flags_raw:
        if isinstance(red_flags_raw, list):
            flags_text = ", ".join(safe(f) for f in red_flags_raw if f)
        else:
            flags_text = safe(red_flags_raw)
        if flags_text:
            lines.append(f"⚠️ <b>Red flags:</b> {flags_text}")

    comment = resume.llm_comment
    if comment and verdict and verdict.lower() != "мимо":
        lines.append(f"<i>{safe(comment)}</i>")

    lines.append(f'<a href="{html.escape(resume_url)}">Открыть на hh.ru</a>')

    return "\n".join(lines)


def build_inline_keyboard(event_id: int, resume_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Подходит",
                    callback_data=f"screen:{event_id}:approve",
                ),
                InlineKeyboardButton(
                    text="❌ Не подходит",
                    callback_data=f"screen:{event_id}:reject",
                ),
                InlineKeyboardButton(
                    text="🤔 Спорно",
                    callback_data=f"screen:{event_id}:doubt",
                ),
            ],
            [
                InlineKeyboardButton(text="🔗 hh.ru", url=resume_url),
            ],
        ]
    )
