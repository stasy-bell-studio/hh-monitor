"""Unit tests for parser.run.build_search_params — pure function, no DB needed.

text= format: "(position_name OR syn1 OR ... OR synN) страхование"
Budget: _MAX_TEXT_LEN (255) total, of which 14 chars are overhead
(prefix "(" + suffix ") страхование"), leaving 241 chars for OR terms.
"""

from __future__ import annotations

from typing import Any

import pytest

from hh_monitor.fit.portrait import Filters, Portrait, RegionFilters
from hh_monitor.parser.run import _MAX_TEXT_LEN, _QUERY_OVERHEAD, build_search_params

_OR_BUDGET = _MAX_TEXT_LEN - _QUERY_OVERHEAD  # 241 chars for OR terms


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


# ── text= format ──────────────────────────────────────────────────────────────


def test_no_synonyms_wraps_position_name() -> None:
    """Without synonyms, text= is '(position_name) страхование'."""
    p = _portrait(position_name="Директор", position_synonyms=[])
    result = build_search_params(_base_params(), p)
    assert result["text"] == "(Директор) страхование"


def test_synonyms_joined_with_or_and_wrapped() -> None:
    """Synonyms are joined with ' OR ' and wrapped in parens + страхование."""
    p = _portrait(
        position_name="Директор",
        position_synonyms=["Руководитель", "Управляющий"],
    )
    result = build_search_params(_base_params(), p)
    assert result["text"] == "(Директор OR Руководитель OR Управляющий) страхование"


def test_text_starts_with_paren_and_position_name() -> None:
    """text= always starts with '(<position_name>'."""
    p = _portrait(position_name="Директор", position_synonyms=["X" * 300])
    result = build_search_params(_base_params(), p)
    assert result["text"].startswith("(Директор")


def test_text_ends_with_insurance_suffix() -> None:
    """text= always ends with ') страхование'."""
    p = _portrait(position_name="Директор")
    result = build_search_params(_base_params(), p)
    assert result["text"].endswith(") страхование")


# ── synonym inclusion — dynamic (no hardcoded limit) ─────────────────────────


def test_all_short_synonyms_included() -> None:
    """With 7 short synonyms that all fit, ALL 7 are included (no hardcoded limit)."""
    p = _portrait(
        position_name="A",
        position_synonyms=["B", "C", "D", "E", "F", "G", "H"],
    )
    result = build_search_params(_base_params(), p)
    assert all(s in result["text"] for s in ["A", "B", "C", "D", "E", "F", "G", "H"])


def test_text_does_not_exceed_max_len() -> None:
    """text= (including prefix/suffix) is never longer than _MAX_TEXT_LEN."""
    # These long synonyms won't all fit — algorithm stops at the budget
    long_synonyms = [f"{'X' * 50} synonym {i}" for i in range(5)]
    p = _portrait(position_name="Short", position_synonyms=long_synonyms)
    result = build_search_params(_base_params(), p)
    assert len(result["text"]) <= _MAX_TEXT_LEN


def test_truncation_at_term_boundary() -> None:
    """Synonym that would overflow the OR-terms budget is excluded (no mid-word cut)."""
    # OR-terms budget = 241.
    # "Директор" (8) + " OR " (4) + filler (225) = 237 ≤ 241 → filler included.
    # "Директор" + filler + " OR " + "Лишний" = 237+4+6 = 247 > 241 → "Лишний" excluded.
    filler = "Б" * 225
    p = _portrait(
        position_name="Директор",
        position_synonyms=[filler, "Лишний"],
    )
    result = build_search_params(_base_params(), p)
    assert "Лишний" not in result["text"]
    assert filler in result["text"]


def test_position_name_always_included() -> None:
    """position_name is always the first OR-term even if synonyms overflow."""
    p = _portrait(
        position_name="Директор",
        position_synonyms=["X" * 300],  # would overflow on its own
    )
    result = build_search_params(_base_params(), p)
    # "(Директор) страхование" — the overflow synonym is skipped
    assert "(Директор)" in result["text"] or result["text"].startswith("(Директор OR")


# ── hh_params handling ────────────────────────────────────────────────────────


def test_original_hh_params_text_is_overridden() -> None:
    """An existing 'text' key in hh_params is replaced by the computed one."""
    p = _portrait(position_name="Новый")
    result = build_search_params({"area": [2], "text": "Старый текст"}, p)
    assert result["text"] == "(Новый) страхование"
    assert "Старый" not in result["text"]


def test_original_hh_params_not_mutated() -> None:
    """build_search_params returns a new dict and does not mutate the input."""
    original = {"area": [2, 78], "per_page": 50}
    p = _portrait(position_name="X")
    result = build_search_params(original, p)
    assert "text" not in original  # original unchanged
    assert result is not original


def test_non_text_hh_params_are_preserved() -> None:
    """area, experience, per_page and other hh_params keys are passed through."""
    p = _portrait(position_name="X")
    result = build_search_params({"area": [1, 2], "experience": ["moreThan6"], "per_page": 50}, p)
    assert result["area"] == [1, 2]
    assert result["experience"] == ["moreThan6"]
    assert result["per_page"] == 50


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
    """branch_director portrait produces correct text= with all required synonyms."""
    from hh_monitor.fit.portrait import load_all_portraits

    portrait = load_all_portraits()["branch_director"]
    result = build_search_params({"area": [2114, 130]}, portrait)

    text = result["text"]
    # Format
    assert text.startswith(f"({portrait.position_name}")
    assert text.endswith(") страхование")
    assert len(text) <= _MAX_TEXT_LEN
    # First synonym present
    assert portrait.position_synonyms[0] in text
    # The 3 user-priority synonyms (реорганизованы вверх) включены
    assert "Региональный директор" in text
    assert "Региональный управляющий" in text
    assert "Управляющий представительства" in text
    # Period from portrait (resume_freshness_days=30)
    assert result.get("period") == 30
    # area IDs passed through unchanged
    assert result["area"] == [2114, 130]


def test_branch_director_synonym_count() -> None:
    """branch_director generates at least 7 synonyms (no hardcoded 5-limit)."""
    from hh_monitor.fit.portrait import load_all_portraits

    portrait = load_all_portraits()["branch_director"]
    result = build_search_params({"area": [2114, 130]}, portrait)

    text = result["text"]
    # Strip "(" prefix and ") страхование" suffix, then count OR-terms
    inner = text.removeprefix("(").removesuffix(") страхование")
    terms = inner.split(" OR ")
    # position_name + at least 7 synonyms (previously capped at 5)
    assert len(terms) >= 8, f"Expected ≥8 terms, got {len(terms)}: {terms}"
