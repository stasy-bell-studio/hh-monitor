"""Tests for hh_monitor.weekly_digest — Jinja2 rendering and WeasyPrint PDF smoke.

Coverage targets:
  - Template renders valid HTML from synthetic context.
  - WeasyPrint produces non-empty PDF bytes starting with b'%PDF-'.
  - run_weekly_digest wires template + PDF + bot.send_document correctly.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"


# ── Helpers ───────────────────────────────────────────────────────────────────


def _synthetic_context() -> dict[str, object]:
    return {
        "week_number": 21,
        "date_from": "19.05.2026",
        "date_to": "25.05.2026",
        "generated_at": "25.05.2026 15:00 UTC",
        "total_candidates": 3,
        "positions": [
            {
                "position_code": "branch_director",
                "position_name": "Директор филиала",
                "count": 3,
                "avg_score": 74,
                "top_candidates": [
                    {
                        "hh_resume_id": "abc001",
                        "verdict": "подходит",
                        "real_role": "Директор",
                        "score_total": 80,
                        "comment": "Отличный кандидат с опытом",
                        "url": "https://hh.ru/resume/abc001",
                    },
                    {
                        "hh_resume_id": "abc002",
                        "verdict": "спорно",
                        "real_role": "Зам директора",
                        "score_total": 65,
                        "comment": "",
                        "url": "https://hh.ru/resume/abc002",
                    },
                ],
            }
        ],
        "parser_stats": {
            "runs": 5,
            "snapshots_inserted": 120,
            "dedup_rate": 18,
            "errors": 0,
        },
    }


def _render_html(context: dict[str, object]) -> str:
    from jinja2 import Environment, FileSystemLoader

    env = Environment(loader=FileSystemLoader(str(_TEMPLATES_DIR)), autoescape=True)
    template = env.get_template("weekly_digest.html.j2")
    return template.render(**context)


# ── Tests: template rendering ─────────────────────────────────────────────────


def test_template_renders_non_empty_html() -> None:
    html = _render_html(_synthetic_context())
    assert len(html) > 500
    assert "<!DOCTYPE html>" in html


def test_template_contains_week_number() -> None:
    html = _render_html(_synthetic_context())
    assert "21" in html


def test_template_contains_position_name() -> None:
    html = _render_html(_synthetic_context())
    assert "Директор филиала" in html


def test_template_contains_candidate_score() -> None:
    html = _render_html(_synthetic_context())
    assert "80/100" in html


def test_template_contains_parser_stats() -> None:
    html = _render_html(_synthetic_context())
    assert "120" in html  # snapshots_inserted
    assert "18%" in html  # dedup_rate


def test_template_renders_empty_positions() -> None:
    ctx = _synthetic_context()
    ctx["positions"] = []
    ctx["total_candidates"] = 0
    html = _render_html(ctx)
    assert "Нет кандидатов" in html


def test_template_escapes_html_in_verdict() -> None:
    ctx = _synthetic_context()
    candidates = list(ctx["positions"][0]["top_candidates"])  # type: ignore[index]
    candidates[0] = dict(candidates[0])
    candidates[0]["verdict"] = "<script>alert(1)</script>"
    ctx["positions"] = [dict(ctx["positions"][0], top_candidates=candidates)]  # type: ignore[index]
    html = _render_html(ctx)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


# ── Tests: PDF generation ─────────────────────────────────────────────────────


def test_pdf_bytes_starts_with_pdf_header() -> None:
    from weasyprint import HTML  # type: ignore[import-untyped]

    html = _render_html(_synthetic_context())
    pdf_bytes = HTML(string=html).write_pdf()
    assert isinstance(pdf_bytes, bytes)
    assert len(pdf_bytes) > 0
    assert pdf_bytes[:4] == b"%PDF"


def test_pdf_bytes_non_empty_for_minimal_html() -> None:
    from weasyprint import HTML  # type: ignore[import-untyped]

    html = "<html><body><p>Test</p></body></html>"
    pdf_bytes = HTML(string=html).write_pdf()
    assert pdf_bytes[:4] == b"%PDF"


# ── Tests: run_weekly_digest integration ─────────────────────────────────────


@pytest.mark.asyncio
async def test_run_weekly_digest_calls_send_document() -> None:
    """run_weekly_digest should generate PDF and call bot.send_document once."""
    from hh_monitor.weekly_digest.run import run_weekly_digest

    mock_session = MagicMock()
    mock_bot = AsyncMock()
    mock_bot.send_document = AsyncMock()

    synthetic_data = {
        "positions": _synthetic_context()["positions"],
        "total_candidates": 3,
        "parser_stats": _synthetic_context()["parser_stats"],
    }

    with patch(
        "hh_monitor.weekly_digest.run._collect_data",
        new_callable=AsyncMock,
        return_value=synthetic_data,
    ):
        await run_weekly_digest(mock_session, mock_bot)

    mock_bot.send_document.assert_called_once()
    call_kwargs = mock_bot.send_document.call_args[1]
    assert "document" in call_kwargs
    assert "caption" in call_kwargs
    assert "дайджест" in call_kwargs["caption"].lower()


@pytest.mark.asyncio
async def test_run_weekly_digest_pdf_content() -> None:
    """The BufferedInputFile data passed to send_document must be valid PDF bytes."""
    from aiogram.types import BufferedInputFile

    from hh_monitor.weekly_digest.run import run_weekly_digest

    mock_session = MagicMock()
    mock_bot = AsyncMock()
    mock_bot.send_document = AsyncMock()

    synthetic_data = {
        "positions": [],
        "total_candidates": 0,
        "parser_stats": {
            "runs": 0,
            "snapshots_inserted": 0,
            "dedup_rate": 0,
            "errors": 0,
        },
    }

    with patch(
        "hh_monitor.weekly_digest.run._collect_data",
        new_callable=AsyncMock,
        return_value=synthetic_data,
    ):
        await run_weekly_digest(mock_session, mock_bot)

    call_kwargs = mock_bot.send_document.call_args[1]
    doc = call_kwargs["document"]
    assert isinstance(doc, BufferedInputFile)
    assert doc.data[:4] == b"%PDF"
