"""Dossier prompt constants and helpers for the LLM enrichment pipeline.

Two-part system prompt:
  Part A — UNIVERSAL_CRITIC_PROMPT: hard-coded, role + tone + forbidden phrases + schema.
  Part B — position-specific critic lens: generated once per search via meta-prompt,
            stored in searches.llm_critic_prompt.

build_full_prompt(critic_lens) assembles the final system prompt.
parse_dossier(raw) parses the 8-field JSON response from DeepSeek (v3 schema).

v3 schema adds two machine-readable fields to the 6 original:
  score        — integer 0-100, explicit numeric rating.
  verdict_class — one of "подходит" | "спорно" | "мимо" | "стоп-сигнал".
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

Структура ответа: ровно JSON с 9 полями:
  "real_role" — одной строкой реальная роль кандидата по совокупности опыта (должности, цифры, P&L, штат, число подчинённых), а не по заголовку резюме. Без воды, ≤120 символов.
  "facts_confirmed" — что подтверждено фактами из резюме (даты, должности, цифры, география).
  "weak_spots" — слабые места и что не подтверждено (пробелы, размытые формулировки, отсутствие KPI/P&L/штата/цифр).
  "red_flags" — красные флаги и несостыковки (сроки <1.5 года, понижения, разрывы, скачки индустрий).
  "interview_questions" — массив строк из 3–5 точечных вопросов на разрыв красных флагов.
  "verdict" — прямой вердикт. Внутри — гипотеза мотивации перехода одной строкой + итог: рекомендую / не рекомендую / нужно интервью с проверкой.
  "score" — целое число от 0 до 100: числовая оценка кандидата. 0–29 = мимо, 30–59 = спорно, 60–79 = нужно интервью, 80–100 = рекомендую. Обязательное поле.
  "verdict_class" — ровно одно значение из: "подходит", "спорно", "мимо", "стоп-сигнал". Обязательное поле.
  "insurance_domain" — ровно одно значение из: "yes", "partial", "no". Обязательное поле.
    "yes"     — кандидат работал в страховании: компания-страховщик или профильные продажи ОСАГО/КАСКО/ДМС/ИФЛ/андеррайтинг.
    "partial" — косвенный страховой опыт: автоматизация страховых процессов, банковский кросс-сейл без профильной страховой роли, разовые страховые задачи.
    "no"      — страхового опыта нет вообще.
  Правило: лизинговый менеджер «автоматизировавший страхование» → "partial". Банкир без страховых ролей → "no". Сотрудник страховщика с ОСАГО/КАСКО/ДМС → "yes".

Никакого предисловия, никакой обёртки в ```json блок, никаких пояснений после JSON.
Объём: 200–400 слов суммарно по всем полям."""

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
    """Parse 6-field dossier JSON from LLM response.

    Returns a dict with keys: real_role (str), facts_confirmed, weak_spots,
    red_flags, interview_questions (list[str]), verdict.

    On JSONDecodeError: returns {"verdict": raw_text, "real_role": "", ...rest None}.
    On missing fields: real_role defaults to ""; other fields default to None.
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
        log.warning("llm_enrich.dossier_json_decode_error")
        log.debug("llm_enrich.dossier_json_decode_error.detail", raw_preview=raw[:200])
        return {
            "real_role": "",
            "facts_confirmed": None,
            "weak_spots": None,
            "red_flags": None,
            "interview_questions": None,
            "verdict": raw,
            "insurance_domain": "partial",
        }

    # Normalise interview_questions: accept string → split, list → flatten nested, else None.
    iq = data.get("interview_questions")
    if isinstance(iq, str):
        iq = _split_numbered_list(iq) or [iq]
    elif isinstance(iq, list):
        flat_iq: list[str] = []
        warned_iq = False
        for item in iq:
            if isinstance(item, str):
                flat_iq.append(item)
            else:
                if not warned_iq:
                    log.warning(
                        "llm_enrich.interview_questions_nested",
                        item_type=type(item).__name__,
                    )
                    warned_iq = True
                if isinstance(item, list):
                    flat_iq.extend(str(x) for x in item)
                else:
                    flat_iq.append(str(item))
        iq = flat_iq or None
    else:
        iq = None
    data["interview_questions"] = iq

    # Ensure all 6 core keys exist (real_role → "", others → None).
    # score / verdict_class — kept as-is if LLM included them.
    for key in ("facts_confirmed", "weak_spots", "red_flags", "interview_questions", "verdict"):
        data.setdefault(key, None)
    data.setdefault("real_role", "")
    if not isinstance(data.get("real_role"), str):
        data["real_role"] = ""

    # 9th field: insurance_domain — fail-safe default "partial" (keeps governor active).
    # "yes" is NOT the safe default: it would disable the governor for unrecognised responses.
    _raw_id = data.get("insurance_domain")
    if isinstance(_raw_id, str) and _raw_id in {"yes", "partial", "no"}:
        data["insurance_domain"] = _raw_id
    else:
        data["insurance_domain"] = "partial"

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


# Keywords that indicate a recognised verdict pattern (used to detect "truly unrecognised")
_VERDICT_KEYWORDS = (
    "стоп-сигнал",
    "стоп сигнал",
    "мимо",
    "не рекомендую",
    "нужно интервью",
    "нужна проверка",
    "спорно",
    "рекомендую",
    "подходит",
)
_CLASS_TO_SCORE: dict[str, int] = {
    "мимо": 20,
    "спорно": 50,
    "подходит": 80,
    "стоп-сигнал": 0,
}


def extract_llm_score(dossier: dict[str, Any], resume_id: str) -> int:
    """Extract llm_score from a parsed dossier dict.

    Priority:
    1. Integer ``score`` field (0–100), clamped.
    2. ``verdict_class`` string field (if LLM included it).
    3. Free-form ``verdict`` text → derive_verdict_class → map to int.
    4. Unrecognised text → 0 + warning.
    """
    raw_score = dossier.get("score")
    if raw_score is not None and not isinstance(raw_score, bool):
        try:
            return max(0, min(100, int(raw_score)))
        except (ValueError, TypeError):
            pass

    # Resolve the text to classify: prefer dossier.verdict_class, then verdict
    vc_field = dossier.get("verdict_class")
    if isinstance(vc_field, str):
        verdict_text = vc_field
    else:
        v = dossier.get("verdict")
        if isinstance(v, list):
            verdict_text = " ".join(str(x) for x in v)
        elif isinstance(v, str):
            verdict_text = v
        else:
            verdict_text = ""

    vl = verdict_text.lower()
    if not any(kw in vl for kw in _VERDICT_KEYWORDS):
        log.warning("llm_enrich.score_parse_fallback", resume_id=resume_id)
        log.debug("llm_enrich.score_parse_fallback.detail", verdict_preview=verdict_text[:100])
        return 0

    verdict_class = derive_verdict_class(verdict_text)
    return _CLASS_TO_SCORE.get(verdict_class, 0)


def derive_verdict_class(verdict: str) -> str:
    """Map free-form verdict text → 'подходит' | 'спорно' | 'мимо' | 'стоп-сигнал'."""
    v = verdict.lower()
    if "стоп-сигнал" in v or "стоп сигнал" in v:
        return "стоп-сигнал"
    if "не рекомендую" in v or "мимо" in v:
        return "мимо"
    if "нужно интервью" in v or "нужна проверка" in v or "спорно" in v:
        return "спорно"
    if "рекомендую" in v or "подходит" in v:
        return "подходит"
    return "мимо"
