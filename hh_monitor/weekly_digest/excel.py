"""Styled 4-sheet Excel workbook for the weekly HR digest.

Sheets:
  - «Кандидаты»  — all scored candidates (rating desc), color-scale on Рейтинг.
  - «По позициям» — per-position funnel breakdown.
  - «Воронка»    — KPI cells + BarChart over the funnel stages.
  - «Динамика»   — weekly_series rows + LineChart (Найдено/Отправлено/Одобрено).

Style only is borrowed from hh_monitor/digest/export_xlsx.py (header fill/font,
freeze panes, auto-filter, widths, wrap). The «Статус скрининга» column here is a
read-only label — no DataValidation dropdown, no taken/reserve taxonomy.
"""

from __future__ import annotations

from io import BytesIO
from typing import TYPE_CHECKING

from openpyxl import Workbook  # type: ignore[import-untyped]
from openpyxl.chart import BarChart, LineChart, Reference  # type: ignore[import-untyped]
from openpyxl.formatting.rule import ColorScaleRule  # type: ignore[import-untyped]
from openpyxl.styles import Alignment, Font, PatternFill  # type: ignore[import-untyped]
from openpyxl.utils import get_column_letter  # type: ignore[import-untyped]

from hh_monitor.weekly_digest.run import _status_label

if TYPE_CHECKING:
    from hh_monitor.weekly_digest.run import _DigestData, _WeekPoint

_FILL_HEADER = PatternFill(start_color="1A3C6E", end_color="1A3C6E", fill_type="solid")
_FONT_HEADER = Font(bold=True, color="FFFFFF", size=10)
_FONT_LINK = Font(color="0563C1", underline="single", size=9)
_FONT_DATA = Font(size=9)
_ALIGN_WRAP = Alignment(wrap_text=True, vertical="top")
_ALIGN_CENTER = Alignment(horizontal="center", vertical="top")


def _style_header(ws: object, headers: list[tuple[str, int]]) -> None:
    for col_idx, (header, width) in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=col_idx, value=header)  # type: ignore[attr-defined]
        cell.font = _FONT_HEADER
        cell.fill = _FILL_HEADER
        cell.alignment = _ALIGN_CENTER
        ws.column_dimensions[get_column_letter(col_idx)].width = width  # type: ignore[attr-defined]
    ws.freeze_panes = "A2"  # type: ignore[attr-defined]
    last = get_column_letter(len(headers))
    ws.auto_filter.ref = f"A1:{last}1"  # type: ignore[attr-defined]


def _sheet_candidates(wb: Workbook, data: _DigestData) -> None:
    ws = wb.active
    ws.title = "Кандидаты"
    headers: list[tuple[str, int]] = [
        ("Позиция", 24),
        ("Рейтинг", 9),
        ("Соответствие портрету", 12),
        ("Оценка ИИ", 10),
        ("Вердикт", 14),
        ("Реальная роль", 22),
        ("Сильные стороны", 40),
        ("Слабые места", 36),
        ("Риски", 32),
        ("Вывод", 44),
        ("Статус скрининга", 16),
        ("Причина", 30),
        ("Ссылка", 14),
        ("Дата", 12),
    ]
    _style_header(ws, headers)

    for i, c in enumerate(data["candidates_all"], start=2):
        values = [
            c["position_name"],
            c["score_total"],
            c["fit_score"],
            c["llm_score"],
            c["llm_verdict"] or "",
            c["llm_real_role"],
            c["facts"],
            c["weak"],
            c["risks"],
            c["conclusion"],
            _status_label(c["screening_status"]),
            c["reason"],
            "hh.ru",
            c["created_at"].strftime("%d.%m.%Y"),
        ]
        for col_idx, value in enumerate(values, start=1):
            cell = ws.cell(row=i, column=col_idx, value=value)
            cell.alignment = _ALIGN_WRAP
            cell.font = _FONT_DATA
        link_cell = ws.cell(row=i, column=13)
        link_cell.hyperlink = c["url"]
        link_cell.font = _FONT_LINK

    n = len(data["candidates_all"])
    if n:
        rng = f"B2:B{n + 1}"
        ws.conditional_formatting.add(
            rng,
            ColorScaleRule(
                start_type="num",
                start_value=0,
                start_color="F8696B",
                mid_type="num",
                mid_value=50,
                mid_color="FFEB84",
                end_type="num",
                end_value=100,
                end_color="63BE7B",
            ),
        )


