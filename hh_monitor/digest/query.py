"""Candidate query for digest export.

Fetches one deduplicated row per resume for a given search_code, joining the
latest snapshot payload via a PostgreSQL LATERAL subquery.

The DISTINCT ON (r.hh_resume_id) inner query eliminates duplicates caused by
multiple events (NEW, UPDATED_EXPERIENCE …) for the same resume.  The outer
ORDER BY sorts the final result by score_total DESC.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

# ── SQL ───────────────────────────────────────────────────────────────────────
#
# Structure:
#   Inner  — JOIN resumes → events → searches (filter by search_code)
#             + LATERAL JOIN on snapshots (latest payload per resume)
#             + DISTINCT ON (hh_resume_id) to deduplicate multiple events
#   Outer  — re-sort by score_total DESC NULLS LAST
#
_CANDIDATE_SQL = sa.text(
    """
    SELECT
        hh_resume_id,
        fit_score,
        llm_score,
        llm_verdict,
        llm_comment,
        llm_red_flags,
        score_total,
        screening_status,
        payload
    FROM (
        SELECT DISTINCT ON (r.hh_resume_id)
            r.hh_resume_id,
            r.fit_score,
            r.llm_score,
            r.llm_verdict,
            r.llm_comment,
            r.llm_red_flags,
            r.score_total,
            r.screening_status,
            snap.payload
        FROM resumes r
        JOIN events e  ON e.hh_resume_id = r.hh_resume_id
        JOIN searches sc ON e.search_id = sc.id
        JOIN LATERAL (
            SELECT s.payload
            FROM snapshots s
            WHERE s.hh_resume_id = r.hh_resume_id
            ORDER BY s.fetched_at DESC
            LIMIT 1
        ) snap ON TRUE
        WHERE sc.search_code = :search_code
          AND r.score_total  >= :min_score
          AND (r.screening_status IS NULL OR :include_screened)
          AND NOT r.archived
        ORDER BY r.hh_resume_id
    ) t
    ORDER BY score_total DESC NULLS LAST
    """
)


# ── Data class ────────────────────────────────────────────────────────────────


@dataclass
class CandidateRow:
    """One candidate row ready for export.

    ``payload`` is the raw hh.ru snapshot dict from which current_role,
    region, age, and total_experience are extracted by the exporters.
    """

    hh_resume_id: str
    score_total: int | None
    fit_score: int | None
    llm_score: int | None
    llm_verdict: str | None
    llm_comment: str | None
    llm_red_flags: list[Any] = field(default_factory=list)
    screening_status: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    # ── Derived helpers ───────────────────────────────────────────────────────

    @property
    def url(self) -> str:
        return f"https://hh.ru/resume/{self.hh_resume_id}"

    @property
    def current_role(self) -> str:
        """Latest experience position, fallback to resume title."""
        experiences: list[Any] = self.payload.get("experience") or []
        if experiences:
            exp_list = [e for e in experiences if isinstance(e, dict)]
            if exp_list:
                latest = max(exp_list, key=lambda e: str(e.get("start") or ""))
                pos = latest.get("position") or ""
                if pos:
                    return str(pos)
        return str(self.payload.get("title") or "")

    @property
    def region(self) -> str:
        area = self.payload.get("area")
        if isinstance(area, dict):
            return str(area.get("name") or "")
        return str(area or "")

    @property
    def age(self) -> int | None:
        val = self.payload.get("age")
        return int(val) if val is not None else None

    @property
    def total_exp_months(self) -> int | None:
        te = self.payload.get("total_experience")
        if isinstance(te, dict):
            m = te.get("months")
            return int(m) if m is not None else None
        return None

    @property
    def red_flags_str(self) -> str:
        if not self.llm_red_flags:
            return ""
        return "; ".join(str(f) for f in self.llm_red_flags)


# ── Query ─────────────────────────────────────────────────────────────────────


async def fetch_candidates(
    session: AsyncSession,
    search_code: str,
    min_score: int = 60,
    include_screened: bool = False,
) -> list[CandidateRow]:
    """Return candidates for *search_code* that meet the score threshold.

    Args:
        session:         Async DB session (caller owns lifecycle).
        search_code:     Value of searches.search_code to filter by.
        min_score:       Minimum score_total; rows below are excluded.
        include_screened: If False (default), exclude already-screened resumes
                          (screening_status IS NOT NULL).

    Returns:
        List of :class:`CandidateRow` sorted by score_total DESC.
        Empty list if search_code not found or no qualifying resumes.
    """
    result = await session.execute(
        _CANDIDATE_SQL,
        {
            "search_code": search_code,
            "min_score": min_score,
            "include_screened": include_screened,
        },
    )
    rows = result.mappings().all()

    candidates: list[CandidateRow] = []
    for row in rows:
        # JSONB payload may arrive as a dict or a JSON string depending on driver
        raw_payload = row["payload"]
        if isinstance(raw_payload, str):
            payload: dict[str, Any] = json.loads(raw_payload)
        else:
            payload = dict(raw_payload) if raw_payload else {}

        # llm_red_flags is JSONB — may be a list, dict, or None
        raw_flags = row["llm_red_flags"]
        if isinstance(raw_flags, list):
            red_flags: list[Any] = raw_flags
        elif raw_flags is None:
            red_flags = []
        else:
            red_flags = [raw_flags]

        candidates.append(
            CandidateRow(
                hh_resume_id=row["hh_resume_id"],
                score_total=row["score_total"],
                fit_score=row["fit_score"],
                llm_score=row["llm_score"],
                llm_verdict=row["llm_verdict"],
                llm_comment=row["llm_comment"],
                llm_red_flags=red_flags,
                screening_status=row["screening_status"],
                payload=payload,
            )
        )
    return candidates
