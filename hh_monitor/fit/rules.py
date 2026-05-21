"""Rule-based fit scorer for hh.ru resume payloads.

Pure module — no I/O, no side effects except debug-level structlog calls.
"""

from datetime import date
from typing import Any

import structlog

from hh_monitor.fit.portrait import Portrait

logger = structlog.get_logger(__name__)


# ── date helpers ──────────────────────────────────────────────────────────────


def _today() -> date:
    """Return today's date.  Isolated so tests can monkeypatch it."""
    return date.today()


def _parse_ym_months(start_str: str, end_str: str | None) -> int | None:
    """Parse two ``YYYY-MM`` strings and return the month difference.

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
    """Sum months from ``experience[].start``/``end`` as a fallback.

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


# ── main scorer ───────────────────────────────────────────────────────────────


def compute(resume_payload: dict[str, Any], portrait: Portrait) -> tuple[int, dict[str, int]]:
    """Score a resume against a portrait.  Pure function — no side effects.

    Returns:
        (score, breakdown) where *score* is clamped to [0, 100] and
        *breakdown* maps rule name → point delta.  Rules that cannot be
        evaluated (missing data) are **omitted** from *breakdown* and
        contribute 0 to the score.
    """
    breakdown: dict[str, int] = {}
    score = 0

    # ── Title match (+25) ─────────────────────────────────────────────────
    title: str = (resume_payload.get("title") or "").lower()
    breakdown["title_match"] = (
        25 if any(kw.lower() in title for kw in portrait.title_keywords) else 0
    )

    # ── Experience keywords (+15) ─────────────────────────────────────────
    exp_text = " ".join(
        " ".join(
            filter(
                None,
                [e.get("description", ""), e.get("position", "")],
            )
        )
        for e in (resume_payload.get("experience") or [])
    ).lower()
    breakdown["experience_keywords"] = (
        15 if any(kw.lower() in exp_text for kw in portrait.experience_keywords) else 0
    )

    # ── Total experience (+20 / +10 / -10 / skipped) ─────────────────────
    #
    # Primary source: total_experience.months (pre-computed by hh.ru).
    # Fallback:       sum months from experience[].start/end (YYYY-MM strings).
    # Skip:           if neither source is available — rule omitted from breakdown.
    te_raw = resume_payload.get("total_experience")
    months: int | None = None

    if isinstance(te_raw, dict) and te_raw.get("months") is not None:
        months = int(te_raw["months"])
    else:
        fallback = _experience_months_fallback(resume_payload.get("experience") or [])
        if fallback is not None and fallback > 0:
            months = fallback
        else:
            logger.debug("fit.total_experience.missing", resume_id=resume_payload.get("id"))

    if months is not None:
        if months >= portrait.preferred_total_months:
            breakdown["total_experience"] = 20
        elif months >= portrait.min_total_months:
            breakdown["total_experience"] = 10
        else:
            breakdown["total_experience"] = -10

    # ── Salary fit (+10 / -15 / skipped) ─────────────────────────────────
    #
    # Skip when salary is absent, currency is unknown, or currency ≠ RUR.
    # We never convert foreign currencies (no exchange-rate calls in the PoC).
    salary_raw = resume_payload.get("salary") or {}
    salary_amount: int | None = salary_raw.get("amount") if isinstance(salary_raw, dict) else None
    salary_currency: str | None = (
        salary_raw.get("currency") if isinstance(salary_raw, dict) else None
    )

    if salary_amount is None or salary_currency != "RUR":
        if salary_currency is not None and salary_currency != "RUR":
            logger.debug(
                "fit.salary.non_rur_skipped",
                currency=salary_currency,
                resume_id=resume_payload.get("id"),
            )
        # rule skipped — no key added to breakdown
    else:
        if portrait.max_salary is None or salary_amount <= portrait.max_salary:
            breakdown["salary_fit"] = 10
        else:
            breakdown["salary_fit"] = -15

    # ── Education (+5) ────────────────────────────────────────────────────
    edu_level_id: str = ((resume_payload.get("education") or {}).get("level") or {}).get("id", "")
    preferred_edu = portrait.preferred_education_levels
    breakdown["education"] = 5 if (preferred_edu and edu_level_id in preferred_edu) else 0

    # ── Area (+10) ────────────────────────────────────────────────────────
    #
    # Payload format: area = {"id": str, "name": str, "url": str}.
    # Match logic: any portrait.preferred_areas entry is a substring of the
    # payload area name (case-insensitive).  "Санкт-Петербург" therefore
    # matches "Санкт-Петербург и область".
    area_raw = resume_payload.get("area")
    area_name: str = ""
    if isinstance(area_raw, dict):
        area_name = area_raw.get("name") or ""
    elif area_raw is not None:
        logger.debug(
            "fit.area.unexpected_type",
            type=type(area_raw).__name__,
            resume_id=resume_payload.get("id"),
        )

    area_matched = (
        portrait.preferred_areas
        and area_name
        and any(pa.lower() in area_name.lower() for pa in portrait.preferred_areas)
    )
    breakdown["area"] = 10 if area_matched else 0

    # ── Age (+5) ──────────────────────────────────────────────────────────
    age: int | None = resume_payload.get("age")
    if portrait.age_range is not None and age is not None:
        lo, hi = portrait.age_range
        breakdown["age"] = 5 if lo <= age <= hi else 0
    else:
        breakdown["age"] = 0

    score = sum(breakdown.values())
    return max(0, min(100, score)), breakdown
