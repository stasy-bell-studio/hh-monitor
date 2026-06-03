#!/usr/bin/env python3
"""Generate hh_monitor/regions/ru_areas.py from GET https://api.hh.ru/areas/113.

No authentication required — /areas/{id} is a public HH API endpoint.

Usage:
    python scripts/generate_ru_areas.py > hh_monitor/regions/ru_areas.py
"""

from __future__ import annotations

import sys
from datetime import date
from typing import Any, NotRequired, TypedDict, cast

import httpx

from hh_monitor.regions.expander import _normalize_region

_HH_AREAS_URL = "https://api.hh.ru/areas/113"


class _RawArea(TypedDict):
    id: str
    name: str
    areas: NotRequired[list[_RawArea]]


def _recurse(
    node: _RawArea,
    parent_id: int,
    city_to_subjects: dict[str, set[int]],
) -> None:
    """Walk the area tree depth-first; record every descendant against its subject."""
    children = node.get("areas")
    if children is None:
        return
    for child in children:
        cname = _normalize_region(child["name"])
        city_to_subjects.setdefault(cname, set()).add(parent_id)
        _recurse(child, parent_id, city_to_subjects)


def _collect_cities(
    subjects: list[_RawArea],
) -> tuple[dict[str, int], dict[str, int], dict[str, tuple[int, ...]]]:
    """Build (RU_AREAS, RU_CITIES, RU_AMBIGUOUS_CITIES) from the raw subjects list."""
    ru_areas: dict[str, int] = {}
    city_to_subjects: dict[str, set[int]] = {}

    for subj in subjects:
        subj_name = _normalize_region(subj["name"])
        subj_id = int(subj["id"])
        ru_areas[subj_name] = subj_id
        _recurse(subj, subj_id, city_to_subjects)

    ru_cities: dict[str, int] = {}
    ru_ambiguous: dict[str, tuple[int, ...]] = {}
    for cname, ids in city_to_subjects.items():
        if cname in ru_areas:
            continue
        if len(ids) == 1:
            ru_cities[cname] = next(iter(ids))
        else:
            ru_ambiguous[cname] = tuple(sorted(ids))

    return ru_areas, ru_cities, ru_ambiguous


def fetch_russia_regions() -> list[_RawArea]:
    resp = httpx.get(_HH_AREAS_URL, headers={"User-Agent": "hh-monitor/dev"}, timeout=15)
    resp.raise_for_status()
    data = cast(dict[str, Any], resp.json())
    return cast(list[_RawArea], data.get("areas", []))


def main() -> None:
    try:
        regions = fetch_russia_regions()
    except httpx.HTTPError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    ru_areas, ru_cities, ru_ambiguous = _collect_cities(regions)
    today = date.today().isoformat()

    print(f"# Generated from GET {_HH_AREAS_URL} on {today}.")
    print("# To refresh: python scripts/generate_ru_areas.py > hh_monitor/regions/ru_areas.py")
    print("# ruff: noqa: RUF001, E501  — Cyrillic; some city names exceed 100 chars")
    print()
    print("from __future__ import annotations")
    print()
    print("RU_AREAS: dict[str, int] = {")
    for name in sorted(ru_areas):
        print(f'    "{name}": {ru_areas[name]},')
    print("}")
    print()
    print("RU_CITIES: dict[str, int] = {")
    for name in sorted(ru_cities):
        print(f'    "{name}": {ru_cities[name]},')
    print("}")
    print()
    print("RU_AMBIGUOUS_CITIES: dict[str, tuple[int, ...]] = {")
    for name in sorted(ru_ambiguous):
        print(f'    "{name}": {ru_ambiguous[name]!r},')
    print("}")
    print()
    print(f"# {len(ru_areas)} federal subjects, {len(ru_cities)} cities fetched.")


if __name__ == "__main__":
    main()
