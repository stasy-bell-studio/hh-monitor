"""Portrait schema and loader.

Supports both YAML (new format, config/portraits/*.yaml) and JSON (legacy,
stored in searches.portrait column).  The Pydantic model retains legacy
fields for backward compatibility with existing tests and DB-stored portraits.
"""

import json
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

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


class PrefilterConfig(BaseModel):
    """Pre-filter settings applied to search list items before the metered GET /resumes/{id}.

    Rules use only fields available in the free search-list response.
    Empty lists disable the corresponding rule (backward-compatible default).
    """

    area_ids_require: list[int] = []
    area_ids_stop: list[int] = []
    required_industry_ids: list[str] = []
    stop_company_names: list[str] = []
    stop_employer_ids: list[str] = []
    stop_company_ids: list[str] = []


# ── Weight configuration ──────────────────────────────────────────────────────


class Weights(BaseModel):
    """Per-rule score weights.

    Legacy fields (used by old rules.py, max theoretical sum = 90):
      title_match, experience_keywords, total_experience, salary_fit,
      education, region, age

    New fields (Lesnitskaya etalon v1, used by new fit/rules.py):
      agent_network_experience (10), osago_knowledge (9),
      target_region_primary (8), target_region_adjacent (4),
      ifl_experience (7), top4_competitor_experience (6),
      higher_specialized_education (5).
      NOTE: region score = max(target_region_primary, target_region_adjacent).

    CC-16b: the achievable max is DYNAMIC, not a fixed 45.  The base of six
    criteria sums to 10+9+8+7+6+5 = 45; insurance_experience (2g, soft mode)
    and motor_experience (2h, motor_experience_preferred) are added to the
    denominator only when those criteria are active for the portrait.
    """

    # ── Legacy weights (kept for backward compat) ────────────────────────────
    title_match: int = 25
    experience_keywords: int = 15
    total_experience: int = 20
    salary_fit: int = 10  # removed from new formula per Lesnitskaya etalon 5.7
    education: int = 5
    region: int = 10
    age: int = 5

    # ── New etalon weights (Lesnitskaya v1) ──────────────────────────────────
    agent_network_experience: int = 10
    osago_knowledge: int = 9
    target_region_primary: int = 8
    target_region_adjacent: int = 4
    ifl_experience: int = 7
    top4_competitor_experience: int = 6
    higher_specialized_education: int = 5
    # CC-16b scored criteria weights (graduated; see fit/rules.py 2g/2h).
    # insurance_experience: ≥36mo → full, ≥12mo → half, else 0.
    insurance_experience: int = 12
    # motor_experience: ≥24mo → full, ≥12mo → half, else 0.
    motor_experience: int = 6
    # Raw points deducted from total when forbidden_industry_mode="soft" fires.
    forbidden_industry_soft_penalty: int = 9
    # Raw points deducted from total when role_match_mode="soft" fires and
    # role is confirmed mismatched (unknown role is NOT penalized).
    role_mismatch_soft_penalty: int = 9


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

    model_config = ConfigDict(extra="forbid")

    position_code: str
    position_name: str

    # ── Position context (used by LLM prompt) ────────────────────────────────
    # Free-text description of the role — LLM derives evaluation criteria from
    # it when evaluation_focus is empty.
    position_description: str = ""
    # Optional fixed evaluation questions for this position.
    # Empty → LLM auto-derives 4-6 criteria from position_description.
    evaluation_focus: list[str] = []
    # Static critic lens injected into the LLM system prompt when
    # searches.llm_critic_prompt is empty.  Multi-line string of red-flag patterns.
    critic_lens: str = ""
    # Per-position company overrides (fall back to _global.yaml if empty)
    target_companies_override: list[str] = []
    stop_companies_override: list[str] = []

    # ── Search / hh.ru params ────────────────────────────────────────────────
    search_params: dict[str, Any] = {}
    # Synonyms used to build the hh.ru text= query (OR-joined with position_name).
    # Order = priority; parser uses first 5 to stay within hh.ru ~256-char limit.
    position_synonyms: list[str] = []
    # Filter period= in hh.ru API (days since last update); 0 = no filter.
    resume_freshness_days: int = 0

    # ── Scoring structure ────────────────────────────────────────────────────
    filters: Filters = Field(default_factory=Filters)
    prefilter: PrefilterConfig = Field(default_factory=PrefilterConfig)
    weights: Weights = Field(default_factory=Weights)
    # "soft" (default) → signal recorded in breakdown, candidate not skipped.
    # "hard" → restores old hard-reject behaviour for that signal.
    role_match_mode: Literal["soft", "hard"] = "soft"
    forbidden_industry_mode: Literal["soft", "hard"] = "soft"
    # CC-16b: "soft" (default) → insurance is NOT a hard gate; scored as criterion
    # 2g.  "hard" → hard gate 1h active; 2g disabled (mutually exclusive).
    insurance_experience_mode: Literal["soft", "hard"] = "soft"
    # "cap" (default) → governor active: off-domain scores capped to floor.
    # "off" → governor disabled: score_total returned unchanged regardless of insurance_domain.
    domain_governor_mode: Literal["cap", "off"] = "cap"
    stop_words: list[str] = []
    must_have_keywords: list[str] = []
    nice_to_have_keywords: list[str] = []

    # ── Experience requirements ──────────────────────────────────────────────
    # Minimum months of insurance-specific experience (separate from total_exp).
    min_insurance_experience_months: int = 0
    # Minimum months of motor (КАСКО/ОСАГО/МТПЛ) insurance experience; 0 = disabled.
    min_motor_experience_months: int = 0
    # Whether motor insurance (КАСКО/ОСАГО) experience is preferred (soft, not hard filter).
    motor_experience_preferred: bool = False
    # Minimum months at most recent job; fewer = red_flag for LLM.
    min_tenure_last_job_months: int = 0
    # Max allowed career gap in months; 0 = no restriction.
    max_career_gap_months: int = 0

    # ── Hard filters — demographics / education ──────────────────────────────
    # If True, candidates without higher education are hard-rejected (fit_score=0).
    higher_education_required: bool = False
    # Preferred specialisation areas for bonus in higher_specialized_education criterion.
    preferred_education_fields: list[str] = []
    # Required citizenship; None = no restriction.
    citizenship: str | None = None

    # ── Company signals ──────────────────────────────────────────────────────
    # Subset of _global.yaml target_companies that give a fit_score boost.
    bonus_companies: list[str] = []
    # Industries that trigger hard-reject (стоп-сигнал verdict) when found in
    # most recent experience entry.  Values are case-insensitive substrings.
    forbidden_industries: list[str] = []

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
