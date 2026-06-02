"""Tests for the action-first HR digest message (Commit 2)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from hh_monitor.weekly_digest.run import (
    _build_hr_message,
    _empty_digest_text,
)


def _candidate(**kw: object) -> dict[str, object]:
    base: dict[str, object] = {
        "position_name": "Директор филиала",
        "score_total": 82,
        "fit_score": 60,
        "llm_score": 90,
        "llm_verdict": "подходит",
        "llm_real_role": "Директор",
        "facts": "",
        "weak": "",
        "risks": "",
        "conclusion": "Сильный кандидат с опытом управления филиалом.",
        "screening_status": None,
        "reason": "",
        "url": "https://hh.ru/resume/abc123",
        "created_at": datetime.now(UTC),
        "sent_at": datetime.now(UTC) - timedelta(days=4),
        "age_days": 4,
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
        "candidates_all": [],
        "top": [_candidate()],
        "pending": [_candidate(age_days=4), _candidate(age_days=1, score_total=70)],
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


_NOW = datetime(2026, 6, 2, tzinfo=UTC)
_FROM = _NOW - timedelta(days=7)


def test_message_contains_funnel_labels() -> None:
    msg = _build_hr_message(_data(), [], 23, _FROM, _NOW)  # type: ignore[arg-type]
    assert "Найдено: 10" in msg
    assert "Отправлено: 6" in msg
    assert "Одобрено: 3" in msg
    assert "Отклонено: 2" in msg
    assert "Спорно: 1" in msg
    assert "Ждут: 2" in msg
    assert "Конверсия отправлено→одобрено: 50%" in msg


def test_message_has_pending_block_and_warning() -> None:
    msg = _build_hr_message(_data(), [], 23, _FROM, _NOW)  # type: ignore[arg-type]
    assert "Требуют решения (2)" in msg
    assert "висит 4 дн" in msg
    assert "⚠️ " in msg  # oldest pending (age 4 >= 3) flagged


def test_message_pending_empty_all_resolved() -> None:
    msg = _build_hr_message(_data(pending=[]), [], 23, _FROM, _NOW)  # type: ignore[arg-type]
    assert "✅ Все разобраны" in msg


def test_message_has_pre_table_and_top() -> None:
    msg = _build_hr_message(_data(), [], 23, _FROM, _NOW)  # type: ignore[arg-type]
    assert "<pre>" in msg and "</pre>" in msg
    assert "Позиция" in msg and "Найд" in msg
    assert "🏆" in msg
    assert "hh.ru" in msg


def test_message_deltas_with_prev_week() -> None:
    series = [
        {"week_label": "a", "found": 0, "sent": 0, "approved": 0},
        {"week_label": "b", "found": 0, "sent": 0, "approved": 0},
        {"week_label": "c", "found": 4, "sent": 2, "approved": 1},
        {"week_label": "d", "found": 10, "sent": 6, "approved": 3},
    ]
    msg = _build_hr_message(_data(), series, 23, _FROM, _NOW)  # type: ignore[arg-type]
    assert "Найдено: 10 ↑6" in msg  # 10 vs prev 4


def test_empty_week_text() -> None:
    stats = {
        "runs": 7,
        "snapshots_inserted": 0,
        "dedup_rate": 0,
        "errors": 0,
        "resumes_viewed": 312,
    }
    text = _empty_digest_text(_FROM, _NOW, stats)  # type: ignore[arg-type]
    assert "📭" in text
    assert "Еженедельная сводка" in text
    assert "7 прогонов" in text
    assert "312 резюме" in text
