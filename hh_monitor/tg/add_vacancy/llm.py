"""LLM + deterministic helpers for the "Add Vacancy" FSM wizard.

- parse_to_portrait_dict: LLM extracts a Portrait-shaped dict from free HR text.
- compute_gaps:           which expected Portrait fields stayed empty/default.
- derive_initial_hh_params: minimal hh.ru params for searches.hh_params.
- draft_critic_prompt:    thin wrapper over critic_lens_builder (B2 variant b).
"""

from __future__ import annotations

import json
import re
from typing import Any

import structlog

from hh_monitor.fit.portrait import Portrait, PrefilterConfig
from hh_monitor.llm_enrich import client as llm_client
from hh_monitor.llm_enrich.critic_lens_builder import generate_critic_lens_from_portrait
from hh_monitor.regions.expander import resolve_region_names
from hh_monitor.searches.codes import slugify

log = structlog.get_logger(__name__)

# Top-level keys the LLM is allowed to populate.  Everything else (weights,
# *_override, critic_lens, search_params, legacy fields, position_code/name) is
# stripped before validation — those are set by us or filled by Pydantic defaults.
_ALLOWED_KEYS: frozenset[str] = frozenset(
    {
        "position_description",
        "evaluation_focus",
        "position_synonyms",
        "filters",
        "stop_words",
        "must_have_keywords",
        "nice_to_have_keywords",
        "min_insurance_experience_months",
        "min_motor_experience_months",
        "motor_experience_preferred",
        "min_tenure_last_job_months",
        "max_career_gap_months",
        "higher_education_required",
        "preferred_education_fields",
        "citizenship",
        "bonus_companies",
        "forbidden_industries",
        "resume_freshness_days",
        "min_total_months",
        "stop_companies_override",
        "target_companies_override",
    }
)

# Nested filters keys the LLM may populate (others dropped before validation).
_ALLOWED_FILTER_KEYS: frozenset[str] = frozenset(
    {"regions", "salary_range", "education_level"}
)

_PARSE_PROMPT = """\
Ты — HR-аналитик. Тебе дан свободный текст с описанием \
вакансии и идеального кандидата. Извлеки структурированный портрет и верни СТРОГО \
JSON-объект со следующими допустимыми ключами (любой ключ можно опустить, если в \
тексте нет данных — НЕ придумывай):

- position_description: string — связное описание роли (можно слегка причесать исходный текст)
- evaluation_focus: array[string] — 4-6 критериев оценки кандидата
- position_synonyms: array[string] — синонимы названия роли (для поиска), по приоритету
- filters: object с под-ключами:
    - regions: object { primary: array[string], adjacent: array[string], stop: array[string] }
    - salary_range: [min_rub, max_rub] или null
- stop_words: array[string] — слова, при наличии которых кандидат отсекается
- must_have_keywords: array[string] — обязательные требования
- nice_to_have_keywords: array[string] — желательные требования
- min_insurance_experience_months: int — минимум опыта в страховании в месяцах
- min_motor_experience_months: int — минимум опыта в моторных видах в месяцах
- motor_experience_preferred: bool
- min_tenure_last_job_months: int
- max_career_gap_months: int
- higher_education_required: bool
- preferred_education_fields: array[string]
- citizenship: string или null
- bonus_companies: array[string] — все компании-доноры, чьи сотрудники получают \
плюс при оценке (включай все, из которых HR хочет видеть кандидатов)
- forbidden_industries: array[string] — индустрии-стоп на последнем месте работы
- resume_freshness_days: int — давность резюме в днях. Если HR явно \
указал срок («не старше 14 дней», «свежие», «активные за месяц») — \
соответствующее число (14 / 14 / 30). Если HR не упоминает — \
ОПУСТИ ключ, не придумывай.
- min_total_months: int — минимальный общий стаж в месяцах. Если HR \
говорит «общий опыт от 5 лет» — поставь 60. Не путать с \
min_insurance_experience_months (опыт именно в страховании).
- stop_companies_override: array[string] — конкретные компании-стопы \
под эту вакансию (например: ["Капитал Лайф"]). Эти компании \
ДОБАВЯТСЯ к глобальному стоп-листу, не заменят его. Если HR не \
называл конкретных стоп-компаний — опусти.

НЕ возвращай ключи: weights, search_params, critic_lens, position_code, position_name, \
title_keywords, experience_keywords, \
preferred_total_months, min_salary, max_salary, preferred_education_levels, \
preferred_areas, age_range (на верхнем уровне).

Название позиции: __POSITION_NAME__

Текст от HR:
__RAW_TEXT__
"""


