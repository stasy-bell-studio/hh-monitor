import asyncio
import json
import re
import secrets
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import typer

from hh_monitor.config import settings
from hh_monitor.db.engine import async_session_factory
from hh_monitor.db.models import OAuthToken, Resume, Search, Snapshot
from hh_monitor.errors import HHApiError, HHQuotaExceeded, HHServiceNotActive, SearchNotFoundError
from hh_monitor.fit.portrait import Portrait, load_portrait
from hh_monitor.fit.rules import compute as fit_compute
from hh_monitor.hh import cache, endpoints
from hh_monitor.hh.client import HHClient
from hh_monitor.hh.oauth import (
    build_authorize_url,
    exchange_code_for_token,
    get_valid_token,
)

app = typer.Typer(name="hh-monitor", help="HR Resume Monitor for SK 21 Vek")
hh_app = typer.Typer(help="HH.ru API commands")
app.add_typer(hh_app, name="hh")


@hh_app.command("auth")
def hh_auth() -> None:
    """Authorize via hh.ru OAuth (Authorization Code flow)."""
    state = secrets.token_urlsafe(16)
    url = build_authorize_url(state=state)
    typer.echo(f"\nOpen this URL in your browser:\n\n{url}\n")
    typer.echo(
        "After authorization, copy the full callback URL from your browser and paste it here."
    )
    callback_url = typer.prompt("Callback URL")

    parsed = urlparse(callback_url)
    qs = parse_qs(parsed.query)

    returned_state = qs.get("state", [None])[0]
    if returned_state != state:
        typer.echo(f"Error: state mismatch (expected {state}, got {returned_state})", err=True)
        raise typer.Exit(1)

    code_list = qs.get("code")
    if not code_list:
        typer.echo("Error: no 'code' parameter in callback URL", err=True)
        raise typer.Exit(1)
    code = code_list[0]

    try:
        token = asyncio.run(_exchange_code(code))
    except HHApiError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc

    remaining = int((token.expires_at - datetime.now(UTC)).total_seconds())
    typer.echo(f"Token saved. Expires in {remaining} seconds.")


async def _exchange_code(code: str) -> OAuthToken:
    async with async_session_factory() as session:
        return await exchange_code_for_token(code, session)


@hh_app.command("me")
def hh_me() -> None:
    """Show current authorized user info from /me."""
    try:
        asyncio.run(_me())
    except HHApiError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc


async def _me() -> None:
    async with async_session_factory() as session:
        client = HHClient(
            token_provider=lambda: get_valid_token(session),
            user_agent=settings.hh_user_agent,
        )
        result = await endpoints.me(client)

    typer.echo(f"Logged in as {result.first_name} {result.last_name}")
    if result.employer:
        typer.echo(f"Employer: {result.employer.name} (id={result.employer.id})")
    if result.manager:
        typer.echo(f"Manager id: {result.manager.id}")


dictionaries_app = typer.Typer(help="Dictionary cache commands")
hh_app.add_typer(dictionaries_app, name="dictionaries")


@dictionaries_app.command("refresh")
def hh_dictionaries_refresh() -> None:
    """Fetch /dictionaries and /areas and save to local cache."""
    try:
        asyncio.run(_dictionaries_refresh())
    except HHApiError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc


async def _dictionaries_refresh() -> None:
    async with async_session_factory() as session:
        client = HHClient(
            token_provider=lambda: get_valid_token(session),
            user_agent=settings.hh_user_agent,
        )
        dicts = await endpoints.dictionaries_raw(client)
        areas = await endpoints.areas_raw(client)
        await cache.save_dictionary(session, "dictionaries", dicts)
        await cache.save_dictionary(session, "areas", areas)

    typer.echo(f"Cached: dictionaries ({len(dicts)} keys), areas ({len(areas)} entries).")


# ── detector ─────────────────────────────────────────────────────────────────

detector_app = typer.Typer(help="Resume change detector commands")
app.add_typer(detector_app, name="detector")


@detector_app.command("run")
def detector_run() -> None:
    """Detect changes in resume snapshots and persist events to DB."""
    counts = asyncio.run(_detector_run())
    typer.echo(
        f"Detector finished: processed={counts['processed']}, "
        f"emitted={counts['emitted']}, "
        f"skipped_idempotent={counts['skipped_idempotent']}"
    )


async def _detector_run() -> dict[str, int]:
    from hh_monitor.detector.run import run_detector

    async with async_session_factory() as session:
        return await run_detector(session)


