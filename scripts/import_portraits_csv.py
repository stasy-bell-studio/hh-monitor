#!/usr/bin/env python
"""Import portrait definitions from a CSV file (provided by Tatiana Lesnitskaya).

Usage:
    poetry run python scripts/import_portraits_csv.py <csv_path> [--portraits-dir DIR]

CSV columns (comma-separated, UTF-8, header row required):
    position_code         — snake_case identifier, e.g. branch_director
    position_name         — human-readable Russian name
    region_primary        — semicolon-separated list of primary regions
    region_adjacent       — semicolon-separated list of adjacent regions
    region_stop           — semicolon-separated list of stop regions (may be empty)
    age_min               — integer, minimum age (leave blank to skip)
    age_max               — integer, maximum age (leave blank to skip)
    salary_min            — integer RUB, min salary (leave blank to skip)
    salary_max            — integer RUB, max salary / salary_fit ceiling
    education_min         — comma-separated education level ids (hh.ru dict)
    stop_words            — semicolon-separated words that are soft red flags
    must_have_keywords    — semicolon-separated mandatory keywords for LLM prompt
    nice_to_have_keywords — semicolon-separated desired keywords for LLM prompt

Idempotent: re-running on the same CSV overwrites existing YAML files.
Validates each row via Pydantic Portrait before writing.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import yaml

# Allow running as a script from repo root
sys.path.insert(0, str(Path(__file__).parent.parent))

from hh_monitor.fit.portrait import Portrait

_DEFAULT_PORTRAITS_DIR = Path(__file__).parent.parent / "config" / "portraits"


def _split(value: str, sep: str = ";") -> list[str]:
    """Split a delimited string into a stripped, non-empty list."""
    return [v.strip() for v in value.split(sep) if v.strip()]


def _parse_int(value: str) -> int | None:
    value = value.strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def import_csv(csv_path: Path, portraits_dir: Path) -> int:
    """Parse *csv_path* and write YAML portraits to *portraits_dir*.

    Returns the number of portraits written.
    """
    portraits_dir.mkdir(parents=True, exist_ok=True)
    written = 0

    with csv_path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        for row_num, row in enumerate(reader, start=2):  # 2 = first data row
            position_code = row.get("position_code", "").strip()
            if not position_code:
                print(f"  [row {row_num}] SKIP — empty position_code", file=sys.stderr)
                continue

            age_min = _parse_int(row.get("age_min", ""))
            age_max = _parse_int(row.get("age_max", ""))
            salary_min = _parse_int(row.get("salary_min", ""))
            salary_max = _parse_int(row.get("salary_max", ""))

            portrait_data: dict = {
                "position_code": position_code,
                "position_name": row.get("position_name", "").strip(),
                "filters": {
                    "regions": {
                        "primary": _split(row.get("region_primary", "")),
                        "adjacent": _split(row.get("region_adjacent", "")),
                        "stop": _split(row.get("region_stop", "")),
                    },
                    "age_range": [age_min, age_max] if (age_min and age_max) else None,
                    "salary_range": (
                        [salary_min, salary_max] if (salary_min and salary_max) else None
                    ),
                    "education_level": _split(row.get("education_min", ""), sep=","),
                },
                "stop_words": _split(row.get("stop_words", "")),
                "must_have_keywords": _split(row.get("must_have_keywords", "")),
                "nice_to_have_keywords": _split(row.get("nice_to_have_keywords", "")),
                # Legacy fields with safe defaults — extend after import as needed
                "title_keywords": _split(row.get("must_have_keywords", "")),
                "experience_keywords": _split(row.get("must_have_keywords", "")),
                "min_total_months": 0,
                "preferred_total_months": 36,
            }

            # Validate via Pydantic before writing
            try:
                Portrait.model_validate(portrait_data)
            except Exception as exc:
                print(
                    f"  [row {row_num}] VALIDATION ERROR for {position_code}: {exc}",
                    file=sys.stderr,
                )
                continue

            out_path = portraits_dir / f"{position_code}.yaml"
            with out_path.open("w", encoding="utf-8") as out:
                yaml.dump(
                    portrait_data,
                    out,
                    allow_unicode=True,
                    default_flow_style=False,
                    sort_keys=False,
                )
            print(f"  [row {row_num}] OK  → {out_path.name}")
            written += 1

    return written


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_path", type=Path, help="Path to the CSV file")
    parser.add_argument(
        "--portraits-dir",
        type=Path,
        default=_DEFAULT_PORTRAITS_DIR,
        help="Output directory for YAML portraits",
    )
    args = parser.parse_args()

    if not args.csv_path.exists():
        print(f"ERROR: file not found: {args.csv_path}", file=sys.stderr)
        sys.exit(1)

    print(f"Importing portraits from {args.csv_path} → {args.portraits_dir}")
    n = import_csv(args.csv_path, args.portraits_dir)
    print(f"\nDone: {n} portrait(s) written.")


if __name__ == "__main__":
    main()
