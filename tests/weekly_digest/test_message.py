"""Tests for the action-first HR digest message (Commit 2)."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from hh_monitor.weekly_digest.run import (
    _build_hr_message,
    _empty_digest_text,
    _parser_ops_text,
    _pending_block,
    _stats_from_runs,
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
        "pending": [_candidate(age_days=4), _candidate(age_days=1, score_total=70)],
        "parser_stats": {
            "runs": 5,
            "snapshots_inserted": 120,
            "dedup_rate": 18,
            "partial": 0,
            "limit": 0,
            "broken": 0,
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


def test_message_has_pre_table() -> None:
    msg = _build_hr_message(_data(), [], 23, _FROM, _NOW)  # type: ignore[arg-type]
    assert "<pre>" in msg and "</pre>" in msg
    assert "Позиция" in msg and "Найд" in msg
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
        "partial": 0,
        "limit": 0,
        "broken": 0,
        "resumes_viewed": 312,
    }
    text = _empty_digest_text(_FROM, _NOW, stats)  # type: ignore[arg-type]
    assert "📭" in text
    assert "Еженедельная сводка" in text
    assert "7 прогонов" in text
    assert "312 резюме" in text


# ── Pending block: verdict filter (Commit A) ─────────────────────────────────


def test_pending_verdict_filter() -> None:
    pending = [
        _candidate(llm_verdict="подходит", position_name="Alpha", age_days=1),
        _candidate(llm_verdict="спорно", position_name="Beta", age_days=2),
        _candidate(llm_verdict="мимо", position_name="Gamma", age_days=1),
    ]
    block = _pending_block(pending)  # type: ignore[arg-type]
    assert "Alpha" in block
    assert "Beta" in block
    assert "Gamma" not in block
    assert "🔴 +1 с вердиктом «мимо» — в Excel" in block


def test_pending_invariant() -> None:
    pending = [
        _candidate(llm_verdict="подходит", age_days=1),
        _candidate(llm_verdict="мимо", age_days=2),
        _candidate(llm_verdict="мимо", age_days=3),
    ]
    block = _pending_block(pending)  # type: ignore[arg-type]
    shown_count = sum(1 for line in block.splitlines() if "hh.ru" in line)
    m = re.search(r"🔴 \+(\d+)", block)
    miss_count = int(m.group(1)) if m else 0
    assert shown_count + miss_count == len(pending)


def test_pending_all_miss() -> None:
    pending = [
        _candidate(llm_verdict="мимо", age_days=5),
        _candidate(llm_verdict="мимо", age_days=3),
    ]
    block = _pending_block(pending)  # type: ignore[arg-type]
    assert block == "🔴 +2 с вердиктом «мимо» — в Excel"
    assert "hh.ru" not in block
    assert "⚠️" not in block


def test_pending_warning_on_oldest_shown() -> None:
    pending = [
        _candidate(llm_verdict="мимо", age_days=10),  # hidden, should NOT trigger ⚠️
        _candidate(llm_verdict="подходит", age_days=4),  # oldest shown → gets ⚠️
        _candidate(llm_verdict="спорно", age_days=1),
    ]
    block = _pending_block(pending)  # type: ignore[arg-type]
    lines = block.splitlines()
    assert any("⚠️" in line and "4 дн" in line for line in lines)


# ── Parser stats: bucketing + admin message (Commit B) ───────────────────────


def _make_run(
    status: str,
    finished_at: datetime | None = datetime(2026, 6, 1, tzinfo=UTC),
    snapshots_inserted: int = 0,
    snapshots_skipped: int = 0,
    resumes_viewed: int = 0,
) -> object:
    from unittest.mock import MagicMock

    r = MagicMock()
    r.status = status
    r.finished_at = finished_at
    r.snapshots_inserted = snapshots_inserted
    r.snapshots_skipped = snapshots_skipped
    r.resumes_viewed = resumes_viewed
    return r


def test_stats_from_runs_bucketing() -> None:
    runs = [
        _make_run("ok"),
        _make_run("ok"),
        _make_run("partial_errors"),
        _make_run("quota_exceeded"),
        _make_run("view_limit_exhausted"),
        _make_run("cancelled"),
        _make_run("running", finished_at=None),
    ]
    stats = _stats_from_runs(runs)  # type: ignore[arg-type]
    assert stats["partial"] == 1
    assert stats["limit"] == 2
    assert stats["broken"] == 2
    assert stats["runs"] == 7


def test_stats_from_runs_ok_not_counted_as_bad() -> None:
    runs = [_make_run("ok"), _make_run("ok")]
    stats = _stats_from_runs(runs)  # type: ignore[arg-type]
    assert stats["partial"] == 0
    assert stats["limit"] == 0
    assert stats["broken"] == 0


def test_parser_ops_text_no_broken() -> None:
    stats = {
        "runs": 10,
        "snapshots_inserted": 200,
        "dedup_rate": 15,
        "partial": 3,
        "limit": 1,
        "broken": 0,
        "resumes_viewed": 0,
    }
    text = _parser_ops_text(23, stats)  # type: ignore[arg-type]
    assert "Сбоев нет" in text
    assert "⚠️" not in text


def test_parser_ops_text_with_broken() -> None:
    stats = {
        "runs": 10,
        "snapshots_inserted": 200,
        "dedup_rate": 15,
        "partial": 0,
        "limit": 0,
        "broken": 2,
        "resumes_viewed": 0,
    }
    text = _parser_ops_text(23, stats)  # type: ignore[arg-type]
    assert "Прерванных запусков: 2" in text
    assert "Сбоев нет" not in text


def test_parser_ops_text_labels() -> None:
    stats = {
        "runs": 5,
        "snapshots_inserted": 120,
        "dedup_rate": 18,
        "partial": 2,
        "limit": 1,
        "broken": 0,
        "resumes_viewed": 0,
    }
    text = _parser_ops_text(23, stats)  # type: ignore[arg-type]
    assert "Проверок hh.ru: 5" in text
    assert "собрано резюме: 120" in text
    assert "повторов пропущено: 18%" in text
    assert "Недоступных резюме (удалены/скрыты): 2" in text
    assert "дневной лимит hh.ru: 1" in text
