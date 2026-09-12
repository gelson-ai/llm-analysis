"""
Benchmark categorization + consistency checks (Sections 9, 10).
"""
from __future__ import annotations

import json
from collections import defaultdict
from typing import Optional

from config import settings


def load_benchmark_categories() -> dict:
    with open(settings.BENCHMARK_CATEGORIES_PATH) as f:
        return json.load(f)


def categorize_benchmark(benchmark_name: str, categories_config: dict) -> str:
    """Return the category a benchmark belongs to, or 'uncategorized' if it
    is not present in the config. Never guesses a category from the name.

    Supports one deliberate exception to exact matching: "Design Arena: ..."
    sub-benchmarks (one per arena/category pair, e.g.
    "Design Arena: agents/pptxslides") are matched against a bare
    "Design Arena" config entry, since there are dozens of these and listing
    every one individually in benchmark_categories.json would be noise.
    """
    for category, benchmarks in categories_config.get("categories", {}).items():
        if benchmark_name in benchmarks:
            return category
        if benchmark_name.startswith("Design Arena: ") and "Design Arena" in benchmarks:
            return category
    return "uncategorized"


def consistency_warnings(benchmark_records: list[dict]) -> list[dict]:
    """Section 10: inspect every benchmark we might compare across models and
    flag anything that would make combining scores unsafe.

    Checks performed per benchmark_name:
      - multiple distinct benchmark_version values in use
      - multiple distinct score_unit values in use (scale inconsistency)
      - records missing benchmark_version entirely, mixed with records that
        have one (can't tell if they're the same version)
    """
    warnings: list[dict] = []
    by_name: dict[str, list[dict]] = defaultdict(list)
    for rec in benchmark_records:
        by_name[rec["benchmark_name"]].append(rec)

    for name, records in by_name.items():
        versions = {r.get("benchmark_version") for r in records}
        units = {r.get("score_unit") for r in records}

        if len(versions) > 1:
            warnings.append({
                "severity": "warning",
                "benchmark_name": name,
                "issue": "multiple_versions",
                "detail": f"{name} has records under {len(versions)} distinct benchmark_version values: "
                          f"{sorted(str(v) for v in versions)}. Do not combine these without explicit "
                          f"normalization.",
            })

        if None in versions and len(versions) > 1:
            warnings.append({
                "severity": "warning",
                "benchmark_name": name,
                "issue": "missing_version_mixed_with_versioned",
                "detail": f"{name} has some records with a benchmark_version and some without. "
                          f"Cannot confirm the unversioned records use the same methodology.",
            })

        if len(units) > 1:
            warnings.append({
                "severity": "warning",
                "benchmark_name": name,
                "issue": "inconsistent_score_scale",
                "detail": f"{name} scores appear in {len(units)} different representations: "
                          f"{sorted(str(u) for u in units)}. Rescale explicitly before comparing.",
            })

        # Duplicate (model_id, benchmark_name) pairs = multiple measurements
        # for the same model on the same benchmark.
        seen = defaultdict(int)
        for r in records:
            if r.get("model_id"):
                seen[r["model_id"]] += 1
        dupes = {mid: count for mid, count in seen.items() if count > 1}
        if dupes:
            warnings.append({
                "severity": "info",
                "benchmark_name": name,
                "issue": "multiple_measurements_per_model",
                "detail": f"{len(dupes)} model(s) have more than one {name} measurement, e.g. {list(dupes.items())[:5]}. "
                          f"Decide on a deduplication rule (latest timestamp? highest score?) before ranking.",
            })

    return warnings
