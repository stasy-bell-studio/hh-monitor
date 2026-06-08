"""Introspection-driven editable-field registry for the portrait editor.

The field list is built by walking the Pydantic models themselves
(:class:`Portrait`, :class:`Filters`, :class:`RegionFilters`, :class:`Weights`)
rather than a hand-maintained name list, so the editor can never silently drift
from the schema.  Each leaf field is classified by its declared type into an
input *kind*; the handler picks the prompt / keyboard from that kind.

Excluded from manual editing:
  position_code — identity / YAML key (read-only);
  prefilter     — re-derived on save from regions/industries;
  critic_lens   — regenerated read-only as searches.llm_critic_prompt.
"""

from __future__ import annotations

import copy
import inspect
import json
import types
from dataclasses import dataclass, field
from typing import Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel

from hh_monitor.fit.portrait import Filters, Portrait, RegionFilters, Weights

# ── Sections (menu grouping; order matters) ─────────────────────────────────────

SEC_MAIN = "main"
SEC_REGIONS = "regions"
SEC_FILTERS = "filters"
SEC_WEIGHTS = "weights"

SECTION_LABELS: dict[str, str] = {
    SEC_MAIN: "📋 Основное",
    SEC_REGIONS: "📍 Регионы",
    SEC_FILTERS: "🎚 Фильтры",
    SEC_WEIGHTS: "⚖️ Веса",
}
SECTION_ORDER: tuple[str, ...] = (SEC_MAIN, SEC_REGIONS, SEC_FILTERS, SEC_WEIGHTS)

_EXCLUDE: set[tuple[str, ...]] = {
    ("position_code",),
    ("prefilter",),
    ("critic_lens",),
}

# RU labels keyed by path; unmapped paths fall back to a humanized field name.
_LABELS: dict[tuple[str, ...], str] = {
    ("position_name",): "Название позиции",
    ("position_description",): "Описание",
    ("evaluation_focus",): "Критерии оценки",
    ("position_synonyms",): "Синонимы (hh.ru)",
    ("resume_freshness_days",): "Срок обновления резюме",
    ("target_companies_override",): "Целевые компании (override)",
    ("stop_companies_override",): "Стоп-компании (override)",
    ("search_params",): "Параметры hh.ru (JSON)",
    ("stop_words",): "Стоп-слова",
    ("must_have_keywords",): "Обязательные требования",
    ("nice_to_have_keywords",): "Желательные требования",
    ("forbidden_industries",): "Запретные индустрии",
    ("bonus_companies",): "Бонусные компании",
    ("role_match_mode",): "Режим роли",
    ("forbidden_industry_mode",): "Режим запретных индустрий",
    ("insurance_experience_mode",): "Режим опыта страхования",
    ("domain_governor_mode",): "Domain governor",
    ("min_insurance_experience_months",): "Опыт страхования, мес.",
    ("min_motor_experience_months",): "Опыт моторного, мес.",
    ("motor_experience_preferred",): "Моторный опыт желателен",
    ("min_tenure_last_job_months",): "Стаж на посл. месте, мес.",
    ("max_career_gap_months",): "Макс. разрыв карьеры, мес.",
    ("higher_education_required",): "Высшее обязательно",
    ("preferred_education_fields",): "Профильные специальности",
    ("citizenship",): "Гражданство",
    ("filters", "regions", "primary"): "Регионы — целевые",
    ("filters", "regions", "adjacent"): "Регионы — соседние",
    ("filters", "regions", "stop"): "Регионы — стоп",
    ("filters", "age_range"): "Возраст (min, max)",
    ("filters", "salary_range"): "Зарплата (min, max)",
    ("filters", "education_level"): "Уровни образования",
}


@dataclass(frozen=True)
class FieldDesc:
    path: tuple[str, ...]
    label: str
    kind: str  # csv | pair | int | bool | literal | str | json
    section: str
    choices: tuple[str, ...] = field(default=())
    optional: bool = False


class FieldParseError(ValueError):
    """Raised when a typed value cannot be parsed for the target field."""


_UNION_ORIGINS = {Union, types.UnionType}


def _classify(annotation: Any) -> tuple[str, tuple[str, ...], bool]:
    """Map a declared annotation to (kind, literal_choices, optional)."""
    optional = False
    origin = get_origin(annotation)
    if origin in _UNION_ORIGINS:
        non_none = [a for a in get_args(annotation) if a is not type(None)]
        optional = len(non_none) < len(get_args(annotation))
        if len(non_none) != 1:
            return ("", (), optional)
        annotation = non_none[0]
        origin = get_origin(annotation)
    if origin is Literal:
        return ("literal", tuple(str(a) for a in get_args(annotation)), optional)
    if origin is list:
        inner = (get_args(annotation) or (str,))[0]
        return (("csv", (), optional) if inner is str else ("", (), optional))
    if origin is tuple:
        return ("pair", (), optional)
    if origin is dict:
        return ("json", (), optional)
    if annotation is bool:  # bool before int (bool ⊂ int)
        return ("bool", (), optional)
    if annotation is int:
        return ("int", (), optional)
    if annotation is str:
        return ("str", (), optional)
    return ("", (), optional)


