"""Tests for hh_monitor.fit.portrait — YAML/JSON loaders and CSV importer."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest
import yaml

from hh_monitor.fit.portrait import Portrait, load_all_portraits, load_portrait

# ── Fixtures ─────────────────────────────────────────────────────────────────


MINIMAL_DATA: dict = {
    "position_code": "test_pos",
    "position_name": "Test Position",
}

FULL_DATA: dict = {
    "position_code": "branch_director",
    "position_name": "Директор филиала",
    "search_params": {"text": "директор страхование", "experience": "between3And6"},
    "filters": {
        "regions": {
            "primary": ["Самарская область"],
            "adjacent": ["Оренбургская область"],
            "stop": ["Москва"],
        },
        "age_range": [30, 55],
        "salary_range": [120000, 450000],
        "education_level": ["higher", "bachelor"],
    },
    "weights": {
        "title_match": 25,
        "experience_keywords": 15,
        "total_experience": 20,
        "salary_fit": 10,
        "education": 5,
        "region": 10,
        "age": 5,
    },
    "title_keywords": ["директор филиала"],
    "experience_keywords": ["страхование"],
    "min_total_months": 36,
    "preferred_total_months": 72,
    "stop_words": ["студент"],
    "must_have_keywords": ["страхование"],
    "nice_to_have_keywords": ["MBA"],
}


# ── load_portrait — YAML ──────────────────────────────────────────────────────


def test_load_portrait_yaml_minimal(tmp_path: Path) -> None:
    """Minimal YAML with only required fields validates without error."""
    p = tmp_path / "minimal.yaml"
    p.write_text(yaml.dump(MINIMAL_DATA), encoding="utf-8")
    portrait = load_portrait(p)
    assert portrait.position_code == "test_pos"
    assert portrait.position_name == "Test Position"
    # defaults
    assert portrait.filters.regions.primary == []
    assert portrait.weights.title_match == 25


def test_load_portrait_yaml_full(tmp_path: Path) -> None:
    """Full YAML round-trips without data loss."""
    p = tmp_path / "full.yaml"
    p.write_text(yaml.dump(FULL_DATA, allow_unicode=True), encoding="utf-8")
    portrait = load_portrait(p)
    assert portrait.position_code == "branch_director"
    assert portrait.filters.regions.primary == ["Самарская область"]
    assert portrait.filters.regions.stop == ["Москва"]
    assert portrait.filters.age_range == (30, 55)
    assert portrait.filters.salary_range == (120000, 450000)
    assert portrait.filters.education_level == ["higher", "bachelor"]
    assert portrait.weights.region == 10
    assert portrait.min_total_months == 36
    assert portrait.must_have_keywords == ["страхование"]
    assert portrait.stop_words == ["студент"]


def test_load_portrait_yml_extension(tmp_path: Path) -> None:
    """.yml extension is accepted the same as .yaml."""
    p = tmp_path / "pos.yml"
    p.write_text(yaml.dump(MINIMAL_DATA), encoding="utf-8")
    portrait = load_portrait(p)
    assert portrait.position_code == "test_pos"


# ── load_portrait — JSON ──────────────────────────────────────────────────────


def test_load_portrait_json(tmp_path: Path) -> None:
    """JSON format (legacy) loads correctly."""
    p = tmp_path / "legacy.json"
    p.write_text(json.dumps(FULL_DATA), encoding="utf-8")
    portrait = load_portrait(p)
    assert portrait.position_code == "branch_director"
    assert portrait.filters.regions.primary == ["Самарская область"]


def test_load_portrait_json_minimal(tmp_path: Path) -> None:
    """Minimal JSON also passes validation with correct defaults."""
    p = tmp_path / "min.json"
    p.write_text(json.dumps(MINIMAL_DATA), encoding="utf-8")
    portrait = load_portrait(p)
    assert portrait.position_code == "test_pos"
    assert portrait.title_keywords == []


# ── Validation errors ─────────────────────────────────────────────────────────


def test_load_portrait_missing_position_code(tmp_path: Path) -> None:
    """Missing position_code raises a Pydantic ValidationError."""
    from pydantic import ValidationError

    p = tmp_path / "bad.yaml"
    p.write_text(yaml.dump({"position_name": "No Code"}), encoding="utf-8")
    with pytest.raises(ValidationError):
        load_portrait(p)


def test_load_portrait_missing_position_name(tmp_path: Path) -> None:
    """Missing position_name raises a Pydantic ValidationError."""
    from pydantic import ValidationError

    p = tmp_path / "bad.yaml"
    p.write_text(yaml.dump({"position_code": "no_name"}), encoding="utf-8")
    with pytest.raises(ValidationError):
        load_portrait(p)


def test_load_portrait_string_path(tmp_path: Path) -> None:
    """load_portrait accepts a plain string path."""
    p = tmp_path / "s.yaml"
    p.write_text(yaml.dump(MINIMAL_DATA), encoding="utf-8")
    portrait = load_portrait(str(p))
    assert portrait.position_code == "test_pos"


# ── load_all_portraits ────────────────────────────────────────────────────────


def test_load_all_portraits_from_dir(tmp_path: Path) -> None:
    """load_all_portraits reads *.yaml, skips _schema.yaml and non-YAML files."""
    # valid portrait
    (tmp_path / "pos_a.yaml").write_text(
        yaml.dump({"position_code": "pos_a", "position_name": "Position A"}),
        encoding="utf-8",
    )
    # second valid portrait
    (tmp_path / "pos_b.yaml").write_text(
        yaml.dump({"position_code": "pos_b", "position_name": "Position B"}),
        encoding="utf-8",
    )
    # underscore file — must be skipped
    (tmp_path / "_schema.yaml").write_text("just a comment", encoding="utf-8")
    # non-YAML file — must be skipped
    (tmp_path / "readme.txt").write_text("ignore me", encoding="utf-8")

    portraits = load_all_portraits(tmp_path)
    assert set(portraits.keys()) == {"pos_a", "pos_b"}
    assert portraits["pos_a"].position_name == "Position A"


def test_load_all_portraits_empty_dir(tmp_path: Path) -> None:
    """Empty directory returns an empty dict."""
    assert load_all_portraits(tmp_path) == {}


def test_load_all_portraits_default_dir() -> None:
    """Default portraits dir (config/portraits/) loads at least branch_director."""
    portraits = load_all_portraits()
    assert "branch_director" in portraits
    bd = portraits["branch_director"]
    assert bd.filters.regions.primary  # non-empty
    assert bd.weights.title_match == 25


# ── CSV importer ──────────────────────────────────────────────────────────────


def _write_csv(path: Path, rows: list[dict]) -> None:
    """Write a CSV with the columns expected by import_portraits_csv."""
    fieldnames = [
        "position_code",
        "position_name",
        "region_primary",
        "region_adjacent",
        "region_stop",
        "age_min",
        "age_max",
        "salary_min",
        "salary_max",
        "education_min",
        "stop_words",
        "must_have_keywords",
        "nice_to_have_keywords",
    ]
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            # fill missing fields with empty string
            writer.writerow({f: row.get(f, "") for f in fieldnames})


def test_import_csv_basic(tmp_path: Path) -> None:
    """Happy path: one row produces a valid YAML file keyed by position_code."""
    from scripts.import_portraits_csv import import_csv

    csv_path = tmp_path / "portraits.csv"
    out_dir = tmp_path / "out"
    _write_csv(
        csv_path,
        [
            {
                "position_code": "agency_director",
                "position_name": "Директор агентства",
                "region_primary": "Самарская область;Саратовская область",
                "region_adjacent": "Оренбургская область",
                "region_stop": "",
                "age_min": "28",
                "age_max": "50",
                "salary_min": "80000",
                "salary_max": "300000",
                "education_min": "higher,bachelor",
                "stop_words": "студент;стажёр",
                "must_have_keywords": "страхование;продажи",
                "nice_to_have_keywords": "MBA",
            }
        ],
    )
    written = import_csv(csv_path, out_dir)
    assert written == 1
    out_file = out_dir / "agency_director.yaml"
    assert out_file.exists()

    # load and validate round-trip
    portrait = load_portrait(out_file)
    assert portrait.position_code == "agency_director"
    assert "Самарская область" in portrait.filters.regions.primary
    assert "Оренбургская область" in portrait.filters.regions.adjacent
    assert portrait.filters.age_range == (28, 50)
    assert portrait.filters.salary_range == (80000, 300000)
    assert "higher" in portrait.filters.education_level
    assert "студент" in portrait.stop_words
    assert "страхование" in portrait.must_have_keywords


def test_import_csv_idempotent(tmp_path: Path) -> None:
    """Re-running import on the same CSV overwrites files without error."""
    from scripts.import_portraits_csv import import_csv

    csv_path = tmp_path / "p.csv"
    out_dir = tmp_path / "out"
    row = {
        "position_code": "dup_pos",
        "position_name": "Duplicate Position",
        "region_primary": "Москва",
    }
    _write_csv(csv_path, [row])
    assert import_csv(csv_path, out_dir) == 1
    assert import_csv(csv_path, out_dir) == 1  # second run should also succeed


def test_import_csv_skips_empty_position_code(tmp_path: Path) -> None:
    """Rows without position_code are skipped silently."""
    from scripts.import_portraits_csv import import_csv

    csv_path = tmp_path / "p.csv"
    out_dir = tmp_path / "out"
    _write_csv(csv_path, [{"position_code": "", "position_name": "No Code"}])
    written = import_csv(csv_path, out_dir)
    assert written == 0


def test_import_csv_optional_fields_blank(tmp_path: Path) -> None:
    """Blank age/salary fields produce None ranges without error."""
    from scripts.import_portraits_csv import import_csv

    csv_path = tmp_path / "p.csv"
    out_dir = tmp_path / "out"
    _write_csv(
        csv_path,
        [
            {
                "position_code": "sparse_pos",
                "position_name": "Sparse Position",
                "region_primary": "Казань",
                # age_min, age_max, salary_min, salary_max all blank
            }
        ],
    )
    written = import_csv(csv_path, out_dir)
    assert written == 1
    portrait = load_portrait(out_dir / "sparse_pos.yaml")
    assert portrait.filters.age_range is None
    assert portrait.filters.salary_range is None


def test_import_csv_partial_age_produces_no_range(tmp_path: Path) -> None:
    """age_min without age_max → no age_range (both required)."""
    from scripts.import_portraits_csv import import_csv

    csv_path = tmp_path / "p.csv"
    out_dir = tmp_path / "out"
    _write_csv(
        csv_path,
        [
            {
                "position_code": "partial_age",
                "position_name": "Partial Age",
                "age_min": "25",
                "age_max": "",  # missing
            }
        ],
    )
    import_csv(csv_path, out_dir)
    portrait = load_portrait(out_dir / "partial_age.yaml")
    assert portrait.filters.age_range is None


def test_portrait_direct_validation() -> None:
    """Portrait.model_validate works directly with a dict."""
    p = Portrait.model_validate(FULL_DATA)
    assert p.position_code == "branch_director"
    assert p.weights.experience_keywords == 15
    assert p.nice_to_have_keywords == ["MBA"]
