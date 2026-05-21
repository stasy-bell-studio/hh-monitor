# Portrait specification

A **portrait** («портрет») is a JSON file in `portraits/` that describes the
ideal candidate profile for one position. The fit-scoring engine
(`hh_monitor.fit.rules.compute`) reads portrait fields and returns a score in
the range `[0, 100]` plus a per-rule breakdown.

## JSON schema

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `position_code` | `string` | ✓ | Unique slug; must match `searches.position_code` in the DB |
| `position_name` | `string` | ✓ | Human-readable position title (shown in CLI output) |
| `title_keywords` | `string[]` | ✓ | Substrings matched case-insensitively against the candidate's current job title. Any match → **+25** |
| `experience_keywords` | `string[]` | ✓ | Substrings matched against concatenated `experience[].description` and `experience[].position`. Any match → **+15** |
| `min_total_months` | `int` | ✓ | Minimum acceptable total experience in months. Candidate below this threshold → **−10** |
| `preferred_total_months` | `int` | ✓ | Preferred total experience. Candidate ≥ this → **+20**; between `min` and `preferred` → **+10** |
| `min_salary` | `int \| null` | — | Reserved, not used in scoring yet |
| `max_salary` | `int \| null` | — | Maximum acceptable salary (RUR). Candidate ≤ max → **+10**; candidate above → **−15**; `null` or absent → **+10** |
| `preferred_education_levels` | `string[]` | — | `id` values from HH `/dictionaries` → `education_level`. Match → **+5** |
| `preferred_areas` | `string[]` | — | City names as returned in `area.name` (e.g. `"Санкт-Петербург"`). Match → **+10** |
| `age_range` | `[int, int] \| null` | — | Inclusive `[lo, hi]` age range. In range → **+5**; `null` or absent → age rule disabled, **0** |

## Scoring summary

| Rule | Key in breakdown | Points |
|------|-----------------|--------|
| Title keyword match | `title_match` | +25 / 0 |
| Experience keyword match | `experience_keywords` | +15 / 0 |
| Total experience | `total_experience` | +20 / +10 / −10 |
| Salary fit | `salary_fit` | +10 / −15 |
| Education | `education` | +5 / 0 |
| Area | `area` | +10 / 0 |
| Age | `age` | +5 / 0 |
| **Maximum possible** | | **90** |

> The final score is clamped to `[0, 100]`.

## How HR updates a portrait

1. Open the relevant file, e.g. `portraits/branch_director.json`.
2. Edit the fields you need (keywords, thresholds, salary cap, etc.).
3. Commit with a message like `chore(portraits): raise max_salary for branch_director`.
4. The new weights are applied automatically on the next detector/parser run.

Weights are calibrated jointly with the HR team. Current values are a PoC
approximation — adjust as signal quality improves.
