# OpenRouter Model Price-to-Performance Data Pipeline

Data acquisition and validation phase only. This does **not** produce a
final performance score or a value ranking — it collects OpenRouter's model
inventory and pricing reliably, investigates what benchmark data (if any)
OpenRouter actually exposes, and reports coverage so that decision can be
made on evidence.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate   # on Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Run

```bash
python run_pipeline.py
```

This requires normal internet access to `https://openrouter.ai`. **It will
not run inside a network-restricted sandbox** — this project was scaffolded
and tested (with synthetic data) inside one, so the first real run needs to
happen from a normal terminal on your machine.

Run the test suite (uses synthetic fixtures, no network needed):

```bash
python -m pytest tests/ -v
```

## Refresh on demand

The dashboard is a single self-contained HTML file with the data snapshot
baked in, so a copy opened straight from disk can never update itself - it has
no way to rewrite `data/normalized/` or republish the HTML. The refresh button
therefore needs a small local server:

```bash
python dashboard/serve.py
```

On Windows you can just double-click **`start_dashboard.bat`**, which starts the
server and opens the browser for you. Otherwise run the command above, open the
address it prints (`http://127.0.0.1:8765/`), then use **Refresh data** in the
masthead.

The server has to stay running: if you close that window, the page still loads
from the browser cache but refresh will report that it cannot reach the
service. The same work is available from the command line:

```bash
python dashboard/refresh.py                # fetch + rebuild (one command)
python dashboard/refresh.py --skip-fetch   # rebuild from data already on disk
```

`dashboard/refresh.py` is the single entry point: it runs the pipeline, locks
this week's picks, then rebuilds the HTML. `dashboard/serve.py` only executes
it - which is deliberate, so the identical command can later be called by a
GitHub Action with no changes.

A refresh fails safely: if the fetch fails, nothing is rebuilt and the
previously published dashboard keeps being served.

Server options:

| Flag | Default | Purpose |
| --- | --- | --- |
| `--host` | `127.0.0.1` | Bind address. Loopback only unless you change it. |
| `--port` | `8765` | Port. |
| `--token` | none | Require a token on refresh (`Authorization: Bearer <token>`). |
| `--cooldown-seconds` | `600` | Minimum gap between refreshes; earlier requests get `429`. |
| `--max-age-hours` | `72` | Freshness window enforced by the build. |
| `--skip-fetch` | off | Rebuild only, no network. |

Guards: one refresh at a time (`409`), a cooldown (`429`) with `Retry-After`,
cross-origin `POST`s rejected (`403`), a required JSON content type (`415`),
an optional token (`401`), and only the dashboard file is served, by exact
filename - no directory listing and no traversal.

> **Before anyone outside this machine can reach it:** add TLS, real
> authentication (not a shared token) and per-user rate limits, and prefer the
> GitHub Action path so the pipeline never runs on an internet-facing box.
> Opening the dashboard as a plain file still works - the refresh control just
> disables itself and says why.

## The weekly Model of the Week

The headline recommendation is the best-value model for the selected metric
(composite index divided by blended price). It is now tracked per week:

- a week runs **Monday 00:00:00 to Sunday 23:59:59 Asia/Manila** (fixed UTC+8);
- the pick is **locked on the first refresh of the week** and is never
  overwritten by later refreshes in that week;
- if a later refresh produces a different #1, the card shows
  *"Current leader today: ..."* and the change is appended to a revision log,
  so Thursday/Friday movement stays visible without rewriting the week's record;
- that new #1 normally becomes next week's pick, because next Monday's first
  refresh locks from then-current data.

So a mid-week refresh updates prices, charts, tables and coverage, but the
week's recorded pick stands. History lives in
`data/analysis/weekly_picks.json` - one entry per metric per week, with the
top-3 contenders and any revisions. Ties break deterministically on value
ratio, then score, then price, then model id.

**Snapshot note:** an on-demand refresh deliberately writes at most one
snapshot per day (`--no-snapshot` is passed when today's snapshot already
exists). This relaxes design principle 8 below, which otherwise said *every*
run snapshots - clicking refresh repeatedly would grow `data/snapshots/`
without bound.

