"""Tests for hh_monitor.weekly_digest.excel — 4-sheet workbook builder."""

from __future__ import annotations

from datetime import UTC, datetime
from io import BytesIO

from openpyxl import load_workbook  # type: ignore[import-untyped]

from hh_monitor.weekly_digest.excel import _humanize_field, build_digest_workbook


def _candidate(**kw: object) -> dict[str, object]:
    base: dict[str, object] = {
        "position_name": "Директор филиала",
        "score_total": 82,
        "fit_score": 60,
        "llm_score": 90,
        "llm_verdict": "подходит",
        "llm_real_role": "Директор",
        "facts": "опыт 10 лет",
        "weak": "нет высшего",
        "risks": "частые смены работы",
        "conclusion": "Сильный кандидат.",
        "screening_status": "approve",
        "reason": "",
        "url": "https://hh.ru/resume/abc123",
        "created_at": datetime(2026, 6, 1, tzinfo=UTC),
        "sent_at": None,
        "age_days": None,
    }
    base.update(kw)
    return base


def _data(**kw: object) -> dict[str, object]:
    base: dict[str, object] = {
        "funnel": {
            "found": 10,
            "sent": 6,
            "approved": 3,
            "rejected": 2,
            "doubt": 1,
            "pending": 2,
        },
        "per_position": [
            {
                "position_name": "Директор филиала",
                "count": 10,
                "n_fit": 4,
                "n_doubt": 3,
                "n_miss": 3,
                "avg_score": 71,
                "sent": 6,
                "approved": 3,
                "rejected": 2,
            }
        ],
        "candidates_all": [_candidate(), _candidate(score_total=70, screening_status=None)],
        "top": [],
        "pending": [],
        "parser_stats": {
            "runs": 5,
            "snapshots_inserted": 120,
            "dedup_rate": 18,
            "errors": 0,
            "resumes_viewed": 240,
        },
    }
    base.update(kw)
    return base


_SERIES = [
    {"week_label": "05.05–12.05", "found": 4, "sent": 2, "approved": 1},
    {"week_label": "12.05–19.05", "found": 7, "sent": 4, "approved": 2},
    {"week_label": "19.05–26.05", "found": 8, "sent": 5, "approved": 2},
    {"week_label": "26.05–02.06", "found": 10, "sent": 6, "approved": 3},
]


def _load(data: dict[str, object], series: list[dict[str, object]]):  # type: ignore[no-untyped-def]
    raw = build_digest_workbook(data, series)  # type: ignore[arg-type]
    assert raw[:2] == b"PK"
    return load_workbook(BytesIO(raw))


def test_workbook_has_four_named_sheets() -> None:
    wb = _load(_data(), _SERIES)
    assert wb.sheetnames == ["Кандидаты", "По позициям", "Воронка", "Динамика"]


def test_candidates_header_and_rows() -> None:
    wb = _load(_data(), _SERIES)
    ws = wb["Кандидаты"]
    assert ws.cell(row=1, column=1).value == "Позиция"
    assert ws.cell(row=1, column=2).value == "Рейтинг"
    assert ws.cell(row=1, column=11).value == "Статус скрининга"
    assert ws.max_row >= 3  # header + 2 candidates
    assert ws.cell(row=2, column=2).value == 82
    assert ws.cell(row=2, column=11).value == "Одобрен ✅"


def test_candidates_color_scale_on_rating() -> None:
    wb = _load(_data(), _SERIES)
    ws = wb["Кандидаты"]
    rules = ws.conditional_formatting
    found = False
    for rng in rules:
        if "B2" in str(rng):
            found = True
    assert found, "ColorScaleRule expected on the Рейтинг column (B)"


def test_positions_sheet_row() -> None:
    wb = _load(_data(), _SERIES)
    ws = wb["По позициям"]
    assert ws.cell(row=1, column=1).value == "Позиция"
    assert ws.cell(row=2, column=1).value == "Директор филиала"
    assert ws.cell(row=2, column=2).value == 10


def test_funnel_sheet_has_chart() -> None:
    wb = _load(_data(), _SERIES)
    ws = wb["Воронка"]
    assert len(ws._charts) == 1


def test_dynamics_rows_match_series() -> None:
    wb = _load(_data(), _SERIES)
    ws = wb["Динамика"]
    assert ws.cell(row=1, column=1).value == "Неделя"
    # header + 4 series rows
    assert ws.max_row == len(_SERIES) + 1
    assert len(ws._charts) == 1


def test_empty_dynamics_no_chart() -> None:
    wb = _load(_data(), [])
    ws = wb["Динамика"]
    assert len(ws._charts) == 0


def test_humanize_field_real_dict() -> None:
    value = {"Опыт управления": "250+ агентов", "Продуктовая экспертиза": "10 лет"}
    assert _humanize_field(value) == "Опыт управления: 250+ агентов\nПродуктовая экспертиза: 10 лет"


def test_humanize_field_dict_repr_string() -> None:
    # The single-quoted Python-dict reprs seen in the week-23 export.
    value = "{'Опыт управления': '250+ агентов', 'Продуктовая экспертиза': '10 лет'}"
    assert _humanize_field(value) == "Опыт управления: 250+ агентов\nПродуктовая экспертиза: 10 лет"


def test_humanize_field_plain_string_passthrough() -> None:
    assert _humanize_field("опыт 10 лет в продажах") == "опыт 10 лет в продажах"


def test_humanize_field_none_and_empty() -> None:
    assert _humanize_field(None) == ""
    assert _humanize_field("") == ""


def test_humanize_field_malformed_dict_string_unchanged() -> None:
    # Looks dict-ish but is not valid — must fall back to the original, no raise.
    malformed = "{сильные стороны: опыт, лидерство"
    assert _humanize_field(malformed) == malformed


def test_candidates_dict_fields_humanized() -> None:
    facts = "{'Опыт управления': '250+ агентов', 'Экспертиза': 'логистика'}"
    weak = "{'Слабое место': 'нет высшего'}"
    data = _data(
        candidates_all=[_candidate(facts=facts, weak=weak)],
        pending=[],
    )
    wb = _load(data, _SERIES)
    ws = wb["Кандидаты"]
    facts_cell = ws.cell(row=2, column=7).value
    weak_cell = ws.cell(row=2, column=8).value
    assert facts_cell == "Опыт управления: 250+ агентов\nЭкспертиза: логистика"
    assert weak_cell == "Слабое место: нет высшего"
    assert "{'" not in str(facts_cell)
    assert "{'" not in str(weak_cell)


def test_candidates_link_cell_is_real_url() -> None:
    data = _data(
        candidates_all=[_candidate(url="https://hh.ru/resume/known999")],
        pending=[],
    )
    wb = _load(data, _SERIES)
    ws = wb["Кандидаты"]
    assert ws.cell(row=2, column=13).value == "https://hh.ru/resume/known999"


def test_candidates_link_cell_empty_when_no_url() -> None:
    data = _data(
        candidates_all=[_candidate(url="")],
        pending=[],
    )
    wb = _load(data, _SERIES)
    ws = wb["Кандидаты"]
    cell = ws.cell(row=2, column=13).value
    assert cell in ("", None)
    assert cell != "hh.ru"
