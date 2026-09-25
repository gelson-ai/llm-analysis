#!/usr/bin/env python3
"""
Rebuild the video-generation model dashboard HTML from the video pipeline's
normalized data. Pure local file I/O - no network access needed.

Sibling of build_dashboard.py (chat) and build_image_dashboard.py (image), and
deliberately shares none of their inputs or outputs:

    image page (build_image_dashboard.py)   video page (this script)
    data/normalized/image_models.json       data/normalized/video_dashboard_models.json
    data/normalized/media_prompt_bench.     data/normalized/video_dashboard_prompt_benchmarks.json
    data/analysis/media_coverage_report.    data/analysis/video_dashboard_coverage_report.json
    dashboard/image_template.html           dashboard/video_template.html
    dashboard/image_model_analysis.html     dashboard/video_model_analysis.html

Nothing here writes a status sidecar, a snapshot directory, or any path the chat
pipeline (run_pipeline.py / dashboard/refresh.py) or the media pipeline reads or
watches. The payload's section names deliberately MIRROR the image page's
(rows/coverage/budget/trend/weekly/constants/sources) so the two pages stay
structurally parallel and their templates can be kept in step.

Metric definitions (decided, not derived):
  performance  = sum(checks_passed) / sum(checks_total) over ALL of a model's
                 rows - one flat pool, checks-weighted. Computed by
                 src.video_coverage.pool_prompt_benchmarks so the number the
                 pipeline reports and the number this page prints cannot differ.
  price        = mean cost_usd of those SAME rows: the observed USD cost of one
                 generated clip. Deliberately NOT the catalogue's per-second
                 rate, so price and performance come from the same generations.
                 The catalogue rate is shown beside it, never blended in.
  value        = performance / price (higher is better).
  rankable     = has a value AND clears the minimum-evidence gate
                 (>= 8 prompts AND >= 40 attempted checks, from
                 src.video_coverage). A model can be listed with a pass rate and
                 still not be ranked; it simply does not compete for a rank or
                 the Model of the Week.

There is no preference/arena panel: OpenRouter's video catalogue publishes no
benchmarks block and Artificial Analysis' Video Arena has no published scores.
The section that would hold one instead reports per-prompt coverage, which is the
only quality evidence that actually exists for these models.

Usage:
    python build_video_dashboard.py [--max-age-hours 192]

Exit codes:
    0  - dashboard rebuilt successfully -> dashboard/video_model_analysis.html
    1  - video data missing/stale, or the join produced no models. Nothing is
         written in that case.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DASHBOARD_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = DASHBOARD_DIR.parent
for _path in (str(PROJECT_ROOT), str(DASHBOARD_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from config import settings  # noqa: E402  (needs the path insert above)
import page_shell  # noqa: E402  (sibling module - shared with both other builders)
import video_picks  # noqa: E402  (this dashboard's OWN weekly picks)
from src.video_coverage import (  # noqa: E402
    BUDGET_MIN_PASS_RATE,
    COST_UNIT_NOTE,
    MIN_CHECKS_FOR_RANKING,
    MIN_PROMPTS_FOR_RANKING,
    PROMPT_COMPARABILITY_NOTE,
    VIDEO_BENCHMARK_NOTE,
    pool_prompt_benchmarks,
)

# --- inputs: video-pipeline outputs only -----------------------------------
VIDEO_MODELS_PATH = settings.VIDEO_DASHBOARD_MODELS_JSON_PATH
PROMPT_BENCHMARKS_PATH = settings.VIDEO_DASHBOARD_PROMPT_BENCHMARKS_JSON_PATH
COVERAGE_PATH = settings.VIDEO_DASHBOARD_COVERAGE_REPORT_PATH
QUALITY_PATH = settings.VIDEO_DASHBOARD_DATA_QUALITY_REPORT_PATH
SNAPSHOTS_DIR = settings.VIDEO_DASHBOARD_SNAPSHOTS_DIR
# Read-only: this dashboard's weekly history, which refresh_video.py writes.
WEEKLY_PICKS_PATH = settings.VIDEO_DASHBOARD_WEEKLY_PICKS_PATH

# --- outputs ---------------------------------------------------------------
TEMPLATE_PATH = DASHBOARD_DIR / "video_template.html"
OUTPUT_PATH = DASHBOARD_DIR / "video_model_analysis.html"

# --- tunables (echoed into the payload so labels, tests and JS agree) ------
TOP_N_BARS = 12
BUDGET_TOP_N = 5

# Scatter point sizing, from how much evidence stands behind a point. Prompt
# COUNT is a poor size signal here (every benchmarked model appears on 10-12
# prompts), so attempts are used instead - the same choice the image page makes.
POINT_RADIUS_MIN = 4.0
POINT_RADIUS_MAX = 11.0

# The trend component is a placeholder until at least this many dated snapshots
# exist.
MIN_TREND_POINTS = 2

# The video pipeline is not on the chat pipeline's weekly schedule yet, so a
# manual rebuild can legitimately follow a run by more than a few days.
DEFAULT_MAX_AGE_HOURS = 192.0

METHODOLOGY_NOTE = (
    "Performance is every judged check this model passed divided by every check it attempted, "
    "pooled flat across the prompts it was benchmarked on - no per-prompt grouping or weighting. "
    "Price is the mean cost_usd of those same generations: the observed cost of one generated clip, "
    "not the catalogue's per-second rate, so price and performance come from the identical rows. "
    "Two caveats travel with that cost, and both are structural rather than incidental. First, the "
    "benchmark pages do not publish the CLIP LENGTH they generated, so two models' observed costs "
    "may describe clips of different durations - the comparison is between things that were asked "
    "for, not a controlled like-for-like. Second, the catalogue rate is quoted PER OUTPUT SECOND and "
    "its basis is resolution- and audio-dependent (plain rate, a 720p tier, an audio-qualified rate), "
    "which is why the reference table prints the rate basis next to the rate instead of merging them. "
    "Generation time is captured per row and reported separately from cost and quality."
)

PROVENANCE_NOTE = (
    "Benchmarks are OpenRouter's media prompt benchmark pages for video. Artificial Analysis "
    "publishes no machine-readable video score and OpenRouter's video catalogue carries no "
    "benchmarks block, so nothing on this page implies a preference or arena rating - the prompt "
    "coverage panel is the whole of the quality evidence. Clip length is NOT published per row, so "
    "observed cost per clip is the only measured money figure and may compare clips of different "
    "lengths; the catalogue rate is shown beside it, never blended into it. Output resolution for "
    "each benchmarked model comes from the page's own embedded asset data. Uptime and provider-level "
    "effective pricing are not captured by this pipeline and are not shown."
)

# Why a catalogue rate can be missing, derived from the units the model actually
# publishes rather than asserted - the reader is told which pricing scheme the
# model uses instead of seeing an unexplained blank.
_RATE_ABSENCE_LABELS = (
    ("video_token", "per-token pricing only"),
    ("megapixel_second", "per-megapixel-second pricing only"),
    ("generation", "minimum-charge pricing only"),
    ("image", "input-image pricing only"),
    ("second", "no published per-second rate"),
)

# The labels themselves, exposed so a test can assert they are derived HERE
# rather than hardcoded in the template - a scheme list in the markup would go
# stale the first time OpenRouter invents a new unit.
RATE_ABSENCE_LABELS_AS_TEXT = tuple(label for _, label in _RATE_ABSENCE_LABELS)


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------
def _load_json(path: Path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def read_retrieved_at() -> Optional[str]:
    """The pipeline stamps every normalized record with retrieved_at; the first
    record's value is the snapshot timestamp for the whole run."""
    if not VIDEO_MODELS_PATH.exists():
        return None
    try:
        models = _load_json(VIDEO_MODELS_PATH)
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
    if not VIDEO_MODELS_PATH.exists():
        return False, f"{VIDEO_MODELS_PATH} does not exist - has the video pipeline ever run?"

    try:
        models = _load_json(VIDEO_MODELS_PATH)
    except (ValueError, OSError) as exc:
        return False, f"could not read {VIDEO_MODELS_PATH}: {exc}"
    if not models:
        return False, "video_dashboard_models.json exists but is empty"

    retrieved_at = models[0].get("retrieved_at")
    if not retrieved_at:
        return False, "video_dashboard_models.json has no retrieved_at timestamp - can't verify freshness"

    age_hours = data_age_hours(retrieved_at)
    if age_hours is None:
        return False, f"could not parse retrieved_at: {retrieved_at!r}"
    if age_hours > max_age_hours:
        return False, (
            f"video data is {age_hours:.1f} hours old (retrieved_at={retrieved_at}), "
            f"older than the {max_age_hours}-hour freshness window - the video "
            f"pipeline may not have run"
        )
    return True, f"video data is {age_hours:.1f} hours old (retrieved_at={retrieved_at}) - fresh"


