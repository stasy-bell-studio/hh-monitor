"""Unit tests for scripts/generate_ru_areas.py — _collect_cities recursion.

No network: all tests use a mock area tree injected directly into _collect_cities.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Load the generator script without going through the package install path.
sys.path.insert(0, str(Path(__file__).parents[2] / "scripts"))
from generate_ru_areas import _collect_cities  # type: ignore[import-untyped]

# ── Shared mock tree ──────────────────────────────────────────────────────────────
#
# Subject A (id=100)
#   City X (id=1)  ← will be ambiguous (same name under Subject B)
#   City Y (id=2)
#     Village Z (id=3)  ← depth-3 node; must still map to Subject A
#
# Subject B (id=200)
#   City X (id=4)  ← ambiguous: same name as Subject A's City X
#   City W (id=5)
#
# Subject C (id=300)
#   City Named Like Subject A (id=6)  ← name == "subject a"; must be skipped (in RU_AREAS)

_MOCK_SUBJECTS = [
    {
        "id": "100",
        "name": "Subject A",
        "areas": [
            {"id": "1", "name": "City X", "areas": []},
            {
                "id": "2",
                "name": "City Y",
                "areas": [
                    {"id": "3", "name": "Village Z", "areas": []},
                ],
            },
        ],
    },
    {
        "id": "200",
        "name": "Subject B",
        "areas": [
            {"id": "4", "name": "City X", "areas": []},
            {"id": "5", "name": "City W", "areas": []},
        ],
    },
    {
        "id": "300",
        "name": "Subject C",
        "areas": [
            {"id": "6", "name": "Subject A", "areas": []},  # same as a subject name
        ],
    },
]


def _run():
    return _collect_cities(_MOCK_SUBJECTS)  # type: ignore[arg-type]


def test_ru_areas_contains_only_subjects():
    ru_areas, _, _ = _run()
    assert ru_areas == {"subject a": 100, "subject b": 200, "subject c": 300}


def test_city_resolves_to_parent_subject():
    _, ru_cities, _ = _run()
    assert ru_cities["city y"] == 100


def test_depth3_node_resolves_to_top_subject():
    """Village Z is 3 levels deep; must map to Subject A (100), not City Y."""
    _, ru_cities, _ = _run()
    assert ru_cities["village z"] == 100


def test_unambiguous_city_in_second_subject():
    _, ru_cities, _ = _run()
    assert ru_cities["city w"] == 200


def test_ambiguous_city_not_in_ru_cities():
    _, ru_cities, _ = _run()
    assert "city x" not in ru_cities


def test_ambiguous_city_in_ru_ambiguous_with_sorted_tuple():
    _, _, ru_ambiguous = _run()
    assert ru_ambiguous["city x"] == (100, 200)


def test_subject_name_collision_skipped_from_ru_cities():
    """A city named the same as an existing subject must not appear in RU_CITIES."""
    _, ru_cities, _ = _run()
    assert "subject a" not in ru_cities


def test_city_name_with_trailing_digits_and_whitespace_normalized():
    """City name 'Некийгород (42)  ' must appear in RU_CITIES as 'некийгород'."""
    subjects = [
        {
            "id": "500",
            "name": "Тестовая область",
            "areas": [
                {"id": "10", "name": "Некийгород (42)  ", "areas": []},
            ],
        }
    ]
    _, ru_cities, _ = _collect_cities(subjects)  # type: ignore[arg-type]
    assert "некийгород" in ru_cities
    assert ru_cities["некийгород"] == 500
    # The raw form must NOT appear as a key.
    assert "некийгород (42)  " not in ru_cities
