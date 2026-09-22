#!/usr/bin/env python3
"""
Rebuild the image-generation model dashboard HTML from the media pipeline's
normalized data. Pure local file I/O - no network access needed.

This is the media-side sibling of build_dashboard.py and deliberately shares
none of its inputs or outputs:

    chat page                          image page (this script)
    data/normalized/models.json        data/normalized/image_models.json
    data/normalized/model_benchmarks   data/normalized/media_prompt_benchmarks.json
    data/analysis/coverage_report      data/analysis/media_coverage_report.json
    dashboard/template.html            dashboard/image_template.html
    dashboard/price_performance_final  dashboard/image_model_analysis.html

Nothing here writes a status sidecar, a snapshot directory, or any path the
chat pipeline (run_pipeline.py / dashboard/refresh.py) reads or watches.

Metric definitions (decided, not derived):
  performance  = sum(checks_passed) / sum(checks_total) over ALL of a model's
                 rows in media_prompt_benchmarks - one flat pool, no category
                 grouping. Note this is a checks-weighted mean of the per-prompt
                 pass rates, NOT the unweighted mean of `pass_rate`.
  price        = mean `cost_usd` over those same rows. Deliberately NOT the
                 catalog's pricing/SKU fields: only the benchmark rows' own
                 cost makes performance and price apples-to-apples, because
                 both come from the same generations.
  value        = performance / price (higher is better).
Design Arena Elo/win-rate is NEVER blended into the value score - it is
surfaced in its own panel and nothing in this dataset implies Artificial
Analysis coverage.

Usage:
    python build_image_dashboard.py [--max-age-hours 192]

Exit codes:
    0  - dashboard rebuilt successfully -> dashboard/image_model_analysis.html
    1  - media data missing/stale, or the join produced no models. Nothing is
         written in this case.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DASHBOARD_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = DASHBOARD_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(DASHBOARD_DIR) not in sys.path:
    sys.path.insert(0, str(DASHBOARD_DIR))

from config import settings  # noqa: E402  (needs the path insert above)
import page_shell  # noqa: E402  (sibling module - shared with the chat builder)

# --- inputs: media-pipeline outputs only -----------------------------------
IMAGE_MODELS_PATH = settings.MEDIA_IMAGE_MODELS_JSON_PATH
PROMPT_BENCHMARKS_PATH = settings.MEDIA_PROMPT_BENCHMARKS_JSON_PATH
DESIGN_ARENA_PATH = settings.MEDIA_BENCHMARKS_JSON_PATH
COVERAGE_PATH = settings.MEDIA_COVERAGE_REPORT_PATH
QUALITY_PATH = settings.MEDIA_DATA_QUALITY_REPORT_PATH
SNAPSHOTS_DIR = settings.MEDIA_SNAPSHOTS_DIR

# --- outputs ---------------------------------------------------------------
TEMPLATE_PATH = DASHBOARD_DIR / "image_template.html"
OUTPUT_PATH = DASHBOARD_DIR / "image_model_analysis.html"

# --- tunables (echoed into the payload so labels, tests and JS agree) ------
TOP_N_BARS = 12

# A "budget" model is one that is cheap AND still clears a minimum pass rate.
# The threshold is a judgement call, so it lives here as one named constant and
# travels with the payload - the card prints it rather than implying it.
#
# 0.70 was chosen from the observed distribution of the 42 benchmarked models
# on 2026-09-17 (min 0.288, p25 0.627, median 0.746, p75 0.814, max 0.932):
# 25 of 42 clear it, so the card is neither empty nor a copy of the full table,
# and clearing it means "at or above roughly the middle of the field".
BUDGET_MIN_PASS_RATE = 0.70
BUDGET_TOP_N = 5

# Scatter point sizing. Evidence per model is sum(checks_total) - how many
# judged checks actually stand behind the point. Prompt COUNT is not usable for
# this: every image model appears on 14 prompts and three on 15, so prompt
# count carries almost no signal.
POINT_RADIUS_MIN = 4.0
POINT_RADIUS_MAX = 11.0

# The trend component is a placeholder until at least this many dated
# media snapshots exist.
MIN_TREND_POINTS = 2

# The media pipeline is not on the chat pipeline's weekly schedule yet, so a
# manual rebuild can legitimately follow a media run by more than a few days.
DEFAULT_MAX_AGE_HOURS = 192.0

DESIGN_ARENA_SOURCE = "Design Arena"

CATEGORY_LABELS = {
    "graphicdesign": "Graphic design",
    "image": "Image",
    "logo": "Logo",
    "imageediting": "Image editing",
}

METHODOLOGY_NOTE = (
    "Performance is every judged check this model passed divided by every check it attempted, "
    "pooled flat across its benchmark prompts - no per-category grouping or weighting. Price is the "
    "mean cost_usd of those same generations, not the catalogue's per-image/per-megapixel/per-token "
    "rate, so price and performance come from the identical rows and are directly comparable. "
    "Cost comparability caveat: no benchmark row records the resolution or settings it was generated "
    "at, so these costs are only comparable if the benchmark harness held resolution fixed across "
    "models - an assumption this dataset cannot verify."
)

PROVENANCE_NOTE = (
    "Benchmarks are OpenRouter's media prompt benchmark pages plus the Design Arena preference scores "
    "published on OpenRouter's catalogue. Artificial Analysis publishes no image-generation data and "
    "nothing on this page implies otherwise. Design Arena is never folded into the value score. "
    "Latency, uptime and provider-level effective pricing are not captured by this pipeline and are "
    "not shown."
)


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------
def _load_json(path: Path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def read_retrieved_at() -> Optional[str]:
    """The pipeline stamps every normalized record with retrieved_at; the first
    record's value is the snapshot timestamp for the whole run."""
    if not IMAGE_MODELS_PATH.exists():
        return None
    try:
        models = _load_json(IMAGE_MODELS_PATH)
    except (ValueError, OSError):
        return None
    if not models:
        return None
    return models[0].get("retrieved_at")