# ── fit ───────────────────────────────────────────────────────────────────────

fit_app = typer.Typer(help="Fit scoring commands")
app.add_typer(fit_app, name="fit")


@fit_app.command("score")
def fit_score(
    hh_resume_id: str = typer.Argument(..., help="HH resume ID to score"),
    portrait_path: Path = typer.Option(  # noqa: B008
        ..., "--portrait", "-p", help="Path to portrait JSON file"
    ),
) -> None:
    """Score the latest snapshot of a resume against a portrait."""
    try:
        portrait = load_portrait(portrait_path)
    except (FileNotFoundError, OSError) as exc:
        typer.echo(f"Error: cannot read portrait file: {exc}", err=True)
        raise typer.Exit(1) from exc

    try:
        result = asyncio.run(_load_latest_snapshot(hh_resume_id))
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc

    if result is None:
        typer.echo(f"Error: no snapshot found for resume '{hh_resume_id}'", err=True)
        raise typer.Exit(1)

    score, breakdown = fit_compute(result, portrait)
    typer.echo(f"Resume:   {hh_resume_id}")
    typer.echo(f"Portrait: {portrait.position_name} ({portrait.position_code})")
    typer.echo(f"Score:    {score}/100")
    typer.echo("")
    typer.echo("Breakdown:")
    for rule, delta in breakdown.items():
        sign = "+" if delta > 0 else ""
        typer.echo(f"  {rule:<25} {sign}{delta}")


async def _load_latest_snapshot(hh_resume_id: str) -> dict | None:  # type: ignore[type-arg]
    from sqlalchemy import select

    async with async_session_factory() as session:
        row = await session.execute(
            select(Snapshot)
            .where(Snapshot.hh_resume_id == hh_resume_id)
            .order_by(Snapshot.fetched_at.desc())
            .limit(1)
        )
        snap = row.scalar_one_or_none()
        if snap is None:
            return None
        payload = snap.payload
        # SQLAlchemy may return dict directly (JSONB) or a string; normalise.
        if isinstance(payload, str):
            return json.loads(payload)
        return payload


# ── searches ─────────────────────────────────────────────────────────────────

searches_app = typer.Typer(help="Saved search management")
app.add_typer(searches_app, name="searches")


def _slugify(name: str) -> str:
    """'Director SPb' → 'director_spb'"""
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


@searches_app.command("add")
def searches_add(
    name: str = typer.Option(..., "--name", help="Human-readable position name"),
    query: str = typer.Option(..., "--query", help="hh.ru search params as JSON string"),
    portrait_path: Path = typer.Option(  # noqa: B008
        ..., "--portrait", "-p", help="Path to portrait JSON file"
    ),
    code: str | None = typer.Option(None, "--code", help="Unique position slug (auto from name)"),
) -> None:
    """Add a new saved search with its scoring portrait."""
    try:
        hh_params: dict[str, object] = json.loads(query)
    except json.JSONDecodeError as exc:
        typer.echo(f"Error: --query is not valid JSON: {exc}", err=True)
        raise typer.Exit(1) from exc

    try:
        portrait_obj = load_portrait(portrait_path)
    except (FileNotFoundError, OSError, Exception) as exc:
        typer.echo(f"Error: cannot load portrait: {exc}", err=True)
        raise typer.Exit(1) from exc

    position_code = code or _slugify(name)
    portrait_dict = portrait_obj.model_dump(mode="json")

    search_id = asyncio.run(_searches_add(position_code, name, hh_params, portrait_dict))
    typer.echo(f"Created search id={search_id} code={position_code}")


async def _searches_add(
    position_code: str,
    position_name: str,
    hh_params: dict,  # type: ignore[type-arg]
    portrait: dict,  # type: ignore[type-arg]
) -> int:
    async with async_session_factory() as session:
        search = Search(
            position_code=position_code,
            position_name=position_name,
            hh_params=hh_params,
            portrait=portrait,
        )
        session.add(search)
        await session.flush()
        search_id: int = search.id
        await session.commit()
        return search_id


@searches_app.command("list")
def searches_list() -> None:
    """List all saved searches."""
    asyncio.run(_searches_list())