# ---------------------------------------------------------------------------
# metric helpers (pure)
# ---------------------------------------------------------------------------
def point_radius(evidence_checks: Optional[int], lo: Optional[int], hi: Optional[int]) -> Optional[float]:
    """Scatter radius for a model, from how much evidence stands behind it."""
    if evidence_checks is None:
        return None
    if lo is None or hi is None or hi <= lo:
        return round((POINT_RADIUS_MIN + POINT_RADIUS_MAX) / 2, 3)
    clamped = min(max(evidence_checks, lo), hi)
    fraction = (clamped - lo) / (hi - lo)
    return round(POINT_RADIUS_MIN + fraction * (POINT_RADIUS_MAX - POINT_RADIUS_MIN), 3)


def _unit_families(model: dict) -> list[str]:
    raw = (model.get("pricing") or {}).get("unit_families")
    if isinstance(raw, dict):
        return [str(key) for key in raw]
    if isinstance(raw, (list, tuple, set)):
        return [str(item) for item in raw]
    return []


def rate_absence_label(model: dict) -> Optional[str]:
    """Why this model has no usable catalogue rate, in the model's own terms.

    Returns None when a rate IS published, so the caller can treat the string as
    "explain the blank" rather than "the blank".
    """
    if isinstance(model.get("comparable_price"), (int, float)) and not isinstance(
        model.get("comparable_price"), bool
    ):
        return None

    haystack = " ".join(_unit_families(model)).lower()
    if not haystack:
        haystack = str((model.get("pricing") or {}).get("raw_pricing_skus") or "").lower()
    for needle, label in _RATE_ABSENCE_LABELS:
        if needle in haystack:
            return label
    return "no published rate"


