"""Prompt assembly and LLM response schema for resume enrichment.

Architecture:
  - SYSTEM_PROMPT: constant — insurance HR senior partner persona.
  - build_system_prompt(global_ctx): appends market context from _global.yaml.
  - prompt_template.j2 (user message): position + candidate resume block.
  - build_messages(): assembles [system, user] list for the LLM API.
  - LlmResponse: Pydantic schema for the structured JSON response.
  - parse_response(): parse raw LLM text → LlmResponse.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

import jinja2
from pydantic import BaseModel, Field, field_validator

from hh_monitor.fit.portrait import GlobalContext, Portrait

# ── Paths ─────────────────────────────────────────────────────────────────────

_TEMPLATE_PATH = Path(__file__).parent.parent.parent / "config" / "portraits" / "prompt_template.j2"

_jinja_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(_TEMPLATE_PATH.parent)),
    autoescape=False,
    trim_blocks=True,
    lstrip_blocks=True,
    undefined=jinja2.StrictUndefined,
)

# ── System prompt (constant) ──────────────────────────────────────────────────

SYSTEM_PROMPT = """Ты — senior HR-партнёр со специализацией на страховом рынке РФ (15+ лет executive search в B2C-страховании). Твоя задача — оценить соответствие резюме кандидата идеальному портрету для конкретной позиции в страховой компании среднего размера.

Ключевые принципы оценки:

1. ОЦЕНИВАЙ ПО СУЩЕСТВУ, не по красивым словам.
   НЕТ: "Управлял агентской сетью" без цифр = слабо.
   ДА:  "Развил сеть с 30 до 120 агентов за 2 года, премия +180%" = сильно.

2. УЧИТЫВАЙ КОНКУРЕНТНЫЕ СИГНАЛЫ.
   Переход из целевых insurance-компаний (см. блок ниже в user-message) — нейтрально-позитивный сигнал.
   Переход из банка / автодилера / лизинга / МФО — серьёзный red flag для большинства insurance-позиций (другая ментальность продаж и работы).

3. ПОНИМАЙ, ЧТО РЕАЛЬНО СТОИТ ЗА ДОЛЖНОСТЬЮ.
   "Директор филиала" бывает разный: реальный руководитель регионального бизнеса (десятки агентов, P&L, KPI выручки) или продажник с понтовой подписью (3 человека в подчинении, продаёт сам). Всегда выводи real_role отдельно от заявленного job title.

4. RED FLAGS — ИЗ ФАКТОВ, НЕ ИЗ ПОДОЗРЕНИЙ.
   ДА:  Частая смена работы без роста, перерывы без объяснения, понижение в должности, регулярная смена отрасли, работа в стоп-компании, gap >12 мес без объяснения.
   НЕТ: "Не нравится формулировка", "странное фото", "слишком молодой".

5. БУДЬ КОНКРЕТЕН В COMMENT.
   HR прочитает 2-3 предложения в TG-карточке и должен сразу понять, стоит ли смотреть глубже. Никаких "перспективный кандидат с хорошим опытом". Только конкретика по фактам резюме.

6. ЕСЛИ в user-message блок "ФОКУСНЫЕ ВОПРОСЫ" заполнен — оцени именно по этим критериям. Если блок пуст и стоит инструкция "КРИТЕРИИ ОЦЕНКИ" — выведи 4-6 ключевых критериев самостоятельно из ОПИСАНИЯ РОЛИ.