def data_age_hours(retrieved_at: Optional[str]) -> Optional[float]:
    if not retrieved_at:
        return None
    try:
        moment = datetime.fromisoformat(retrieved_at)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - moment).total_seconds() / 3600


def check_freshness(max_age_hours: float) -> tuple[bool, str]:
    if not IMAGE_MODELS_PATH.exists():
        return False, f"{IMAGE_MODELS_PATH} does not exist - has the media pipeline ever run?"

    try:
        models = _load_json(IMAGE_MODELS_PATH)
    except (ValueError, OSError) as exc:
        return False, f"could not read {IMAGE_MODELS_PATH}: {exc}"
    if not models:
        return False, "image_models.json exists but is empty"

    retrieved_at = models[0].get("retrieved_at")
    if not retrieved_at:
        return False, "image_models.json has no retrieved_at timestamp - can't verify freshness"

    age_hours = data_age_hours(retrieved_at)
    if age_hours is None:
        return False, f"could not parse retrieved_at: {retrieved_at!r}"
    if age_hours > max_age_hours:
        return False, (
            f"media data is {age_hours:.1f} hours old (retrieved_at={retrieved_at}), "
            f"older than the {max_age_hours}-hour freshness window - the media "
            f"pipeline may not have run"
        )
    return True, f"media data is {age_hours:.1f} hours old (retrieved_at={retrieved_at}) - fresh"


# ---------------------------------------------------------------------------
# metric helpers (pure)
# ---------------------------------------------------------------------------
def image_benchmark_rows(prompt_rows: list[dict]) -> list[dict]:
    """Only the image half of the prompt benchmark file, and only rows that
    resolved to a catalogue model."""
    return [
        row for row in prompt_rows
        if row.get("media_type") == "image" and row.get("model_id")
    ]


