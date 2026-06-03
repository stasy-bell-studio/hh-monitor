"""Tests for Portrait extra="forbid" and YAML portrait loading."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from hh_monitor.fit.portrait import Portrait


def test_portrait_rejects_unknown_top_level_key() -> None:
    with pytest.raises(ValidationError) as exc_info:
        Portrait.model_validate(
            {
                "position_code": "x",
                "position_name": "X",
                "stop_companies": ["a"],
            }
        )
    assert "stop_companies" in str(exc_info.value)
