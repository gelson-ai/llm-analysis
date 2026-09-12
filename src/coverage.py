"""
Coverage and data-quality reporting (Sections 8, 20-E, 20-F).
"""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Optional

from config import settings
from src.categorize import categorize_benchmark


def build_coverage_report(
    normalized_models: list[dict],
    normalized_benchmarks: list[dict],
    categories_config: dict,
) -> dict:
    total_models = len(normalized_models)
    with_pricing = sum(1 for m in normalized_models if m.get("has_valid_pricing"))
    without_pricing = total_models - with_pricing

    models_with_benchmark_ids = {b["model_id"] for b in normalized_benchmarks if b.get("model_id")}
    with_benchmarks = len(models_with_benchmark_ids)
    without_benchmarks = total_models - with_benchmarks

    per_benchmark = defaultdict(set)
    for b in normalized_benchmarks:
        if b.get("model_id"):
            per_benchmark[b["benchmark_name"]].add(b["model_id"])

    benchmark_coverage = []
    for name, model_ids in per_benchmark.items():
        n = len(model_ids)
        benchmark_coverage.append({
            "benchmark_name": name,
            "category": categorize_benchmark(name, categories_config),
            "number_of_models": n,
            "percentage_of_model_inventory": round(100 * n / total_models, 2) if total_models else 0.0,
        })
    benchmark_coverage.sort(key=lambda r: -r["number_of_models"])

    priority_coverage = {}
    for name in settings.PRIORITY_BENCHMARKS:
        match = next((row for row in benchmark_coverage if row["benchmark_name"] == name), None)
        priority_coverage[name] = match or {
            "benchmark_name": name,
            "category": categorize_benchmark(name, categories_config),
            "number_of_models": 0,
            "percentage_of_model_inventory": 0.0,
            "note": "not found in collected benchmark data",
        }

    # Potential ranking candidates: models with pricing AND at least one
    # benchmark in every priority category we care about.
    category_by_benchmark = {}
    for cat, benches in categories_config.get("categories", {}).items():
        for bname in benches:
            category_by_benchmark[bname] = cat

    models_by_category_covered = defaultdict(set)
    for b in normalized_benchmarks:
        if not b.get("model_id"):
            continue
        cat = category_by_benchmark.get(b["benchmark_name"])
        if cat:
            models_by_category_covered[cat].add(b["model_id"])

    required_categories = {"reasoning", "general_knowledge", "instruction_following"}
    candidate_sets = [models_by_category_covered.get(c, set()) for c in required_categories]
    ranking_candidates = set.intersection(*candidate_sets) if all(candidate_sets) else set()
    priced_model_ids = {m["model_id"] for m in normalized_models if m.get("has_valid_pricing")}
    ranking_candidates &= priced_model_ids

    return {
        "model_inventory": {
            "total_models": total_models,
            "models_with_pricing": with_pricing,
            "models_without_pricing": without_pricing,
            "models_with_benchmark_data": with_benchmarks,
            "models_without_benchmark_data": without_benchmarks,
        },
        "benchmark_coverage": benchmark_coverage,
        "priority_benchmark_coverage": priority_coverage,
        "potential_ranking_candidates": {
            "count": len(ranking_candidates),
            "model_ids": sorted(ranking_candidates),
            "requires": "priced + at least one benchmark in each of: reasoning, general_knowledge, instruction_following",
        },
        "missing_benchmark_data_count": without_benchmarks,
    }


