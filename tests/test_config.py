"""Tests for hh_monitor.config.Settings."""

from __future__ import annotations

from hh_monitor.config import Settings


def test_score_fit_min_for_llm_default_is_zero() -> None:
    """The LLM fit-gate has an explicit in-code default of 0 (not env-only) — a
    missing env var keeps the gate at 0 (no filtering), never undefined (P2-6)."""
    assert Settings.model_fields["score_fit_min_for_llm"].default == 0
