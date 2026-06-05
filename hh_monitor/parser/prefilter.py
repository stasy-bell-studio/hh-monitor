"""Pre-filter for hh.ru search list items (Рубеж 3).

Applied BEFORE the metered GET /resumes/{id} call — uses only fields available
in the free search-list response.  Pure function, no I/O, no side effects.

When a required field is absent from the list item the corresponding rule is
skipped and the candidate passes (null-safe, defers the decision to R4).
"""

from __future__ import annotations

from typing import Any

from hh_monitor.fit.portrait import Portrait
from hh_monitor.fit.rules import (
    _HIGHER_EDU_IDS,
    _INSURANCE_STEMS,
    _experience_months_fallback,
)


def apply_prefilter(item: dict[str, Any], portrait: Portrait) -> list[str]:
    """Apply pre-filter rules to a search list item.

    Returns a list of rejection reason codes.  Empty list = item passes all rules.

    Reason codes:
        ``area_id_not_allowed``       — area.id not in prefilter.area_ids_require
        ``area_id_stopped``           — area.id in prefilter.area_ids_stop
        ``age``                       — age outside filters.age_range
        ``total_experience``          — total_experience.months < min_total_months
        ``education``                 — higher_education_required and level not higher
        ``required_industry_missing`` — no experience entry matches required industry
        ``stop_employer``             — experience employer.id in stop_employer_ids
        ``stop_company``              — experience company substring or company_id matches
        ``forbidden_industry``        — experience industry/company matches forbidden_industry_names
    """
    reasons: list[str] = []
    pf = portrait.prefilter

    # ── Area ID ──────────────────────────────────────────────────────────────
    if pf.area_ids_require or pf.area_ids_stop:
        area_raw: dict[str, Any] = item.get("area") or {}
        area_id_str: str = str(area_raw.get("id", ""))
        area_id: int | None
        try:
            area_id = int(area_id_str)
        except (ValueError, TypeError):
            area_id = None

        if area_id is not None:
            if pf.area_ids_require and area_id not in pf.area_ids_require:
                reasons.append("area_id_not_allowed")
            if pf.area_ids_stop and area_id in pf.area_ids_stop:
                reasons.append("area_id_stopped")

    # ── Age ──────────────────────────────────────────────────────────────────
    if portrait.filters.age_range is not None:
        age_raw = item.get("age")
        if age_raw is not None:
            try:
                age = int(age_raw)
            except (ValueError, TypeError):
                age = None
            if age is not None:
                lo, hi = portrait.filters.age_range
                if not (lo <= age <= hi):
                    reasons.append("age")

    # ── Total experience ──────────────────────────────────────────────────────
    if portrait.min_total_months > 0:
        te_raw = item.get("total_experience")
        total_months: int | None = None
        if isinstance(te_raw, dict) and te_raw.get("months") is not None:
            try:
                total_months = int(te_raw["months"])
            except (ValueError, TypeError):
                total_months = None
        else:
            total_months = _experience_months_fallback(item.get("experience") or [])
        if total_months is not None and total_months < portrait.min_total_months:
            reasons.append("total_experience")

    # ── Education ────────────────────────────────────────────────────────────
    if portrait.higher_education_required:
        edu: dict[str, Any] = item.get("education") or {}
        edu_level_id: str = (edu.get("level") or {}).get("id", "") or ""
        if edu_level_id and edu_level_id not in _HIGHER_EDU_IDS:
            reasons.append("education")

    # ── Required industry ────────────────────────────────────────────────────
    if pf.required_industry_ids:
        req_set = set(pf.required_industry_ids)
        experiences: list[Any] = item.get("experience") or []
        found_industry = False
        for exp in experiences:
            if not isinstance(exp, dict):
                continue
            industries = exp.get("industries")
            if industries:
                # Path (a): id prefix match — "43" matches "43.646" and "43.*"
                for ind in industries:
                    if not isinstance(ind, dict):
                        continue
                    ind_id: str = str(ind.get("id", ""))
                    for req in req_set:
                        if ind_id == req or ind_id.startswith(req + "."):
                            found_industry = True
                            break
                    if found_industry:
                        break
                if found_industry:
                    break
                # Path (b): industry name stem fallback
                for ind in industries:
                    if not isinstance(ind, dict):
                        continue
                    ind_name = str(ind.get("name", "")).lower()
                    if any(stem in ind_name for stem in _INSURANCE_STEMS):
                        found_industry = True
                        break
                if found_industry:
                    break
            else:
                # Path (c): no industries — check company+position stems (mirrors R4)
                cp = (
                    f"{exp.get('company', '')} {exp.get('position', '')}".lower()
                )
                if any(stem in cp for stem in _INSURANCE_STEMS):
                    found_industry = True
                    break
        if not found_industry:
            reasons.append("required_industry_missing")

    # ── Stop employer / stop company ──────────────────────────────────────────
    if pf.stop_employer_ids or pf.stop_company_names or pf.stop_company_ids:
        stop_emp_set = set(pf.stop_employer_ids)
        stop_co_id_set = set(pf.stop_company_ids)
        stop_co_names = [n.lower() for n in pf.stop_company_names]
        experiences = item.get("experience") or []
        for exp in experiences:
            if not isinstance(exp, dict):
                continue
            if stop_emp_set:
                emp = exp.get("employer") or {}
                emp_id: str = str(emp.get("id", ""))
                if emp_id and emp_id in stop_emp_set:
                    reasons.append("stop_employer")
                    break
            if stop_co_names:
                company: str = str(exp.get("company", "") or "").lower()
                if company and any(name in company for name in stop_co_names):
                    reasons.append("stop_company")
                    break
            if stop_co_id_set:
                co_id: str = str(exp.get("company_id", "") or "")
                if co_id and co_id in stop_co_id_set:
                    reasons.append("stop_company")
                    break

    # ── Forbidden industry ────────────────────────────────────────────────────
    # hh.ru search-list items include experience[].industries (list of {id, name})
    # when the candidate filled in industry data; falls back to company name string
    # when industries is absent or empty.
    if pf.forbidden_industry_names:
        forbidden_lower = [n.lower() for n in pf.forbidden_industry_names]
        experiences = item.get("experience") or []
        for exp in experiences:
            if not isinstance(exp, dict):
                continue
            industries = exp.get("industries")
            if industries:
                for ind in industries:
                    if not isinstance(ind, dict):
                        continue
                    ind_name = str(ind.get("name", "") or "").lower()
                    if ind_name and any(f in ind_name for f in forbidden_lower):
                        reasons.append("forbidden_industry")
                        break
            else:
                company = str(exp.get("company", "") or "").lower()
                if company and any(f in company for f in forbidden_lower):
                    reasons.append("forbidden_industry")
            if "forbidden_industry" in reasons:
                break

    return reasons
