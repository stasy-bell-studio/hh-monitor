"""Tests for _ALLOWED_KEYS, _ALLOWED_FILTER_KEYS and _PARSE_PROMPT."""

from __future__ import annotations

from hh_monitor.tg.add_vacancy.llm import _ALLOWED_FILTER_KEYS, _ALLOWED_KEYS, _PARSE_PROMPT

# Fields that must be invited in the prompt and present in _ALLOWED_KEYS.
# target_companies_override is intentionally excluded: it stays in _ALLOWED_KEYS for the
# merge step in parse_to_portrait_dict() but is NOT invited in the prompt.
_NEW_FIELDS = {
    "resume_freshness_days",
    "min_total_months",
    "stop_companies_override",
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
    forbidden_block = _PARSE_PROMPT[start:end_offset] if end_offset != -1 else _PARSE_PROMPT[start:]
    for field in _NEW_FIELDS:
        assert field not in forbidden_block, (
            f"'{field}' must NOT appear in the forbidden-keys sentence"
        )


# ── AC1 / AC2 / AC3: age_range and target_companies_override hardening ─────────


def test_age_range_not_in_allowed_filter_keys() -> None:
    """AC2: age_range stripped from filters before validation."""
    assert "age_range" not in _ALLOWED_FILTER_KEYS


def test_age_range_not_invited_in_prompt() -> None:
    """AC1: prompt contains no invitation to populate age_range."""
    # The forbidden-block mention ("age_range (на верхнем уровне)") is an anti-invitation;
    # we verify there is no field-definition line that invites LLM to fill age_range.
    # A definition line starts with "- age_range:" in the prompt body.
    assert "- age_range:" not in _PARSE_PROMPT


def test_target_companies_override_not_in_prompt() -> None:
    """AC3: prompt contains no invitation for target_companies_override."""
    assert "target_companies_override" not in _PARSE_PROMPT


def test_target_companies_override_still_in_allowed_keys() -> None:
    """Merge logic in parse_to_portrait_dict() depends on tco passing _strip_forbidden."""
    assert "target_companies_override" in _ALLOWED_KEYS