## What one run produces

```text
data/raw/openrouter_models.json        - complete, unmodified API response
data/raw/openrouter_benchmarks.json    - benchmark discovery probe results
data/normalized/models.json            - one record per OpenRouter model
data/normalized/benchmarks.json        - one record per model/benchmark measurement (empty until a benchmark source is confirmed - see below)
data/normalized/model_benchmarks.json  - same as benchmarks.json (kept separate per spec naming)
data/analysis/coverage_report.json     - model & benchmark coverage stats
data/analysis/data_quality_report.json - errors/warnings/info (dupes, missing prices, unmatched benchmark IDs, version/scale inconsistencies)
data/analysis/benchmark_discovery.json - what the discovery step found (endpoint probes + field scan)
data/analysis/sample_inspection.json   - explicit check for qwen/qwen3-8b and Solar Pro 4
data/analysis/report.txt               - human-readable summary
data/snapshots/YYYY-MM-DD/             - a timestamped copy of the above, never overwritten
```

## The benchmark question — CONFIRMED against a live run (2026-09-08)

OpenRouter has no documented public "benchmarks" endpoint, but a real run
against `/api/v1/models` (428 models) found the answer: **benchmark data is
embedded directly on each model object**, under a `benchmarks` field. Here
is exactly what's there:

```json
"benchmarks": {
  "artificial_analysis": {
    "intelligence_index": 46.9,
    "coding_index": 71.8,
    "agentic_index": 49.9
  },
  "design_arena": [
    {"arena": "models", "category": "3d", "elo": 1150, "rank": 60, "win_rate": 41.2}
  ]
}
```

**The important catch: none of this is GPQA Diamond, IFBench, or
AA-Omniscience.** OpenRouter exposes exactly three Artificial Analysis
*composite* indices (intelligence / coding / agentic) — not the individual
evals that go into them — plus a separate "Design Arena" benchmark (a
different provider than Artificial Analysis, kept distinct per the
no-mixing-providers rule) covering UI/code/game-dev generation quality.

What this means for the three product categories (reasoning, general
knowledge, instruction following):

- **AA Intelligence Index** is the closest available proxy, but it's a
  blend across many underlying evals (Artificial Analysis's own
  methodology, which changes over time) — not a clean reasoning-only or
  knowledge-only score. Using it as a stand-in is a real assumption, not a
  fact, and is currently left `uncategorized` in
  `config/benchmark_categories.json` rather than silently mapped to
  "reasoning."
- **AA Coding Index** is coding — correctly excluded per the original spec.
- **AA Agentic Index** and **Design Arena** don't correspond to any of the
  three target categories at all.
- Coverage is also uneven: of 428 models, 179 have `coding_index`, 98 have
  `agentic_index`, but only 55 have `intelligence_index` — so even the best
  available proxy covers under 13% of the inventory.

**Answer to the project's central question, as it stands today:** no —
OpenRouter's public model-listing API does not expose enough standardized
per-capability (reasoning / knowledge / instruction-following) benchmark
data to rank models on those dimensions specifically. Getting GPQA Diamond,
IFBench, or AA-Omniscience scores would require a different source —
Artificial Analysis's own site/API directly, or another benchmark
aggregator — which is a scope decision for the next phase, not something to
paper over here.

The pipeline still extracts and reports everything OpenRouter *does* expose
(`src/normalize.py` → `extract_benchmark_entries_from_raw_models`), so
`data/normalized/benchmarks.json` is populated and the coverage numbers in
`data/analysis/report.txt` are real — they just measure different things
than originally hoped for. `config/benchmark_categories.json` documents this
history explicitly under `_not_found_in_live_data`.

One more real finding worth knowing about: the consistency-check step
(Section 10) caught `meta-llama/llama-4-maverick` reporting an
`agentic_index` of `0.6` while every other model is on a roughly 0–100
scale — flagged as an `inconsistent_score_scale` warning in
`data_quality_report.json` rather than silently averaged in. That's the
kind of thing this step exists to catch.