def _label(path: tuple[str, ...]) -> str:
    return _LABELS.get(path, path[-1].replace("_", " ").capitalize())


def _is_model(annotation: Any) -> bool:
    return inspect.isclass(annotation) and issubclass(annotation, BaseModel)


def _build_fields() -> list[FieldDesc]:
    out: list[FieldDesc] = []

    def _add(model: type[BaseModel], prefix: tuple[str, ...], section: str) -> None:
        for name, info in model.model_fields.items():
            path = (*prefix, name)
            if path in _EXCLUDE or _is_model(info.annotation):
                continue
            kind, choices, optional = _classify(info.annotation)
            if not kind:
                continue
            out.append(FieldDesc(path, _label(path), kind, section, choices, optional))

    _add(Portrait, (), SEC_MAIN)
    _add(RegionFilters, ("filters", "regions"), SEC_REGIONS)
    # Filters scalars (regions is a nested model → skipped by _is_model in _add)
    _add(Filters, ("filters",), SEC_FILTERS)
    _add(Weights, ("weights",), SEC_WEIGHTS)
    return out


FIELDS: list[FieldDesc] = _build_fields()


def fields_in(section: str) -> list[tuple[int, FieldDesc]]:
    return [(i, d) for i, d in enumerate(FIELDS) if d.section == section]


# ── nested dict get / set ───────────────────────────────────────────────────────


def get_value(portrait_dict: dict[str, Any], path: tuple[str, ...]) -> Any:
    cur: Any = portrait_dict
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _set_value(portrait_dict: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    cur = portrait_dict
    for key in path[:-1]:
        nxt = cur.get(key)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[key] = nxt
        cur = nxt
    cur[path[-1]] = value


def with_value(
    portrait_dict: dict[str, Any], path: tuple[str, ...], value: Any
) -> dict[str, Any]:
    """Return a deep copy of *portrait_dict* with *path* set to *value*."""
    candidate = copy.deepcopy(portrait_dict)
    _set_value(candidate, path, value)
    return candidate


# ── display / parse ─────────────────────────────────────────────────────────────


def format_value(desc: FieldDesc, value: Any) -> str:
    if desc.kind == "csv":
        return ", ".join(value) if value else "—"
    if desc.kind == "pair":
        return f"{value[0]}–{value[1]}" if value else "—"
    if desc.kind == "bool":
        return "да" if value else "нет"
    if desc.kind == "json":
        return json.dumps(value, ensure_ascii=False) if value else "{}"
    if value is None or value == "":
        return "—"
    return str(value)


def input_hint(desc: FieldDesc) -> str:
    if desc.kind == "csv":
        return "значения через запятую (пустая строка — очистить)"
    if desc.kind == "pair":
        return "два числа: «min, max» (или «-» чтобы очистить)"
    if desc.kind == "int":
        return "целое число"
    if desc.kind == "json":
        return 'JSON-объект, например {"experience": "between3And6"}'
    if desc.optional:
        return "текст (или «-» чтобы очистить)"
    return "текст"


def parse_value(desc: FieldDesc, raw: str) -> Any:
    raw = raw.strip()
    if desc.kind == "csv":
        return [s.strip() for s in raw.split(",") if s.strip()]
    if desc.kind == "pair":
        if raw in ("", "-", "—"):
            return None
        parts = [
            p.strip()
            for p in raw.replace("–", "-").replace(",", "-").split("-")
            if p.strip()
        ]
        if len(parts) != 2:
            raise FieldParseError(
                "Нужно два числа, например «100000, 450000», или «-» чтобы очистить."
            )
        try:
            return [int(parts[0]), int(parts[1])]
        except ValueError as exc:
            raise FieldParseError("Границы должны быть целыми числами.") from exc
    if desc.kind == "int":
        try:
            return int(raw)
        except ValueError as exc:
            raise FieldParseError("Нужно целое число.") from exc
    if desc.kind == "str":
        if desc.optional and raw in ("", "-", "—"):
            return None
        return raw
    if desc.kind == "json":
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise FieldParseError(f"Неверный JSON: {exc}") from exc
        if not isinstance(obj, dict):
            raise FieldParseError("Ожидался JSON-объект (словарь).")
        return obj
    raise FieldParseError("Это поле нельзя изменить вводом текста.")