async def _searches_list() -> None:
    from sqlalchemy import select

    async with async_session_factory() as session:
        rows = (await session.execute(select(Search).order_by(Search.id))).scalars().all()

    if not rows:
        typer.echo("No searches found. Use 'searches add' to create one.")
        return

    typer.echo(f"{'ID':<4} {'Code':<20} {'Name':<25} {'HH Params'}")
    typer.echo("-" * 80)
    for s in rows:
        params_str = json.dumps(s.hh_params, ensure_ascii=False)
        if len(params_str) > 35:
            params_str = params_str[:32] + "..."
        typer.echo(f"{s.id:<4} {s.position_code:<20} {s.position_name:<25} {params_str}")


# ── parse ─────────────────────────────────────────────────────────────────────

parse_app = typer.Typer(help="Resume parser commands")
app.add_typer(parse_app, name="parse")


@parse_app.command("run")
def parse_run(
    search_id: int = typer.Option(..., "--search-id", help="ID of the saved search to run"),
    max_pages: int = typer.Option(5, "--max-pages", help="Maximum pages to fetch"),
) -> None:
    """Fetch resumes from hh.ru and save snapshots to the database."""
    try:
        result = asyncio.run(_parse_run(search_id, max_pages))
    except SearchNotFoundError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    except (HHQuotaExceeded, HHServiceNotActive) as exc:
        typer.echo(f"Error: HH API quota/service error — {exc}", err=True)
        raise typer.Exit(1) from exc
    except HHApiError as exc:
        typer.echo(f"Error: HH API error {exc.status_code}", err=True)
        raise typer.Exit(1) from exc

    typer.echo(
        f"Parser run id={result['parser_run_id']}  "
        f"seen={result['resumes_seen']}  "
        f"inserted={result['snapshots_inserted']}  "
        f"skipped={result['snapshots_skipped_dedup']}  "
        f"errors={result['errors']}"
    )


async def _parse_run(search_id: int, max_pages: int) -> dict:  # type: ignore[type-arg]
    from hh_monitor.parser.run import run_parser

    async with async_session_factory() as session:
        client = HHClient(
            token_provider=lambda: get_valid_token(session),
            user_agent=settings.hh_user_agent,
        )
        return await run_parser(session, client, search_id, max_pages=max_pages)


# ── pipeline ──────────────────────────────────────────────────────────────────

pipeline_app = typer.Typer(help="End-to-end pipeline commands")
app.add_typer(pipeline_app, name="pipeline")


@pipeline_app.command("run")
def pipeline_run(
    search_id: int = typer.Option(..., "--search-id", help="ID of the saved search"),
    portrait_path: Path | None = typer.Option(  # noqa: B008
        None,
        "--portrait",
        "-p",
        help="Portrait JSON override (default: use portrait stored in the search)",
    ),
    top: int = typer.Option(10, "--top", help="Number of top candidates to display"),
    max_pages: int = typer.Option(5, "--max-pages", help="Maximum pages to fetch"),
    no_parse: bool = typer.Option(
        False, "--no-parse", help="Skip parsing; score existing snapshots"
    ),
) -> None:
    """Parse → detect → score → show top-N candidates.

    Use --no-parse to skip the hh.ru API fetch and re-score the snapshots
    already stored in the database.  Useful for iterating on portraits without
    burning quota.
    """
    try:
        asyncio.run(_pipeline_run(search_id, portrait_path, top, max_pages, no_parse))
    except SearchNotFoundError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    except (HHQuotaExceeded, HHServiceNotActive) as exc:
        typer.echo(f"Error: HH API quota/service error — {exc}", err=True)
        raise typer.Exit(1) from exc
    except HHApiError as exc:
        typer.echo(f"Error: HH API error {exc.status_code}", err=True)
        raise typer.Exit(1) from exc


_HH_RESUME_BASE = "https://hh.ru/resume"