_CODE_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.DOTALL)


def _extract_json_object(text: str) -> str:
    """Strip Markdown code fences, then slice to the outermost { … }."""
    m = _CODE_FENCE_RE.search(text)
    candidate = m.group(1) if m else text
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError(
            "LLM returned no JSON object in portrait response"
            f" (got {len(text.strip())} chars of text)"
        )
    return candidate[start : end + 1]


def _drop_none_values(data: dict[str, Any]) -> dict[str, Any]:
    """Recursively remove keys whose value is None from nested dicts."""
    return {
        k: _drop_none_values(v) if isinstance(v, dict) else v
        for k, v in data.items()
        if v is not None
    }


def _strip_forbidden(data: dict[str, Any]) -> dict[str, Any]:
    """Keep only whitelisted top-level keys; sanitise nested ``filters``."""
    cleaned: dict[str, Any] = {k: v for k, v in data.items() if k in _ALLOWED_KEYS}
    filters = cleaned.get("filters")
    if isinstance(filters, dict):
        cleaned["filters"] = {k: v for k, v in filters.items() if k in _ALLOWED_FILTER_KEYS}
    return _drop_none_values(cleaned)


async def parse_to_portrait_dict(raw_text: str, position_name: str) -> dict[str, Any]:
    """Extract a Portrait-shaped dict from *raw_text* via the LLM.

    Strips forbidden keys, injects position_code (slug) + position_name, validates
    against :class:`Portrait`, and returns ``portrait.model_dump()``.

    Raises:
        ValueError / pydantic.ValidationError if the LLM output cannot be coerced
        into a valid Portrait.  The FSM handler offers retry/cancel on failure.
    """
    prompt = _PARSE_PROMPT.replace("__POSITION_NAME__", position_name).replace(
        "__RAW_TEXT__", raw_text
    )
    raw = await llm_client.chat_completion_messages(
        [{"role": "user", "content": prompt}],
        max_tokens=16000,
        temperature=0.2,
        response_json_schema={"type": "object"},
    )
    text = llm_client.extract_text(raw)
    try:
        parsed: Any = json.loads(_extract_json_object(text))
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLM returned non-JSON portrait: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"LLM portrait is not a JSON object: {type(parsed).__name__}")

    cleaned = _strip_forbidden(parsed)
    # Merge target_companies_override → bonus_companies (Bug C fix).
    # _ALLOWED_KEYS keeps target_companies_override so _strip_forbidden passes it through;
    # we consume + remove it here before validation so Portrait never sees it populated.
    tco: list[str] = cleaned.pop("target_companies_override", None) or []
    if tco:
        existing: list[str] = list(cleaned.get("bonus_companies", []))
        seen = {c.lower() for c in existing}
        for c in tco:
            if c.lower() not in seen:
                existing.append(c)
                seen.add(c.lower())
        cleaned["bonus_companies"] = existing
    cleaned["position_code"] = slugify(position_name)
    cleaned["position_name"] = position_name

    portrait = Portrait.model_validate(cleaned)
    log.info("add_vacancy.portrait_parsed", position_code=portrait.position_code)
    return portrait.model_dump(mode="json")


# Fields the wizard expects the LLM to fill, with human-readable labels for the
# S4 "Что я мог не дозаполнить" section.  Path "a.b.c" descends nested models.
_GAP_FIELDS: tuple[tuple[str, str], ...] = (
    ("position_description", "Описание позиции"),
    ("evaluation_focus", "Критерии оценки"),
    ("position_synonyms", "Синонимы роли (для поиска)"),
    ("must_have_keywords", "Обязательные требования"),
    ("nice_to_have_keywords", "Желательные требования"),
    ("stop_words", "Стоп-слова"),
    ("forbidden_industries", "Запретные индустрии"),
    ("filters.regions.primary", "Целевые регионы"),
    ("filters.salary_range", "Зарплатная вилка"),
    ("min_insurance_experience_months", "Опыт в страховании (мес.)"),
)


def _resolve_path(portrait: Portrait, path: str) -> Any:
    obj: Any = portrait
    for part in path.split("."):
        obj = getattr(obj, part, None)
        if obj is None:
            return None
    return obj


def _is_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str | list | tuple | dict):
        return len(value) == 0
    if isinstance(value, int) and not isinstance(value, bool):
        return value == 0
    return False


