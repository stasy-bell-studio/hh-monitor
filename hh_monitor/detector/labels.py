from __future__ import annotations

from typing import Any

from hh_monitor.detector.types import EventType


def fmt_change_value(v: object) -> str:
    """Render a before/after value for display; None → em dash."""
    return "—" if v is None else str(v)


def describe_change(event_type: str, details: dict[str, Any] | None) -> str:
    """Human-readable «что менялось» for one event.

    Covers every event type — not just UPDATED_*. NEW/REACTIVATED/REMOVED carry no
    before/after in ``details`` (so we never index details["before"] for them); unknown
    types yield "" rather than raising.

    Single source of truth for the event_type → label mapping, shared by the weekly
    digest (history sheet) and the Telegram «✏️ Обновлено» card line.
    """
    d = details or {}
    if event_type == EventType.NEW.value:
        return "Новое резюме"
    if event_type == EventType.REACTIVATED.value:
        return "Возобновлено"
    if event_type == EventType.REMOVED.value:
        return "Снято"
    if event_type in (EventType.UPDATED_POSITION.value, EventType.UPDATED_SALARY.value):
        return f"{fmt_change_value(d.get('before'))} → {fmt_change_value(d.get('after'))}"
    if event_type == EventType.UPDATED_EXPERIENCE.value:
        before = d.get("before") or {}
        after = d.get("after") or {}
        bm = before.get("months") if isinstance(before, dict) else None
        am = after.get("months") if isinstance(after, dict) else None
        return f"стаж {fmt_change_value(bm)}→{fmt_change_value(am)} мес"
    return ""
