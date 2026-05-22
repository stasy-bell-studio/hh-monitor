# Portrait Specification

A **portrait** («портрет») describes the ideal candidate profile for one position.
Portraits live in `config/portraits/*.yaml` and are loaded by `hh_monitor.fit.portrait`.
The fit-scoring engine (`hh_monitor.fit.rules.compute`) reads portrait fields and
returns a score in `[0, 100]` plus a per-rule breakdown.

---

## File format

Portraits are YAML files.  JSON is also accepted (legacy, backward compat).
The canonical format is YAML; see `config/portraits/_schema.yaml` for a fully
annotated template and `config/portraits/branch_director.yaml` for a real example.

---

## Pydantic schema

### Top-level `Portrait`

| Field | Type | Required | Description |
|---|---|---|---|
| `position_code` | `str` | ✓ | Unique slug — must match `searches.position_code` in DB |
| `position_name` | `str` | ✓ | Human-readable title (shown in CLI output and LLM prompt) |
| `search_params` | `dict` | — | HH.ru search query params (`text`, `experience`, etc.) |
| `filters` | `Filters` | — | Geography, age, salary, education filters |
| `weights` | `Weights` | — | Per-rule score weights (defaults sum to 90) |
| `stop_words` | `list[str]` | — | Soft red flags passed to LLM prompt |
| `must_have_keywords` | `list[str]` | — | Mandatory competencies for LLM prompt |
| `nice_to_have_keywords` | `list[str]` | — | Desired competencies for LLM prompt |
| `title_keywords` | `list[str]` | — | Matched against resume title (rule-based scorer) |
| `experience_keywords` | `list[str]` | — | Matched against experience text (rule-based scorer) |
| `min_total_months` | `int` | — | Minimum acceptable experience in months (default 0) |
| `preferred_total_months` | `int` | — | Preferred experience (default 24) |
| `min_salary` | `int\|null` | — | Reserved (not yet scored) |
| `max_salary` | `int\|null` | — | Max acceptable RUR salary — used by legacy salary rule |
| `preferred_education_levels` | `list[str]` | — | HH education level ids (legacy compat) |
| `preferred_areas` | `list[str]` | — | Fallback primary regions when `filters.regions.primary` is empty |
| `age_range` | `[int,int]\|null` | — | Fallback age range when `filters.age_range` is None |

### `Filters`

| Field | Type | Description |
|---|---|---|
| `regions` | `RegionFilters` | Three-tier geography filter |
| `age_range` | `[int,int]\|null` | Inclusive `[lo, hi]` age range |
| `salary_range` | `[int,int]\|null` | `[min_rub, max_rub]` salary range |
| `education_level` | `list[str]` | HH education level ids |

### `RegionFilters`

| Field | Scoring | Description |
|---|---|---|
| `primary` | `+weights.region` | Target regions (default +10) |
| `adjacent` | `+weights.region // 2` | Neighbouring regions (default +5) |
| `stop` | `-(10**6)` → score=0, LLM skipped | Forbidden regions |

**Match logic:** portrait region entry is a case-insensitive substring of `area.name`
from the resume payload.  Example: `"Самарская область"` matches
`"Самара, Самарская область"`.

**Priority:** `stop > primary > adjacent > no-match`.

Callers detect stop-region via `breakdown.get("area", 0) < 0`.

### `Weights`

All fields are integers.  Default sum = 90 (leaves headroom for extra rules).

| Field | Default | Rule |
|---|---|---|
| `title_match` | 25 | Title keyword match |
| `experience_keywords` | 15 | Experience text keyword match |
| `total_experience` | 20 | Total months of experience |
| `salary_fit` | 10 | Salary within budget |
| `education` | 5 | Education level match |
| `region` | 10 | Primary region weight (adjacent = half) |
| `age` | 5 | Age within range |

---

## Rule-based scoring summary

| Rule | Breakdown key | Points |
|---|---|---|
| Title keyword match | `title_match` | +25 / 0 |
| Experience keyword match | `experience_keywords` | +15 / 0 |
| Total experience ≥ preferred | `total_experience` | +20 |
| Total experience ≥ min | `total_experience` | +10 |
| Total experience < min | `total_experience` | −10 |
| Salary ≤ max_salary | `salary_fit` | +10 |
| Salary > max_salary | `salary_fit` | −15 |
| Education level match | `education` | +5 / 0 |
| Primary region | `area` | +10 |
| Adjacent region | `area` | +5 |
| No region match | `area` | 0 |
| Stop region | `area` | −∞ → clamps to 0 |
| Age in range | `age` | +5 / 0 |
| **Max possible** | | **90** |

Final score is clamped to `[0, 100]`.

---

## CSV import

Tatiana Lesnitskaya provides portrait definitions as a CSV file.
Run the importer to produce/update YAML files:

```bash
poetry run hh-monitor portraits import portraits.csv
# or direct script:
poetry run python scripts/import_portraits_csv.py portraits.csv
```

CSV columns: `position_code`, `position_name`, `region_primary` (;-sep),
`region_adjacent` (;-sep), `region_stop`, `age_min`, `age_max`, `salary_min`,
`salary_max`, `education_min` (,-sep), `stop_words`, `must_have_keywords`,
`nice_to_have_keywords`.

The importer validates each row via Pydantic before writing.  Idempotent —
re-running on the same CSV safely overwrites existing files.

---

## How HR updates a portrait

1. Open `config/portraits/<position_code>.yaml`.
2. Edit the fields (keywords, thresholds, regions, etc.).
3. Run `poetry run hh-monitor portraits validate` to confirm syntax.
4. Commit: `chore(portraits): <what changed and why>`.
5. The new portrait is applied on the next `llm run` or `pipeline run`.

Weights are calibrated jointly with the HR team.  Current values are a first
approximation — adjust as signal quality improves.
