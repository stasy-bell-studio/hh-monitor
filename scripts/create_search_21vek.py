#!/usr/bin/env python3
"""Create the branch_director_21vek production search in the database.

Usage (from the project root):
    poetry run python scripts/create_search_21vek.py

What it does:
  1. Loads config/portraits/branch_director.yaml (source of truth).
  2. Builds hh_params: 27 area IDs + experience filter + text= (from
     build_search_params) + period=30 (from portrait.resume_freshness_days).
  3. Inserts a Search row with position_code='branch_director' (canonical
     portrait code, shared with any other branch_director searches).
  4. Prints a verification summary.

Re-running is idempotent — prints a notice if the search already exists.

Note on position_code:
  position_code='branch_director' aligns with the branch_director.yaml portrait
  and is the canonical value after the session-5.7 cleanup migration.  On a
  production VPS where the legacy SPb search (id=1) has been decommissioned,
  this script creates a single row.  If id=1 still exists with the same
  position_code, the idempotency check aborts safely without creating a
  duplicate.
"""

import asyncio
import sys
from pathlib import Path

# ── make hh_monitor importable when run as a plain script ─────────────────────
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import yaml  # noqa: E402  (after sys.path patch)
from sqlalchemy import select  # noqa: E402

from hh_monitor.db.engine import async_session_factory  # noqa: E402
from hh_monitor.db.models import Search  # noqa: E402
from hh_monitor.fit.portrait import load_portrait  # noqa: E402
from hh_monitor.parser.run import build_search_params  # noqa: E402
from hh_monitor.regions.expander import PRIMARY_AREA_IDS_21VEK  # noqa: E402

# ── 27 area IDs for branch_director_21vek ────────────────────────────────────
#
# Primary coverage (from branch_director.yaml):
#   1    Москва
#   2    Санкт-Петербург
#   145  Краснодарский край
#   1020 Ростовская область
#   1103 Самарская область
#   1090 Нижегородская область
#   1828 Воронежская область
#   1077 Республика Татарстан (Казань)
#   1438 Свердловская область (Екатеринбург)
#   2114 Республика Крым (Симферополь)
#   130  Севастополь            ← оба ID для Севастополя (вложен под Крым И самостоятельно)
#   1530 Новосибирская область
#   1481 Омская область
#   1754 Тюменская область
#   1563 Иркутская область
#   1187 Саратовская область
#   1192 Волгоградская область
#   1317 Красноярский край
#   1905 Пермский край
#   1575 Кемеровская область
#   1556 Алтайский край
#   1261 Ставропольский край
#   1342 Оренбургская область
# New territories (exist на api.hh.ru, могут потребовать RF VPN):
#   2209 Херсонская область
#   2155 Запорожская область
#   2173 ЛНР
#   2134 ДНР
AREA_IDS: list[int] = list(PRIMARY_AREA_IDS_21VEK)

_PORTRAIT_YAML = _ROOT / "config" / "portraits" / "branch_director.yaml"
_POSITION_CODE = "branch_director"
_POSITION_NAME = "Директор филиала (21 Век)"


async def main() -> None:
    # ── 1. Load portrait ──────────────────────────────────────────────────────
    portrait = load_portrait(_PORTRAIT_YAML)
    portrait_raw: dict = yaml.safe_load(_PORTRAIT_YAML.read_text(encoding="utf-8"))  # type: ignore[type-arg]

    # ── 2. Build hh_params (text= + period= added by build_search_params) ────
    base_params: dict = {  # type: ignore[type-arg]
        "area": AREA_IDS,
        "experience": ["between3And6", "moreThan6"],
    }
    hh_params = build_search_params(base_params, portrait)

    print("=" * 70)
    print(f"Создаём поиск: {_POSITION_CODE!r}")
    print(f"  text= ({len(hh_params['text'])} символов):")
    print(f"    {hh_params['text']}")
    print(f"  area: {len(hh_params['area'])} регионов")
    print(f"  experience: {hh_params['experience']}")
    print(f"  period: {hh_params.get('period')}")
    print("=" * 70)

    # ── 3. Insert (idempotent) ────────────────────────────────────────────────
    async with async_session_factory() as session:
        existing = (
            await session.execute(select(Search.id).where(Search.position_code == _POSITION_CODE))
        ).scalar_one_or_none()

        if existing is not None:
            print(f"⚠️  Поиск уже существует (id={existing}). Ничего не создаём.")
            print("   Для обновления удалите строку вручную и запустите скрипт заново.")
            return

        search = Search(
            position_code=_POSITION_CODE,
            position_name=_POSITION_NAME,
            hh_params=hh_params,
            portrait=portrait_raw,
            active=True,
        )
        session.add(search)
        await session.commit()

        # Re-fetch to confirm id and stored length
        row = (
            await session.execute(
                select(Search.id, Search.position_code).where(
                    Search.position_code == _POSITION_CODE
                )
            )
        ).one()

        print(f"✅ Создан: id={row[0]}, position_code={row[1]!r}")
        print()
        print("Для проверки выполните в psql:")
        print(
            "  SELECT id, position_code, "
            "length(hh_params::text) AS hh_params_len, "
            "hh_params->>'text' AS text_query, "
            "jsonb_array_length(hh_params->'area') AS area_count "
            f"FROM searches WHERE position_code='{_POSITION_CODE}';"
        )


if __name__ == "__main__":
    asyncio.run(main())
