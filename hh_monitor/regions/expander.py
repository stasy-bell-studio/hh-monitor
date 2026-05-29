"""Region-name resolver for hh.ru area IDs.

resolve_region_names() maps portrait region strings to hh.ru area IDs and
returns a separate list of names that could not be resolved.  The caller
decides how to surface unknowns (review-card warning, log, etc.) — this
module never logs.

Lookup priority per name (after normalisation):
  1. Contains "21 век" / "21vek" → PRIMARY_AREA_IDS_21VEK macro.
  2. Exact match in _MACROS (legacy alias dict).
  3. Exact match in RU_AREAS (all 88 RF federal subjects from HH /areas/113).
  4. Stripped-type-word alias (e.g. "нижегородская" → нижегородская область).
  5. Spelling alias (спб/питер → санкт-петербург).
"""

from __future__ import annotations

import re

from hh_monitor.regions.ru_areas import RU_AREAS

PRIMARY_AREA_IDS_21VEK: tuple[int, ...] = (
    1,
    2,
    145,
    1020,
    1103,
    1090,
    1828,
    1077,
    1438,
    2114,
    130,
    1530,
    1481,
    1754,
    1563,
    1187,
    1192,
    1317,
    1905,
    1575,
    1556,
    1261,
    1342,
    2209,
    2155,
    2173,
    2134,
)
# Actual region mapping (verified against live GET /areas/113 on 2026-05-29):
#   1    Москва                  1020 Калининградская обл.   1317 Пермский край
#   2    Санкт-Петербург         1828 Брянская область       1905 Тамбовская область
#   145  Ленинградская область   1077 Республика Карелия     1575 Пензенская область
#   1103 Смоленская область      1438 Краснодарский край     1556 Республика Мордовия
#   1090 Псковская область       2114 Республика Крым        1261 Свердловская область
#   130  Севастополь (город,     1530 Ростовская область     1342 Тюменская область
#        parent=2114, не в       1481 Ставропольский край    2209 Херсонская область
#        /areas/113)             1754 Ивановская область     2155 Запорожская область
#                                1563 Оренбургская область   2173 ЛНР
#                                1187 Республика Хакасия     2134 ДНР
#                                1192 Забайкальский край

_MACROS: dict[str, tuple[int, ...]] = {
    "все регионы 21 века": PRIMARY_AREA_IDS_21VEK,
    "все регионы 21 век": PRIMARY_AREA_IDS_21VEK,
    "21 век": PRIMARY_AREA_IDS_21VEK,
    "21век": PRIMARY_AREA_IDS_21VEK,  # noqa: RUF001 — intentional Cyrillic
    "21 vek": PRIMARY_AREA_IDS_21VEK,
    "21vek": PRIMARY_AREA_IDS_21VEK,
    "филиалы 21 века": PRIMARY_AREA_IDS_21VEK,
    "все регионы": PRIMARY_AREA_IDS_21VEK,
}

# Geographical type words stripped when building short-form aliases.
_TYPE_WORDS: frozenset[str] = frozenset(
    {"область", "край", "республика", "округ", "автономный", "автономная", "автономное"}
)

# Genuine spelling variants only — no city→region mappings here.
_SPELLING_ALIASES: dict[str, int] = {
    "спб": 2,
    "питер": 2,
    "санкт петербург": 2,  # without hyphen
}

_TRAILING_PARENS_RE = re.compile(r"\s*\(\d+\)$")


def _build_stripped_aliases() -> dict[str, int]:
    """Short-form aliases derived from RU_AREAS by removing type words.

    E.g. "нижегородская область" → "нижегородская": 1679,
         "республика татарстан"  → "татарстан": 1624.
    """
    aliases: dict[str, int] = {}
    for name, area_id in RU_AREAS.items():
        words = name.split()
        stripped = " ".join(w for w in words if w not in _TYPE_WORDS).strip()
        if stripped and stripped != name:
            aliases.setdefault(stripped, area_id)
    return aliases


_STRIPPED_ALIASES: dict[str, int] = _build_stripped_aliases()


def resolve_region_names(
    names: list[str],
) -> tuple[list[int], list[str]]:
    """Map region name strings to hh.ru area IDs.

    Returns ``(resolved_ids, unknown_names)``.  resolved_ids is deduped
    preserving first-occurrence order.  unknown_names preserves original
    casing from the input.  This function never emits log events — callers
    decide how to surface unknowns.
    """
    ids: list[int] = []
    unknown: list[str] = []

    for name in names:
        norm = _TRAILING_PARENS_RE.sub("", name.strip().lower())

        # 1. 21Vek contains-based detection — catches any paraphrase.
        if "21 век" in norm or "21vek" in norm or "21 vek" in norm:
            ids.extend(PRIMARY_AREA_IDS_21VEK)
            continue

        # 2. Legacy exact-match macros.
        macro = _MACROS.get(norm)
        if macro is not None:
            ids.extend(macro)
            continue

        # 3. Official RF region name (from HH /areas/113).
        area_id = RU_AREAS.get(norm)
        if area_id is not None:
            ids.append(area_id)
            continue

        # 4. Auto-stripped type-word alias.
        area_id = _STRIPPED_ALIASES.get(norm)
        if area_id is not None:
            ids.append(area_id)
            continue

        # 5. Spelling variant alias.
        area_id = _SPELLING_ALIASES.get(norm)
        if area_id is not None:
            ids.append(area_id)
            continue

        unknown.append(name)

    return list(dict.fromkeys(ids)), unknown