Output: ТОЛЬКО валидный JSON по схеме из user-message. Без markdown-блоков. Без префиксов "Вот мой ответ:". Только JSON, ничего кроме."""


def build_system_prompt(global_ctx: GlobalContext) -> str:
    """Return system prompt, appending market context if present."""
    if not global_ctx.market_context:
        return SYSTEM_PROMPT
    return f"{SYSTEM_PROMPT}\n\n# Контекст рынка\n\n{global_ctx.market_context}"


# ── Response schema ───────────────────────────────────────────────────────────

VALID_VERDICTS = frozenset({"подходит", "спорно", "мимо", "стоп-сигнал"})

VerdictLiteral = Literal["подходит", "спорно", "мимо", "стоп-сигнал"]


class LlmResponse(BaseModel):
    """Parsed and validated LLM response for a single resume.

    The new prompt schema uses Russian verdicts and adds match_breakdown.
    Legacy English verdicts (strong_yes/yes/maybe/no/strong_no) from v1 cache
    entries are accepted via the coercion validator for backward compat.
    """

    score: int = Field(ge=0, le=100, alias="llm_score", default=0)
    verdict: str = Field(alias="llm_verdict", default="мимо")
    real_role: str = Field(alias="llm_real_role", default="")
    match_breakdown: dict[str, int] = Field(default_factory=dict)
    red_flags: list[str] = Field(alias="llm_red_flags", default_factory=list)
    comment: str = Field(alias="llm_comment", default="")

    model_config = {"populate_by_name": True}

    # ── Convenience properties (backward compat aliases) ──────────────────
    @property
    def llm_score(self) -> int:
        return self.score

    @property
    def llm_verdict(self) -> str:
        return self.verdict

    @property
    def llm_comment(self) -> str:
        return self.comment

    @property
    def llm_red_flags(self) -> list[str]:
        return self.red_flags

    @property
    def llm_real_role(self) -> str:
        return self.real_role

    @field_validator("score", mode="before")
    @classmethod
    def _coerce_score(cls, v: Any) -> int:
        """Accept numeric strings and floats; clamp to [0, 100]."""
        return max(0, min(100, int(float(v))))

    @field_validator("verdict", mode="before")
    @classmethod
    def _normalize_verdict(cls, v: Any) -> str:
        """Accept both Russian v2 verdicts and legacy English v1 verdicts."""
        s = str(v).strip().lower()
        # Map legacy v1 English verdicts → Russian v2
        _LEGACY_MAP = {
            "strong_yes": "подходит",
            "yes": "подходит",
            "maybe": "спорно",
            "no": "мимо",
            "strong_no": "мимо",
        }
        return _LEGACY_MAP.get(s, s)

    def model_dump_for_db(self) -> dict[str, Any]:
        """Return a dict suitable for storing in the resumes table columns."""
        return {
            "llm_score": self.score,
            "llm_verdict": self.verdict,
            "llm_comment": self.comment,
            "llm_red_flags": self.red_flags,
            "llm_real_role": self.real_role,
        }


# ── Resume payload normaliser ─────────────────────────────────────────────────

_SKIP_KEYS = frozenset({"actions", "photo", "negotiations_history"})


def _parse_experience_months(start: str | None, end: str | None) -> int:
    """Parse YYYY-MM strings → month count.  Returns 0 on any error."""
    try:
        if not isinstance(start, str):
            return 0
        sy, sm = int(start[:4]), int(start[5:7])
        if end:
            ey, em = int(end[:4]), int(end[5:7])
        else:
            from datetime import date

            today = date.today()
            ey, em = today.year, today.month
        return max(0, (ey - sy) * 12 + (em - sm))
    except (ValueError, TypeError, IndexError):
        return 0


def _normalize_resume_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Flatten raw hh.ru resume JSON into a Jinja2-friendly dict.

    Fields produced:
        hh_resume_id, title, age, area, salary, total_experience_months,
        education, experience (list), key_skills (list), about
    """
    # Salary: prefer RUR, show amount only
    salary_raw = payload.get("salary") or {}
    salary_str: str | None = None
    if isinstance(salary_raw, dict) and salary_raw.get("amount"):
        amount = salary_raw["amount"]
        currency = salary_raw.get("currency", "")
        salary_str = f"{amount:,} {currency}".strip() if currency else f"{amount:,}"

    # Education: top level → level.name
    edu_raw = (payload.get("education") or {}).get("level") or {}
    education_str: str | None = edu_raw.get("name") or None

    # Experience entries
    exp_list: list[dict[str, Any]] = []
    for e in payload.get("experience") or []:
        if not isinstance(e, dict):
            continue
        start = e.get("start")
        end = e.get("end")
        months = _parse_experience_months(start, end)
        exp_list.append(
            {
                "company": e.get("company") or e.get("employer", {}).get("name") or "",
                "position": e.get("position") or "",
                "start": start or "",
                "end": end,  # None = current
                "months": months,
                "description": e.get("description") or "",
            }
        )

    # Key skills
    skills: list[str] = []
    for s in payload.get("key_skills") or []:
        if isinstance(s, dict):
            skills.append(s.get("name") or "")
        elif isinstance(s, str):
            skills.append(s)
    skills = [s for s in skills if s]

    # Total experience months
    te_raw = payload.get("total_experience")
    if isinstance(te_raw, dict) and te_raw.get("months") is not None:
        total_exp_months: int = int(te_raw["months"])
    elif exp_list:
        total_exp_months = sum(e["months"] for e in exp_list)
    else:
        total_exp_months = 0

    return {
        "hh_resume_id": payload.get("id") or payload.get("hh_resume_id") or "",
        "title": payload.get("title") or "",
        "age": payload.get("age"),
        "area": (payload.get("area") or {}).get("name")
        if isinstance(payload.get("area"), dict)
        else payload.get("area"),
        "salary": salary_str,
        "total_experience_months": total_exp_months,
        "education": education_str,
        "experience": exp_list,
        "key_skills": skills,
        "about": payload.get("skills") or payload.get("about") or None,
    }


