"""Rule-based fit scorer for hh.ru resume payloads.

Pure module — no I/O, no side effects except debug-level structlog calls.

Scoring v2 (Lesnitskaya etalon v1, session 5.7):
  1. Eight hard filters run in full — ALL triggered reasons are collected
     (no early return).  If any fire → fit_score=0.
     breakdown["hard_reject_reasons"] — list of all triggered reason codes.
     breakdown["hard_reject_reason"]  — first reason (backward-compatible alias).
  2. Six weighted scored criteria; max achievable raw sum = 45.
  3. fit_score = round(total_raw / 45 * 100), clamped to [0, 100].

Scoring v1 (legacy) — salary fit removed in 5.7 per Lesnitskaya etalon.
  Legacy breakdown keys (title_match, experience_keywords, etc.) are no
  longer computed; the new keys replace them.

Hard reject reason codes (may appear in hard_reject_reasons list):
  "age"                   — candidate age outside portrait.filters.age_range
  "education"             — higher_education_required=True but no higher edu
  "stop_region"           — candidate area in filters.regions.stop
  "forbidden_industry"    — most recent job in portrait.forbidden_industries
  "current_role_unknown"  — portrait has synonyms but experience empty and no
                            resume.title; we cannot determine current role
  "current_role_mismatch" — latest position title does not match portrait
                            synonyms or the manager+branch combo rule;
                            activated only when portrait.position_synonyms ≠ []
  "career_gap"            — longest gap > portrait.max_career_gap_months
  "total_experience"      — total months < portrait.min_total_months
  "insurance_experience"  — insurance months < portrait.min_insurance_experience_months
"""

import re
from datetime import date
from typing import Any

import structlog

from hh_monitor.fit.portrait import Portrait

logger = structlog.get_logger(__name__)

# Education levels considered "higher" for hard-filter purposes
_HIGHER_EDU_IDS: frozenset[str] = frozenset({"higher", "bachelor", "master", "candidate", "doctor"})

# Keyword stems for insurance-related experience detection (substring match)
# "страхов" catches: страхование, страховая, страховой, страховщик, etc.
_INSURANCE_STEMS: frozenset[str] = frozenset(
    {"страхов", "insurance", "осаго", "каско", "дмс", "ифл"}
)

# Regex for agent-network FULL match: covers all inflections of "агентская сеть"
# and "руководство филиалом".  "агентск" matches агентской, агентскую, etc.
_RE_AGENT_FULL = re.compile(r"агентск|руководство\s+филиал", re.IGNORECASE)
# Regex for agent-network PARTIAL match: any word starting with "агент" —
# covers агент, агенты, агентов, агентами, агентах, etc.
# (когда есть "агентск", _RE_AGENT_FULL уже сработал; elif гарантирует no double-count)
_RE_AGENT_PARTIAL = re.compile(r"\bагент", re.IGNORECASE)

# ── current_role_mismatch filter — stem sets ─────────────────────────────────
#
# Activated only when portrait.position_synonyms is non-empty.
# Two-path matching (see _matches_role):
#   (a) portrait synonym / position_name is a substring of the current title
#   (b) title contains one stem from group A AND one from group B
#
# Stems cover Russian inflection via plain substring:
#   "управляющ"        → управляющий, управляющего, управляющим
#   "руководитель"     → руководителя, руководителем
#   "региональн"       → региональный, регионального
#   "отделени"         → отделения, отделении, отделению
#   "представительств" → представительства, представительстве

# Group A — leadership / management role words
_ROLE_GROUP_A: frozenset[str] = frozenset(
    {"директор", "руководитель", "управляющ", "начальник", "региональн", "менеджер"}
)
# Group B — branch / office / subsidiary scope words
_ROLE_GROUP_B: frozenset[str] = frozenset({"филиал", "отделени", "представительств", "офис"})


# ── date helpers ──────────────────────────────────────────────────────────────