def aggregate_performance(rows: list[dict]) -> dict[str, dict]:
    """One flat pool per model: sum(checks_passed) / sum(checks_total).

    This is deliberately not the mean of the per-row `pass_rate` values. Those
    differ whenever a model's prompts were judged at different check counts
    (observed: 4 and 5), because a flat aggregate weights each *check* equally
    while a mean of rates weights each *prompt* equally.
    """
    pools: dict[str, dict] = defaultdict(
        lambda: {"checks_passed": 0, "checks_total": 0, "prompts": 0, "costs": [], "names": []}
    )
    for row in rows:
        model_id = row.get("model_id")
        if not model_id:
            continue
        pool = pools[model_id]

        passed = row.get("checks_passed")
        total = row.get("checks_total")
        if isinstance(passed, int) and isinstance(total, int):
            pool["checks_passed"] += passed
            pool["checks_total"] += total
            pool["prompts"] += 1

        cost = row.get("cost_usd")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            pool["costs"].append(float(cost))

        name = row.get("model_name")
        if name:
            pool["names"].append(name)

    aggregated: dict[str, dict] = {}
    for model_id, pool in pools.items():
        total = pool["checks_total"]
        costs = pool["costs"]
        names = pool["names"]
        pass_rate = (pool["checks_passed"] / total) if total else None
        aggregated[model_id] = {
            "checks_passed": pool["checks_passed"],
            "checks_total": total,
            "pass_rate": round(pass_rate, 6) if pass_rate is not None else None,
            "prompts": pool["prompts"],
            "avg_cost_usd": round(sum(costs) / len(costs), 6) if costs else None,
            "cost_rows": len(costs),
            "evidence_checks": total,
            "benchmark_name": names[0] if names else None,
        }
    return aggregated


def value_score(pass_rate: Optional[float], avg_cost_usd: Optional[float]) -> Optional[float]:
    """Performance per dollar. Undefined - never zero, never guessed - when the
    model has no benchmark rows or no usable benchmark cost."""
    if pass_rate is None or avg_cost_usd is None:
        return None
    if avg_cost_usd <= 0:
        return None
    return pass_rate / avg_cost_usd


def point_radius(evidence_checks: Optional[int], lo: Optional[int], hi: Optional[int]) -> Optional[float]:
    """Scatter radius for a model, from how much evidence stands behind it."""
    if evidence_checks is None:
        return None
    if lo is None or hi is None or hi <= lo:
        return round((POINT_RADIUS_MIN + POINT_RADIUS_MAX) / 2, 3)
    clamped = min(max(evidence_checks, lo), hi)
    fraction = (clamped - lo) / (hi - lo)
    return round(POINT_RADIUS_MIN + fraction * (POINT_RADIUS_MAX - POINT_RADIUS_MIN), 3)


# ---------------------------------------------------------------------------
# payload assembly
# ---------------------------------------------------------------------------
def build_design_arena_panel(arena_rows: list[dict]) -> dict:
    """Design Arena only, pivoted by category. Rows from any other source are
    ignored rather than merged, so this panel can never accidentally carry a
    different benchmark family."""
    models: dict[str, dict] = {}
    categories: list[str] = []

    for row in arena_rows:
        if row.get("benchmark_source") != DESIGN_ARENA_SOURCE:
            continue
        model_id = row.get("model_id")
        if not model_id:
            continue

        raw = row.get("raw_data") or {}
        category = raw.get("category")
        if not category:
            name = row.get("benchmark_name") or ""
            category = name.rsplit("/", 1)[-1] if "/" in name else None
        if not category:
            continue
        if category not in categories:
            categories.append(category)

        entry = models.setdefault(model_id, {
            "model_id": model_id,
            "model_name": row.get("raw_model_ref") or model_id,
            "categories": {},
        })
        entry["categories"][category] = {
            "elo": raw.get("elo"),
            "win_rate": row.get("score"),
            "score_unit": row.get("score_unit"),
            "rank": raw.get("rank"),
        }

    ordered_categories = sorted(categories)
    ordered_models = sorted(models.values(), key=lambda m: m["model_id"])
    return {
        "source": DESIGN_ARENA_SOURCE,
        "categories": [
            {"key": key, "label": CATEGORY_LABELS.get(key, key.replace("_", " ").title())}
            for key in ordered_categories
        ],
        "models": ordered_models,
        "model_count": len(ordered_models),
        "row_count": sum(len(m["categories"]) for m in ordered_models),
    }