def _max_duration(model: dict) -> Optional[int]:
    durations = model.get("supported_durations")
    if isinstance(durations, list):
        numbers = [d for d in durations if isinstance(d, int) and not isinstance(d, bool)]
        if numbers:
            return max(numbers)
    return None


def build_model_rows(models: list[dict], pool: dict[str, dict]) -> list[dict]:
    """One row per catalogue model - never filtered. A model with no benchmark
    rows stays in the dataset with value=None so the page can label it "Unrated"
    instead of quietly dropping it."""
    rows: list[dict] = []

    for model in models:
        model_id = model.get("model_id")
        if not model_id:
            continue

        agg = pool.get(model_id, {})
        catalogue_rate = model.get("comparable_price")
        has_catalogue_rate = bool(model.get("has_valid_pricing")) and isinstance(
            catalogue_rate, (int, float)
        )

        pass_rate = agg.get("pass_rate")
        avg_cost = agg.get("avg_cost_usd")

        rows.append({
            "model_id": model_id,
            "model_name": model.get("model_name"),
            "provider": model.get("provider"),
            "benchmark_name": (agg.get("prompt_slugs") or [None])[0],
            # Whether the CATALOGUE published a resolvable per-second rate. The
            # value score does NOT use it (it uses the benchmark rows' own cost),
            # so a model can be rankable with this False - it just cannot be
            # cross-checked against a published rate, and the page says so with
            # `rate_absence_label` rather than printing $0.00.
            "has_catalog_rate": has_catalogue_rate,
            "rate_absence_label": rate_absence_label(model),
            "benchmarked": model_id in pool,
            "pricing_unit": model.get("pricing_unit"),
            "comparable_price": catalogue_rate if has_catalogue_rate else None,
            "comparable_price_basis": model.get("comparable_price_basis"),
            # A derived reading of the same rate, because "per second" is hard to
            # judge at $0.03-$0.30 and a 5-second clip is the shortest duration
            # most of these models offer.
            "cost_per_5s_clip": round(catalogue_rate * 5, 4) if has_catalogue_rate else None,
            # --- capability, straight from the catalogue (all nullable) ------
            "supported_resolutions": model.get("supported_resolutions"),
            "aspect_ratios": model.get("supported_aspect_ratios"),
            "sizes": model.get("supported_sizes"),
            "durations": model.get("supported_durations"),
            "max_duration_seconds": _max_duration(model),
            "frame_images": model.get("supported_frame_images"),
            # Tri-state on purpose: True, False, or None when unpublished. The
            # template renders None as "-", never as "No".
            "generate_audio": model.get("generate_audio"),
            "supports_seed": model.get("seed"),
            "upscale_factor": model.get("upscale_factor"),
            # --- benchmark evidence -----------------------------------------
            "prompts": agg.get("prompts"),
            "checks_passed": agg.get("checks_passed"),
            "checks_total": agg.get("checks_total"),
            "pass_rate": pass_rate,
            "avg_cost_usd": avg_cost,
            "cost_rows": agg.get("cost_rows"),
            "output_resolutions": agg.get("output_resolutions") or [],
            "resolution_mixed": bool(agg.get("resolution_mixed")),
            "mean_generation_seconds": agg.get("mean_generation_seconds"),
            "duration_ms_mean": agg.get("duration_ms_mean"),
            "evidence_checks": agg.get("evidence_checks"),
            "value": agg.get("value"),
            "evidence_gate_passed": bool(agg.get("evidence_gate_passed")),
            "rankable": bool(agg.get("rankable")),
            "value_rank": None,
            "point_radius": None,
        })

    # Rank only what may be ranked, then size the scatter points. A model that
    # fails the evidence gate keeps its value (it is real) but takes no rank.
    ranked = sorted(
        (row for row in rows if row["rankable"]),
        key=lambda row: (-row["value"], row["model_id"]),
    )
    for position, row in enumerate(ranked, start=1):
        row["value_rank"] = position

    evidence = [row["evidence_checks"] for row in rows if row["evidence_checks"]]
    lo, hi = (min(evidence), max(evidence)) if evidence else (None, None)
    for row in rows:
        row["point_radius"] = point_radius(row["evidence_checks"], lo, hi)

    return rows