def _today() -> date:
    """Return today's date.  Isolated so tests can monkeypatch it."""
    return date.today()


def _parse_ym(s: str | None) -> int | None:
    """Parse 'YYYY-MM' → absolute months (year*12 + month), or None on error."""
    if not isinstance(s, str) or len(s) < 7:
        return None
    try:
        return int(s[:4]) * 12 + int(s[5:7])
    except (ValueError, IndexError):
        return None


def _parse_ym_months(start_str: str, end_str: str | None) -> int | None:
    """Parse two YYYY-MM strings → month delta, or None on error.

    If *end_str* is ``None`` the current month is used (open-ended role).
    Returns ``None`` on any parse error so callers can skip gracefully.
    """
    try:
        s_year, s_month = int(start_str[:4]), int(start_str[5:7])
        if end_str is not None:
            e_year, e_month = int(end_str[:4]), int(end_str[5:7])
        else:
            today = _today()
            e_year, e_month = today.year, today.month
        return (e_year - s_year) * 12 + (e_month - s_month)
    except (ValueError, AttributeError, IndexError, TypeError):
        return None


def _experience_months_fallback(experiences: list[Any]) -> int | None:
    """Sum months from experience[].start/end strings as a fallback.

    hh.ru experience items carry ``start`` (``YYYY-MM``) and ``end``
    (``YYYY-MM`` or ``null`` for the current role) but no pre-computed
    ``months`` field.  This function computes the total from those strings.

    Returns ``None`` if no valid entries could be parsed.
    """
    total = 0
    found = False
    for entry in experiences:
        if not isinstance(entry, dict):
            continue
        start_str = entry.get("start")
        if not isinstance(start_str, str):
            continue
        months = _parse_ym_months(start_str, entry.get("end"))
        if months is not None:
            total += max(0, months)
            found = True
    return total if found else None


def _insurance_experience_months(experiences: list[Any]) -> int:
    """Sum months from experience entries that appear insurance-related.

    An entry is considered insurance-related if any *_INSURANCE_STEMS* keyword
    appears in the concatenated company name, position title, or description.
    """
    total = 0
    for entry in experiences:
        if not isinstance(entry, dict):
            continue
        text = " ".join(
            filter(
                None,
                [
                    entry.get("company", ""),
                    entry.get("position", ""),
                    entry.get("description", ""),
                ],
            )
        ).lower()
        if not any(stem in text for stem in _INSURANCE_STEMS):
            continue
        months = _parse_ym_months(entry.get("start", ""), entry.get("end"))
        if months is not None:
            total += max(0, months)
    return total


def _max_career_gap_months(experiences: list[Any]) -> int:
    """Return the largest gap in months between consecutive experience entries.

    Uses start/end dates; open-ended roles (end=None) use the current month.
    Returns 0 if fewer than two parseable entries.
    """
    today = _today()
    today_abs = today.year * 12 + today.month
    dated: list[tuple[int, int]] = []  # (start_abs, end_abs)

    for entry in experiences:
        if not isinstance(entry, dict):
            continue
        start_abs = _parse_ym(entry.get("start"))
        if start_abs is None:
            continue
        end_str = entry.get("end")
        end_abs = _parse_ym(end_str) if end_str else today_abs
        if end_abs is None:
            end_abs = today_abs
        dated.append((start_abs, max(start_abs, end_abs)))

    if len(dated) < 2:
        return 0

    dated.sort(key=lambda x: x[0])  # oldest first
    max_gap = 0
    for i in range(len(dated) - 1):
        gap = dated[i + 1][0] - dated[i][1]  # next_start - prev_end
        if gap > max_gap:
            max_gap = gap
    return max(0, max_gap)


def _latest_experience(experiences: list[Any]) -> dict[str, Any] | None:
    """Return the most recent experience entry by start date, or None."""
    best: dict[str, Any] | None = None
    best_start = -1
    for entry in experiences:
        if not isinstance(entry, dict):
            continue
        start_abs = _parse_ym(entry.get("start"))
        if start_abs is not None and start_abs > best_start:
            best_start = start_abs
            best = entry
    return best


