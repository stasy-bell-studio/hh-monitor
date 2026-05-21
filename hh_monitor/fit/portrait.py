import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel


class Portrait(BaseModel):
    position_code: str
    position_name: str
    title_keywords: list[str]
    experience_keywords: list[str]
    min_total_months: int
    preferred_total_months: int
    min_salary: int | None = None
    max_salary: int | None = None
    preferred_education_levels: list[str] = []
    preferred_areas: list[str] = []
    age_range: tuple[int, int] | None = None


def load_portrait(path: Path | str) -> Portrait:
    data: dict[str, Any] = json.loads(Path(path).read_text(encoding="utf-8"))
    return Portrait.model_validate(data)
