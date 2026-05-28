"""Tests for hh_monitor.regions.expander — V1 macro recognition."""

from __future__ import annotations

import structlog.testing

from hh_monitor.regions.expander import PRIMARY_AREA_IDS_21VEK, expand_region_names


def test_expander_recognizes_canonical_macro() -> None:
    result = expand_region_names(["все регионы 21 века"])
    assert result == list(PRIMARY_AREA_IDS_21VEK)
    assert len(result) == 27


def test_expander_case_and_whitespace_insensitive() -> None:
    result = expand_region_names(["  21 ВЕК  ", "  21VEK"])
    assert len(result) == 27
    assert result == list(PRIMARY_AREA_IDS_21VEK)


def test_expander_dedupes_multiple_macros() -> None:
    result = expand_region_names(["21vek", "21 век", "все регионы 21 века"])
    assert result == list(PRIMARY_AREA_IDS_21VEK)


def test_expander_unknown_name_warns_and_skips() -> None:
    with structlog.testing.capture_logs() as captured:
        result = expand_region_names(["Хабаровск"])
    assert result == []
    assert len(captured) == 1
    assert captured[0]["event"] == "regions.unknown_name"
    assert captured[0]["name"] == "Хабаровск"


def test_expander_empty_input() -> None:
    with structlog.testing.capture_logs() as captured:
        result = expand_region_names([])
    assert result == []
    assert captured == []


def test_expander_mixed_macro_and_unknown() -> None:
    with structlog.testing.capture_logs() as captured:
        result = expand_region_names(["21vek", "Камчатка"])
    assert len(result) == 27
    assert result == list(PRIMARY_AREA_IDS_21VEK)
    assert len(captured) == 1
    assert captured[0]["event"] == "regions.unknown_name"
    assert captured[0]["name"] == "Камчатка"
