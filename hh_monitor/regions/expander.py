"""V1 macro recognizer for hh.ru area IDs.

Resolves known "21 Век all-regions" string macros to the 27 area IDs used in
production searches.  V2 (future) will resolve arbitrary city/region names via
hh.ru /areas.
"""

from __future__ import annotations

import structlog

log = structlog.get_logger(__name__)

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
# Region name table (matches branch_director.yaml primary coverage):
#   1    Москва                  1090 Нижегородская область  1317 Красноярский край
#   2    Санкт-Петербург         1828 Воронежская область     1905 Пермский край
#   145  Краснодарский край      1077 Республика Татарстан    1575 Кемеровская область
#   1020 Ростовская область      1438 Свердловская область    1556 Алтайский край
#   1103 Самарская область       2114 Республика Крым         1261 Ставропольский край
#                                130  Севастополь             1342 Оренбургская область
#   1530 Новосибирская область   1187 Саратовская область     2209 Херсонская область
#   1481 Омская область          1192 Волгоградская область   2155 Запорожская область
#   1754 Тюменская область                                    2173 ЛНР
#   1563 Иркутская область                                    2134 ДНР

_MACROS: dict[str, tuple[int, ...]] = {
    "все регионы 21 века": PRIMARY_AREA_IDS_21VEK,
    "все регионы 21 век": PRIMARY_AREA_IDS_21VEK,
    "21 век": PRIMARY_AREA_IDS_21VEK,
    "21век": PRIMARY_AREA_IDS_21VEK,
    "21 vek": PRIMARY_AREA_IDS_21VEK,
    "21vek": PRIMARY_AREA_IDS_21VEK,
    "филиалы 21 века": PRIMARY_AREA_IDS_21VEK,
    "все регионы": PRIMARY_AREA_IDS_21VEK,
}


def expand_region_names(names: list[str]) -> list[int]:
    """Map region name strings to hh.ru area IDs.

    Known macros expand to PRIMARY_AREA_IDS_21VEK.  Unknown names emit a
    WARNING and are skipped (V2 will resolve them via hh.ru /areas).
    Duplicate IDs are removed preserving first-occurrence order.
    """
    result: list[int] = []
    for name in names:
        key = name.strip().lower()
        ids = _MACROS.get(key)
        if ids is not None:
            result.extend(ids)
        else:
            log.warning("regions.unknown_name", name=name)
    return list(dict.fromkeys(result))