def _matches_role(title: str, portrait: Portrait) -> bool:
    """Return True if *title* is compatible with the portrait's target role.

    Two matching paths (either is sufficient):

    (a) **Synonym check** — portrait.position_name or any entry in
        portrait.position_synonyms is a case-insensitive substring of *title*.
        Example: "Директор регионального офиса по продажам" matches the synonym
        "Директор регионального офиса".

    (b) **Combo check** — *title* (lowercased) contains at least one stem from
        ``_ROLE_GROUP_A`` (management word) AND at least one from
        ``_ROLE_GROUP_B`` (branch/office word).
        Example: "Управляющий офисом" → "управляющ" ∈ A, "офис" ∈ B → True.

    Russian inflection is handled by stem-based substring search, not regex,
    keeping the implementation dependency-free and easy to extend.
    """
    title_lower = title.lower()

    # Path (a): portrait position_name or any synonym is a substring of title
    if portrait.position_name.lower() in title_lower:
        return True
    for syn in portrait.position_synonyms:
        if syn.lower() in title_lower:
            return True

    # Path (b): combo — at least one management word + one scope word
    has_a = any(stem in title_lower for stem in _ROLE_GROUP_A)
    has_b = any(stem in title_lower for stem in _ROLE_GROUP_B)
    return has_a and has_b


def _region_match(area_name: str, regions: list[str]) -> bool:
    """Return True if any portrait region string is a substring of *area_name*."""
    area_lower = area_name.lower()
    return bool(area_name) and any(r.lower() in area_lower for r in regions)


# ── main scorer ───────────────────────────────────────────────────────────────