def build_prompt_coverage(prompt_rows: list[dict], rows: list[dict]) -> dict:
    """Per-prompt coverage: which model was judged on which prompt, and how it
    scored.

    This is the panel that stands in for a preference/arena section. Rows are the
    benchmark rows themselves (same source, same numbers the pass rate pools), so
    it cannot tell a different story from the headline metric.
    """
    prompts: list[dict] = []
    for row in prompt_rows:
        slug = row.get("prompt_slug")
        if not slug or any(p["slug"] == slug for p in prompts):
            continue
        prompts.append({
            "slug": slug,
            "name": row.get("prompt_name") or slug,
            "row_count": sum(1 for r in prompt_rows if r.get("prompt_slug") == slug),
        })

    known = {row["model_id"]: row for row in rows}
    cells_by_model: dict[str, dict] = {}
    for row in prompt_rows:
        model_id = row.get("model_id")
        slug = row.get("prompt_slug")
        if not model_id or not slug:
            continue
        passed, total = row.get("checks_passed"), row.get("checks_total")
        cells_by_model.setdefault(model_id, {})[slug] = {
            "checks_passed": passed,
            "checks_total": total,
            "pass_rate": row.get("pass_rate"),
            "cost_usd": row.get("cost_usd"),
        }

    models = [
        {
            "model_id": model_id,
            "model_name": (known.get(model_id) or {}).get("model_name") or model_id,
            "cells": cells,
            "prompt_count": len(cells),
        }
        for model_id, cells in sorted(cells_by_model.items())
    ]

    return {
        "prompts": prompts,
        "models": models,
        "prompt_count": len(prompts),
        "model_count": len(models),
        "row_count": len(prompt_rows),
        "note": PROMPT_COMPARABILITY_NOTE,
    }