def build_model_rows(models: list[dict], performance: dict[str, dict], arena: dict) -> list[dict]:
    """One row per catalogue model - never filtered. A priced model with no
    benchmark rows stays in the dataset with value=None so the page can label
    it "Unrated" instead of quietly dropping it."""
    arena_ids = {m["model_id"] for m in arena.get("models", [])}
    rows: list[dict] = []

    for model in models:
        model_id = model.get("model_id")
        if not model_id:
            continue

        agg = performance.get(model_id, {})
        endpoint_pricing = model.get("endpoint_pricing") or {}
        providers: list[str] = []
        pricing_lines: list[dict] = []
        for endpoint in endpoint_pricing.get("endpoints") or []:
            provider = endpoint.get("provider_name")
            if provider and provider not in providers:
                providers.append(provider)
            for line in endpoint.get("pricing_lines") or []:
                pricing_lines.append({
                    "provider": provider,
                    "billable": line.get("billable"),
                    "unit": line.get("unit"),
                    "unit_family": line.get("unit_family"),
                    "variant": line.get("variant"),
                    "usd": line.get("usd_amount"),
                })

        pass_rate = agg.get("pass_rate")
        avg_cost = agg.get("avg_cost_usd")
        rows.append({
            "model_id": model_id,
            "model_name": model.get("model_name"),
            "benchmark_name": agg.get("benchmark_name"),
            "provider": model.get("provider"),
            # Whether the CATALOGUE published a resolvable rate. The value score
            # does not use it (it uses the benchmark rows' own cost), so a model
            # can be rankable with this False - it just cannot be cross-checked
            # against a published rate.
            "has_catalog_rate": bool(model.get("has_valid_pricing")),
            "benchmarked": model_id in performance,
            "design_arena": model_id in arena_ids,
            "pricing_unit": model.get("pricing_unit"),
            "comparable_price": model.get("comparable_price"),
            "comparable_price_basis": model.get("comparable_price_basis"),
            "endpoint_count": endpoint_pricing.get("endpoint_count"),
            "providers": providers,
            "pricing_lines": pricing_lines,
            "resolutions": model.get("supported_resolutions"),
            "aspect_ratios": model.get("supported_aspect_ratios"),
            "sizes": model.get("supported_sizes"),
            "durations": model.get("supported_durations"),
            "prompts": agg.get("prompts"),
            "checks_passed": agg.get("checks_passed"),
            "checks_total": agg.get("checks_total"),
            "pass_rate": pass_rate,
            "avg_cost_usd": avg_cost,
            "cost_rows": agg.get("cost_rows"),
            "evidence_checks": agg.get("evidence_checks"),
            "value": value_score(pass_rate, avg_cost),
            "value_rank": None,
            "point_radius": None,
        })

    # Rank only what can actually be ranked, then size the scatter points.
    ranked = sorted(
        (row for row in rows if row["value"] is not None),
        key=lambda row: (-row["value"], row["model_id"]),
    )
    for position, row in enumerate(ranked, start=1):
        row["value_rank"] = position

    evidence = [row["evidence_checks"] for row in rows if row["evidence_checks"]]
    lo, hi = (min(evidence), max(evidence)) if evidence else (None, None)
    for row in rows:
        row["point_radius"] = point_radius(row["evidence_checks"], lo, hi)

    return rows


