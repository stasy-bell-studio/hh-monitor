# hh-monitor — HR Resume Monitor for SK 21 Vek

## Project Context

Internal corporate tool for ООО «Страховая компания 21 век» that automates daily monitoring of the [hh.ru](http://hh.ru) resume database for six key positions. Output is a weekly digest of best-fit candidates delivered to HR via Notion and Telegram. **Closed corporate use only**, not for public distribution.

**Stakeholders:**

- **Tech lead / single developer**: Alexander Lukin (user of this Claude Code session)
- **HR owner / business owner**: Tatiana Lesnitskaya
- **Server / DevOps**: Vladimir Mikhaylov (server provisioning in progress)
- **PII / security gatekeeper**: Elena Golubeva (DPO)

## Hard Constraints (NEVER violate)

- ❌ **NEVER** call [hh.ru](http://hh.ru) contact-disclosure endpoints. Contacts are opened manually by HR in the [hh.ru](http://hh.ru) UI, never via API. The bot does not request, store, log, or display phone numbers, emails, or any other contact data of candidates.
- ❌ **NEVER** commit secrets (`client_secret`, OAuth tokens, Telegram bot tokens, Notion API keys, database passwords) to git. Use `.env` files (gitignored) and `.env.example` for documentation.
- ❌ **NEVER** call vacancy management endpoints (`POST /vacancies`, `PUT /vacancies/{id}`, etc.) — we don't create or edit vacancies through this app.
- ❌ **NEVER** export the resume database in bulk to any external system. We store only structured snapshots of resumes we have already legitimately viewed via API, for diff detection. We don't redistribute them.
- ❌ **NEVER** use `print` for logging — use the `structlog` logger configured in `hh_monitor.logging`.
- ❌ **NEVER** add a Python dependency without explicit approval from the user in the chat. Stick to the stack defined below.
- ✅ All candidate PII (full name, age, region, work history) stored in DB must be considered sensitive. The DB lives only on Russian-jurisdiction servers (local dev or corporate prod server). No cloud DB outside RF.

## Architecture (4 components)

```mermaid
flowchart LR
	A["Agent 1: Parser<br>daily cron"] -->|writes snapshots| DB[("PostgreSQL")]
	DB -->|reads consecutive snapshots| B["Agent 2: Detector<br>diff engine"]
	B -->|writes events| DB
	DB -->|reads events + fit_score| C["Notion Sync Worker"]
	C -->|upserts pages| N["Notion: База резюме HH"]
	DB -->|reads hot events| T["Telegram Bot"]
	T -->|sends alerts| TG["HR Telegram chat"]
	hh["hh.ru API"] -.->|OAuth + REST| A
```

**Agent 1 — Parser** (`hh_monitor.parser`)

Daily cron job. For each of the 6 saved searches: fetches the result list, then for each new or seen-recently resume calls `GET /resumes/{id}` and persists a full JSON snapshot to `snapshots` table. Respects [hh.ru](http://hh.ru) rate limits and the daily view quota. Resumable on crash (writes a `parser_runs` row).

**Agent 2 — Detector** (`hh_monitor.detector`)

Runs after parser completes. For each resume with ≥2 snapshots, diffs the latest two and emits typed events: `NEW`, `UPDATED_EXPERIENCE`, `UPDATED_SALARY`, `UPDATED_POSITION`, `REACTIVATED`, `REMOVED`. Writes to `events` table. Idempotent — re-running on the same snapshots produces no duplicate events.

**Notion Sync Worker** (`hh_monitor.notion_sync`)

For each new event, upserts the corresponding page in the Notion «База резюме HH» database. Maps event type → property values + content. Uses the Notion API (token from env). Cross-references the «Сотрудники» database to flag candidates already employed at SK 21 Vek.

**Telegram Bot** (`hh_monitor.tg_bot`)

Two modes: (a) hot alerts pushed when `fit_score > 90`, (b) on-demand `/digest` command returning current top-N for each position. Uses `aiogram` v3.

## Database Schema (PostgreSQL 16)

```sql
-- Saved hh.ru search definitions (6 positions × parameters)
CREATE TABLE searches (
	id             SERIAL PRIMARY KEY,
	position_code  TEXT NOT NULL UNIQUE,  -- 'branch_director', 'agency_director', etc.
	position_name  TEXT NOT NULL,
	hh_params      JSONB NOT NULL,        -- raw hh.ru search query params
	portrait       JSONB NOT NULL,        -- ideal-candidate profile for fit scoring
	active         BOOLEAN NOT NULL DEFAULT TRUE,
	created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Master list of resumes we have ever seen (one row per hh resume_id)
CREATE TABLE resumes (
	hh_resume_id   TEXT PRIMARY KEY,
	first_seen_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
	last_seen_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
	notion_page_id TEXT,                  -- set after first Notion sync
	archived       BOOLEAN NOT NULL DEFAULT FALSE  -- true when removed from hh.ru
);
CREATE INDEX idx_resumes_last_seen ON resumes(last_seen_at);

-- Full JSON snapshot of resume content at a point in time
CREATE TABLE snapshots (
	id             BIGSERIAL PRIMARY KEY,
	hh_resume_id   TEXT NOT NULL REFERENCES resumes(hh_resume_id),
	fetched_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
	payload        JSONB NOT NULL,        -- exact /resumes/{id} response
	content_hash   TEXT NOT NULL          -- sha256 of canonical payload, for dedup
);
CREATE INDEX idx_snapshots_resume_time ON snapshots(hh_resume_id, fetched_at DESC);
CREATE UNIQUE INDEX uq_snapshots_dedup ON snapshots(hh_resume_id, content_hash);

-- Detected change events (driven by detector)
CREATE TABLE events (
	id             BIGSERIAL PRIMARY KEY,
	hh_resume_id   TEXT NOT NULL REFERENCES resumes(hh_resume_id),
	event_type     TEXT NOT NULL,         -- NEW / UPDATED_EXPERIENCE / UPDATED_SALARY / UPDATED_POSITION / REACTIVATED / REMOVED
	search_id      INTEGER REFERENCES searches(id),
	details        JSONB,                 -- structured before/after for the change
	fit_score      INTEGER,               -- 0..100, computed at event emission
	created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
	notion_synced  BOOLEAN NOT NULL DEFAULT FALSE,
	telegram_sent  BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX idx_events_pending_notion ON events(notion_synced) WHERE notion_synced = FALSE;
CREATE INDEX idx_events_pending_telegram ON events(telegram_sent) WHERE telegram_sent = FALSE;

-- Audit trail of each parser cron execution
CREATE TABLE parser_runs (
	id             BIGSERIAL PRIMARY KEY,
	started_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
	finished_at    TIMESTAMPTZ,
	status         TEXT NOT NULL,         -- running / ok / failed / partial
	searches_run   INTEGER NOT NULL DEFAULT 0,
	resumes_seen   INTEGER NOT NULL DEFAULT 0,
	resumes_viewed INTEGER NOT NULL DEFAULT 0,
	error          TEXT
);
```

## [HH.ru](http://HH.ru) API Surface

**OAuth 2.0 Authorization Code flow (employer)** — see `docs/hh-oauth.md` (to be written in session 1).

**Endpoints we use:**

- `GET https://hh.ru/oauth/authorize` — user authorization redirect
- `POST https://hh.ru/oauth/token` — exchange code for token; refresh
- `GET /me` — verify token works
- `GET /employers/{employer_id}/managers` — service info
- `GET /areas` — region dictionary (cache locally, refresh weekly)
- `GET /dictionaries` — experience / employment / schedule dictionaries (cache locally)
- `GET /resumes` — **paid, requires DBSearch service** — search by saved params
- `GET /resumes/{resume_id}` — **paid, counts against view quota** — full resume content
- `GET /saved_searches/resumes` — list saved searches (optional, we may store params locally instead)

**Endpoints we DO NOT use** (explicit non-goals):

- Any `/resumes/{id}/contacts` / contact-revealing methods — PII boundary
- Any `POST`/`PUT`/`DELETE` on `/vacancies` — read-only stance on vacancies
- Any `/negotiations` (responses, invitations) — out of scope

**Rate limiting & errors:**

- Default: stay under 1 req/sec to [hh.ru](http://hh.ru) API. Use exponential backoff on `429`.
- `403 quota_exceeded` on `/resumes/{id}` means daily view quota exhausted — abort parser run gracefully, record in `parser_runs.status = 'partial'`, resume next day.
- `403` without quota mention = service not active for employer — alert via Telegram immediately and abort.
- `404` on `/resumes/{id}` = resume removed by candidate — mark `resumes.archived = TRUE`, emit `REMOVED` event.

## Tech Stack (locked)

| Layer | Choice |
| --- | --- |
| Language | Python 3.12+ |
| Package manager | Poetry |
| HTTP client | `httpx` (async) |
| ORM | SQLAlchemy 2.0 (async) |
| Migrations | Alembic |
| DB | PostgreSQL 16 (Docker locally, native on prod server) |
| CLI | `typer` |
| Logging | `structlog` (JSON to stdout) |
| Tests | `pytest`  • `pytest-asyncio` |
| Lint / format | `ruff` (replaces black + flake8 + isort) |
| Types | `mypy --strict` on `hh_monitor/*` |
| Telegram | `aiogram` v3 |
| Notion | direct `httpx` calls (Notion API v1, no SDK) |
| Scheduler | Plain Linux cron on prod; `apscheduler` for local dev |
| Container | Docker Compose for db; Dockerfile for app (added later) |

**Do not add other dependencies without asking the user.** Specifically: no FastAPI, no Django, no Celery, no Redis (yet), no Pydantic v1, no requests/aiohttp.

## Repository Layout

```
hh-monitor/
├── CLAUDE.md                  # this file
├── README.md                  # human-facing project intro
├── pyproject.toml             # Poetry config
├── poetry.lock
├── .env.example               # documented env vars; commit this
├── .env                       # actual secrets; NEVER commit
├── .gitignore
├── docker-compose.yml         # local Postgres (mounts init-test-db.sh)
├── alembic.ini
├── alembic/                   # migrations
│   └── versions/
├── hh_monitor/
│   ├── __init__.py
│   ├── cli.py                 # typer entry point
│   ├── config.py              # pydantic-settings, loads .env
│   ├── logging.py             # structlog setup
│   ├── db/
│   │   ├── __init__.py
│   │   ├── engine.py          # SQLAlchemy async engine
│   │   └── models.py          # ORM models matching the schema above
│   ├── hh/
│   │   ├── __init__.py
│   │   ├── oauth.py           # token storage, refresh
│   │   ├── client.py          # httpx wrapper with retry/backoff
│   │   └── endpoints.py       # typed methods: me(), search_resumes(), get_resume()
│   ├── parser/
│   │   ├── __init__.py
│   │   └── run.py             # main parser loop
│   ├── detector/
│   │   ├── __init__.py
│   │   ├── diff.py            # pure functions: snapshot diff -> events
│   │   └── run.py
│   ├── fit/
│   │   ├── __init__.py
│   │   ├── rules.py           # v1 rule-based scorer
│   │   └── portrait.py        # portrait schema + loader
│   ├── notion_sync/
│   │   ├── __init__.py
│   │   └── run.py
│   └── tg_bot/
│       ├── __init__.py
│       └── bot.py
├── scripts/
│   ├── init-test-db.sh        # creates hh_monitor_test on first volume init
│   └── seed_fixtures.py       # seeds synthetic resumes for smoke testing
├── tests/
│   ├── conftest.py
│   ├── fixtures/              # sample hh.ru JSON responses
│   └── test_*.py
└── docs/
	├── hh-oauth.md
	├── portrait-spec.md         # how the portrait JSON is structured
	└── decisions.md             # ADRs
```

## Conventions

**Code style:**

- All new modules: type hints on every function signature.
- Async by default for any I/O.
- Pure functions where possible (especially `detector.diff` and `fit.rules`) — keep them DB-free and easy to unit-test.
- Docstrings in Russian or English are both fine, but consistent within a module.
- Errors: prefer typed exceptions from `hh_monitor.errors`, never bare `Exception`.

**Tests:**

- Every PR adds tests for new logic. Target: ≥80% coverage on `detector/`, `fit/`, `hh/oauth.py`.
- HTTP is mocked with `respx`. No real [hh.ru](http://hh.ru) calls in tests.
- DB is `pytest-postgresql` ephemeral (or a per-test transaction rollback).

**Commits:**

- Conventional commits: `feat:`, `fix:`, `chore:`, `refactor:`, `test:`, `docs:`.
- Each commit message: brief subject in English, ≤72 chars. Body in Russian or English as needed.
- One logical change per commit.

**Branches:**

- `main` — protected, always green.
- Feature work on `feat/<short-name>`. PR to `main`. Squash-merge.

## How Claude Code Should Work

1. **Read this file first** at the start of every session.
2. **Confirm the plan** with the user before making large changes (>3 files or any new dependency).
3. **Run tests** after every meaningful change: `poetry run pytest`.
4. **Run lint** before declaring a task done: `poetry run ruff check . && poetry run ruff format --check . && poetry run mypy hh_monitor`.
5. **Commit in small, logical steps**, not one giant commit at the end.
6. **Ask for clarification** when business rules (e.g. fit-score thresholds, event types, portrait fields) are ambiguous. Don't guess.
7. **When in doubt about [hh.ru](http://hh.ru) API behavior**, fetch the official docs from `https://github.com/hhru/api/blob/master/docs/` and cite which doc file in the PR description.
8. **Don't touch `CLAUDE.md`** without asking the user — it's the source of truth maintained jointly with the Notion AI assistant («Сэм»).

## Environment Variables (`.env.example`)

```bash
# hh.ru OAuth (filled in after app moderation)
HH_CLIENT_ID=
HH_CLIENT_SECRET=
HH_REDIRECT_URI=https://localhost:8080/callback
HH_USER_AGENT="SK21Vek HR Monitor (luk44646@gmail.com)"

# Database
DATABASE_URL=postgresql+asyncpg://hh_monitor:hh_monitor_dev@localhost:5432/hh_monitor
TEST_DATABASE_URL=postgresql+asyncpg://hh_monitor:hh_monitor_dev@localhost:5432/hh_monitor_test

# Notion
NOTION_API_TOKEN=
NOTION_DATABASE_RESUMES_ID=
NOTION_DATABASE_EMPLOYEES_ID=

# Telegram
TELEGRAM_BOT_TOKEN=
TELEGRAM_HR_CHAT_ID=

# Runtime
ENV=local                     # local | prod
LOG_LEVEL=INFO
```

When prod env is provisioned by Mikhaylov, only `DATABASE_URL`, `HH_REDIRECT_URI`, and `ENV=prod` change. Application code stays identical.