def build_coverage(coverage_report: dict, quality_report: dict, rows: list[dict]) -> dict:
    """Five independent questions, counted separately so no model can be silently
    dropped by collapsing them into one number:

      priced        - did the catalogue publish a resolvable per-second rate?
      benchmarked   - does the model have judged benchmark rows at all?
      rankable      - can a value score be computed AND does it clear the gate?
      gated_out     - has a value but too little evidence to be ranked
      unrated       - has no benchmark rows, so it cannot be rated at all

    rankable is NOT "priced and benchmarked": the value score comes from the
    benchmark rows' own cost, which exists whether or not a catalogue rate does.
    """
    with_catalog_rate = [row for row in rows if row["has_catalog_rate"]]
    benchmarked = [row for row in rows if row["benchmarked"]]
    rankable = [row for row in rows if row["rankable"]]
    gated_out = [row for row in rows if row["benchmarked"] and not row["rankable"]]
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
        "gated_out_models": len(gated_out),
        "gated_out_ids": [row["model_id"] for row in gated_out],
        "benchmarked_unrankable_ids": [
            row["model_id"] for row in benchmarked if row["value"] is None
        ],
        "prompt_count_min": min(prompt_counts) if prompt_counts else None,
        "prompt_count_max": max(prompt_counts) if prompt_counts else None,
        # The full pipeline reports travel with the payload, as on the image page,
        # so the provenance panel and the coverage tables share one source.
        "video_coverage_report": coverage_report,
        "video_data_quality_report": quality_report,
    }


def budget_models(rows: list[dict], min_pass_rate: Optional[float] = None,
                  top_n: Optional[int] = None) -> list[dict]:
    """Cheapest models that still clear a minimum pass rate.

    Eligibility includes the evidence gate, matching the ranking rule: a "budget
    pick" is a recommendation, and a recommendation drawn from a single prompt is
    not one. Reuses avg_cost_usd (observed benchmark cost), so the "cheap" here is
    the same money the value score uses.
    """
    threshold = BUDGET_MIN_PASS_RATE if min_pass_rate is None else min_pass_rate
    limit = BUDGET_TOP_N if top_n is None else top_n
    eligible = [
        row for row in rows
        if row["rankable"] and row["pass_rate"] is not None and row["pass_rate"] >= threshold
    ]
    eligible.sort(key=lambda row: (row["avg_cost_usd"], row["model_id"]))
    return eligible[:limit]


def trend_state() -> dict:
    """How much dated video-snapshot history exists. Read-only: this only ever
    lists directories under data/video_snapshots/, which is this dashboard's own
    root and cannot collide with data/snapshots/ or data/media_snapshots/."""
    dates: list[str] = []
    if SNAPSHOTS_DIR.exists():
        for entry in sorted(SNAPSHOTS_DIR.iterdir()):
            if entry.is_dir() and (entry / "video_dashboard_prompt_benchmarks.json").exists():
                dates.append(entry.name)
    return {
        "snapshot_dates": dates,
        "points": len(dates),
        "min_points": MIN_TREND_POINTS,
        "ready": len(dates) >= MIN_TREND_POINTS,
    }