## Design principles this pipeline follows

1. Raw data is always preserved (`data/raw/`), separately from normalized.
2. No model is ever dropped for lacking pricing or benchmarks — the pipeline
   distinguishes "model exists" / "has pricing" / "has benchmark data" /
   "has enough benchmark data to rank" as four separate facts.
3. Missing benchmark data is represented as `null` / absence, never as a
   zero score.
4. Benchmark provenance is always recorded (`benchmark_source`,
   `source_platform`) — no mixing of benchmark providers.
5. `benchmark_version` / timestamps are `null` when OpenRouter doesn't
   provide them — never fabricated.
6. The fetch step fails loudly (raises `OpenRouterAPIError`, writes nothing
   to `data/normalized/`) rather than silently writing a partial dataset —
   see `MIN_EXPECTED_MODEL_COUNT` in `config/settings.py`.
7. The cost formula (`3×input + 1×output ) / 4` per Section 12) is a
   configurable assumption in `config/settings.py`, with alternative ratios
   listed for later sensitivity analysis — not hardcoded into the report.
8. Every run writes an immutable, timestamped snapshot under
   `data/snapshots/` in addition to updating the "latest" files in
   `data/raw/` and `data/normalized/`. (Exception: on-demand refreshes from
   `dashboard/refresh.py` write at most one snapshot per day - see
   "The weekly Model of the Week" above.)

## Project layout

```text
config/
  settings.py                  - all tunable assumptions (weights, endpoints, thresholds)
  benchmark_categories.json    - benchmark → category mapping (hypothesis, needs verification)
src/
  openrouter_client.py         - HTTP fetch with retries/timeout/validation, fails loudly
  discover.py                  - benchmark endpoint/field discovery (Section 3)
  normalize.py                 - model & benchmark normalization, pricing, matching layer
  categorize.py                - benchmark categorization + version/scale consistency checks
  coverage.py                  - coverage report + data-quality report + human-readable report
  pipeline.py                  - orchestrates the full run + snapshotting
dashboard/
  template.html                - hand-edited dashboard source (HTML + CSS + JS)
  build_dashboard.py           - injects the data payload, writes price_performance_final.html
  weekly_picks.py              - Monday-Sunday pick locking, revision log, tie-breaking
  refresh.py                   - fetch -> lock weekly picks -> rebuild (single entry point)
  serve.py                     - local server + on-demand refresh endpoint
  price_performance_final.html - generated artifact (never edit by hand)
run_pipeline.py                 - entrypoint (python run_pipeline.py)
tests/                          - pytest suite using synthetic fixtures (no network required)
```

## Known open questions for the next phase

- **Decide whether AA Intelligence Index is an acceptable proxy** for
  "reasoning + general knowledge" given it only covers ~13% of the
  inventory and blends multiple capabilities — or whether ranking should
  wait for a real per-capability benchmark source. This is the key decision
  gating Section 18/19 (performance score, value ranking).
- If per-capability scores are required, evaluate pulling from Artificial
  Analysis directly (their own API/site) rather than OpenRouter.
- `qwen/qwen3-8b` and Solar Pro 4 (`upstage/solar-pro4`, no hyphen before
  "4") were both present and correctly matched in the live run —
  `data/analysis/sample_inspection.json` checks this every run going
  forward.
- 5 models (`openrouter/auto`, `openrouter/auto-beta`, `openrouter/fusion`,
  `openrouter/pareto-code`, `openrouter/bodybuilder`) use OpenRouter's `-1`
  pricing sentinel for dynamic/auto-router pricing — handled as "pricing
  unavailable," not a literal negative price (see `is_dynamic_pricing` on
  each normalized model record).
- Whether cache-related pricing fields (`input_cache_read`,
  `input_cache_write`, seen preserved under each model's
  `cache_related_pricing_fields`) need to be folded into the cost model —
  inspect real data before deciding.
