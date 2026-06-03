#!/usr/bin/env python3
"""Maintenance: strip filters.age_range from a saved search's stored portrait.

Usage (from project root):
    poetry run python scripts/strip_search_age_range.py [--search-id 5]
    poetry run python scripts/strip_search_age_range.py [--search-id 5] --commit

DRY-RUN by default — prints the full portrait and the planned change, writes nothing.
Pass --commit to apply. Idempotent: no-op if age_range is already absent.
"""

import asyncio
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import typer  # noqa: E402
from sqlalchemy import select  # noqa: E402

from hh_monitor.db.engine import async_session_factory  # noqa: E402
from hh_monitor.db.models import Search  # noqa: E402

app = typer.Typer(add_completion=False)


def _pretty(obj: object) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)


async def _run(search_id: int, commit: bool) -> None:
    async with async_session_factory() as session:
        row = (
            await session.execute(select(Search).where(Search.id == search_id))
        ).scalar_one_or_none()

        if row is None:
            typer.echo(f"ERROR: Search id={search_id} not found.", err=True)
            raise SystemExit(1)

        portrait: dict = dict(row.portrait)  # type: ignore[arg-type]
        filters: dict = dict(portrait.get("filters") or {})

        typer.echo(f"Search id={search_id}  position_code={row.position_code!r}")
        typer.echo("\n── CURRENT PORTRAIT ────────────────────────────────────────")
        typer.echo(_pretty(portrait))

        if "age_range" not in filters:
            typer.echo("\nfilters.age_range is already absent — no-op.")
            return

        typer.echo(
            f"\nPlanned change: remove filters.age_range = {filters['age_range']!r}"
        )

        if not commit:
            typer.echo("\n[DRY RUN] No changes written. Re-run with --commit to apply.")
            return

        # ── Apply ──────────────────────────────────────────────────────────────
        new_filters = {k: v for k, v in filters.items() if k != "age_range"}
        new_portrait = {**portrait, "filters": new_filters}

        # Explicit assignment so SQLAlchemy detects the change on the JSONB column.
        row.portrait = new_portrait  # type: ignore[assignment]
        await session.commit()

        typer.echo("\n── PORTRAIT AFTER ──────────────────────────────────────────")
        typer.echo(_pretty(new_portrait))
        typer.echo(
            f"\n✅ filters.age_range removed from search id={search_id}."
        )


@app.command()
def main(
    search_id: int = typer.Option(5, "--search-id", help="ID of the search row to patch"),
    commit: bool = typer.Option(
        False, "--commit", help="Apply the change (default: dry-run)"
    ),
) -> None:
    asyncio.run(_run(search_id, commit))


if __name__ == "__main__":
    app()
