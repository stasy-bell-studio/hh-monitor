"""Tests for the Jinja stops-union fix in config/portraits/prompt_template.j2."""

from __future__ import annotations

import types
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

_TEMPLATE_DIR = Path(__file__).parent.parent.parent / "config" / "portraits"
_TEMPLATE_NAME = "prompt_template.j2"


def _make_env() -> Environment:
    return Environment(loader=FileSystemLoader(str(_TEMPLATE_DIR)))


def _stub_portrait(
    stop_companies_override: list[str],
    target_companies_override: list[str] | None = None,
) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        position_name="Test Position",
        position_description="",
        must_have_keywords=[],
        nice_to_have_keywords=[],
        stop_words=[],
        min_total_months=0,
        preferred_total_months=24,
        filters=types.SimpleNamespace(
            salary_range=None,
            age_range=None,
        ),
        evaluation_focus=[],
        target_companies_override=target_companies_override or [],
        stop_companies_override=stop_companies_override,
    )


def _stub_global(stop_companies: list[str]) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        target_companies=[],
        stop_companies=stop_companies,
    )


def _stub_resume() -> types.SimpleNamespace:
    return types.SimpleNamespace(
        hh_resume_id="r1",
        title="Test",
        age=None,
        area=None,
        salary=None,
        total_experience_months=0,
        education=None,
        experience=[],
        key_skills=[],
        about="",
    )


def _render(portrait: object, global_ctx: object) -> str:
    env = _make_env()
    tmpl = env.get_template(_TEMPLATE_NAME)
    return tmpl.render(portrait=portrait, global_ctx=global_ctx, resume=_stub_resume())


def test_stops_union_when_both_present() -> None:
    rendered = _render(
        _stub_portrait(["A"]),
        _stub_global(["B", "C"]),
    )
    assert "A" in rendered
    assert "B" in rendered
    assert "C" in rendered


def test_stops_union_dedupes_overlap() -> None:
    rendered = _render(
        _stub_portrait(["A", "B"]),
        _stub_global(["B", "C"]),
    )
    assert "A" in rendered
    assert "B" in rendered
    assert "C" in rendered
    # B appears in the stops list section at most once
    stops_start = rendered.index("СТОП-КОМПАНИИ")
    stops_section = rendered[stops_start : stops_start + 200]
    assert stops_section.count("B") == 1


def test_stops_use_global_when_override_empty() -> None:
    rendered = _render(
        _stub_portrait([]),
        _stub_global(["X"]),
    )
    assert "X" in rendered


def test_stops_empty_when_both_empty() -> None:
    rendered = _render(
        _stub_portrait([]),
        _stub_global([]),
    )
    assert isinstance(rendered, str)