def compute_gaps(portrait: Portrait, *, is_insurance: bool = True) -> list[str]:
    """Human-readable labels of expected fields the LLM left empty/default.

    When ``is_insurance=False``, the insurance-experience gap field is excluded
    so non-insurance roles are not nagged about insurance experience.
    """
    fields = (
        _GAP_FIELDS
        if is_insurance
        else [f for f in _GAP_FIELDS if f[0] != "min_insurance_experience_months"]
    )
    return [label for path, label in fields if _is_empty(_resolve_path(portrait, path))]


_JSON_WRAPPER_KEYS = ("response", "text", "recommendation", "result", "answer")


def _clean_enrichment_text(raw_text: str) -> str:
    """Unwrap common LLM wrappers (```fences```, {"response": "..."}) into plain prose."""
    s = raw_text.strip()
    fence = _CODE_FENCE_RE.search(s)
    if fence:
        s = fence.group(1).strip()
    if s.startswith("{"):
        try:
            obj: Any = json.loads(s)
        except json.JSONDecodeError:
            obj = None
        if isinstance(obj, dict):
            for key in _JSON_WRAPPER_KEYS:
                val = obj.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()
            for val in obj.values():
                if isinstance(val, str) and val.strip():
                    return val.strip()
    return s


async def generate_enrichment_recs(
    portrait: Portrait, position_name: str, *, is_insurance: bool = True
) -> str:
    """LLM-written prose enrichment recommendations for the S4 review card.

    Returns "" when there are no gaps or when the LLM call fails.
    When ``is_insurance=False``, insurance-experience gaps are excluded from
    the prompt so non-insurance roles are not told to add insurance experience.
    """
    gaps = compute_gaps(portrait, is_insurance=is_insurance)
    if not gaps:
        return ""
    gaps_str = "\n".join(f"- {g}" for g in gaps)
    prompt = (
        f"Ты — ассистент HR-аналитика. Дан портрет вакансии '{position_name}'.\n"
        f"Не заполнены следующие поля:\n{gaps_str}\n\n"
        "Напиши 1-2 предложения (не список): как HR мог бы дополнить описание, "
        "чтобы поиск был точнее. Начни с «Я бы дополнил поля:». Без предисловий. "
        "Верни только обычный текст одним абзацем — без JSON, без markdown, "
        "без фигурных скобок и без кавычек-обёрток."
    )
    try:
        raw = await llm_client.chat_completion_messages(
            [{"role": "user", "content": prompt}],
            max_tokens=256,
            temperature=0.3,
        )
        return _clean_enrichment_text(llm_client.extract_text(raw))
    except Exception:
        return ""


def derive_initial_hh_params(portrait: Portrait) -> dict[str, Any]:
    """Minimal base hh_params for the searches row.

    Sets ``text`` from the position name.  When ``portrait.filters.regions.primary``
    contains recognizable region names or 21Vek macros, ``area`` is populated
    with the corresponding hh.ru area IDs.  Unresolved names are silently
    dropped here — they were already surfaced to the admin on the S4 review
    card via resolve_region_names.  The full hh.ru ``text=`` query and
    ``period=`` are assembled at parse time by build_search_params.
    """
    result: dict[str, Any] = {"text": portrait.position_name}
    primary = portrait.filters.regions.primary
    if primary:
        ids, _ = resolve_region_names(primary)
        if ids:
            result["area"] = ids
    return result


def derive_prefilter(portrait: Portrait) -> PrefilterConfig:
    """Build PrefilterConfig from already-resolved portrait data.

    area_ids_require is intentionally left empty — hh_params.area already
    enforces the region requirement server-side; avoid double filtering.
    forbidden_industry_names is populated only in hard mode; soft mode keeps
    the field as a scoring-only penalty.
    """
    stop_ids, _ = resolve_region_names(portrait.filters.regions.stop)
    forbidden = (
        portrait.forbidden_industries
        if portrait.forbidden_industry_mode == "hard"
        else []
    )
    return PrefilterConfig(
        area_ids_stop=stop_ids,
        forbidden_industry_names=forbidden,
    )


async def draft_critic_prompt(
    portrait: Portrait, position_name: str, user_feedback: str | None = None
) -> str:
    """FSM-facing wrapper: generate the critic prompt from a Portrait.

    Delegates to :func:`generate_critic_lens_from_portrait` (single source of the
    meta-prompt).  No Search row exists yet, so ``search_code`` is None and
    ``position_code`` is derived from the position name.
    """
    return await generate_critic_lens_from_portrait(
        portrait,
        position_name=position_name,
        position_code=slugify(position_name),
        search_code=None,
        user_feedback=user_feedback,
    )
