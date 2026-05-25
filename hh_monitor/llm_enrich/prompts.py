"""Dossier prompt constants and helpers for the LLM enrichment pipeline.

Two-part system prompt:
  Part A — UNIVERSAL_CRITIC_PROMPT: hard-coded, role + tone + forbidden phrases + schema.
  Part B — position-specific critic lens: generated once per search via meta-prompt,
            stored in searches.llm_critic_prompt.

build_full_prompt(critic_lens) assembles the final system prompt.
parse_dossier(raw) parses the 5-field JSON response from DeepSeek.
"""

from __future__ import annotations

import contextlib
import json
import re
from typing import Any

import structlog

log = structlog.get_logger(__name__)

# ── Part A: universal critic role + rules ────────────────────────────────────

UNIVERSAL_CRITIC_PROMPT = """\
Ты — опытный HR-директор с 15+ годами найма руководящих позиций в страховании. \
Нанимаешь в свой бизнес, своими деньгами платишь зарплату. \
Твоя задача — найти слабое, а не похвалить.

Тон: без вежливости, без маркетинговых формулировок, без обтекаемых оценок. \
Прямые формулировки. Если кандидат слабый — пишешь прямо. \
Если сильный — обосновываешь конкретными цифрами и фактами из резюме.

Правило подтверждения: каждое утверждение либо ссылается на конкретный факт из \
резюме (цитата/дата/цифра), либо явно помечается «не подтверждено».

Запрещённые фразы (анти-паттерны): «опытный руководитель», «успешный опыт», \
«подходит по возрасту», «обладает компетенциями», «хорошо разбирается», \
«зарекомендовал себя», «эффективно управлял» — без конкретной цифры/факта рядом. \
Если использовал такую фразу без подтверждения — самопроверка перед выводом, замени или удали.

Структура ответа: ровно JSON с 5 полями:
  "facts_confirmed" — что подтверждено фактами из резюме (даты, должности, цифры, география).
  "weak_spots" — слабые места и что не подтверждено (пробелы, размытые формулировки, отсутствие KPI/P&L/штата/цифр).
  "red_flags" — красные флаги и несостыковки (сроки <1.5 года, понижения, разрывы, скачки индустрий).
  "interview_questions" — массив из 3–5 точечных вопросов на разрыв красных флагов.
  "verdict" — прямой вердикт. Внутри — гипотеза мотивации перехода одной строкой + итог: рекомендую / не рекомендую / нужно интервью с проверкой.

Никакого предисловия, никакой обёртки в ```json блок, никаких пояснений после JSON.
Объём: 200–350 слов суммарно по всем полям."""

# Phrases that the prompt explicitly forbids — checked post-hoc for monitoring
_FORBIDDEN_PHRASES = [
    "опытный руководитель",
    "успешный опыт",
    "подходит по возрасту",
    "обладает компетенциями",
    "хорошо разбирается",
    "зарекомендовал себя",
    "эффективно управлял",
]


def build_full_prompt(critic_lens: str) -> str:
    """Assemble the final system prompt: Part A + Part B.

    If *critic_lens* is empty, returns Part A only (with a logged warning).
    """
    if not critic_lens.strip():
        log.warning("llm_enrich.empty_critic_lens")
        return UNIVERSAL_CRITIC_PROMPT
    return f"{UNIVERSAL_CRITIC_PROMPT}\n\nСПЕЦИФИКА ДАННОЙ ВАКАНСИИ:\n{critic_lens}"


# ── Dossier response parser ──────────────────────────────────────────────────

_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)
_NUMBERED_RE = re.compile(r"\d+\.\s+")


def _split_numbered_list(text: str) -> list[str]:
    """Split '1. Q1 2. Q2 3. Q3' → ['Q1', 'Q2', 'Q3']."""
    parts = _NUMBERED_RE.split(text)
    return [p.strip() for p in parts if p.strip()]


def parse_dossier(raw: str) -> dict[str, Any]:
    """Parse 5-field dossier JSON from LLM response.

    Returns a dict with keys: facts_confirmed, weak_spots, red_flags,
    interview_questions (list[str]), verdict.

    On JSONDecodeError: returns {"verdict": raw_text, ...rest None}.
    On missing fields: populates None for each missing key.
    On interview_questions as string: splits by numbered markers.
    """
    raw = raw.strip()
    data: dict[str, Any] | None = None

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        m = _JSON_BLOCK_RE.search(raw)
        if m:
            with contextlib.suppress(json.JSONDecodeError):
                data = json.loads(m.group(0))

    if data is None:
        log.warning("llm_enrich.dossier_json_decode_error", raw_preview=raw[:200])
        return {
            "facts_confirmed": None,
            "weak_spots": None,
            "red_flags": None,
            "interview_questions": None,
            "verdict": raw,
        }

    # Normalise interview_questions: accept string → split, or list
    iq = data.get("interview_questions")
    if isinstance(iq, str):
        iq = _split_numbered_list(iq) or [iq]
        data["interview_questions"] = iq
    elif not isinstance(iq, list):
        data["interview_questions"] = None

    # Ensure all 5 keys exist (fill missing with None)
    for key in ("facts_confirmed", "weak_spots", "red_flags", "interview_questions", "verdict"):
        data.setdefault(key, None)

    return data


def check_forbidden_phrases(text: str, resume_id: str) -> None:
    """Log a warning if the dossier text contains any forbidden marketing phrases."""
    found = [p for p in _FORBIDDEN_PHRASES if p in text.lower()]
    if found:
        log.warning(
            "llm_enrich.forbidden_phrases_detected",
            resume_id=resume_id,
            phrases=found,
        )


# ── Score/verdict derivation ─────────────────────────────────────────────────
# Maps free-form dossier verdict text → numeric llm_score + structured class.
# Used to keep resumes.llm_score / resumes.llm_verdict populated for TG bot.


def derive_score_from_verdict(verdict: str) -> int:
    """Heuristically derive a numeric LLM score from free-form verdict text."""
    v = verdict.lower()
    if "не рекомендую" in v:
        return 20
    if "нужно интервью" in v or "нужна проверка" in v:
        return 60
    if "рекомендую" in v:
        return 80
    return 50


def derive_verdict_class(verdict: str) -> str:
    """Map free-form verdict text → 'подходит' | 'спорно' | 'мимо'."""
    v = verdict.lower()
    if "не рекомендую" in v:
        return "мимо"
    if "нужно интервью" in v or "нужна проверка" in v:
        return "спорно"
    if "рекомендую" in v:
        return "подходит"
    return "спорно"