def build_payload() -> dict:
    models = _load_json(VIDEO_MODELS_PATH)
    prompt_rows = _load_json(PROMPT_BENCHMARKS_PATH)
    coverage_report = _load_json(COVERAGE_PATH)
    quality_report = _load_json(QUALITY_PATH)

    video_rows = [row for row in prompt_rows if row.get("media_type") == "video" and row.get("model_id")]
    pool = pool_prompt_benchmarks(prompt_rows, "video")
    rows = build_model_rows(models, pool)
    budget = budget_models(rows)

    def stamp(records):
        return records[0].get("retrieved_at") if records else None

    return {
        "rows": rows,
        "prompt_coverage": build_prompt_coverage(video_rows, rows),
        "coverage": build_coverage(coverage_report, quality_report, rows),
        "budget": {
            "min_pass_rate": BUDGET_MIN_PASS_RATE,
            "top_n": BUDGET_TOP_N,
            "model_ids": [row["model_id"] for row in budget],
        },
        "trend": trend_state(),
        # The current week's locked "Model of the week". refresh_video.py locks it
        # BEFORE this build runs, so the page always shows the pick that belongs to
        # the snapshot it is rendering.
        "weekly": video_picks.embed_view(video_picks.load_history(WEEKLY_PICKS_PATH)),
        "constants": {
            "top_n_bars": TOP_N_BARS,
            "point_radius_min": POINT_RADIUS_MIN,
            "point_radius_max": POINT_RADIUS_MAX,
            "evidence_min_prompts": MIN_PROMPTS_FOR_RANKING,
            "evidence_min_checks": MIN_CHECKS_FOR_RANKING,
            # The distinct output sizes actually generated across the benchmark
            # rows, and how many models mix more than one. Computed rather than
            # written down, so the caveat cannot drift from the data.
            "resolution_values": sorted(
                {res for row in rows for res in (row.get("output_resolutions") or [])}
            ),
            "resolution_mixed_models": sum(1 for row in rows if row.get("resolution_mixed")),
            "audio_models": sum(1 for row in rows if row.get("generate_audio") is True),
            "audio_unknown_models": sum(1 for row in rows if row.get("generate_audio") is None),
            "value_definition": (
                "pass rate (checks passed / checks attempted) divided by the mean observed "
                "benchmark cost of one generated clip in USD"
            ),
            "cost_unit_note": COST_UNIT_NOTE,
            "methodology_note": METHODOLOGY_NOTE,
            "provenance_note": PROVENANCE_NOTE,
            "benchmark_note": VIDEO_BENCHMARK_NOTE,
        },
        "sources": {
            "video_catalog_endpoint": "/api/v1/videos/models",
            "prompt_benchmarks": "https://openrouter.ai/benchmarks/media/videos",
        },
        "data_retrieved_at": stamp(models),
        "benchmarks_retrieved_at": stamp(video_rows),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def render(payload: dict) -> Path:
    if not TEMPLATE_PATH.exists():
        raise FileNotFoundError(f"{TEMPLATE_PATH} does not exist - the video template is missing")

    with open(TEMPLATE_PATH, encoding="utf-8") as f:
        html = f.read()
    html = html.replace("__DATA__", json.dumps(payload, separators=(",", ":")))
    # Nav bar + theme toggle + refresh control are shared with the other pages
    # rather than duplicated per template, so the three cannot drift apart.
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
                        help=f"Refuse to rebuild if the video data is older than this (default {DEFAULT_MAX_AGE_HOURS:g}h)")
    args = parser.parse_args()

    fresh, message = check_freshness(args.max_age_hours)
    if not fresh:
        print(f"STALE: {message}", file=sys.stderr)
        return 1
    print(f"OK: {message}")

    payload = build_payload()
    if not payload["rows"]:
        print("STALE: no video models found - refusing to publish an empty dashboard", file=sys.stderr)
        return 1

    written = render(payload)

    coverage = payload["coverage"]
    print(
        f"Coverage: {coverage['catalog_models']} catalogue / {coverage['priced_models']} with a catalogue rate / "
        f"{coverage['benchmarked_models']} benchmarked / {coverage['rankable_models']} rankable / "
        f"{coverage['unrated_models']} unrated / {coverage['benchmarked_unpriced_models']} ranked without a catalogue rate / "
        f"{coverage['gated_out_models']} below the evidence gate"
    )
    print(
        f"Prompts per benchmarked model: {coverage['prompt_count_min']}-{coverage['prompt_count_max']}. "
        f"Evidence gate: >= {payload['constants']['evidence_min_prompts']} prompts and "
        f">= {payload['constants']['evidence_min_checks']} checks. "
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
