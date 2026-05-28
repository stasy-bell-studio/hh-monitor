"""Tests for hh_monitor.searches.codes — slug + uniqueness (AC12)."""

from __future__ import annotations

from typing import Any

import pytest

from hh_monitor.db.models import Search
from hh_monitor.searches.codes import next_unique_search_code, slugify

# ── slugify ────────────────────────────────────────────────────────────────────


def test_slugify_cyrillic_transliteration() -> None:
    assert slugify("Директор филиала") == "direktor-filiala"


def test_slugify_latin_passthrough() -> None:
    assert slugify("Senior Backend Python") == "senior-backend-python"


def test_slugify_mixed_and_punctuation() -> None:
    assert slugify("Андеррайтер (моторные!)") == "anderrayter-motornye"


def test_slugify_collapses_repeats_and_trims() -> None:
    assert slugify("  ---Менеджер   по   продажам---  ") == "menedzher-po-prodazham"


def test_slugify_truncates_to_max_len() -> None:
    long_name = "Очень длинное название позиции которое точно превышает лимит сорока символов"
    slug = slugify(long_name)
    assert len(slug) <= 40
    assert not slug.endswith("-")


def test_slugify_empty_fallback() -> None:
    assert slugify("!!!@@@###") == "search"
    assert slugify("") == "search"


def test_slugify_yo_and_special_letters() -> None:
    assert slugify("Ёлка щука") == "elka-schuka"


# ── next_unique_search_code ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unique_code_no_collision(db_session: Any) -> None:
    code = await next_unique_search_code(db_session, "direktor-filiala")
    assert code == "direktor-filiala"


@pytest.mark.asyncio
async def test_unique_code_single_collision(db_session: Any) -> None:
    db_session.add(
        Search(
            search_code="direktor-filiala",
            position_code="direktor-filiala",
            position_name="X",
            hh_params={},
            portrait={},
        )
    )
    await db_session.flush()
    code = await next_unique_search_code(db_session, "direktor-filiala")
    assert code == "direktor-filiala-2"


@pytest.mark.asyncio
async def test_unique_code_multiple_collisions(db_session: Any) -> None:
    for sc in ("mgr", "mgr-2", "mgr-3"):
        db_session.add(
            Search(
                search_code=sc,
                position_code="mgr",
                position_name="X",
                hh_params={},
                portrait={},
            )
        )
    await db_session.flush()
    code = await next_unique_search_code(db_session, "mgr")
    assert code == "mgr-4"
