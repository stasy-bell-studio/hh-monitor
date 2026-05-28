"""search_code / position_code slug generation with cyrillic transliteration.

The FSM "Add Vacancy" wizard builds codes from a human position name typed by an
admin (usually Russian).  The legacy ``cli._slugify`` only kept ASCII ``[a-z0-9]``
which collapses a fully-cyrillic name to an empty string, so we need explicit
transliteration here.

Slug rules (Session 12 spec):
  1. transliterate cyrillic → latin
  2. lowercase
  3. replace any run of non-[a-z0-9] with a single "-"
  4. trim leading/trailing "-"
  5. truncate to ``max_len`` (default 40), trimming a trailing "-" again
A uniqueness suffix "-2", "-3", … is appended by :func:`next_unique_search_code`
against existing ``searches.search_code`` values.
"""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hh_monitor.db.models import Search

# GOST 7.79-2000 System B-ish transliteration; pragmatic, lossy, ASCII-only output.
_TRANSLIT: dict[str, str] = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}

_SLUG_MAX_LEN = 40
_FALLBACK_SLUG = "search"


def _transliterate(text: str) -> str:
    out: list[str] = []
    for ch in text:
        lower = ch.lower()
        if lower in _TRANSLIT:
            mapped = _TRANSLIT[lower]
            out.append(mapped.upper() if ch.isupper() and mapped else mapped)
        else:
            out.append(ch)
    return "".join(out)


def slugify(name: str, max_len: int = _SLUG_MAX_LEN) -> str:
    """'Директор филиала' → 'direktor-filiala'; 'Senior Backend' → 'senior-backend'.

    Always returns a non-empty ASCII slug (falls back to 'search' if the input
    has no usable alphanumerics after transliteration).
    """
    translit = _transliterate(name).lower()
    slug = re.sub(r"[^a-z0-9]+", "-", translit).strip("-")
    slug = slug[:max_len].strip("-")
    return slug or _FALLBACK_SLUG


async def next_unique_search_code(session: AsyncSession, base: str) -> str:
    """Return *base*, else 'base-2', 'base-3', … until unique in searches.search_code.

    The suffix is appended after truncation so the final code may slightly exceed
    ``_SLUG_MAX_LEN`` by the suffix length — acceptable, the column is unbounded Text.
    """
    existing = set(
        (
            await session.execute(
                select(Search.search_code).where(Search.search_code.is_not(None))
            )
        )
        .scalars()
        .all()
    )
    if base not in existing:
        return base
    n = 2
    while f"{base}-{n}" in existing:
        n += 1
    return f"{base}-{n}"