def build_coverage(coverage_report: dict, quality_report: dict, rows: list[dict]) -> dict:
    """Four independent questions, counted separately so no model can be
    silently dropped by collapsing them into one number:

      priced       - did the catalogue publish a resolvable rate?
      benchmarked  - does the model have benchmark rows at all?
      rankable     - can a value score be computed?
      unrated      - has no benchmark rows, so it cannot be rated at all

    rankable is NOT "priced and benchmarked": the value score is computed from
    the benchmark rows' own cost, which exists whether or not the catalogue
    published a rate. So today all 42 benchmarked models are rankable, and 4 of
    them are ranked without a published rate to cross-check against.
    """
    with_catalog_rate = [row for row in rows if row["has_catalog_rate"]]
    benchmarked = [row for row in rows if row["benchmarked"]]
    rankable = [row for row in rows if row["value"] is not None]
    unrated = [row for row in rows if not row["benchmarked"]]
    no_catalog_rate = [row for row in rows if not row["has_catalog_rate"]]
    prompt_counts = [row["prompts"] for row in benchmarked if row["prompts"]]

    return {
        "catalog_models": len(rows),
        "priced_models": len(with_catalog_rate),
        "benchmarked_models": len(benchmarked),
        "rankable_models": len(rankable),
        "unrated_models": len(unrated),
        "unrated_ids": [row["model_id"] for row in unrated],
        "unrated_but_priced_models": sum(1 for row in unrated if row["has_catalog_rate"]),
        "unrated_but_priced_ids": [row["model_id"] for row in unrated if row["has_catalog_rate"]],
        "benchmarked_unpriced_models": sum(1 for row in no_catalog_rate if row["benchmarked"]),
        "benchmarked_unpriced_ids": [row["model_id"] for row in no_catalog_rate if row["benchmarked"]],
        # Empty today. Kept so a future run where a benchmarked model has no
        # usable cost shows up as a named list instead of vanishing.
        "benchmarked_unrankable_ids": [
            row["model_id"] for row in benchmarked if row["value"] is None
        ],
        "design_arena_models": sum(1 for row in rows if row["design_arena"]),
        "prompt_count_min": min(prompt_counts) if prompt_counts else None,
        "prompt_count_max": max(prompt_counts) if prompt_counts else None,
        "media_coverage_report": coverage_report,
        "media_data_quality_report": quality_report,
    }


def budget_models(rows: list[dict], min_pass_rate: Optional[float] = None,
                  top_n: Optional[int] = None) -> list[dict]:
    """Cheapest models that still clear a minimum pass rate. Reuses avg_cost_usd
    (benchmark cost), so the "cheap" here is the same money the value score uses."""
    threshold = BUDGET_MIN_PASS_RATE if min_pass_rate is None else min_pass_rate
    limit = BUDGET_TOP_N if top_n is None else top_n
    eligible = [
        row for row in rows
        if row["value"] is not None and row["pass_rate"] is not None and row["pass_rate"] >= threshold
    ]
    eligible.sort(key=lambda row: (row["avg_cost_usd"], row["model_id"]))
    return eligible[:limit]


def trend_state() -> dict:
    """How much dated media-snapshot history exists. Read-only: this only ever
    lists directories under data/media_snapshots/, which is the media
    pipeline's own root and cannot collide with data/snapshots/."""
    dates: list[str] = []
    if SNAPSHOTS_DIR.exists():
        for entry in sorted(SNAPSHOTS_DIR.iterdir()):
            if entry.is_dir() and (entry / "media_prompt_benchmarks.json").exists():
                dates.append(entry.name)
    return {
        "snapshot_dates": dates,
        "points": len(dates),
        "min_points": MIN_TREND_POINTS,
        "ready": len(dates) >= MIN_TREND_POINTS,
    }


