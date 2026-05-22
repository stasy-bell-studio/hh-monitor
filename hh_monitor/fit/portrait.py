"""Portrait schema and loader.

Supports both YAML (new format, config/portraits/*.yaml) and JSON (legacy,
stored in searches.portrait column).  The Pydantic model retains legacy
fields for backward compatibility with existing tests and DB-stored portraits.
"""

import json
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

# ── Region / geography filters ────────────────────────────────────────────────


class RegionFilters(BaseModel):
    """Three-tier geography filter used by fit/rules.py.

    primary  — target regions; matching resume scores +weights.region points.
    adjacent — neighbouring regions;          scores +weights.region // 2.
    stop     — forbidden regions; fit_score forced to 0, LLM skipped.
    """

    primary: list[str] = []
    adjacent: list[str] = []
    stop: list[str] = []


class Filters(BaseModel):
    regions: RegionFilters = Field(default_factory=RegionFilters)
    age_range: tuple[int, int] | None = None
    salary_range: tuple[int, int] | None = None  # [min_rub, max_rub]
    education_level: list[str] = []


# ── Weight configuration ──────────────────────────────────────────────────────


class Weights(BaseModel):
    """Per-rule score weights.  Default sum = 90 (leaves room for edge cases)."""

    title_match: int = 25
    experience_keywords: int = 15
    total_experience: int = 20
    salary_fit: int = 10
    education: int = 5
    region: int = 10
    age: int = 5


# ── Main portrait model ───────────────────────────────────────────────────────


class Portrait(BaseModel):
    """Ideal-candidate profile used for rule-based scoring and LLM prompting.

    New fields (YAML portraits):
      search_params, filters, weights,
      stop_words, must_have_keywords, nice_to_have_keywords,
      position_description, evaluation_focus,
      target_companies_override, stop_companies_override

    Legacy fields (backward compat with existing DB portraits and tests):
      title_keywords, experience_keywords, min_total_months,
      preferred_total_months, min_salary, max_salary,
      preferred_education_levels, preferred_areas, age_range
    """

    position_code: str
    position_name: str

    # ── Position context (used by LLM prompt) ────────────────────────────────
    # Free-text description of the role — LLM derives evaluation criteria from
    # it when evaluation_focus is empty.
    position_description: str = ""
    # Optional fixed evaluation questions for this position.
    # Empty → LLM auto-derives 4-6 criteria from position_description.
    evaluation_focus: list[str] = []
    # Per-position company overrides (fall back to _global.yaml if empty)
    target_companies_override: list[str] = []
    stop_companies_override: list[str] = []

    # ── New fields ───────────────────────────────────────────────────────────
    search_params: dict[str, Any] = {}
    filters: Filters = Field(default_factory=Filters)
    weights: Weights = Field(default_factory=Weights)
    stop_words: list[str] = []
    must_have_keywords: list[str] = []
    nice_to_have_keywords: list[str] = []

    # ── Legacy fields (kept for backward compat) ─────────────────────────────
    title_keywords: list[str] = []
    experience_keywords: list[str] = []
    min_total_months: int = 0
    preferred_total_months: int = 24
    min_salary: int | None = None
    max_salary: int | None = None
    preferred_education_levels: list[str] = []
    # preferred_areas → fallback primary regions when filters.regions.primary is empty
    preferred_areas: list[str] = []
    # age_range → fallback when filters.age_range is None
    age_range: tuple[int, int] | None = None


# ── Global market context ─────────────────────────────────────────────────────


class GlobalContext(BaseModel):
    """Global insurance-market context shared across all portraits.

    Loaded from config/portraits/_global.yaml.  Used by the LLM prompt to
    provide company-level signals (target companies, stop companies, market
    narrative) without duplicating them in every position YAML.
    """

    target_companies: list[str] = []
    stop_companies: list[str] = []
    market_context: str = ""


# ── Loaders ───────────────────────────────────────────────────────────────────

_PORTRAITS_DIR = Path(__file__).parent.parent.parent / "config" / "portraits"
_GLOBAL_YAML = _PORTRAITS_DIR / "_global.yaml"


def load_portrait(path: Path | str) -> Portrait:
    """Load and validate a portrait from a YAML or JSON file."""
    p = Path(path)
    raw = p.read_text(encoding="utf-8")
    if p.suffix in (".yaml", ".yml"):
        data: dict[str, Any] = yaml.safe_load(raw)
    else:
        data = json.loads(raw)
    return Portrait.model_validate(data)


def load_global_context(path: Path | str | None = None) -> GlobalContext:
    """Load the global insurance-market context from *path*.

    Falls back to ``config/portraits/_global.yaml`` if *path* is None.
    Returns an empty ``GlobalContext`` if the file does not exist, so callers
    never have to handle a missing file themselves.
    """
    p = Path(path) if path is not None else _GLOBAL_YAML
    if not p.exists():
        return GlobalContext()
    with p.open(encoding="utf-8") as f:
        data: dict[str, Any] = yaml.safe_load(f) or {}
    return GlobalContext.model_validate(data)


def load_all_portraits(portraits_dir: Path | None = None) -> dict[str, Portrait]:
    """Load all *.yaml portraits from *portraits_dir* (default: config/portraits/).

    Files starting with ``_`` (like ``_schema.yaml``) and non-YAML files are
    skipped.  Returns a mapping ``{position_code: Portrait}``.
    """
    directory = Path(portraits_dir) if portraits_dir else _PORTRAITS_DIR
    portraits: dict[str, Portrait] = {}
    for p in sorted(directory.glob("*.yaml")):
        if p.name.startswith("_"):
            continue
        portrait = load_portrait(p)
        portraits[portrait.position_code] = portrait
    return portraits
