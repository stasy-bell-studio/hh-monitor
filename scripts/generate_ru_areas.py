#!/usr/bin/env python3
"""Generate hh_monitor/regions/ru_areas.py from GET https://api.hh.ru/areas/113.

No authentication required — /areas/{id} is a public HH API endpoint.

Usage:
    python scripts/generate_ru_areas.py > hh_monitor/regions/ru_areas.py
"""

from __future__ import annotations

import sys
from datetime import date

import httpx

_HH_AREAS_URL = "https://api.hh.ru/areas/113"


def fetch_russia_regions() -> list[dict[str, object]]:
    resp = httpx.get(_HH_AREAS_URL, headers={"User-Agent": "hh-monitor/dev"}, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    areas: list[dict[str, object]] = data.get("areas", [])
    return areas


def main() -> None:
    try:
        regions = fetch_russia_regions()
    except httpx.HTTPError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    today = date.today().isoformat()

    print(f"# Generated from GET {_HH_AREAS_URL} on {today}.")
    print("# To refresh: python scripts/generate_ru_areas.py > hh_monitor/regions/ru_areas.py")
    print("# ruff: noqa: RUF001  — intentional Cyrillic strings (looks-like-Latin false positives)")
    print()
    print("from __future__ import annotations")
    print()
    print("RU_AREAS: dict[str, int] = {")
    for region in sorted(regions, key=lambda r: str(r["name"])):
        name = str(region["name"]).lower()
        area_id = int(str(region["id"]))
        print(f"    {name!r}: {area_id},")
    print("}")
    print()
    print(f"# {len(regions)} federal subjects fetched.")


if __name__ == "__main__":
    main()
