from typing import Any

from hh_monitor.fit.portrait import Portrait


def compute(resume_payload: dict[str, Any], portrait: Portrait) -> tuple[int, dict[str, int]]:
    """Score a resume against a portrait. Pure function — no side effects.

    Returns:
        (score, breakdown) where score is clamped to [0, 100] and
        breakdown maps rule name → point delta.
    """
    breakdown: dict[str, int] = {}
    score = 0

    # ── Title match (+25) ─────────────────────────────────────────────────
    title: str = (resume_payload.get("title") or "").lower()
    if any(kw.lower() in title for kw in portrait.title_keywords):
        breakdown["title_match"] = 25
    else:
        breakdown["title_match"] = 0

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
    if any(kw.lower() in exp_text for kw in portrait.experience_keywords):
        breakdown["experience_keywords"] = 15
    else:
        breakdown["experience_keywords"] = 0

    # ── Total experience (+20 / +10 / -10) ───────────────────────────────
    months: int = (resume_payload.get("total_experience") or {}).get("months") or 0
    if months >= portrait.preferred_total_months:
        breakdown["total_experience"] = 20
    elif months >= portrait.min_total_months:
        breakdown["total_experience"] = 10
    else:
        breakdown["total_experience"] = -10

    # ── Salary fit (+10 / -15) ────────────────────────────────────────────
    salary_amount: int | None = (resume_payload.get("salary") or {}).get("amount")
    if salary_amount is None or portrait.max_salary is None or salary_amount <= portrait.max_salary:
        breakdown["salary_fit"] = 10
    else:
        breakdown["salary_fit"] = -15

    # ── Education (+5) ────────────────────────────────────────────────────
    edu_level_id: str = ((resume_payload.get("education") or {}).get("level") or {}).get("id", "")
    if portrait.preferred_education_levels and edu_level_id in portrait.preferred_education_levels:
        breakdown["education"] = 5
    else:
        breakdown["education"] = 0

    # ── Area (+10) ────────────────────────────────────────────────────────
    area_name: str = (resume_payload.get("area") or {}).get("name", "")
    if portrait.preferred_areas and area_name in portrait.preferred_areas:
        breakdown["area"] = 10
    else:
        breakdown["area"] = 0

    # ── Age (+5) ──────────────────────────────────────────────────────────
    age: int | None = resume_payload.get("age")
    if portrait.age_range is not None and age is not None:
        lo, hi = portrait.age_range
        breakdown["age"] = 5 if lo <= age <= hi else 0
    else:
        breakdown["age"] = 0

    score = sum(breakdown.values())
    return max(0, min(100, score)), breakdown