def compute(resume_payload: dict[str, Any], portrait: Portrait) -> tuple[int, dict[str, Any]]:
    """Score a resume against a portrait.  Pure function — no side effects.

    Scoring v2 (Lesnitskaya etalon v1):
      1. All eight hard filters run — ALL triggered reasons are collected into
         ``breakdown["hard_reject_reasons"]`` (list).  If any fired → score=0.
         ``breakdown["hard_reject_reason"]`` contains the first reason string
         for backward compatibility with callers that check that key.
      2. Six weighted criteria with a raw max of 45.
      3. fit_score = round(total_raw / 45 * 100), clamped to [0, 100].

    Returns:
        ``(score, breakdown)`` where *score* is in ``[0, 100]``.

        *breakdown* maps criterion name → int points. Hard-rejected resumes
        additionally have:
          ``breakdown["hard_reject_reasons"]`` — list[str] of all triggered codes
          ``breakdown["hard_reject_reason"]``  — str, first element (compat alias)
    """
    breakdown: dict[str, Any] = {}
    resume_id: str = resume_payload.get("id") or resume_payload.get("hh_resume_id", "")
    experiences: list[Any] = resume_payload.get("experience") or []

    # ── Helpers shared across rules ───────────────────────────────────────────
    area_raw = resume_payload.get("area")
    area_name: str = ""
    if isinstance(area_raw, dict):
        area_name = area_raw.get("name") or ""
    elif isinstance(area_raw, str):
        area_name = area_raw

    edu: dict[str, Any] = resume_payload.get("education") or {}
    edu_level_id: str = (edu.get("level") or {}).get("id", "")

    # ── STEP 1: Hard filters — collect ALL triggered reasons ──────────────────
    # All eight filters run regardless of prior triggers; we collect every
    # fired reason so callers can see the full picture in one pass.
    hard_reject_reasons: list[str] = []

    # 1a. Age (only when both portrait and payload provide the value)
    age: int | None = resume_payload.get("age")
    if portrait.filters.age_range is not None and age is not None:
        lo, hi = portrait.filters.age_range
        if not (lo <= age <= hi):
            hard_reject_reasons.append("age")
            logger.debug("fit.hard_reject.age", age=age, range=(lo, hi), resume_id=resume_id)

    # 1b. Higher education required
    if portrait.higher_education_required and edu_level_id not in _HIGHER_EDU_IDS:
        hard_reject_reasons.append("education")
        logger.debug("fit.hard_reject.education", edu_level=edu_level_id, resume_id=resume_id)

    # 1c. Stop region
    if portrait.filters.regions.stop and _region_match(area_name, portrait.filters.regions.stop):
        hard_reject_reasons.append("stop_region")
        logger.debug("fit.hard_reject.stop_region", area=area_name, resume_id=resume_id)

    # 1d. Forbidden industry — check ONLY the most recent experience entry
    if portrait.forbidden_industries:
        latest = _latest_experience(experiences)
        if latest is not None:
            latest_text = " ".join(
                filter(
                    None,
                    [
                        latest.get("company", ""),
                        latest.get("position", ""),
                        latest.get("description", ""),
                    ],
                )
            ).lower()
            for industry in portrait.forbidden_industries:
                if industry.lower() in latest_text:
                    hard_reject_reasons.append("forbidden_industry")
                    logger.debug(
                        "fit.hard_reject.forbidden_industry",
                        industry=industry,
                        resume_id=resume_id,
                    )
                    break  # one match per resume is enough; avoid double-counting

    # 1e. Current role mismatch
    # Only active when portrait.position_synonyms is non-empty — that signals
    # the portrait is configured for role-matching.  Skipped for generic/test
    # portraits with no synonyms to preserve backward compatibility.
    if portrait.position_synonyms:
        _current_title: str | None = None
        if experiences:
            _latest_exp = _latest_experience(experiences)
            if _latest_exp is not None:
                _pos = _latest_exp.get("position")
                if _pos:
                    _current_title = str(_pos).strip() or None
        if not _current_title:
            _raw_title = resume_payload.get("title")
            if _raw_title:
                _current_title = str(_raw_title).strip() or None

        if not _current_title:
            hard_reject_reasons.append("current_role_unknown")
            logger.debug("fit.hard_reject.current_role_unknown", resume_id=resume_id)
        elif not _matches_role(_current_title, portrait):
            hard_reject_reasons.append("current_role_mismatch")
            logger.debug(
                "fit.hard_reject.current_role_mismatch",
                current_role=_current_title,
                resume_id=resume_id,
            )

    # 1f. Career gap
    if portrait.max_career_gap_months > 0:
        max_gap = _max_career_gap_months(experiences)
        if max_gap > portrait.max_career_gap_months:
            hard_reject_reasons.append("career_gap")
            logger.debug(
                "fit.hard_reject.career_gap",
                gap_months=max_gap,
                max_allowed=portrait.max_career_gap_months,
                resume_id=resume_id,
            )

    # 1g. Total experience
    te_raw = resume_payload.get("total_experience")
    total_months: int | None = None
    if isinstance(te_raw, dict) and te_raw.get("months") is not None:
        total_months = int(te_raw["months"])
    else:
        total_months = _experience_months_fallback(experiences)

    if (
        portrait.min_total_months > 0
        and total_months is not None
        and total_months < portrait.min_total_months
    ):
        hard_reject_reasons.append("total_experience")
        logger.debug(
            "fit.hard_reject.total_experience",
            months=total_months,
            required=portrait.min_total_months,
            resume_id=resume_id,
        )

    # 1h. Insurance-specific experience
    if portrait.min_insurance_experience_months > 0:
        ins_months = _insurance_experience_months(experiences)
        if ins_months < portrait.min_insurance_experience_months:
            hard_reject_reasons.append("insurance_experience")
            logger.debug(
                "fit.hard_reject.insurance_experience",
                months=ins_months,
                required=portrait.min_insurance_experience_months,
                resume_id=resume_id,
            )

    # ── If any hard filter fired → return score=0 with full reasons list ──────
    if hard_reject_reasons:
        breakdown["hard_reject_reasons"] = hard_reject_reasons
        breakdown["hard_reject_reason"] = hard_reject_reasons[0]  # backward-compat alias
        return 0, breakdown

    # ── STEP 2: Weighted scored criteria ─────────────────────────────────────
    # Build full searchable text from experience + key_skills
    exp_parts: list[str] = []
    for entry in experiences:
        if isinstance(entry, dict):
            exp_parts.extend(
                filter(
                    None,
                    [
                        entry.get("company", ""),
                        entry.get("position", ""),
                        entry.get("description", ""),
                    ],
                )
            )
    exp_text = " ".join(exp_parts).lower()

    key_skills_raw = resume_payload.get("key_skills") or []
    skills_text = " ".join(
        s["name"] if isinstance(s, dict) else str(s) for s in key_skills_raw
    ).lower()

    full_text = f"{exp_text} {skills_text}"
    w = portrait.weights

    # 2a. Agent network experience (10 full / 5 partial)
    if _RE_AGENT_FULL.search(full_text):
        breakdown["agent_network_experience"] = w.agent_network_experience
    elif _RE_AGENT_PARTIAL.search(full_text):
        breakdown["agent_network_experience"] = w.agent_network_experience // 2
    else:
        breakdown["agent_network_experience"] = 0

    # 2b. ОСАГО / КАСКО knowledge (9)
    breakdown["osago_knowledge"] = (
        w.osago_knowledge if ("осаго" in full_text or "каско" in full_text) else 0
    )

    # 2c. Region score — take max(primary, adjacent), never additive
    primary_pts = (
        w.target_region_primary if _region_match(area_name, portrait.filters.regions.primary) else 0
    )
    adjacent_pts = (
        w.target_region_adjacent
        if _region_match(area_name, portrait.filters.regions.adjacent)
        else 0
    )
    breakdown["region"] = max(primary_pts, adjacent_pts)

    # 2d. ИФЛ experience (7)
    # Match "ифл" or "физических лиц" (covers both nominative "имущество физических лиц"
    # and genitive "имущества физических лиц" common in Russian HR text).
    breakdown["ifl_experience"] = (
        w.ifl_experience if ("ифл" in full_text or "физических лиц" in full_text) else 0
    )

    # 2e. Top-4 competitor experience (6)
    breakdown["top4_competitor_experience"] = (
        w.top4_competitor_experience
        if any(company.lower() in exp_text for company in portrait.bonus_companies)
        else 0
    )

    # 2f. Higher education with relevant specialization (5)
    has_higher_edu = edu_level_id in _HIGHER_EDU_IDS
    primary_edu: list[Any] = edu.get("primary") or []
    spec_text = (
        primary_edu[0].get("name", "").lower()
        if primary_edu and isinstance(primary_edu[0], dict)
        else ""
    )

    # Stem-based match: use first max(5, len-2) chars to handle Russian inflection.
    # "финансы" (7) → "финан" matches "финансовый"; "экономика" (9) → "экономи"
    # matches "экономический"; etc.
    def _field_stem(f: str) -> str:
        return f.lower()[: max(5, len(f) - 2)]

    has_spec_match = bool(portrait.preferred_education_fields) and any(
        _field_stem(field) in spec_text for field in portrait.preferred_education_fields
    )
    breakdown["higher_specialized_education"] = (
        w.higher_specialized_education if (has_higher_edu and has_spec_match) else 0
    )

    # ── STEP 3: Normalize to 0-100 ───────────────────────────────────────────
    _MAX_RAW = 45  # achievable max (primary wins over adjacent; max 8, not 8+4)
    scored_keys = {
        "agent_network_experience",
        "osago_knowledge",
        "region",
        "ifl_experience",
        "top4_competitor_experience",
        "higher_specialized_education",
    }
    total_raw = sum(int(breakdown[k]) for k in scored_keys)
    fit_score = round(total_raw / _MAX_RAW * 100)
    return max(0, min(100, fit_score)), breakdown
