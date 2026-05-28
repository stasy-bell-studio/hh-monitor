"""Tests for _ALLOWED_KEYS and _PARSE_PROMPT in hh_monitor.tg.add_vacancy.llm."""

from __future__ import annotations

from hh_monitor.tg.add_vacancy.llm import _ALLOWED_KEYS, _PARSE_PROMPT

_NEW_FIELDS = {
    "resume_freshness_days",
    "min_total_months",
    "stop_companies_override",
    "target_companies_override",
}


def test_allowed_keys_contains_new_fields() -> None:
    assert _NEW_FIELDS <= _ALLOWED_KEYS


def test_parse_prompt_whitelist_mentions_new_fields() -> None:
    for field in _NEW_FIELDS:
        assert field in _PARSE_PROMPT, f"'{field}' missing from _PARSE_PROMPT whitelist"


def test_parse_prompt_forbidden_block_does_not_mention_new_fields() -> None:
    marker = "НЕ возвращай ключи"
    start = _PARSE_PROMPT.index(marker)
    # Slice to the next blank line (or end of string)
    end_offset = _PARSE_PROMPT.find("\n\n", start)
    forbidden_block = (
        _PARSE_PROMPT[start:end_offset] if end_offset != -1 else _PARSE_PROMPT[start:]
    )
    for field in _NEW_FIELDS:
        assert field not in forbidden_block, (
            f"'{field}' must NOT appear in the forbidden-keys sentence"
        )
