# OpenRouter Model Price-to-Performance Data Pipeline

Three dashboards are published from this repo, each with its own pipeline and
source data:

| Page | Pipeline | Source |
| --- | --- | --- |
| **Chat models** (`index.html`) | `run_pipeline.py` | `/api/v1/models` + Artificial Analysis indices |
| **Image models** (`image_model_analysis.html`) | `run_media_pipeline.py` | `/api/v1/images/models` + media prompt benchmarks + Design Arena |
| **Video models** (`video_model_analysis.html`) | `run_video_pipeline.py` | `/api/v1/videos/models` + video prompt benchmarks |

The three pipelines are deliberately independent - separate entry points,
snapshot roots, lock files and status files - so one cannot break another. A
single **Update All Latest AI Data** button (and `dashboard/refresh_all.py`) runs all three
and reports per-dashboard success/failure.

Data acquisition and validation phase only for the chat pipeline. Neither page
invokes a generation model: everything shown is scraped or read from published
catalogues, so refreshing never spends money on model API calls.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate   # on Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Run

```bash
python run_pipeline.py        # chat models
python run_media_pipeline.py  # image models (~90s)
python run_video_pipeline.py  # video models
```

Both require normal internet access to `https://openrouter.ai`. **They will
not run inside a network-restricted sandbox** — this project was scaffolded
and tested (with synthetic data) inside one, so the first real run needs to
happen from a normal terminal on your machine.

Run the test suite (uses saved fixtures, no network needed):

```bash
python -m pytest tests/ -v
```

## Refresh on demand

Each dashboard is a single self-contained HTML file with the data snapshot
baked in, so a copy opened straight from disk can never update itself - it has
no way to rewrite `data/normalized/` or republish the HTML. The refresh button
therefore needs a small local server:

```bash
python dashboard/serve.py
```

On Windows you can just double-click **`start_dashboard.bat`**, which starts the
server and opens the browser for you. Otherwise run the command above, open the
address it prints (`http://127.0.0.1:8765/`), then use **Update All Latest AI
Data** in the masthead - it is on all three pages and refreshes all dashboards in
one click.

The server has to stay running: if you close that window, the page still loads
from the browser cache but refresh will report that it cannot reach the
service. The same work is available from the command line:

```bash
python dashboard/refresh_all.py              # all dashboards, one command
python dashboard/refresh_all.py --targets media
python dashboard/refresh_all.py --targets video
python dashboard/refresh_all.py --skip-fetch # rebuild from data on disk
```

`dashboard/refresh_all.py` is the single entry point: it runs `refresh.py`
(chat), `refresh_media.py` (image) and `refresh_video.py` (video) as
**sequential, separate processes** and reports each outcome. It never merges
their code paths, so one pipeline cannot break another. A failure does not skip
the remaining targets - you get a per-dashboard summary rather than an
all-or-nothing result.
`dashboard/serve.py` only executes it, which is deliberate, so the identical
command is what CI calls.

A refresh fails safely: if a fetch fails, that dashboard is not rebuilt and the
previously published version keeps being served.

Server options:

| Flag | Default | Purpose |
| --- | --- | --- |
| `--host` | `127.0.0.1` | Bind address. Loopback only unless you change it. |
| `--port` | `8765` | Port. |
| `--token` | none | Require a token on refresh (`Authorization: Bearer <token>`). |
| `--cooldown-seconds` | `600` | Minimum gap between refreshes; earlier requests get `429`. |
| `--max-age-hours` | `72` | Freshness window enforced by the **chat** build. |
| `--media-max-age-hours` | `192` | Freshness window for the **image** build. Wider on purpose: media shares the weekly cadence. |
| `--video-max-age-hours` | `192` | Freshness window for the **video** build. |
| `--timeout-seconds` | `2100` | Whole-refresh ceiling; exceeds three 600-second target ceilings. |
| `--skip-fetch` | off | Rebuild only, no network (all pipelines). |

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

The **image dashboard** has its own Model of the Week with the same week
semantics over a single metric (pass rate ÷ benchmark cost), and its own history
file at `data/analysis/media_weekly_picks.json`. The two are deliberately
separate files: they lock different picks over different model sets, so a shared
record would let one pipeline's lock overwrite the other's. Its tie-break is the
one the page's own value ranking already uses - value, then model id - so the
locked pick can never disagree with the rank printed beside it. On all three pages the
card is labelled "Model of the week", shows the week range, and sits directly
above a head-to-head comparison against any other model in the snapshot.

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

## Image & Video Generation Models

Everything above this section describes the **chat/LLM** catalog. Image- and
video-generation models are collected by a second, deliberately independent
pipeline that reads two different, dedicated endpoints:

```bash
python run_media_pipeline.py                       # full run
python run_media_pipeline.py --no-snapshot         # don't write a snapshot
python run_media_pipeline.py --skip-image-pricing  # 2 requests instead of ~54
```

### Why it is a separate pipeline, not a mode of the existing one

Three reasons, all confirmed against the live API:

1. **Different endpoints.** `GET /api/v1/images/models` and
   `GET /api/v1/videos/models` are dedicated, documented catalogs. The generic
   catalog can also be filtered with
   `/api/v1/models?output_modalities=image|video`, but that returns the same
   models with pricing mostly zeroed out and no `pricing_skus`, so the dedicated
   endpoints are the primary source.
2. **Different pricing model.** Chat models are priced per token
   (`pricing.prompt` / `pricing.completion`). Media models are priced per
   *unit*: per image, per second of video, per video token, per
   megapixel-second, or as a per-generation minimum. Real cost for these models
   cannot be expressed as token pricing at all, so forcing them through
   `src/normalize.py`'s token path would produce nonsense. They get their own
   normalizer, `src/normalize_media.py`, which reuses `normalize.py`'s
   *principles* (never drop a model, never fabricate a price, preserve the raw
   object) without touching its code.
3. **Much smaller catalog.** The chat inventory is several hundred models; the
   media catalogs were 52 image and 29 video models on 2026-09-17. They
   therefore have their own floors (`MIN_EXPECTED_IMAGE_MODEL_COUNT` /
   `MIN_EXPECTED_VIDEO_MODEL_COUNT`, both 10), because reusing the chat
   pipeline's `MIN_EXPECTED_MODEL_COUNT` of 100 would falsely fail every run.

`run_media_pipeline.py` writes **only new files** - it never overwrites,
merges into, or reshapes anything the chat pipeline produces - and it is
deliberately **not** wired into `dashboard/refresh.py` or `run_weekly.bat`.
Hooking it into the weekly refresh is a separate decision.

### Where media pricing actually lives

- **Video models** publish a `pricing_skus` map inline on the list endpoint.
  The values are strings in *mixed units and vocabularies* - for example
  `"duration_seconds_720p": "0.08"` (USD per second) sitting next to
  `"cents_per_second_output": "3"` (cents per second), plus
  `"video_tokens"`, `"cents_per_megapixel_second_precise"`, `"reference_images"`
  and `"minimum_cents_per_generation"`. The published docs example
  (`per-video-second`) does not occur in live data, so the parser never assumes
  a fixed key list; an unrecognised key is reported as an unknown unit with no
  USD figure rather than guessed at.
- **Image models publish no pricing on the list endpoint at all.** Their cost
  is only in the per-model record
  `GET /api/v1/images/models/{id}/endpoints`, so the pipeline makes one extra
  request per image model to read it (~52 requests, ~40 s serialised with a
  small politeness delay). `--skip-image-pricing` skips that fan-out; image
  models are then written with no price at all.

Two unit assumptions are made, and both are recorded explicitly rather than
applied silently - see `pricing_unit_assumptions` in the coverage report and
the matching entries in the data-quality report:

- keys prefixed `cents_` are converted to USD (divided by 100);
- keys with no explicit currency marker are treated as USD (every bare key
  observed live is USD).

Each model also distinguishes `has_any_price` (there is some price) from
`has_valid_pricing` (there is a price in a unit comparable across models - USD
per second for video, USD per image for image). A model priced only per video
token is in the first bucket but not the second, and that stays visible rather
than being flattened into a single flag.

### Benchmarks

There are two media benchmark sources, and they are kept separate because they
measure different things:

**1. Design Arena** (`data/normalized/media_benchmarks.json`) — per-category Elo
and win rates, extracted when present, reusing the exact extraction logic the
chat pipeline already uses. It is **not** on the dedicated media endpoints: the
dedicated image endpoint publishes no `benchmarks` block at all, so these rows
come from the generic catalog filtered by output modality
(`/api/v1/models?output_modalities=image`), which exposes `design_arena` for a
subset of image models. Video models publish no Design Arena data on any
endpoint. Artificial Analysis composite indices — chat-model scores — are
explicitly filtered out and never mixed into the media benchmark output. Each
record carries `source_endpoint` so its origin is never ambiguous.

