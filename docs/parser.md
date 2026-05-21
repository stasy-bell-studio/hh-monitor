# Parser — design and operation notes

## Purpose

`hh_monitor.parser` is **Agent 1** in the architecture.  Its only job is to
fetch the current list of matching resumes from hh.ru and persist a full JSON
snapshot for each one.  It does **not** diff snapshots, compute fit scores, or
send notifications — those belong to the detector and notification workers.

## Algorithm (`run_parser`)

```
1.  Load the Search row.           → SearchNotFoundError if absent.
2.  INSERT ParserRun(status='running').
3.  For page in 0 … max_pages-1:
    a.  GET /resumes?<search_params>&page=<p>&per_page=50
        → HHQuotaExceeded | HHServiceNotActive → commit partial, re-raise.
    b.  For each item in the page:
        i.  sleep(_sleep)           rate-limit: ≤ 2 req/s at default 0.5 s.
        ii. GET /resumes/{id}
            → 404 (HHNotFound)     → payload = {"id": resume_id}, errors++
            → quota/service error  → commit partial, re-raise.
            → other HHApiError     → errors++, continue (skip snapshot).
        iii. UPSERT resumes(last_seen_at = NOW())
        iv.  content_hash = sha256(json.dumps(payload, sort_keys=True))
             if hash == latest snapshot hash → skip (dedup).
        v.   INSERT Snapshot(payload, content_hash).
    c.  Break if page ≥ total_pages − 1 (server says no more pages).
4.  UPDATE ParserRun: status, counts, finished_at.  COMMIT.
5.  Re-raise abort exception if set (quota / service error).
```

## 404 handling

When `GET /resumes/{id}` returns 404 the candidate deleted their resume.
The parser stores a **minimal snapshot** `{"id": resume_id}` so the detector
can recognise the REMOVED event via `diff_snapshots(prev=full, curr=empty)`.
The `errors` counter is incremented; the run continues with the next item.

`resumes.archived` is **not** set here — that flag is dead code from an earlier
design iteration; the detector drives the archived state through events.

## Deduplication

Content-hash dedup prevents redundant snapshots when a resume hasn't changed
since the last run.  `_get_last_hash` fetches the `content_hash` of the most
recent snapshot; if it matches the current payload's hash the INSERT is skipped
and `snapshots_skipped` is incremented.

## Rate limiting

`_sleep: float = 0.5` between individual resume fetches → ≤ 2 req/s.
Pass `_sleep=0` in tests to avoid artificial delays.

hh.ru also imposes a **daily view quota** on `GET /resumes/{id}`.  On a
`403 quota_exceeded` response the parser commits whatever partial state it has
and re-raises `HHQuotaExceeded` so the CLI can print a user-friendly message.
The `parser_runs.status` column will contain `'quota_exceeded'`.

## employer_id

If `HH_EMPLOYER_ID` is set in `.env`, it is merged into every
`GET /resumes` query as `employer_id=<value>`.  This scopes the search to
resumes that responded to your employer's vacancies.  The OAuth token already
restricts access, so this parameter is optional — omit it for broad searches.

## ParserRun status values

| status            | meaning                                      |
|-------------------|----------------------------------------------|
| `running`         | still in progress                            |
| `ok`              | completed with zero errors                   |
| `partial_errors`  | completed but some resume fetches failed     |
| `quota_exceeded`  | aborted by daily view quota (403)            |

## CLI usage

```bash
# Add a saved search (required before parsing)
poetry run hh-monitor searches add \
  --name "Branch Director SPb" \
  --query '{"text":"директор филиала","area":"2","experience":"between3And6"}' \
  --portrait portraits/branch_director.json

# List saved searches
poetry run hh-monitor searches list

# Parse resumes for search id=1
poetry run hh-monitor parse run --search-id 1 --max-pages 5

# Full pipeline: parse → detect → score → top-N
poetry run hh-monitor pipeline run --search-id 1 --top 10
```

## Return value of `run_parser`

```python
{
    "resumes_seen":           int,   # items returned by /resumes across all pages
    "snapshots_inserted":     int,   # new snapshots written to DB
    "snapshots_skipped_dedup":int,   # unchanged resumes skipped
    "errors":                 int,   # 404s + other transient API errors
    "parser_run_id":          int,   # PK of the ParserRun row
    "resume_ids":             list[str],  # de-duplicated IDs from search results
}
```