async def _pipeline_run(
    search_id: int,
    portrait_path: Path | None,
    top: int,
    max_pages: int,
    no_parse: bool = False,
) -> None:
    from sqlalchemy import select

    from hh_monitor.detector.run import run_detector
    from hh_monitor.parser.run import run_parser

    async with async_session_factory() as session:
        # Load search to get stored portrait
        search = await session.get(Search, search_id)
        if search is None:
            raise SearchNotFoundError(f"Search id={search_id} not found")

        # Resolve portrait: CLI override > stored in search
        if portrait_path is not None:
            portrait = load_portrait(portrait_path)
        else:
            portrait = Portrait.model_validate(search.portrait)

        if no_parse:
            # ── No-parse mode: reuse existing snapshots ────────────────────
            typer.echo("Mode: no-parse (using existing snapshots)")
            rows = (await session.execute(select(Resume.hh_resume_id))).scalars().all()
            resume_ids: list[str] = list(rows)
        else:
            # ── Normal mode: fetch from hh.ru first ───────────────────────
            client = HHClient(
                token_provider=lambda: get_valid_token(session),
                user_agent=settings.hh_user_agent,
            )
            parser_result = await run_parser(session, client, search_id, max_pages=max_pages)
            resume_ids = parser_result["resume_ids"]
            typer.echo(
                f"\nParser run id={parser_result['parser_run_id']}  "
                f"seen={parser_result['resumes_seen']}  "
                f"inserted={parser_result['snapshots_inserted']}  "
                f"skipped={parser_result['snapshots_skipped_dedup']}  "
                f"errors={parser_result['errors']}\n"
            )

        # ── Detect changes ─────────────────────────────────────────────────
        await run_detector(session)

        # ── Score each resume by its latest snapshot ───────────────────────
        scored: list[tuple[str, dict, int, dict]] = []  # type: ignore[type-arg]
        for rid in resume_ids:
            row = (
                await session.execute(
                    select(Snapshot)
                    .where(Snapshot.hh_resume_id == rid)
                    .order_by(Snapshot.fetched_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if row is None:
                continue
            payload = row.payload if isinstance(row.payload, dict) else json.loads(row.payload)
            score, breakdown = fit_compute(payload, portrait)
            scored.append((rid, payload, score, breakdown))

        # ── Sort by score descending, print top-N ─────────────────────────
        scored.sort(key=lambda x: x[2], reverse=True)

        typer.echo(f"Top-{min(top, len(scored))} candidates (portrait: {portrait.position_name}):")
        typer.echo(f"{'#':<3} {'ID':<10} {'Title':<30} {'Score':>5}  {'URL':<55}  Breakdown")
        typer.echo("-" * 120)

        for rank, (rid, payload, score, breakdown) in enumerate(scored[:top], start=1):
            short_id = rid[:8]
            title = (payload.get("title") or "")[:28]
            url = f"{_HH_RESUME_BASE}/{rid}"
            bd_str = " ".join(f"{k}:{v:+d}" for k, v in breakdown.items() if v != 0)
            typer.echo(f"{rank:<3} {short_id:<10} {title:<30} {score:>5}  {url:<55}  {bd_str}")


# ── portraits ─────────────────────────────────────────────────────────────────

portraits_app = typer.Typer(help="Portrait management commands")
app.add_typer(portraits_app, name="portraits")


@portraits_app.command("list")
def portraits_list(
    portraits_dir: Path | None = typer.Option(  # noqa: B008
        None, "--portraits-dir", "-d", help="Directory to scan (default: config/portraits/)"
    ),
) -> None:
    """List all YAML portraits in the portraits directory."""
    from hh_monitor.fit.portrait import load_all_portraits

    portraits = load_all_portraits(portraits_dir)
    if not portraits:
        typer.echo("No portraits found.")
        return
    typer.echo(f"{'Code':<25} {'Name':<35} {'Primary regions'}")
    typer.echo("-" * 90)
    for code, p in sorted(portraits.items()):
        primary = ", ".join(p.filters.regions.primary or p.preferred_areas)
        if len(primary) > 40:
            primary = primary[:37] + "..."
        typer.echo(f"{code:<25} {p.position_name:<35} {primary}")


@portraits_app.command("show")
def portraits_show(
    position_code: str = typer.Argument(..., help="position_code to display"),
    portraits_dir: Path | None = typer.Option(  # noqa: B008
        None, "--portraits-dir", "-d", help="Directory to scan (default: config/portraits/)"
    ),
) -> None:
    """Show the full portrait for a position."""
    from hh_monitor.fit.portrait import load_all_portraits

    portraits = load_all_portraits(portraits_dir)
    p = portraits.get(position_code)
    if p is None:
        typer.echo(
            f"Portrait '{position_code}' not found. Available: {sorted(portraits)}",
            err=True,
        )
        raise typer.Exit(1)

    typer.echo(f"Position: {p.position_name} ({p.position_code})")
    typer.echo(f"Primary regions: {p.filters.regions.primary or p.preferred_areas}")
    typer.echo(f"Adjacent regions: {p.filters.regions.adjacent}")
    typer.echo(f"Stop regions: {p.filters.regions.stop}")
    if p.filters.age_range:
        lo, hi = p.filters.age_range
        typer.echo(f"Age range: {lo}-{hi}")
    if p.filters.salary_range:
        lo_s, hi_s = p.filters.salary_range
        typer.echo(f"Salary range: {lo_s:,}-{hi_s:,} RUB")
    typer.echo(f"Education: {p.filters.education_level}")
    typer.echo(f"Title keywords: {p.title_keywords}")
    typer.echo(f"Experience keywords: {p.experience_keywords}")
    typer.echo(f"Must-have keywords: {p.must_have_keywords}")
    typer.echo(f"Nice-to-have keywords: {p.nice_to_have_keywords}")
    typer.echo(f"Stop words: {p.stop_words}")
    min_mo = p.min_total_months
    pref_mo = p.preferred_total_months
    typer.echo(f"Min experience: {min_mo} months ({min_mo // 12}y)")
    typer.echo(f"Preferred exp: {pref_mo} months ({pref_mo // 12}y)")


@portraits_app.command("validate")
def portraits_validate(
    portraits_dir: Path | None = typer.Option(  # noqa: B008
        None, "--portraits-dir", "-d", help="Directory to scan (default: config/portraits/)"
    ),
) -> None:
    """Validate all YAML portraits — exit 1 if any fail."""

    from pydantic import ValidationError

    from hh_monitor.fit.portrait import _PORTRAITS_DIR, load_portrait

    directory = portraits_dir or _PORTRAITS_DIR
    errors = 0
    ok = 0
    for path in sorted(Path(directory).glob("*.yaml")):
        if path.name.startswith("_"):
            continue
        try:
            load_portrait(path)
            typer.echo(f"  OK  {path.name}")
            ok += 1
        except ValidationError as exc:
            typer.echo(f"  FAIL {path.name}: {exc}", err=True)
            errors += 1
        except Exception as exc:
            typer.echo(f"  ERROR {path.name}: {exc}", err=True)
            errors += 1

    typer.echo(f"\n{ok} OK, {errors} failed.")
    if errors:
        raise typer.Exit(1)


@portraits_app.command("import")
def portraits_import(
    csv_path: Path = typer.Argument(..., help="Path to the CSV file from Lesnitskaya"),  # noqa: B008
    portraits_dir: Path | None = typer.Option(  # noqa: B008
        None, "--portraits-dir", "-d", help="Output directory (default: config/portraits/)"
    ),
) -> None:
    """Import portrait definitions from a CSV file into YAML portraits."""

    from hh_monitor.fit.portrait import _PORTRAITS_DIR
    from scripts.import_portraits_csv import import_csv

    if not csv_path.exists():
        typer.echo(f"Error: file not found: {csv_path}", err=True)
        raise typer.Exit(1)

    out_dir = portraits_dir or _PORTRAITS_DIR
    typer.echo(f"Importing from {csv_path} → {out_dir}")
    n = import_csv(csv_path, out_dir)
    typer.echo(f"\nDone: {n} portrait(s) written.")


# ── llm ───────────────────────────────────────────────────────────────────────

llm_app = typer.Typer(help="LLM enrichment commands")
app.add_typer(llm_app, name="llm")


@llm_app.command("score")
def llm_score(
    hh_resume_id: str = typer.Argument(..., help="HH resume ID to score"),
    search_id: int = typer.Option(..., "--search-id", help="Search ID (determines portrait)"),
) -> None:
    """Show fit + LLM scores for a single resume (reads from DB, no API call)."""
    try:
        asyncio.run(_llm_score(hh_resume_id, search_id))
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc


async def _llm_score(hh_resume_id: str, search_id: int) -> None:
    from sqlalchemy import select

    from hh_monitor.fit.portrait import load_all_portraits
    from hh_monitor.fit.rules import compute as fit_compute_fn

    async with async_session_factory() as session:
        # Load search to get position_code
        s = await session.get(Search, search_id)
        if s is None:
            raise ValueError(f"Search id={search_id} not found")

        # Load portrait
        portraits = load_all_portraits()
        portrait = portraits.get(s.position_code)
        if portrait is None:
            raise ValueError(f"No portrait for position_code={s.position_code!r}")

        # Latest snapshot
        row = (
            await session.execute(
                select(Snapshot)
                .where(Snapshot.hh_resume_id == hh_resume_id)
                .order_by(Snapshot.fetched_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if row is None:
            raise ValueError(f"No snapshot found for resume '{hh_resume_id}'")

        payload = row.payload if isinstance(row.payload, dict) else json.loads(row.payload)
        fit_score_val, breakdown = fit_compute_fn(payload, portrait)

        # Stored LLM results
        resume = await session.get(Resume, hh_resume_id)

    typer.echo(f"Resume:     {hh_resume_id}")
    typer.echo(f"Portrait:   {portrait.position_name} ({portrait.position_code})")
    typer.echo(f"Fit score:  {fit_score_val}/100")
    typer.echo("Breakdown:")
    for rule, delta in breakdown.items():
        sign = "+" if delta > 0 else ""
        typer.echo(f"  {rule:<25} {sign}{delta}")

    if resume and resume.llm_score is not None:
        typer.echo(f"\nLLM score:  {resume.llm_score}/100")
        typer.echo(f"LLM verdict: {resume.llm_verdict}")
        typer.echo(f"LLM comment: {resume.llm_comment}")
        if resume.llm_red_flags:
            typer.echo(f"Red flags:  {resume.llm_red_flags}")
        typer.echo(f"Score total: {resume.score_total}/100")
    else:
        typer.echo("\nLLM: not yet enriched")


@llm_app.command("run")
def llm_run(
    search_id: int = typer.Option(..., "--search-id", help="Search ID to enrich"),
    limit: int = typer.Option(10, "--limit", "-n", help="Max events to process"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Skip API calls; show what would be enriched"
    ),
) -> None:
    """Run LLM enrichment on unenriched events for a search."""
    try:
        result = asyncio.run(_llm_run(search_id, limit, dry_run))
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc

    typer.echo(
        f"LLM run done: search={result['search_id']} ({result['position_code']})  "
        f"processed={result['total_processed']}  "
        f"enriched={result['enriched']}  "
        f"skipped={result['skipped']}  "
        f"errors={result['errors']}"
        + ("  [DRY RUN]" if result["dry_run"] else "")
    )


async def _llm_run(search_id: int, limit: int, dry_run: bool) -> dict:  # type: ignore[type-arg]
    from hh_monitor.llm_enrich.run import run_llm_enrichment

    async with async_session_factory() as session:
        return await run_llm_enrichment(session, search_id, limit=limit, dry_run=dry_run)


@llm_app.command("reset-cache")
def llm_reset_cache(
    hh_resume_id: str = typer.Argument(..., help="HH resume ID whose cache to delete"),
) -> None:
    """Delete all LLM cache entries for a resume (forces re-scoring on next run)."""
    deleted = asyncio.run(_llm_reset_cache(hh_resume_id))
    typer.echo(f"Deleted {deleted} cache entry(ies) for resume '{hh_resume_id}'.")


async def _llm_reset_cache(hh_resume_id: str) -> int:
    from sqlalchemy import delete

    from hh_monitor.db.models import LlmCache

    async with async_session_factory() as session:
        result = await session.execute(
            delete(LlmCache).where(LlmCache.hh_resume_id == hh_resume_id)
        )
        await session.commit()
        rowcount: int = result.rowcount  # type: ignore[attr-defined]
        return rowcount


@llm_app.command("stats")
def llm_stats() -> None:
    """Show LLM enrichment stats — enriched vs pending per search."""
    asyncio.run(_llm_stats())


async def _llm_stats() -> None:
    from sqlalchemy import func, select

    from hh_monitor.db.models import Event

    async with async_session_factory() as session:
        rows = (
            await session.execute(
                select(
                    Search.id,
                    Search.position_code,
                    Search.position_name,
                    func.count(Event.id).label("total"),
                    func.sum(
                        func.cast(Event.llm_enriched, type_=__import__("sqlalchemy").Integer)
                    ).label("enriched"),
                )
                .join(Event, Event.search_id == Search.id, isouter=True)
                .group_by(Search.id, Search.position_code, Search.position_name)
                .order_by(Search.id)
            )
        ).all()

    if not rows:
        typer.echo("No searches found.")
        return

    typer.echo(f"{'ID':<4} {'Code':<25} {'Name':<30} {'Total':>6} {'Enriched':>9} {'Pending':>8}")
    typer.echo("-" * 90)
    for r in rows:
        total = r.total or 0
        enriched = int(r.enriched or 0)
        pending = total - enriched
        typer.echo(
            f"{r.id:<4} {r.position_code:<25} {r.position_name:<30} "
            f"{total:>6} {enriched:>9} {pending:>8}"
        )


if __name__ == "__main__":
    app()
