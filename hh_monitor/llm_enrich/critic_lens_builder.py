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
from hh_monitor.llm_enrich import client as llm_client

log = structlog.get_logger(__name__)

_META_PROMPT_TEMPLATE = """\
Ты — HR-эксперт по найму в страхование. Тебе дан портрет вакансии:
- Название роли: {position_name}
- Position code: {position_code}
- Ключевые требования (выдержка из портрета):
{portrait_summary}

Составь короткую (200–500 слов) критическую линзу для оценки кандидатов на эту роль. \
Линза должна содержать ровно 3 секции:

1. ЧТО ВЫИСКИВАТЬ — какие компетенции, цифры, конкретный опыт критически важны именно \
для этой роли. Не общие слова, а проверяемые вещи (примеры: «опыт ОСАГО как продукта», \
«размер агентской сети в штуках», «P&L филиала с цифрами», «знание региональной специфики Юга РФ»).

2. КРАСНЫЕ ФЛАГИ ПОД ЭТУ РОЛЬ — типичные слабые паттерны кандидатов именно на этой \
позиции (примеры: «переход из банка без страхового бэкграунда», «только КАСКО без ОСАГО», \
«опыт только в госсекторе», «короткие сроки <1.5 года в страховых»).

3. ГДЕ ОБЫЧНО ВРУТ НА ЭТОЙ РОЛИ — типичные приписки и инфляция метрик (примеры: \
«приписывают размер команды», «выдают участие за руководство», \
«инфлируют долю рынка филиала»).

Возврати только текст линзы, без обёрток и предисловий.\
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


async def generate_critic_lens(search: Search) -> str:
    """Generate a position-specific critic lens for *search* via DeepSeek.

    Calls OpenRouter with a meta-prompt built from the search's portrait and
    position metadata.  Returns the raw text (200–800 chars typically).
    """
    portrait_summary = _build_portrait_summary(search.portrait)
    prompt = _META_PROMPT_TEMPLATE.format(
        position_name=search.position_name,
        position_code=search.position_code,
        portrait_summary=portrait_summary,
    )

    log.info(
        "critic_lens.generating",
        search_code=search.search_code,
        position_code=search.position_code,
    )

    raw = await llm_client.chat_completion_messages(
        [{"role": "user", "content": prompt}],
        max_tokens=1024,
        temperature=0.3,
    )
    text = llm_client.extract_text(raw)

    log.info(
        "critic_lens.generated",
        search_code=search.search_code,
        length=len(text),
    )
    return text