def build_data_quality_report(
    raw_models: list[dict],
    normalized_models: list[dict],
    normalized_benchmarks: list[dict],
    consistency_warnings: list[dict],
) -> dict:
    issues = []

    # Missing model IDs
    missing_ids = [i for i, m in enumerate(raw_models) if not m.get("id")]
    if missing_ids:
        issues.append({"severity": "error", "issue": "missing_model_id", "count": len(missing_ids),
                        "detail": f"{len(missing_ids)} raw model object(s) had no 'id' field (indices: {missing_ids[:20]})"})

    # Duplicate model IDs
    id_counts = Counter(m.get("id") for m in raw_models if m.get("id"))
    dupes = {mid: c for mid, c in id_counts.items() if c > 1}
    if dupes:
        issues.append({"severity": "error", "issue": "duplicate_model_id", "count": len(dupes),
                        "detail": f"Duplicate OpenRouter model IDs found: {dupes}"})

    # Missing / invalid prices
    missing_prices = [m["model_id"] for m in normalized_models if not m.get("has_valid_pricing")]
    if missing_prices:
        issues.append({"severity": "info", "issue": "missing_or_invalid_pricing", "count": len(missing_prices),
                        "detail": f"{len(missing_prices)} model(s) have no usable prompt/completion pricing "
                                  f"(sample: {missing_prices[:10]})"})

    dynamic_pricing_models = [m["model_id"] for m in normalized_models if m.get("is_dynamic_pricing")]
    if dynamic_pricing_models:
        issues.append({
            "severity": "info", "issue": "dynamic_pricing_sentinel", "count": len(dynamic_pricing_models),
            "detail": f"{len(dynamic_pricing_models)} model(s) use OpenRouter's '-1' sentinel for dynamic/"
                      f"auto-router pricing (price determined per-request, not fixed): {dynamic_pricing_models}. "
                      f"Treated as missing pricing, not a literal negative price.",
        })

    negative_prices = [
        m["model_id"] for m in normalized_models
        if (m.get("input_price_per_token") or 0) < 0 or (m.get("output_price_per_token") or 0) < 0
    ]
    if negative_prices:
        issues.append({"severity": "error", "issue": "negative_price", "count": len(negative_prices),
                        "detail": f"Model(s) with a genuinely negative (non-sentinel) price: {negative_prices}"})

    # Benchmark quality
    missing_benchmark_model_ref = [b for b in normalized_benchmarks if not b.get("raw_model_ref")]
    if missing_benchmark_model_ref:
        issues.append({"severity": "error", "issue": "missing_benchmark_model_id", "count": len(missing_benchmark_model_ref),
                        "detail": f"{len(missing_benchmark_model_ref)} benchmark record(s) had no model reference at all"})

    unmatched = [b for b in normalized_benchmarks if b.get("raw_model_ref") and not b.get("matched")]
    if unmatched:
        sample = sorted({b["raw_model_ref"] for b in unmatched})[:20]
        issues.append({"severity": "warning", "issue": "unmatched_benchmark_model_id", "count": len(unmatched),
                        "detail": f"{len(unmatched)} benchmark record(s) referenced a model ID that couldn't be "
                                  f"matched to any OpenRouter model (sample refs: {sample})"})

    bench_dupe_counter = Counter(
        (b.get("model_id"), b.get("benchmark_name"), b.get("benchmark_version"))
        for b in normalized_benchmarks if b.get("model_id")
    )
    bench_dupes = {k: c for k, c in bench_dupe_counter.items() if c > 1}
    if bench_dupes:
        issues.append({"severity": "info", "issue": "duplicate_benchmark_record", "count": len(bench_dupes),
                        "detail": f"{len(bench_dupes)} (model, benchmark, version) combination(s) have more than "
                                  f"one measurement - see consistency_warnings for detail"})

    issues.extend(consistency_warnings)

    return {
        "issue_count": len(issues),
        "error_count": sum(1 for i in issues if i.get("severity") == "error"),
        "warning_count": sum(1 for i in issues if i.get("severity") == "warning"),
        "info_count": sum(1 for i in issues if i.get("severity") == "info"),
        "issues": issues,
    }


def render_human_readable_report(coverage_report: dict, data_quality_report: dict) -> str:
    inv = coverage_report["model_inventory"]
    lines = [
        "OpenRouter Model Evaluation Data Collection",
        "=" * 44,
        "Retrieved:",
        f"  Models: {inv['total_models']}",
        f"  Models with pricing: {inv['models_with_pricing']}",
        f"  Models with benchmarks: {inv['models_with_benchmark_data']}",
        "",
        "Priority benchmark coverage:",
    ]
    for name, row in coverage_report["priority_benchmark_coverage"].items():
        lines.append(f"  {name}")
        lines.append(f"    Models: {row['number_of_models']}")
        lines.append(f"    Coverage: {row['percentage_of_model_inventory']}%")
    lines += [
        "",
        f"Potential ranking candidates: {coverage_report['potential_ranking_candidates']['count']}",
        f"Missing benchmark data: {coverage_report['missing_benchmark_data_count']}",
        "",
        f"Data quality: {data_quality_report['error_count']} error(s), "
        f"{data_quality_report['warning_count']} warning(s), {data_quality_report['info_count']} info item(s).",
    ]
    return "\n".join(lines)
