"""Coverage, data-quality and evidence-gate logic for the VIDEO dashboard.

Video-only sibling of src/media_coverage.py, deliberately in its own module: the
media coverage report is embedded verbatim in the published image page, so adding
video fields to that report would change that page's bytes.

It also holds the two pieces of aggregation that the pipeline runner and the
dashboard builder must agree on exactly, so they are computed in one place:

  * ``pool_prompt_benchmarks`` - the per-model pass-rate pool and value score.
    The builder ranks from this and the runner prints its distribution, so the
    number on the page and the number reported by a run cannot drift;
  * ``meets_evidence_gate`` - the minimum-evidence rule for ranking a model.

Nothing here reads or writes a file, and nothing here is imported by the chat or
media pipelines.
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

from config import settings

# ---------------------------------------------------------------------------
# Minimum evidence for ranking
#
# Measured on the 2026-09-22 capture: 24 of the 29 catalogue models are
# benchmarked, each on 10-12 of the 12 prompts and 48-57 attempted checks, with
# pooled pass rates of 50.0-84.6%. Every current model therefore clears these
# thresholds - they exist so a model added later that is benchmarked on a single
# prompt (and so has a pass rate drawn from 5 checks) cannot take the
# Model-of-the-Week slot on noise.
# ---------------------------------------------------------------------------
MIN_PROMPTS_FOR_RANKING = 8
MIN_CHECKS_FOR_RANKING = 40

# The budget table's pass-rate bar, mirrored from the image dashboard so the two
# pages mean the same thing by "still clears the bar".
BUDGET_MIN_PASS_RATE = 0.70

PROMPT_COMPARABILITY_NOTE = (
    "A pass rate is the share of judged checks a model passed across the prompts "
    "it was benchmarked on. Checks are POOLED (total passes over total attempts), "
    "not averaged per prompt, and OpenRouter judges a different number of checks "
    "per prompt - so a pooled rate summarises a model's whole benchmark run and is "
    "not a like-for-like comparison within any single prompt."
)

COST_UNIT_NOTE = (
    "Benchmark cost is the observed USD cost of ONE generated clip, taken from the "
    "same rows as the pass rate. The clip length generated for those rows is not "
    "published, so two models' observed costs may describe clips of different "
    "lengths; the catalogue rate (USD per output second, and the 5-second figure "
    "derived from it) is shown alongside it with its rate basis for that reason."
)

VIDEO_BENCHMARK_NOTE = (
    "Video models publish no preference or arena benchmark: OpenRouter's video "
    "catalogue carries no `benchmarks` block (checked live 2026-09-25), and "
    "Artificial Analysis' Video Arena is a voting interface with no published "
    "score table. Correctness therefore comes only from OpenRouter's media prompt "
    "benchmarks."
)


def _numeric(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _unit_families(model: dict) -> list[str]:
    """The SKU unit families a model publishes, however the parser shaped them."""
    raw = (model.get("pricing") or {}).get("unit_families")
    if isinstance(raw, dict):
        return [str(key) for key in raw]
    if isinstance(raw, (list, tuple, set)):
        return [str(item) for item in raw]
    return []


# ---------------------------------------------------------------------------
# The pass-rate pool (shared by the runner's distribution and the builder's rows)
# ---------------------------------------------------------------------------
def meets_evidence_gate(prompts: Optional[int], checks: Optional[int]) -> bool:
    """Whether a model has enough evidence to be RANKED.

    A model can still be listed, and its pass rate still shown, without clearing
    this - it simply does not compete for a rank or the Model of the Week.
    """
    if not _int(prompts) or not _int(checks):
        return False
    return prompts >= MIN_PROMPTS_FOR_RANKING and checks >= MIN_CHECKS_FOR_RANKING


def pool_prompt_benchmarks(
    prompt_rows: Iterable[dict],
    media_type: str = "video",
) -> dict[str, dict]:
    """Pool one model's benchmark rows into one pass rate, cost and value score.

    Only rows carrying BOTH `checks_passed` and `checks_total` enter the pool, so
    a cost with no judged score can never pull a price into the average without a
    score to pair it with - this pipeline's rule is that price and performance
    come from the same rows.

    Rows whose slug never matched a catalogue model (``model_id`` is None) are
    skipped here and reported in the coverage report instead of being guessed at.
    """
    pool: dict[str, dict] = {}

    for row in prompt_rows:
        if row.get("media_type") != media_type:
            continue
        model_id = row.get("model_id")
        if not model_id:
            continue
        passed, total = row.get("checks_passed"), row.get("checks_total")
        if not (_int(passed) and _int(total)):
            continue

        entry = pool.setdefault(model_id, {
            "model_name": row.get("model_name"),
            "rows": 0,
            "prompt_slugs": [],
            "checks_passed": 0,
            "checks_total": 0,
            "costs": [],
            "generation_seconds": [],
            "duration_ms": [],
            "output_resolutions": [],
            "duration_mismatches": 0,
        })
        entry["rows"] += 1
        entry["checks_passed"] += passed
        entry["checks_total"] += total

        slug = row.get("prompt_slug")
        if slug and slug not in entry["prompt_slugs"]:
            entry["prompt_slugs"].append(slug)

        cost = row.get("cost_usd")
        if _numeric(cost):
            entry["costs"].append(float(cost))
        seconds = row.get("generation_seconds")
        if _numeric(seconds):
            entry["generation_seconds"].append(float(seconds))
        duration = row.get("duration_ms")
        if _numeric(duration):
            entry["duration_ms"].append(float(duration))
            # The payload's durationMs and the rendered badge are two
            # independent readings of the same quantity (generation latency).
            # They agree on every current row; a divergence means one of the two
            # extraction paths changed, which is worth surfacing.
            if _numeric(seconds) and abs(duration / 1000.0 - float(seconds)) > 0.1:
                entry["duration_mismatches"] += 1

        width, height = row.get("output_width"), row.get("output_height")
        if _int(width) and _int(height):
            label = f"{width}x{height}"
            if label not in entry["output_resolutions"]:
                entry["output_resolutions"].append(label)

    pooled: dict[str, dict] = {}
    for model_id, entry in pool.items():
        checks_total = entry["checks_total"]
        pass_rate = round(entry["checks_passed"] / checks_total, 6) if checks_total else None

        costs = entry["costs"]
        avg_cost = round(sum(costs) / len(costs), 6) if costs else None
        value = pass_rate / avg_cost if (pass_rate is not None and avg_cost) else None

        generations = entry["generation_seconds"]
        durations = entry["duration_ms"]
        resolutions = sorted(entry["output_resolutions"])
        prompts = len(entry["prompt_slugs"])

        pooled[model_id] = {
            "model_id": model_id,
            "model_name": entry["model_name"],
            "rows": entry["rows"],
            "prompts": prompts,
            "prompt_slugs": sorted(entry["prompt_slugs"]),
            "checks_passed": entry["checks_passed"],
            "checks_total": checks_total,
            "evidence_checks": checks_total,
            "pass_rate": pass_rate,
            "avg_cost_usd": avg_cost,
            "cost_rows": len(costs),
            "mean_generation_seconds": round(sum(generations) / len(generations), 1) if generations else None,
            "duration_ms_mean": round(sum(durations) / len(durations), 1) if durations else None,
            "duration_mismatches": entry["duration_mismatches"],
            "output_resolutions": resolutions,
            "resolution_mixed": len(resolutions) > 1,
            "value": round(value, 6) if value is not None else None,
            "evidence_gate_passed": meets_evidence_gate(prompts, checks_total),
        }
        pooled[model_id]["rankable"] = bool(
            pooled[model_id]["evidence_gate_passed"] and pooled[model_id]["value"] is not None
        )

    return pooled


def summarize_pool(pooled: dict[str, dict]) -> dict:
    """Distribution facts for a pool - what a run prints, and what the gate was
    chosen from. Returned rather than printed so tests can assert on it."""

    def _quartile(values: list, q: float):
        if not values:
            return None
        return values[min(len(values) - 1, int(round((len(values) - 1) * q)))]

    checks = sorted(int(p["evidence_checks"]) for p in pooled.values())
    prompts = sorted(int(p["prompts"]) for p in pooled.values())
    rates = sorted(p["pass_rate"] for p in pooled.values() if p["pass_rate"] is not None)

    return {
        "models": len(pooled),
        "checks_min": checks[0] if checks else None,
        "checks_median": _quartile(checks, 0.5),
        "checks_max": checks[-1] if checks else None,
        "prompts_min": prompts[0] if prompts else None,
        "prompts_max": prompts[-1] if prompts else None,
        "pass_rate_min": rates[0] if rates else None,
        "pass_rate_median": _quartile(rates, 0.5),
        "pass_rate_max": rates[-1] if rates else None,
        "rankable": sum(1 for p in pooled.values() if p["rankable"]),
        "gate_failures": sorted(p["model_id"] for p in pooled.values() if not p["evidence_gate_passed"]),
        "clearing_budget_pass_rate": sum(
            1 for p in pooled.values()
            if p["pass_rate"] is not None and p["pass_rate"] >= BUDGET_MIN_PASS_RATE
        ),
    }


# ---------------------------------------------------------------------------
# Coverage report
# ---------------------------------------------------------------------------
def build_video_coverage_report(
    video_models: list[dict],
    prompt_benchmarks: list[dict],
    prompt_stats: dict,
    retrieved_at: str,
    pooled: Optional[dict[str, dict]] = None,
) -> dict:
    """What the run actually captured, including what it could NOT resolve."""
    pooled = pooled if pooled is not None else pool_prompt_benchmarks(prompt_benchmarks)

    rates = [m["comparable_price"] for m in video_models if _numeric(m.get("comparable_price"))]
    unit_families: set[str] = set()
    bases: set[str] = set()
    for model in video_models:
        unit_families.update(_unit_families(model))
        if model.get("pricing_unit"):
            unit_families.add(str(model["pricing_unit"]))
        if model.get("comparable_price_basis"):
            bases.add(str(model["comparable_price_basis"]))

    with_rate = [m for m in video_models if _numeric(m.get("comparable_price"))]
    without_rate = [m for m in video_models if not _numeric(m.get("comparable_price"))]
    any_price_no_rate = [m for m in without_rate if m.get("has_any_price")]
    benchmarked_ids = sorted(pooled)
    unrated_ids = sorted(m["model_id"] for m in video_models if m.get("model_id") not in pooled)

    currency_assumed: dict[str, str] = {}
    converted: dict[str, str] = {}
    for model in video_models:
        for sku in (model.get("pricing") or {}).get("skus") or []:
            if sku.get("currency_assumed"):
                currency_assumed[str(sku.get("sku"))] = str(sku.get("raw_value"))
            if sku.get("conversion_applied"):
                converted[str(sku.get("sku"))] = f"{sku.get('raw_value')} -> {sku.get('usd_amount')} USD"

    return {
        "retrieved_at": retrieved_at,
        "model_inventory": {
            "total_models": len(video_models),
            "provider_count": len({m.get("provider") for m in video_models if m.get("provider")}),
            "models_with_resolved_price": len(with_rate),
            "models_without_resolved_price": len(without_rate),
            "price_resolution_rate": round(100.0 * len(with_rate) / len(video_models), 2) if video_models else 0.0,
            "models_with_any_price_data": sum(1 for m in video_models if m.get("has_any_price")),
            "models_with_price_data_but_no_comparable_rate": len(any_price_no_rate),
            "models_with_benchmark_rows": len(benchmarked_ids),
            "unit_families_present": sorted(unit_families),
            "comparable_price_units_used": sorted({str(m.get("pricing_unit")) for m in with_rate if m.get("pricing_unit")}),
            "comparable_price_basis_used": sorted(bases),
            "comparable_price_range": {
                "min": min(rates) if rates else None,
                "max": max(rates) if rates else None,
                "count": len(rates),
            },
        },
        "unrated": {
            "count": len(unrated_ids),
            "model_ids": unrated_ids,
            "note": (
                "Catalogue models with no judged benchmark row. They are listed as "
                "unrated rather than scored zero: no evidence is not the same as "
                "bad evidence."
            ),
        },
        "priced_without_rate": {
            "count": len(any_price_no_rate),
            "model_ids": sorted(m["model_id"] for m in any_price_no_rate),
            "note": (
                "Models that publish pricing but in a unit that cannot be converted to "
                "USD per output second (per-token, per-megapixel-second, or a minimum "
                "charge only). Their catalogue rate is blank; they are STILL ranked on "
                "value when their observed benchmark cost is known, because value uses "
                "the observed cost."
            ),
        },
        "evidence_gate": {
            "min_prompts": MIN_PROMPTS_FOR_RANKING,
            "min_checks": MIN_CHECKS_FOR_RANKING,
            "models_measured": len(pooled),
            "models_passing": sum(1 for p in pooled.values() if p["evidence_gate_passed"]),
            "models_failing": sorted(p["model_id"] for p in pooled.values() if not p["evidence_gate_passed"]),
        },
        "prompt_benchmark_coverage": _prompt_coverage(prompt_stats, prompt_benchmarks),
        "pricing_unit_assumptions": {
            "cents_conversions_applied": converted,
            "currency_assumed": currency_assumed,
            "note": (
                "OpenRouter mixes cents- and USD-denominated keys in one pricing_skus "
                "object, so any conversion is recorded here rather than applied silently."
            ),
        },
        "prompt_comparability_note": PROMPT_COMPARABILITY_NOTE,
        "cost_unit_note": COST_UNIT_NOTE,
        "benchmark_note": VIDEO_BENCHMARK_NOTE,
    }


def _prompt_coverage(prompt_stats: dict, prompt_benchmarks: list[dict]) -> dict:
    by_source: dict[str, int] = {}
    for record in prompt_benchmarks:
        source = (record.get("raw_data") or {}).get("checks_source") or "unknown"
        by_source[source] = by_source.get(source, 0) + 1

    rows_per_prompt = prompt_stats.get("rows_per_prompt") or {}
    return {
        "enabled": True,
        "source": "https://openrouter.ai/benchmarks/media/videos",
        "benchmark_source": prompt_stats.get("benchmark_source", "OpenRouter Media Benchmarks"),
        "pages_fetched": prompt_stats.get("pages_fetched"),
        "pages_skipped": prompt_stats.get("pages_skipped") or [],
        "pages_skipped_count": len(prompt_stats.get("pages_skipped") or []),
        "prompt_page_count": len(rows_per_prompt),
        "rows": prompt_stats.get("rows", len(prompt_benchmarks)),
        "rows_per_prompt": rows_per_prompt,
        "distinct_models_in_benchmarks": prompt_stats.get("distinct_models_in_benchmarks"),
        "models_matched_to_inventory": prompt_stats.get("models_matched_to_inventory"),
        "models_unmatched": len(prompt_stats.get("unmatched_model_slugs") or []),
        "unmatched_model_slugs": prompt_stats.get("unmatched_model_slugs") or [],
        "rows_missing_cost": prompt_stats.get("rows_missing_cost"),
        "rows_missing_generation_time": prompt_stats.get("rows_missing_generation_time"),
        "correctness_extraction_sources": by_source,
        "conflicting_row_values": prompt_stats.get("conflicting_row_values"),
        "note": prompt_stats.get("note") or PROMPT_COMPARABILITY_NOTE,
    }


# ---------------------------------------------------------------------------
# Data-quality report
# ---------------------------------------------------------------------------
def build_video_data_quality_report(
    raw_video_payload: dict,
    video_models: list[dict],
    prompt_benchmarks: list[dict],
    prompt_stats: dict,
    pooled: Optional[dict[str, dict]] = None,
) -> dict:
    """The same envelope shape as the media report: counts plus an issues list,
    so severity is machine-readable rather than buried in prose."""
    pooled = pooled if pooled is not None else pool_prompt_benchmarks(prompt_benchmarks)
    issues: list[dict] = []

    def add(severity: str, issue: str, count: int, detail: str) -> None:
        if count:
            issues.append({"severity": severity, "issue": issue, "count": count, "detail": detail})

    raw_models = raw_video_payload.get("data") if isinstance(raw_video_payload, dict) else None
    raw_count = len(raw_models or [])

    # Fail-loud guard: a truncated or partially rendered response must never be
    # mistaken for a real inventory. (The fetch raises first for an empty or
    # envelope-less body; this catches a short-but-valid one.)
    if raw_count < settings.MIN_EXPECTED_VIDEO_MODEL_COUNT:
        issues.append({
            "severity": "error",
            "issue": "video_catalog_below_floor",
            "count": 1,
            "detail": (
                f"The video catalogue returned {raw_count} model(s), below the configured floor of "
                f"{settings.MIN_EXPECTED_VIDEO_MODEL_COUNT}. A truncated or partially rendered "
                f"response must not be mistaken for a real inventory."
            ),
        })

    without_rate = [m["model_id"] for m in video_models if not _numeric(m.get("comparable_price"))]
    add(
        "warning", "video_model_without_comparable_rate", len(without_rate),
        f"{len(without_rate)} video model(s) publish pricing that cannot be normalised to USD per "
        f"output second: {without_rate}. Their catalogue rate stays blank; value ranking still uses "
        f"their observed benchmark cost.",
    )

    add(
        "warning", "benchmark_rows_missing_cost", int(prompt_stats.get("rows_missing_cost") or 0),
        "Benchmark row(s) carried no USD cost, so they are excluded from a model's mean cost.",
    )
    add(
        "warning", "benchmark_rows_missing_generation_time",
        int(prompt_stats.get("rows_missing_generation_time") or 0),
        "Benchmark row(s) carried no generation time.",
    )
    add(
        "warning", "benchmark_row_conflicting_values", int(prompt_stats.get("conflicting_row_values") or 0),
        "Benchmark row(s) rendered twice on a page with disagreeing values; the highest pass count was kept.",
    )
    add(
        "warning", "benchmark_slug_unmatched",
        len(prompt_stats.get("unmatched_model_slugs") or []),
        f"Benchmark slug(s) matched no catalogue model and are excluded from every per-model metric: "
        f"{prompt_stats.get('unmatched_model_slugs')}",
    )

    for skipped in prompt_stats.get("pages_skipped") or []:
        add(
            "info", "prompt_benchmark_page_skipped", 1,
            f"{skipped.get('page')} publishes no judged checks ({skipped.get('row_blocks')} result-row "
            f"block(s)), so it contributes neither scores NOR costs.",
        )

    mismatches = sum(int(p.get("duration_mismatches") or 0) for p in pooled.values())
    add(
        "warning", "duration_ms_disagrees_with_generation_badge", mismatches,
        "The asset payload's durationMs and the rendered generation-time badge are two readings of "
        "the same quantity and agree on every known row; a disagreement means one extraction path moved.",
    )

    unrated = sorted(m["model_id"] for m in video_models if m.get("model_id") not in pooled and m.get("model_id"))
    add(
        "info", "video_models_without_benchmark_rows", len(unrated),
        f"{len(unrated)} catalogue model(s) have no judged benchmark row and are listed as unrated: {unrated}",
    )

    mixed = sorted(p["model_id"] for p in pooled.values() if p["resolution_mixed"])
    add(
        "info", "resolution_varies_within_a_model", len(mixed),
        f"{len(mixed)} model(s) generated at more than one output resolution across the benchmark pages: "
        f"{mixed}. Observed cost and the catalogue rate basis both move with resolution, so cross-model "
        f"cost comparison is only meaningful alongside the stated rate basis.",
    )

    all_resolutions = sorted({r for p in pooled.values() for r in p["output_resolutions"]})
    add(
        "info", "output_resolutions_observed", len(all_resolutions),
        f"Generated resolutions observed across the benchmark pages: {all_resolutions}",
    )

    return {
        "issue_count": len(issues),
        "error_count": sum(1 for i in issues if i["severity"] == "error"),
        "warning_count": sum(1 for i in issues if i["severity"] == "warning"),
        "info_count": sum(1 for i in issues if i["severity"] == "info"),
        "issues": issues,
    }