# ── Template renderer ─────────────────────────────────────────────────────────


def _render_user_template(
    portrait: Portrait,
    resume: dict[str, Any],
    global_ctx: GlobalContext,
) -> str:
    """Render the Jinja2 user-message template."""
    tmpl = _jinja_env.get_template(_TEMPLATE_PATH.name)
    return tmpl.render(portrait=portrait, resume=resume, global_ctx=global_ctx)


def build_messages(
    portrait: Portrait,
    resume_payload: dict[str, Any],
    global_ctx: GlobalContext,
) -> list[dict[str, str]]:
    """Assemble [system, user] messages list for LLM chat/completions.

    Args:
        portrait:       Portrait instance for the position.
        resume_payload: Raw hh.ru /resumes/{id} response dict.
        global_ctx:     Global insurance-market context from _global.yaml.

    Returns:
        List of two dicts: ``[{"role": "system", ...}, {"role": "user", ...}]``.
    """
    resume = _normalize_resume_payload(resume_payload)
    user_content = _render_user_template(portrait=portrait, resume=resume, global_ctx=global_ctx)
    return [
        {"role": "system", "content": build_system_prompt(global_ctx)},
        {"role": "user", "content": user_content},
    ]


# ── Backward-compat wrapper (used by old tests) ───────────────────────────────


def build_prompt(resume_payload: dict[str, Any], portrait: Portrait) -> str:
    """Legacy shim: return only the user-message string (no system prompt).

    New code should use build_messages() instead.
    """
    from hh_monitor.fit.portrait import GlobalContext

    resume = _normalize_resume_payload(resume_payload)
    return _render_user_template(portrait=portrait, resume=resume, global_ctx=GlobalContext())


# ── Response parser ───────────────────────────────────────────────────────────

_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


def parse_response(raw: str) -> LlmResponse:
    """Parse LLM text output into a validated LlmResponse.

    Strategy:
    1. Try to parse the whole string as JSON.
    2. If that fails, extract the first {...} block via regex and try again.
    3. Validate with Pydantic (raises ValidationError on schema mismatch).

    The JSON schema changed between v1 and v2:
    - v1 used keys: llm_score, llm_verdict, llm_comment, llm_red_flags, llm_real_role
    - v2 uses keys: score, verdict, real_role, match_breakdown, red_flags, comment

    LlmResponse accepts both via aliases (populate_by_name=True).
    """
    raw = raw.strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        m = _JSON_BLOCK_RE.search(raw)
        if not m:
            raise ValueError(f"No JSON object found in LLM response: {raw[:200]!r}") from exc
        data = json.loads(m.group(0))
    return LlmResponse.model_validate(data)
