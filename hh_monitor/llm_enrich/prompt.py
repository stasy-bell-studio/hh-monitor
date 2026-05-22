"""Prompt assembly and LLM response schema for resume enrichment.

Prompt template: config/portraits/prompt_template.j2
Response schema: LlmResponse (Pydantic)
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

import jinja2
from pydantic import BaseModel, Field, field_validator

# ── Paths ─────────────────────────────────────────────────────────────────────

_TEMPLATE_PATH = Path(__file__).parent.parent.parent / "config" / "portraits" / "prompt_template.j2"

_jinja_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(_TEMPLATE_PATH.parent)),
    autoescape=False,
    trim_blocks=True,
    lstrip_blocks=True,
    undefined=jinja2.StrictUndefined,
)

# ── Response schema ───────────────────────────────────────────────────────────

VALID_VERDICTS = frozenset({"strong_yes", "yes", "maybe", "no", "strong_no"})

VerdictLiteral = Literal["strong_yes", "yes", "maybe", "no", "strong_no"]


class LlmResponse(BaseModel):
    """Parsed and validated LLM response for a single resume."""

    llm_score: int = Field(ge=0, le=100)
    llm_verdict: VerdictLiteral
    llm_comment: str = ""
    llm_red_flags: list[str] = Field(default_factory=list)
    llm_real_role: str = ""

    @field_validator("llm_score", mode="before")
    @classmethod
    def _coerce_score(cls, v: Any) -> int:
        """Accept numeric strings and floats; clamp to [0, 100]."""
        return max(0, min(100, int(float(v))))


# ── Prompt builder ────────────────────────────────────────────────────────────


def build_prompt(resume_payload: dict[str, Any], portrait: Any) -> str:
    """Render the Jinja2 prompt template with *resume_payload* and *portrait*.

    *portrait* is a Portrait instance.  The resume JSON is pretty-printed and
    stripped of keys that add noise but no signal for HR purposes.
    """
    _SKIP_KEYS = frozenset({"actions", "photo", "negotiations_history"})
    cleaned = {k: v for k, v in resume_payload.items() if k not in _SKIP_KEYS}
    resume_json = json.dumps(cleaned, ensure_ascii=False, indent=2)
    tmpl = _jinja_env.get_template(_TEMPLATE_PATH.name)
    return tmpl.render(portrait=portrait, resume_json=resume_json)


# ── Response parser ───────────────────────────────────────────────────────────

_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


def parse_response(raw: str) -> LlmResponse:
    """Parse LLM text output into a validated LlmResponse.

    Strategy:
    1. Try to parse the whole string as JSON.
    2. If that fails, extract the first {...} block via regex and try again.
    3. Validate with Pydantic (raises ValidationError on schema mismatch).
    """
    raw = raw.strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        m = _JSON_BLOCK_RE.search(raw)
        if not m:
            raise ValueError(
                f"No JSON object found in LLM response: {raw[:200]!r}"
            ) from exc
        data = json.loads(m.group(0))
    return LlmResponse.model_validate(data)