**2. Prompt benchmarks** (`data/normalized/media_prompt_benchmarks.json`) —
OpenRouter's own per-prompt evaluations at
[`/benchmarks/media/images`](https://openrouter.ai/benchmarks/media/images)
(15 prompts) and
[`/benchmarks/media/videos`](https://openrouter.ai/benchmarks/media/videos)
(12 prompts). Each page publishes, per model: a judged pass count
(*"5 of 5 checks passed"*), the actual cost of that generation in USD, and the
generation time in seconds — ~867 rows across 27 pages.

> **This one is scraped HTML, and that is a deliberate trade-off.** There is no
> public JSON API: the pages are server-rendered markup, and the obvious routes
> (`/api/v1/benchmarks`, `/api/v1/videos/benchmarks`) return
> `401 "No cookie auth credentials found"` — they exist but are session-gated.
> The data is public; only the transport is awkward. So the extraction is
> written defensively and the guards are load-bearing:
>
> - it anchors on the accessibility contract (`aria-label="N of M checks
>   passed"`) and falls back to the visible `N/M` text, recording **which** was
>   used on every row so a silent markup change shows up in the data-quality
>   report as `prompt_benchmark_aria_label_fallback`;
> - a page that parses to **zero** rows is a hard error, and the total across all
>   pages must clear `MIN_EXPECTED_TOTAL_PROMPT_BENCHMARK_ROWS` — because an
>   empty parse is indistinguishable from "these models have no benchmarks",
>   which is exactly the lie this pipeline must not tell;
> - each row is rendered twice by the page, so rows are de-duplicated, and if
>   the two copies ever disagree that is reported rather than silently resolved;
> - the parsed rows are preserved verbatim in
>   `data/raw/openrouter_media_prompt_benchmark_rows.json` together with a
>   sha256 + byte size per page. The full page HTML is deliberately **not**
>   committed: 27 pages is several MB per run, and this repo tracks `data/**`,
>   which would add hundreds of MB a year to history for markup we can re-fetch
>   at any time. The fingerprints are what actually matter for spotting drift.
>
> **Correctness is not a global score.** A pass count is the judged result for
> *one* prompt — 5/5 on `traffic-light` says nothing about `mirror-walk`. Pass
> rates are only comparable within the same `prompt_slug`, so rows are stored
> raw as (model, prompt) and any cross-prompt aggregate would be an
> interpretation, not a fact from the source. Prompt difficulty also genuinely
> varies how many models a page covers: 14 of the 15 image prompts cover 42
> models, but `composite-refs` (the multi-reference prompt) covers only 3, and
> video `walk-out` covers 14 against 24 for the rest. That is why there is no
> per-page row floor — only a zero-row error and a global total floor.

### What one media run produces

```text
data/raw/openrouter_image_models.json            - complete, unmodified response
data/raw/openrouter_video_models.json            - complete, unmodified response
data/raw/openrouter_image_model_endpoints.json   - raw per-model endpoint records + errors
data/normalized/image_models.json / .csv         - one record per image model
data/normalized/video_models.json / .csv         - one record per video model
data/normalized/media_benchmarks.json / .csv     - Design Arena rows
data/normalized/media_prompt_benchmarks.json/.csv- per-prompt correctness/cost/time rows
data/raw/openrouter_media_prompt_benchmark_rows.json - verbatim parsed rows + per-page sha256
data/analysis/media_coverage_report.json         - counts, price resolution rate, unit assumptions
data/analysis/media_data_quality_report.json     - errors/warnings/info
data/media_snapshots/YYYY-MM-DD/                 - timestamped copy of the above
```

Run the media tests on their own (offline, no network needed):

```bash
python -m pytest tests/test_media_fetch.py tests/test_media_normalize.py \
                 tests/test_media_benchmark_scraper.py \
                 tests/test_media_snapshot_paths.py -v
```

> **Why media snapshots live in `data/media_snapshots/`, not `data/snapshots/`.**
> This is load-bearing, not tidiness. `dashboard/refresh.py` decides whether
> today's chat snapshot already exists using `snapshot_exists_for()`, which does
> nothing more than look for a directory directly under `data/snapshots/` named
> `<date>` or `<date>-N`. Writing media snapshots to
> `data/snapshots/<date>/media/` therefore created a directory literally named
> `<date>`, so if the media pipeline ran on a UTC day before that day's chat
> refresh, `refresh.py` passed `--no-snapshot` to the chat pipeline and **that
> day's real chat snapshot was silently never written**.
>
> `data/media_snapshots/` cannot match that pattern, so the two pipelines are
> now fully independent — a media run never affects whether the chat pipeline
> snapshots. `dashboard/refresh.py`, `snapshot_exists_for()` and `SNAPSHOTS_DIR`
> were left untouched; the fix is entirely on the media side, and
> `tests/test_media_snapshot_paths.py` locks it in by calling the real
> `snapshot_exists_for()` rather than a copy of its logic.
