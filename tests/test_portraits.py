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


# ── New mini-5.5 Portrait fields ──────────────────────────────────────────────


def test_portrait_position_description_default_empty() -> None:
    """position_description defaults to empty string."""
    p = Portrait.model_validate(MINIMAL_DATA)
    assert p.position_description == ""


def test_portrait_evaluation_focus_default_empty() -> None:
    """evaluation_focus defaults to empty list."""
    p = Portrait.model_validate(MINIMAL_DATA)
    assert p.evaluation_focus == []


def test_portrait_target_companies_override_default_empty() -> None:
    """target_companies_override defaults to empty list."""
    p = Portrait.model_validate(MINIMAL_DATA)
    assert p.target_companies_override == []


def test_portrait_stop_companies_override_default_empty() -> None:
    """stop_companies_override defaults to empty list."""
    p = Portrait.model_validate(MINIMAL_DATA)
    assert p.stop_companies_override == []


def test_portrait_new_fields_round_trip(tmp_path: Path) -> None:
    """position_description and evaluation_focus survive YAML round-trip."""
    data = {
        **MINIMAL_DATA,
        "position_description": "Руководитель филиала страховой компании.",
        "evaluation_focus": [
            "Управление агентской сетью",
            "P&L-опыт",
        ],
        "target_companies_override": ["ВСК"],
        "stop_companies_override": ["Капитал Лайф"],
    }
    p = tmp_path / "new_fields.yaml"
    p.write_text(yaml.dump(data, allow_unicode=True), encoding="utf-8")
    portrait = load_portrait(p)
    assert portrait.position_description == "Руководитель филиала страховой компании."
    assert portrait.evaluation_focus == ["Управление агентской сетью", "P&L-опыт"]
    assert portrait.target_companies_override == ["ВСК"]
    assert portrait.stop_companies_override == ["Капитал Лайф"]



# ── New mini-5.7 Portrait fields (Lesnitskaya etalon v1) ─────────────────────


def test_portrait_position_synonyms_default_empty() -> None:
    """position_synonyms defaults to empty list."""
    p = Portrait.model_validate(MINIMAL_DATA)
    assert p.position_synonyms == []


def test_portrait_resume_freshness_days_default_zero() -> None:
    """resume_freshness_days defaults to 0 (no filter)."""
    p = Portrait.model_validate(MINIMAL_DATA)
    assert p.resume_freshness_days == 0


def test_portrait_min_insurance_experience_months_default_zero() -> None:
    """min_insurance_experience_months defaults to 0."""
    p = Portrait.model_validate(MINIMAL_DATA)
    assert p.min_insurance_experience_months == 0


def test_portrait_motor_experience_preferred_default_false() -> None:
    """motor_experience_preferred defaults to False."""
    p = Portrait.model_validate(MINIMAL_DATA)
    assert p.motor_experience_preferred is False


def test_portrait_min_tenure_last_job_months_default_zero() -> None:
    """min_tenure_last_job_months defaults to 0."""
    p = Portrait.model_validate(MINIMAL_DATA)
    assert p.min_tenure_last_job_months == 0


def test_portrait_max_career_gap_months_default_zero() -> None:
    """max_career_gap_months defaults to 0 (no restriction)."""
    p = Portrait.model_validate(MINIMAL_DATA)
    assert p.max_career_gap_months == 0


def test_portrait_higher_education_required_default_false() -> None:
    """higher_education_required defaults to False."""
    p = Portrait.model_validate(MINIMAL_DATA)
    assert p.higher_education_required is False


def test_portrait_preferred_education_fields_default_empty() -> None:
    """preferred_education_fields defaults to empty list."""
    p = Portrait.model_validate(MINIMAL_DATA)
    assert p.preferred_education_fields == []


def test_portrait_citizenship_default_none() -> None:
    """citizenship defaults to None (no restriction)."""
    p = Portrait.model_validate(MINIMAL_DATA)
    assert p.citizenship is None


def test_portrait_bonus_companies_default_empty() -> None:
    """bonus_companies defaults to empty list."""
    p = Portrait.model_validate(MINIMAL_DATA)
    assert p.bonus_companies == []


def test_portrait_forbidden_industries_default_empty() -> None:
    """forbidden_industries defaults to empty list."""
    p = Portrait.model_validate(MINIMAL_DATA)
    assert p.forbidden_industries == []


def test_portrait_domain_governor_mode_default_cap() -> None:
    """Portrait without domain_governor_mode field defaults to 'cap'."""
    p = Portrait.model_validate(MINIMAL_DATA)
    assert p.domain_governor_mode == "cap"


