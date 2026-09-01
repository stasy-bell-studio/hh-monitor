# LLM Enrichment — Design & Operations

Module: `hh_monitor/llm_enrich/`  
Provider: корпоративная нейронка (OpenAI-совместимый API) → Qwen 3.8 (`qwen/qwen3.8-27b`)

---

## Purpose

After the rule-based scorer (`fit/rules.py`) assigns a `fit_score` (0–100), resumes
that clear the threshold (`SCORE_FIT_MIN_FOR_LLM`, default 60) are sent to the LLM
for a richer assessment.  The LLM returns a structured JSON verdict which is cached
and stored on the `resumes` row.

**Score formula:**

```
score_total = round(0.1 * fit_score + 0.9 * llm_score)
```

---

## Architecture

```
run_llm_enrichment()
  │
  ├─ SELECT Event rows (llm_enriched=FALSE, limit N)
  │
  └─ for each event:
       │
       ├─ _latest_snapshot()       → payload + content_hash
       ├─ fit_compute()            → fit_score + breakdown
       │
       ├─ stop-region guard        → skip if breakdown["area"] < 0
       ├─ threshold guard          → skip if fit_score < SCORE_FIT_MIN_FOR_LLM
       │
       ├─ llm_cache.get_cached()   → LlmResponse | None
       │
       └─ (cache miss)
            ├─ build_prompt()      → Jinja2 template render
            ├─ client.chat_completion()  → LLM API
            ├─ parse_response()    → LlmResponse (Pydantic)
            └─ llm_cache.save_cached()
       │
       ├─ UPDATE resumes SET llm_score, llm_verdict, llm_comment,
       │         llm_red_flags, llm_real_role, llm_scored_at,
       │         llm_content_hash, score_total
       └─ UPDATE events SET llm_enriched=TRUE
```

---

## Cache

Cache key: `f"{hh_resume_id}|{content_hash}|{prompt_version}"`

- `content_hash` — SHA-256 of the raw resume JSON (from `snapshots.content_hash`)
- `prompt_version` — controlled by `LLM_PROMPT_VERSION` env var (default `v5`)

A cache hit returns the stored `LlmResponse` without calling the API.  The entry
is not updated on a hit (INSERT … ON CONFLICT DO NOTHING).

**Invalidation:**
- Bump `LLM_PROMPT_VERSION` in `.env` to force re-scoring with a new prompt.
- `poetry run hh-monitor llm reset-cache <hh_resume_id>` to delete cache for one resume.

---

## Response Schema (`LlmResponse`)

| Field | Type | Description |
|---|---|---|
| `llm_score` | int 0–100 | Overall LLM score (clamped, float-tolerant) |
| `llm_verdict` | str | One of: `strong_yes`, `yes`, `maybe`, `no`, `strong_no` |
| `llm_comment` | str | 1–3 sentence summary |
| `llm_red_flags` | list[str] | Concerns (empty list if none) |
| `llm_real_role` | str | Last actual job title (empty if unknown) |

---

## Prompt Template

`config/portraits/prompt_template.j2`

Rendered with:
- `portrait` — `Portrait` instance (position_name, must_have_keywords, nice_to_have_keywords, stop_words)
- `resume_json` — cleaned resume payload JSON (keys `actions`, `photo`, `negotiations_history` stripped)

The template instructs the model to return **only** a JSON object.  `response_format={"type":"json_object"}`
is sent in the API request to further enforce this.  `parse_response()` applies a regex fallback
for models that wrap the JSON in prose text.

---

## HTTP Client

`hh_monitor/llm_enrich/client.py`

- Timeout: 60 s
- Max retries: 3 (on 429 and `httpx.TimeoutException`)
- Back-off: exponential with ±25% jitter, capped at 60 s
- 401 → `LlmAuthError` (no retry)
- Other 4xx/5xx → `LlmApiError` (no retry)

---

## CLI Commands

```bash
# Enrich up to 10 events for search id=1
poetry run hh-monitor llm run --search-id 1

# Enrich up to 50 without making API calls (cache hits still applied)
poetry run hh-monitor llm run --search-id 1 --limit 50 --dry-run

# Show fit + LLM scores for a specific resume
poetry run hh-monitor llm score <hh_resume_id> --search-id 1

# Delete cache for a resume (forces re-scoring)
poetry run hh-monitor llm reset-cache <hh_resume_id>

# Show enrichment stats per search
poetry run hh-monitor llm stats
```

---

## Configuration (`.env`)

```bash
LLM_API_KEY=<corporate-llm-key>
LLM_BASE_URL=https://llm.21-vek.spb.ru/v1
LLM_MODEL=qwen/qwen3.8-27b
LLM_PROMPT_VERSION=v1          # bump to invalidate all cache entries
SCORE_FIT_MIN_FOR_LLM=60       # fit_score threshold; 0 = enrich everyone
```

---

## Cost Estimate (Qwen 3.8, self-hosted corporate endpoint)

Модель развёрнута на собственном контуре компании, поэтому платите за инфраструктуру,
а не за токены. Ориентиры по объёму:

| Resumes/day | ~Input tokens | ~Output tokens | ~Cost/day |
|---|---|---|---|
| 50 | 200 K | 25 K | ~$0.08 |
| 200 | 800 K | 100 K | ~$0.33 |

Cache hits cost $0.  Run with `--dry-run` first to check pending counts.