def _sheet_positions(wb: Workbook, data: _DigestData) -> None:
    ws = wb.create_sheet("По позициям")
    headers: list[tuple[str, int]] = [
        ("Позиция", 24),
        ("Найдено", 9),
        ("Подходит", 9),
        ("Спорно", 9),
        ("Мимо", 9),
        ("Ср. рейтинг", 11),
        ("Отправлено", 11),
        ("Одобрено", 10),
        ("Отклонено", 10),
    ]
    _style_header(ws, headers)
    for i, pp in enumerate(data["per_position"], start=2):
        values = [
            pp["position_name"],
            pp["count"],
            pp["n_fit"],
            pp["n_doubt"],
            pp["n_miss"],
            pp["avg_score"],
            pp["sent"],
            pp["approved"],
            pp["rejected"],
        ]
        for col_idx, value in enumerate(values, start=1):
            cell = ws.cell(row=i, column=col_idx, value=value)
            cell.alignment = _ALIGN_WRAP
            cell.font = _FONT_DATA


def _sheet_funnel(wb: Workbook, data: _DigestData) -> None:
    ws = wb.create_sheet("Воронка")
    f = data["funnel"]
    conv = round(f["approved"] / f["sent"] * 100) if f["sent"] else 0
    ws.cell(row=1, column=1, value="Этап").font = _FONT_HEADER
    ws.cell(row=1, column=1).fill = _FILL_HEADER
    ws.cell(row=1, column=2, value="Кол-во").font = _FONT_HEADER
    ws.cell(row=1, column=2).fill = _FILL_HEADER
    ws.column_dimensions["A"].width = 16
    ws.column_dimensions["B"].width = 10
    stages = [
        ("Найдено", f["found"]),
        ("Отправлено", f["sent"]),
        ("Одобрено", f["approved"]),
        ("Отклонено", f["rejected"]),
    ]
    for i, (label, val) in enumerate(stages, start=2):
        ws.cell(row=i, column=1, value=label)
        ws.cell(row=i, column=2, value=val)
    extra_row = len(stages) + 2
    ws.cell(row=extra_row, column=1, value="Спорно")
    ws.cell(row=extra_row, column=2, value=f["doubt"])
    ws.cell(row=extra_row + 1, column=1, value="Ждут")
    ws.cell(row=extra_row + 1, column=2, value=f["pending"])
    ws.cell(row=extra_row + 2, column=1, value="Конверсия, %")
    ws.cell(row=extra_row + 2, column=2, value=conv)

    chart = BarChart()
    chart.title = "Воронка недели"
    chart.type = "col"
    chart.legend = None
    chart_data = Reference(ws, min_col=2, min_row=2, max_row=len(stages) + 1)
    cats = Reference(ws, min_col=1, min_row=2, max_row=len(stages) + 1)
    chart.add_data(chart_data, titles_from_data=False)
    chart.set_categories(cats)
    ws.add_chart(chart, "D2")


def _sheet_dynamics(wb: Workbook, weekly_series: list[_WeekPoint]) -> None:
    ws = wb.create_sheet("Динамика")
    headers: list[tuple[str, int]] = [
        ("Неделя", 16),
        ("Найдено", 10),
        ("Отправлено", 11),
        ("Одобрено", 10),
    ]
    _style_header(ws, headers)
    for i, wp in enumerate(weekly_series, start=2):
        ws.cell(row=i, column=1, value=wp["week_label"])
        ws.cell(row=i, column=2, value=wp["found"])
        ws.cell(row=i, column=3, value=wp["sent"])
        ws.cell(row=i, column=4, value=wp["approved"])

    if weekly_series:
        last = len(weekly_series) + 1
        chart = LineChart()
        chart.title = "Динамика за 4 недели"
        chart_data = Reference(ws, min_col=2, max_col=4, min_row=1, max_row=last)
        cats = Reference(ws, min_col=1, min_row=2, max_row=last)
        chart.add_data(chart_data, titles_from_data=True)
        chart.set_categories(cats)
        ws.add_chart(chart, "F2")


def build_digest_workbook(data: _DigestData, weekly_series: list[_WeekPoint]) -> bytes:
    """Build the 4-sheet weekly-digest workbook and return its bytes."""
    wb = Workbook()
    _sheet_candidates(wb, data)
    _sheet_positions(wb, data)
    _sheet_funnel(wb, data)
    _sheet_dynamics(wb, weekly_series)
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()
