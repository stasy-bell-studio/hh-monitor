"""Generate a position-specific critic lens via DeepSeek meta-prompt.

The critic lens is a 200–500 word text with 3 sections describing what to
look for, red flags, and common embellishments for a specific role.  Generated
once per search and stored in searches.llm_critic_prompt.
"""

from __future__ import annotations

import json
from typing import Any

import structlog

from hh_monitor.db.models import Search
from hh_monitor.fit.portrait import Portrait
from hh_monitor.llm_enrich import client as llm_client

log = structlog.get_logger(__name__)

_META_PROMPT_TEMPLATE = """\
Ты — HR-эксперт. Тебе дан портрет вакансии:
- Название роли: {position_name}
- Position code: {position_code}
- Ключевые требования (выдержка из портрета):
{portrait_summary}

Составь короткую (200–500 слов) критическую линзу для оценки кандидатов на эту роль. \
Линза должна содержать ровно 3 секции:

1. ЧТО ВЫИСКИВАТЬ — какие компетенции, цифры, конкретный опыт критически важны именно \
для этой роли. Не общие слова, а проверяемые вещи. Опирайся на портрет выше.

2. КРАСНЫЕ ФЛАГИ ПОД ЭТУ РОЛЬ — типичные слабые паттерны кандидатов именно на этой \
позиции (примеры: «опыт только в госсекторе», «короткие сроки <1.5 года на последнем месте», \
«нет конкретных цифр в достижениях»). Опирайся на специфику роли из портрета.

3. ГДЕ ОБЫЧНО ВРУТ НА ЭТОЙ РОЛИ — типичные приписки и инфляция метрик (примеры: \
«приписывают размер команды», «выдают участие за руководство», \
«инфлируют долю рынка филиала»).

Возврати только текст линзы, без обёрток и предисловий.\
"""

# Appended to the meta-prompt when the user (HR) asks to rewrite the lens.
_FEEDBACK_TEMPLATE = """\


Учти пожелания HR при пересборке промпта:
{user_feedback}\
"""


def _build_portrait_summary(portrait: dict[str, Any]) -> str:
    """Summarise relevant portrait fields for the meta-prompt."""
    if not isinstance(portrait, dict):
        return "(не указано)"
    skip = {"position_code", "position_name"}
    parts: list[str] = []
    for k, v in portrait.items():
        if k in skip or not v:
            continue
        v_str = json.dumps(v, ensure_ascii=False) if isinstance(v, dict | list) else str(v)
        if len(v_str) > 200:
            v_str = v_str[:197] + "..."
        parts.append(f"  {k}: {v_str}")
    return "\n".join(parts) if parts else "(не указано)"


def build_deterministic_fallback(portrait: Portrait, position_name: str) -> str:
    """Build a non-empty critic lens from portrait fields without LLM.

    Used when the LLM call fails or returns empty/whitespace.  Guaranteed
    non-empty so searches.llm_critic_prompt is never blank.
    """
    parts: list[str] = [f"Критерии оценки для роли «{position_name}»:"]
    if portrait.evaluation_focus:
        parts.append(
            "ЧТО ВЫИСКИВАТЬ\n" + "\n".join(f"• {c}" for c in portrait.evaluation_focus)
        )
    if portrait.must_have_keywords:
        parts.append("ОБЯЗАТЕЛЬНЫЕ ТРЕБОВАНИЯ\n" + ", ".join(portrait.must_have_keywords))
    if portrait.forbidden_industries:
        parts.append("ЗАПРЕТНЫЕ ИНДУСТРИИ\n" + ", ".join(portrait.forbidden_industries))
    if portrait.filters.regions.primary:
        parts.append("РЕГИОНЫ\n" + ", ".join(portrait.filters.regions.primary))
    if len(parts) == 1:
        parts.append("Требования не заполнены — уточни портрет вакансии.")
    return "\n\n".join(parts)


async def _build_critic_lens_core(
    *,
    portrait: Portrait,
    position_name: str,
    position_code: str,
    search_code: str | None,
    user_feedback: str | None,
) -> str:
    """Build and run the critic-lens meta-prompt from plain arguments.

    Receives primitives only — no Search dependency — so it can be reused by the
    FSM "Add Vacancy" wizard before a Search row exists.  Portrait summary mirrors
    the sparse jsonb that was historically stored in ``searches.portrait`` by
    using ``model_dump(exclude_defaults=True, exclude_none=True)``.

    Never raises and never returns empty: LLM exceptions and empty/whitespace
    results both fall back to a deterministic lens built from portrait fields.
    """
    portrait_dict = portrait.model_dump(mode="json", exclude_defaults=True, exclude_none=True)
    portrait_summary = _build_portrait_summary(portrait_dict)
    prompt = _META_PROMPT_TEMPLATE.format(
        position_name=position_name,
        position_code=position_code,
        portrait_summary=portrait_summary,
    )
    if user_feedback:
        prompt += _FEEDBACK_TEMPLATE.format(user_feedback=user_feedback)

    log.info(
        "critic_lens.generating",
        search_code=search_code,
        position_code=position_code,
        with_feedback=bool(user_feedback),
    )

    try:
        raw = await llm_client.chat_completion_messages(
            [{"role": "user", "content": prompt}],
            max_tokens=8192,
            temperature=0.3,
        )
        text = llm_client.extract_text(raw)
    except Exception as exc:
        log.warning(
            "critic_lens.fallback_used",
            reason="exception",
            position_code=position_code,
            error=str(exc),
        )
        return build_deterministic_fallback(portrait, position_name)

    if not text.strip():
        log.warning(
            "critic_lens.fallback_used",
            reason="empty",
            position_code=position_code,
        )
        return build_deterministic_fallback(portrait, position_name)

    log.info(
        "critic_lens.generated",
        search_code=search_code,
        length=len(text),
    )
    return text


async def generate_critic_lens(search: Search) -> str:
    """Generate a position-specific critic lens for an existing *search* row.

    Thin passthrough into :func:`_build_critic_lens_core`; preserves the public
    signature used by ``llm_enrich/run.py`` and ``cli.py``.
    """
    return await _build_critic_lens_core(
        portrait=Portrait.model_validate(search.portrait),
        position_name=search.position_name,
        position_code=search.position_code,
        search_code=search.search_code,
        user_feedback=None,
    )


async def generate_critic_lens_from_portrait(
    portrait: Portrait,
    *,
    position_name: str,
    position_code: str,
    search_code: str | None = None,
    user_feedback: str | None = None,
) -> str:
    """Generate a critic lens from a Portrait without an existing Search row.

    Used by the FSM "Add Vacancy" wizard.  ``user_feedback`` appends an HR
    instruction section to the meta-prompt for a rewrite path.
    """
    return await _build_critic_lens_core(
        portrait=portrait,
        position_name=position_name,
        position_code=position_code,
        search_code=search_code,
        user_feedback=user_feedback,
    )
