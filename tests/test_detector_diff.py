"""100% coverage target for hh_monitor.detector.diff."""

import json
from pathlib import Path

from hh_monitor.detector.diff import diff_snapshots
from hh_monitor.detector.types import EventType

_F = Path(__file__).parent / "fixtures" / "resumes"


def _load(name: str) -> dict:  # type: ignore[type-arg]
    return json.loads((_F / name).read_text())


A_V1 = _load("candidate_a_v1.json")
A_V2 = _load("candidate_a_v2.json")
A_V3 = _load("candidate_a_v3.json")
B_V1 = _load("candidate_b_v1.json")
B_V2 = _load("candidate_b_v2.json")
C_V1 = _load("candidate_c_v1.json")


# ── NEW ────────────────────────────────────────────────────────────────────


def test_new_when_prev_is_none() -> None:
    events = diff_snapshots(None, A_V1, "test_a", curr_snapshot_id=1)
    assert len(events) == 1
    assert events[0].event_type == EventType.NEW
    assert events[0].hh_resume_id == "test_a"
    assert events[0].details["curr_snapshot_id"] == 1
    assert "prev_snapshot_id" not in events[0].details


def test_new_stores_snapshot_id() -> None:
    events = diff_snapshots(None, C_V1, "test_c", curr_snapshot_id=99)
    assert events[0].details["curr_snapshot_id"] == 99


# ── REMOVED ────────────────────────────────────────────────────────────────


def test_removed_when_curr_is_archived() -> None:
    events = diff_snapshots(A_V1, A_V3, "test_a", curr_snapshot_id=2, prev_snapshot_id=1)
    assert len(events) == 1
    assert events[0].event_type == EventType.REMOVED
    assert events[0].details["reason"] == "payload_empty"
    assert events[0].details["prev_snapshot_id"] == 1
    assert events[0].details["curr_snapshot_id"] == 2


def test_removed_when_curr_is_none() -> None:
    events = diff_snapshots(A_V1, None, "test_a", curr_snapshot_id=3, prev_snapshot_id=1)
    assert len(events) == 1
    assert events[0].event_type == EventType.REMOVED


# ── REACTIVATED ────────────────────────────────────────────────────────────


def test_reactivated_when_prev_was_archived() -> None:
    events = diff_snapshots(A_V3, A_V2, "test_a", curr_snapshot_id=4, prev_snapshot_id=2)
    assert len(events) == 1
    assert events[0].event_type == EventType.REACTIVATED
    assert events[0].details["reason"] == "previously_archived"


# ── UPDATED_POSITION ───────────────────────────────────────────────────────


def test_updated_position() -> None:
    events = diff_snapshots(A_V1, A_V2, "test_a", curr_snapshot_id=5, prev_snapshot_id=1)
    types = {e.event_type for e in events}
    assert EventType.UPDATED_POSITION in types
    pos_event = next(e for e in events if e.event_type == EventType.UPDATED_POSITION)
    assert pos_event.details["before"] == "Руководитель отдела продаж"
    assert pos_event.details["after"] == "Директор филиала"


# ── UPDATED_SALARY ─────────────────────────────────────────────────────────


def test_updated_salary() -> None:
    events = diff_snapshots(A_V1, A_V2, "test_a", curr_snapshot_id=5, prev_snapshot_id=1)
    types = {e.event_type for e in events}
    assert EventType.UPDATED_SALARY in types
    sal_event = next(e for e in events if e.event_type == EventType.UPDATED_SALARY)
    assert sal_event.details["before"] == 180000
    assert sal_event.details["after"] == 220000


# ── UPDATED_EXPERIENCE ─────────────────────────────────────────────────────


def test_updated_experience_months() -> None:
    events = diff_snapshots(A_V1, A_V2, "test_a", curr_snapshot_id=5, prev_snapshot_id=1)
    types = {e.event_type for e in events}
    assert EventType.UPDATED_EXPERIENCE in types
    exp_event = next(e for e in events if e.event_type == EventType.UPDATED_EXPERIENCE)
    assert exp_event.details["before"]["months"] == 96
    assert exp_event.details["after"]["months"] == 108


def test_updated_experience_entry_count() -> None:
    # A_V2 has 3 experience entries vs A_V1's 2 — should trigger even if months unchanged
    modified = {**A_V1, "total_experience": {"months": 108}}
    events = diff_snapshots(modified, A_V2, "test_a", curr_snapshot_id=6, prev_snapshot_id=1)
    types = {e.event_type for e in events}
    assert EventType.UPDATED_EXPERIENCE in types


# ── MULTIPLE EVENTS ────────────────────────────────────────────────────────


def test_multiple_events_from_a_v1_to_a_v2() -> None:
    events = diff_snapshots(A_V1, A_V2, "test_a", curr_snapshot_id=5, prev_snapshot_id=1)
    types = {e.event_type for e in events}
    assert types == {
        EventType.UPDATED_POSITION,
        EventType.UPDATED_SALARY,
        EventType.UPDATED_EXPERIENCE,
    }


# ── NO CHANGES ─────────────────────────────────────────────────────────────


def test_no_changes_returns_empty() -> None:
    events = diff_snapshots(B_V1, B_V2, "test_b", curr_snapshot_id=7, prev_snapshot_id=6)
    assert events == []


# ── BOTH ARCHIVED ─────────────────────────────────────────────────────────


def test_both_archived_returns_empty() -> None:
    events = diff_snapshots(A_V3, A_V3, "test_a", curr_snapshot_id=8, prev_snapshot_id=7)
    assert events == []


# ── EDGE CASES ─────────────────────────────────────────────────────────────


def test_missing_salary_field() -> None:
    no_sal = {k: v for k, v in A_V1.items() if k != "salary"}
    events = diff_snapshots(no_sal, A_V2, "test_a", curr_snapshot_id=9, prev_snapshot_id=1)
    sal_event = next((e for e in events if e.event_type == EventType.UPDATED_SALARY), None)
    assert sal_event is not None
    assert sal_event.details["before"] is None


def test_none_to_archived_curr_returns_empty() -> None:
    events = diff_snapshots(None, A_V3, "test_a", curr_snapshot_id=10)
    # prev=None archived, curr=archived → NEW path not taken, both archived → []
    assert events == []


def test_snapshot_ids_always_in_details() -> None:
    for evs in [
        diff_snapshots(None, A_V1, "test_a", curr_snapshot_id=1),
        diff_snapshots(A_V1, A_V3, "test_a", curr_snapshot_id=2, prev_snapshot_id=1),
        diff_snapshots(A_V3, A_V2, "test_a", curr_snapshot_id=3, prev_snapshot_id=2),
        diff_snapshots(A_V1, A_V2, "test_a", curr_snapshot_id=4, prev_snapshot_id=1),
    ]:
        for ev in evs:
            assert "curr_snapshot_id" in ev.details
