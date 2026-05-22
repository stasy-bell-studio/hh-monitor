"""Unit tests for parser.run.build_search_params — pure function, no DB needed."""

from __future__ import annotations

from typing import Any

import pytest

from hh_monitor.fit.portrait import Filters, Portrait, RegionFilters
from hh_monitor.parser.run import _MAX_TEXT_LEN, build_search_params


def _portrait(
    position_name: str = "Директор филиала",
    position_synonyms: list[str] | None = None,
    resume_freshness_days: int = 0,
) -> Portrait:
    return Portrait(
        position_code="test",
        position_name=position_name,
        position_synonyms=position_synonyms or [],
        resume_freshness_days=resume_freshness_days,
        filters=Filters(regions=RegionFilters(primary=[], adjacent=[], stop=[])),
    )


def _base_params(**kwargs: Any) -> dict[str, Any]:
    return {"area": [2], **kwargs}


# ── text= construction ────────────────────────────────────────────────────────


def test_no_synonyms_uses_position_name_only() -> None:
    """Without synonyms, text= equals position_name."""
    p = _portrait(position_name="Директор", position_synonyms=[])
    result = build_search_params(_base_params(), p)
    assert result["text"] == "Директор"


def test_synonyms_joined_with_or() -> None:
    """Synonyms are joined using ' OR ' with position_name first."""
    p = _portrait(
        position_name="Директор",
        position_synonyms=["Руководитель", "Управляющий"],
    )
    result = build_search_params(_base_params(), p)
    assert result["text"] == "Директор OR Руководитель OR Управляющий"


def test_only_first_5_synonyms_included() -> None:
    """At most 5 synonyms are taken from portrait.position_synonyms."""
    p = _portrait(
        position_name="A",
        position_synonyms=["B", "C", "D", "E", "F", "G", "H"],  # 7 synonyms
    )
    result = build_search_params(_base_params(), p)
    parts = result["text"].split(" OR ")
    assert len(parts) == 6  # position_name + 5 synonyms
    assert "G" not in result["text"]
    assert "H" not in result["text"]


def test_text_does_not_exceed_max_len() -> None:
    """text= is never longer than _MAX_TEXT_LEN characters."""
    long_synonyms = [f"{'X' * 50} synonym {i}" for i in range(5)]
    p = _portrait(position_name="Short", position_synonyms=long_synonyms)
    result = build_search_params(_base_params(), p)
    assert len(result["text"]) <= _MAX_TEXT_LEN


def test_truncation_at_term_boundary() -> None:
    """When a term would exceed the limit, it is excluded (not mid-word truncation)."""
    # "Директор" = 8 chars.  After adding filler via " OR " (4 chars):
    # current_len = 8 + 4 + 235 = 247 chars.
    # Adding " OR Лишний" (4+6=10) → 257 > 250 → "Лишний" excluded.
    filler = "Б" * 235
    p = _portrait(
        position_name="Директор",
        position_synonyms=[filler, "Лишний"],
    )
    result = build_search_params(_base_params(), p)
    assert "Лишний" not in result["text"]
    assert filler in result["text"]  # filler IS included (fits in 250)


def test_position_name_always_included() -> None:
    """Even when synonyms fill the text, position_name is always the first term."""
    p = _portrait(
        position_name="Директор",
        position_synonyms=["X" * 250],  # would overflow on its own
    )
    result = build_search_params(_base_params(), p)
    assert result["text"].startswith("Директор")


def test_original_hh_params_text_is_overridden() -> None:
    """An existing 'text' key in hh_params is replaced by the computed one."""
    p = _portrait(position_name="Новый")
    result = build_search_params({"area": [2], "text": "Старый текст"}, p)
    assert result["text"] == "Новый"
    assert "Старый" not in result["text"]


def test_original_hh_params_not_mutated() -> None:
    """build_search_params returns a new dict and does not mutate the input."""
    original = {"area": [2, 78], "per_page": 50}
    p = _portrait(position_name="X")
    result = build_search_params(original, p)
    assert "text" not in original  # original unchanged
    assert result is not original


# ── period= ───────────────────────────────────────────────────────────────────


def test_period_added_when_freshness_set() -> None:
    """resume_freshness_days > 0 → period= is included in result."""
    p = _portrait(resume_freshness_days=30)
    result = build_search_params(_base_params(), p)
    assert result["period"] == 30


def test_period_absent_when_freshness_zero() -> None:
    """resume_freshness_days == 0 → no period key."""
    p = _portrait(resume_freshness_days=0)
    result = build_search_params(_base_params(), p)
    assert "period" not in result


def test_period_absent_when_freshness_not_set() -> None:
    """Default portrait (freshness_days=0) → no period."""
    p = _portrait()
    result = build_search_params(_base_params(), p)
    assert "period" not in result


# ── area ID warnings ──────────────────────────────────────────────────────────


def test_new_territory_area_ids_logged(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """area IDs for new territories trigger a WARNING log (but don't fail)."""
    warnings: list[str] = []
    import hh_monitor.parser.run as _run

    monkeypatch.setattr(
        _run.logger,
        "warning",
        lambda event, **_kw: warnings.append(event),
    )
    p = _portrait()
    result = build_search_params({"area": [2209, 2]}, p)  # 2209 = Херсонская
    assert result is not None  # does not raise
    assert "parser.new_territory_area_ids" in warnings


def test_normal_area_ids_no_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    """Normal area IDs do not produce a new-territory warning."""
    warnings: list[str] = []
    import hh_monitor.parser.run as _run

    monkeypatch.setattr(
        _run.logger,
        "warning",
        lambda event, **_kw: warnings.append(event),
    )
    p = _portrait()
    build_search_params({"area": [2, 78, 2114, 130]}, p)
    assert "parser.new_territory_area_ids" not in warnings


def test_no_area_key_no_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing area key in hh_params → no warning."""
    warnings: list[str] = []
    import hh_monitor.parser.run as _run

    monkeypatch.setattr(
        _run.logger,
        "warning",
        lambda event, **_kw: warnings.append(event),
    )
    p = _portrait()
    build_search_params({"text": "X"}, p)
    assert "parser.new_territory_area_ids" not in warnings


# ── branch_director integration ───────────────────────────────────────────────


def test_branch_director_text_query() -> None:
    """branch_director portrait produces a correct, under-250-char text query."""
    from hh_monitor.fit.portrait import load_all_portraits

    portrait = load_all_portraits()["branch_director"]
    result = build_search_params({"area": [2114, 130]}, portrait)

    text = result["text"]
    assert text.startswith(portrait.position_name)
    assert len(text) <= _MAX_TEXT_LEN
    # First synonym should be present
    assert portrait.position_synonyms[0] in text
    # Period should be included (resume_freshness_days=30 in branch_director.yaml)
    assert result.get("period") == 30
    # No modification of area
    assert result["area"] == [2114, 130]
