# Detector specification

The detector (`hh_monitor.detector`) compares consecutive resume snapshots and
emits typed **events** to the `events` table. It is designed as a pure, idempotent
pipeline: running it twice on the same snapshots produces no duplicate events.

## Pure diff function

```python
diff_snapshots(
    prev: dict | None,
    curr: dict | None,
    hh_resume_id: str,
    curr_snapshot_id: int,
    prev_snapshot_id: int | None = None,
) -> list[DetectedEvent]
```

Takes two consecutive payload snapshots (the full `/resumes/{id}` JSON).
Returns zero or more `DetectedEvent` objects. Has no side effects — no DB, no
logging, no network.

`curr_snapshot_id` (and `prev_snapshot_id` when available) are always stored in
`DetectedEvent.details` so that the caller can persist them for idempotency checks.

## Event types

| `event_type` | Trigger condition |
|---|---|
| `NEW` | `prev` is `None` (first snapshot ever) |
| `REMOVED` | `curr` is archived (see below) and `prev` was not |
| `REACTIVATED` | `prev` was archived and `curr` is not |
| `UPDATED_POSITION` | `curr.title` differs from `prev.title` (neither archived) |
| `UPDATED_SALARY` | `curr.salary` differs from `prev.salary` (neither archived) |
| `UPDATED_EXPERIENCE` | Total months changed **or** experience array changed (neither archived) |

Multiple events can be emitted from a single diff call (e.g. position + salary
changed simultaneously).

### REMOVED criterion

A resume payload is considered **archived** when **all three** of the following
are absent/falsy:

- `payload["title"]`
- `payload["experience"]`
- `payload["total_experience"]`

Or when `payload` itself is `None`.

> **TODO**: Verify this criterion against real hh.ru samples of removed/hidden
> resumes. The hh.ru API does not document the exact shape of an archived
> response — this definition is based on reasonable inference and should be
> confirmed once the parser is running against a live employer account.

## Idempotency

Before processing any resume, `run_detector` queries the `events` table for an
existing row whose `details` JSONB contains `{"curr_snapshot_id": <id>}` (using
the PostgreSQL `@>` containment operator). If such a row exists, the resume is
skipped and counted as `skipped_idempotent`.

This approach is exact and clock-independent — it does not rely on timestamps.

## Runner

```python
async def run_detector(session: AsyncSession) -> dict[str, int]:
    ...
    return {"processed": N, "emitted": M, "skipped_idempotent": K}
```

Processes every `Resume` row in the DB. For each resume, fetches the two most
recent snapshots (ordered by `fetched_at DESC`). Commits once after all events
are inserted. Logs a structured summary via `structlog`.

## CLI

```
poetry run hh-monitor detector run
```

Prints a one-line summary, e.g.:

```
Detector finished: processed=12, emitted=3, skipped_idempotent=9
```
