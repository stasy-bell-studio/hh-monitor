from typing import Any

from hh_monitor.detector.types import DetectedEvent, EventType


def _is_archived(payload: dict[str, Any] | None) -> bool:
    """True when the payload is absent or stripped of all substantive fields.

    hh.ru returns a thin object (only id) when a resume is hidden or removed.
    Criterion: no title AND no experience AND no total_experience.

    TODO: revisit when we have real hh.ru samples for the removed-resume case.
    """
    if payload is None:
        return True
    return (
        not payload.get("title")
        and not payload.get("experience")
        and not payload.get("total_experience")
    )


def diff_snapshots(
    prev: dict[str, Any] | None,
    curr: dict[str, Any] | None,
    hh_resume_id: str,
    curr_snapshot_id: int,
    prev_snapshot_id: int | None = None,
) -> list[DetectedEvent]:
    """Pure function: compare two resume payloads and return detected events.

    Args:
        prev: payload of the older snapshot, or None if first ever.
        curr: payload of the newer snapshot.
        hh_resume_id: resume identifier (stored in every DetectedEvent).
        curr_snapshot_id: DB id of the newer snapshot (stored in details for idempotency).
        prev_snapshot_id: DB id of the older snapshot, if present.

    Returns:
        List of DetectedEvent. May be empty if nothing changed.
        May contain multiple events for a single snapshot pair.
    """
    base: dict[str, Any] = {"curr_snapshot_id": curr_snapshot_id}
    if prev_snapshot_id is not None:
        base["prev_snapshot_id"] = prev_snapshot_id

    prev_archived = _is_archived(prev)
    curr_archived = _is_archived(curr)

    # REMOVED
    if not prev_archived and curr_archived:
        return [
            DetectedEvent(
                event_type=EventType.REMOVED,
                hh_resume_id=hh_resume_id,
                details={**base, "reason": "payload_empty"},
            )
        ]

    # NEW
    if prev_archived and not curr_archived:
        if prev is None:
            return [
                DetectedEvent(
                    event_type=EventType.NEW,
                    hh_resume_id=hh_resume_id,
                    details=base,
                )
            ]
        # prev existed but was archived → REACTIVATED
        return [
            DetectedEvent(
                event_type=EventType.REACTIVATED,
                hh_resume_id=hh_resume_id,
                details={**base, "reason": "previously_archived"},
            )
        ]

    # Both archived → nothing to compare
    if curr_archived:
        return []

    # Both present and full → diff fields
    assert curr is not None and prev is not None  # satisfy type checker
    events: list[DetectedEvent] = []

    # UPDATED_POSITION
    prev_title = prev.get("title")
    curr_title = curr.get("title")
    if prev_title != curr_title:
        events.append(
            DetectedEvent(
                event_type=EventType.UPDATED_POSITION,
                hh_resume_id=hh_resume_id,
                details={**base, "before": prev_title, "after": curr_title},
            )
        )

    # UPDATED_SALARY
    prev_salary = (prev.get("salary") or {}).get("amount")
    curr_salary = (curr.get("salary") or {}).get("amount")
    if prev_salary != curr_salary:
        events.append(
            DetectedEvent(
                event_type=EventType.UPDATED_SALARY,
                hh_resume_id=hh_resume_id,
                details={**base, "before": prev_salary, "after": curr_salary},
            )
        )

    # UPDATED_EXPERIENCE
    prev_months = (prev.get("total_experience") or {}).get("months")
    curr_months = (curr.get("total_experience") or {}).get("months")
    prev_exp = prev.get("experience") or []
    curr_exp = curr.get("experience") or []
    if prev_months != curr_months or prev_exp != curr_exp:
        events.append(
            DetectedEvent(
                event_type=EventType.UPDATED_EXPERIENCE,
                hh_resume_id=hh_resume_id,
                details={
                    **base,
                    "before": {"months": prev_months, "entries": len(prev_exp)},
                    "after": {"months": curr_months, "entries": len(curr_exp)},
                },
            )
        )

    return events
