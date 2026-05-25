"""Candidate query for digest export.

Fetches one deduplicated row per resume for a given search_code, joining the
latest snapshot payload via a PostgreSQL LATERAL subquery and picking the most
recent event per resume (to capture dossier fields from the latest enrichment).

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
#             + DISTINCT ON (hh_resume_id) ORDER BY hh_resume_id, e.created_at DESC
#               → picks the most recent event per resume (for dossier fields)
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
        payload,
        ev_llm_facts_confirmed,
        ev_llm_weak_spots,
        ev_llm_red_flags,
        ev_llm_interview_questions,
        ev_llm_verdict
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
            snap.payload,
            e.llm_facts_confirmed  AS ev_llm_facts_confirmed,
            e.llm_weak_spots       AS ev_llm_weak_spots,
            e.llm_red_flags        AS ev_llm_red_flags,
            e.llm_interview_questions AS ev_llm_interview_questions,
            e.llm_verdict          AS ev_llm_verdict
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
        ORDER BY r.hh_resume_id, e.created_at DESC
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

    The ``llm_*`` fields prefixed with no qualifier come from ``resumes``
    (backward-compat for TG bot / old enriched records).  The ``dossier_*``
    fields come from the latest ``events`` row (commit 9.3+ enrichment).
    """

    hh_resume_id: str
    score_total: int | None
    fit_score: int | None
    llm_score: int | None
    # Structured verdict class from resumes (подходит/спорно/мимо) — backward compat
    llm_verdict: str | None
    # Short prose comment from resumes — used as fallback by PDF/card template
    llm_comment: str | None
    # Old JSONB list of red flag strings from resumes — fallback for xlsx
    llm_red_flags: list[Any] = field(default_factory=list)
    screening_status: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    # ── Dossier fields (events, commit 9.3+) — None for pre-9.3 records ──────
    dossier_facts_confirmed: str | None = None
    dossier_weak_spots: str | None = None
    dossier_red_flags: str | None = None
    dossier_interview_questions: list[str] | None = None
    dossier_verdict: str | None = None

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
        """Fallback red-flags text for xlsx: new dossier text or old list."""
        if self.dossier_red_flags:
            return self.dossier_red_flags
        if not self.llm_red_flags:
            return ""
        return "; ".join(str(f) for f in self.llm_red_flags)

    @property
    def has_dossier(self) -> bool:
        """True if all 5 structured dossier fields are present."""
        return (
            self.dossier_facts_confirmed is not None
            and self.dossier_weak_spots is not None
            and self.dossier_red_flags is not None
            and self.dossier_interview_questions is not None
            and self.dossier_verdict is not None
        )

    @property
    def interview_questions_str(self) -> str:
        """Dossier interview questions joined for xlsx display."""
        if not self.dossier_interview_questions:
            return ""
        return "; ".join(self.dossier_interview_questions)


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

        # resumes.llm_red_flags is JSONB list
        raw_flags = row["llm_red_flags"]
        if isinstance(raw_flags, list):
            red_flags: list[Any] = raw_flags
        elif raw_flags is None:
            red_flags = []
        else:
            red_flags = [raw_flags]

        # events.llm_interview_questions is JSONB — may be list or None
        raw_iq = row["ev_llm_interview_questions"]
        dossier_iq: list[str] | None = None
        if isinstance(raw_iq, list):
            dossier_iq = [str(q) for q in raw_iq]
        elif raw_iq is not None:
            dossier_iq = [str(raw_iq)]

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
                dossier_facts_confirmed=row["ev_llm_facts_confirmed"],
                dossier_weak_spots=row["ev_llm_weak_spots"],
                dossier_red_flags=row["ev_llm_red_flags"],
                dossier_interview_questions=dossier_iq,
                dossier_verdict=row["ev_llm_verdict"],
            )
        )
    return candidates
