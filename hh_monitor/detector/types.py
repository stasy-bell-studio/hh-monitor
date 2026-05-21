from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class EventType(str, Enum):
    NEW = "NEW"
    UPDATED_EXPERIENCE = "UPDATED_EXPERIENCE"
    UPDATED_SALARY = "UPDATED_SALARY"
    UPDATED_POSITION = "UPDATED_POSITION"
    REACTIVATED = "REACTIVATED"
    REMOVED = "REMOVED"


@dataclass(frozen=True)
class DetectedEvent:
    event_type: EventType
    hh_resume_id: str
    details: dict[str, Any] = field(default_factory=dict)