def build_payload() -> dict:
    models = _load_json(IMAGE_MODELS_PATH)
    prompt_rows = _load_json(PROMPT_BENCHMARKS_PATH)
    arena_rows = _load_json(DESIGN_ARENA_PATH)
    coverage_report = _load_json(COVERAGE_PATH)
    quality_report = _load_json(QUALITY_PATH)

    benchmark_rows = image_benchmark_rows(prompt_rows)
    performance = aggregate_performance(benchmark_rows)
    arena = build_design_arena_panel(arena_rows)
    rows = build_model_rows(models, performance, arena)
    budget = budget_models(rows)

    def stamp(records):
        return records[0].get("retrieved_at") if records else None

    return {
        "rows": rows,
        "design_arena": arena,
        "coverage": build_coverage(coverage_report, quality_report, rows),
        "budget": {
            "min_pass_rate": BUDGET_MIN_PASS_RATE,
            "top_n": BUDGET_TOP_N,
            "model_ids": [row["model_id"] for row in budget],
        },
        "trend": trend_state(),
        "constants": {
            "top_n_bars": TOP_N_BARS,
            "point_radius_min": POINT_RADIUS_MIN,
            "point_radius_max": POINT_RADIUS_MAX,
            "value_definition": "pass rate (checks passed / checks attempted) divided by mean benchmark cost in USD",
            "methodology_note": METHODOLOGY_NOTE,
            "provenance_note": PROVENANCE_NOTE,
        },
        "sources": {
            "image_catalog_endpoint": "/api/v1/images/models",
            "image_pricing_endpoint": "/api/v1/images/models/{id}/endpoints",
            "prompt_benchmarks": "https://openrouter.ai/benchmarks/media/images",
            "design_arena_endpoint": "/api/v1/models?output_modalities=image",
        },
        "data_retrieved_at": stamp(models),
        "benchmarks_retrieved_at": stamp(benchmark_rows),
        "design_arena_retrieved_at": stamp(arena_rows),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def render(payload: dict) -> Path:
    if not TEMPLATE_PATH.exists():
        raise FileNotFoundError(f"{TEMPLATE_PATH} does not exist - the image template is missing")

    with open(TEMPLATE_PATH, encoding="utf-8") as f:
        html = f.read()
    html = html.replace("__DATA__", json.dumps(payload, separators=(",", ":")))
    # Nav bar + theme toggle are shared with the chat page rather than
    # duplicated per template, so the two pages cannot drift apart.
    html = page_shell.inject(html)

    # Temp file + swap, so a viewer can never load a half-written dashboard.
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = OUTPUT_PATH.with_name(OUTPUT_PATH.name + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(html)
    os.replace(tmp_path, OUTPUT_PATH)
    return OUTPUT_PATH


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--max-age-hours", type=float, default=DEFAULT_MAX_AGE_HOURS,
                        help=f"Refuse to rebuild if the media data is older than this (default {DEFAULT_MAX_AGE_HOURS:g}h)")
    args = parser.parse_args()

    fresh, message = check_freshness(args.max_age_hours)
    if not fresh:
        print(f"STALE: {message}", file=sys.stderr)
        return 1
    print(f"OK: {message}")

    payload = build_payload()
    if not payload["rows"]:
        print("STALE: no image models found - refusing to publish an empty dashboard", file=sys.stderr)
        return 1

    written = render(payload)

    coverage = payload["coverage"]
    print(
        f"Coverage: {coverage['catalog_models']} catalogue / {coverage['priced_models']} with a catalogue rate / "
        f"{coverage['benchmarked_models']} benchmarked / {coverage['rankable_models']} rankable / "
        f"{coverage['unrated_models']} unrated / {coverage['benchmarked_unpriced_models']} ranked without a catalogue rate / "
        f"{coverage['design_arena_models']} with Design Arena"
    )
    print(
        f"Prompts per benchmarked model: {coverage['prompt_count_min']}-{coverage['prompt_count_max']}. "
        f"Budget threshold {payload['budget']['min_pass_rate']:.0%} selects "
        f"{len(payload['budget']['model_ids'])} model(s)."
    )
    print(f"Trend: {payload['trend']['points']} snapshot(s) recorded "
          f"(placeholder until {payload['trend']['min_points']}).")
    print(f"Rebuilt {written} with {len(payload['rows'])} models "
          f"({coverage['rankable_models']} with a value score).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
