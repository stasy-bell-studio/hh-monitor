"""Tests for hh_monitor.fit.portrait_loader.load_portrait_for_search."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from hh_monitor.db.models import Search
from hh_monitor.fit.portrait import Portrait
from hh_monitor.fit.portrait_loader import load_portrait_for_search


def _make_search(
    *,
    position_code: str,
    portrait_jsonb: dict | None = None,
    search_code: str | None = "sc-test",
) -> Search:
    return Search(
        id=1,
        search_code=search_code,
        position_code=position_code,
        position_name="X",
        hh_params={},
        portrait=portrait_jsonb if portrait_jsonb is not None else {},
        active=True,
        llm_critic_prompt="",
    )


def _make_portrait(position_code: str, position_name: str = "YAML Name") -> Portrait:
    return Portrait(position_code=position_code, position_name=position_name)


def test_yaml_hit_returns_yaml_portrait_even_with_jsonb() -> None:
    """AC21: YAML wins over jsonb when both are present."""
    yaml_portrait = _make_portrait("yaml_role", position_name="YAML Wins")
    search = _make_search(
        position_code="yaml_role",
        portrait_jsonb={"position_code": "yaml_role", "position_name": "DB Loses"},
    )
    with patch(
        "hh_monitor.fit.portrait_loader.load_all_portraits",
        return_value={"yaml_role": yaml_portrait},
    ):
        result = load_portrait_for_search(search)
    assert result is yaml_portrait
    assert result.position_name == "YAML Wins"


def test_yaml_miss_db_hit_returns_validated_jsonb() -> None:
    """AC22: when YAML has no match but jsonb is populated, fall back to DB."""
    search = _make_search(
        position_code="fsm_only",
        portrait_jsonb={
            "position_code": "fsm_only",
            "position_name": "FSM Created",
            "must_have_keywords": ["python"],
        },
    )
    with patch(
        "hh_monitor.fit.portrait_loader.load_all_portraits",
        return_value={"other_role": _make_portrait("other_role")},
    ):
        result = load_portrait_for_search(search)
    assert isinstance(result, Portrait)
    assert result.position_code == "fsm_only"
    assert result.position_name == "FSM Created"
    assert result.must_have_keywords == ["python"]


def test_both_miss_raises_value_error_with_codes() -> None:
    """AC23: both sources empty → ValueError mentioning search_code and position_code."""
    search = _make_search(
        position_code="ghost_role",
        portrait_jsonb={},
        search_code="ghost-1",
    )
    with (
        patch("hh_monitor.fit.portrait_loader.load_all_portraits", return_value={}),
        pytest.raises(ValueError) as exc_info,
    ):
        load_portrait_for_search(search)
    msg = str(exc_info.value)
    assert "ghost-1" in msg
    assert "ghost_role" in msg


def test_both_miss_portrait_none_raises() -> None:
    """portrait jsonb explicitly None (legacy row) is treated as miss."""
    search = _make_search(position_code="ghost", portrait_jsonb=None)
    # _make_search converts None → {}; force None directly:
    search.portrait = None  # type: ignore[assignment]
    with (
        patch("hh_monitor.fit.portrait_loader.load_all_portraits", return_value={}),
        pytest.raises(ValueError),
    ):
        load_portrait_for_search(search)