def test_portrait_domain_governor_mode_off() -> None:
    """Portrait with domain_governor_mode='off' validates and stores the value."""
    p = Portrait.model_validate({**MINIMAL_DATA, "domain_governor_mode": "off"})
    assert p.domain_governor_mode == "off"


def test_portrait_new_etalon_weights_defaults() -> None:
    """New Lesnitskaya-v1 weight fields have correct defaults (sum of max=45)."""
    p = Portrait.model_validate(MINIMAL_DATA)
    w = p.weights
    assert w.agent_network_experience == 10
    assert w.osago_knowledge == 9
    assert w.target_region_primary == 8
    assert w.target_region_adjacent == 4
    assert w.ifl_experience == 7
    assert w.top4_competitor_experience == 6
    assert w.higher_specialized_education == 5
    # Max achievable = sum excluding target_region_adjacent (we take max of primary/adjacent)
    assert (
        w.agent_network_experience
        + w.osago_knowledge
        + w.target_region_primary
        + w.ifl_experience
        + w.top4_competitor_experience
        + w.higher_specialized_education
        == 45
    )


def test_portrait_etalon_fields_round_trip(tmp_path: Path) -> None:
    """All 12 new 5.7 fields survive a YAML round-trip."""
    data = {
        **MINIMAL_DATA,
        "position_synonyms": ["Руководитель филиала", "Управляющий филиалом"],
        "resume_freshness_days": 30,
        "min_insurance_experience_months": 36,
        "motor_experience_preferred": True,
        "min_tenure_last_job_months": 12,
        "max_career_gap_months": 36,
        "higher_education_required": True,
        "preferred_education_fields": ["экономика", "финансы"],
        "citizenship": "РФ",
        "bonus_companies": ["Ресо-Гарантия", "ВСК"],
        "forbidden_industries": ["банк", "лизинг"],
        "weights": {
            "agent_network_experience": 10,
            "osago_knowledge": 9,
            "target_region_primary": 8,
            "target_region_adjacent": 4,
            "ifl_experience": 7,
            "top4_competitor_experience": 6,
            "higher_specialized_education": 5,
        },
    }
    p = tmp_path / "etalon.yaml"
    p.write_text(yaml.dump(data, allow_unicode=True), encoding="utf-8")
    portrait = load_portrait(p)
    assert portrait.position_synonyms == ["Руководитель филиала", "Управляющий филиалом"]
    assert portrait.resume_freshness_days == 30
    assert portrait.min_insurance_experience_months == 36
    assert portrait.motor_experience_preferred is True
    assert portrait.min_tenure_last_job_months == 12
    assert portrait.max_career_gap_months == 36
    assert portrait.higher_education_required is True
    assert portrait.preferred_education_fields == ["экономика", "финансы"]
    assert portrait.citizenship == "РФ"
    assert portrait.bonus_companies == ["Ресо-Гарантия", "ВСК"]
    assert portrait.forbidden_industries == ["банк", "лизинг"]
    assert portrait.weights.agent_network_experience == 10
    assert portrait.weights.ifl_experience == 7


# ── GlobalContext loader ───────────────────────────────────────────────────────


def test_load_global_context_from_default_path() -> None:
    """load_global_context() reads the committed _global.yaml without error."""
    from hh_monitor.fit.portrait import load_global_context

    ctx = load_global_context()
    # 9 companies per Lesnitskaya etalon v1 (session 5.7); СОГАЗ removed
    assert len(ctx.target_companies) == 9
    assert "Ресо-Гарантия" in ctx.target_companies
    assert "Ингосстрах" in ctx.target_companies
    assert len(ctx.stop_companies) >= 1
    assert ctx.market_context  # non-empty


def test_load_global_context_missing_file_returns_empty() -> None:
    """load_global_context with a non-existent path returns an empty GlobalContext."""
    from hh_monitor.fit.portrait import GlobalContext, load_global_context

    ctx = load_global_context(path="/nonexistent/path/_global.yaml")
    assert ctx == GlobalContext()


def test_load_global_context_custom_path(tmp_path: Path) -> None:
    """load_global_context reads a custom _global.yaml correctly."""
    from hh_monitor.fit.portrait import load_global_context

    data = {
        "target_companies": ["ТестСтрах"],
        "stop_companies": ["ПлохойБанк"],
        "market_context": "Тестовый контекст рынка.",
    }
    p = tmp_path / "_global.yaml"
    p.write_text(yaml.dump(data, allow_unicode=True), encoding="utf-8")
    ctx = load_global_context(path=p)
    assert ctx.target_companies == ["ТестСтрах"]
    assert ctx.stop_companies == ["ПлохойБанк"]
    assert ctx.market_context == "Тестовый контекст рынка."

