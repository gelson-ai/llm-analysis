"""
Pipeline orchestration (Sections 16-20).

Ties together fetch -> discover -> normalize -> categorize -> coverage ->
data-quality -> snapshot -> human-readable report.
"""
from __future__ import annotations

import json
import logging
import csv
from datetime import datetime, timezone
from pathlib import Path

from config import settings
from src import discover, normalize
from src.categorize import consistency_warnings, load_benchmark_categories
from src.coverage import build_coverage_report, build_data_quality_report, render_human_readable_report
from src.openrouter_client import OpenRouterAPIError, fetch_models

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("pipeline")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f, indent=2, default=str)


def _write_csv(
    path: Path,
    records: list[dict],
    excluded_fields: set[str] | None = None,
    fieldnames: list[str] | None = None,
) -> None:
    excluded_fields = excluded_fields or set()
    if fieldnames is None:
        fieldnames = list(dict.fromkeys(
            key for record in records for key in record if key not in excluded_fields
        ))

    with open(path, "w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
        writer.writeheader()
        for record in records:
            writer.writerow({
                key: json.dumps(value, default=str) if isinstance(value, (dict, list)) else value
                for key, value in record.items()
                if key not in excluded_fields
            })


def run(snapshot: bool = True) -> dict:
    """Runs the full pipeline. Returns a summary dict. Raises
    OpenRouterAPIError (uncaught, on purpose) if the model inventory can't be
    reliably fetched - see Section 15, 'fail loudly'."""
    retrieved_at = _now_iso()
    run_date = retrieved_at[:10]

    # ---- 1. Fetch raw model inventory -----------------------------------
    logger.info("Fetching OpenRouter model inventory...")
    fetch_result = fetch_models()
    raw_models = fetch_result.raw_json["data"]

    _write_json(settings.RAW_DIR / "openrouter_models.json", fetch_result.raw_json)

    # ---- 2. Discover benchmark data (investigation, non-fatal) ----------
    logger.info("Probing candidate endpoints for benchmark data...")
    endpoint_probe = discover.probe_candidate_endpoints(settings.CANDIDATE_BENCHMARK_ENDPOINTS)
    field_scan = discover.scan_models_for_benchmark_fields(raw_models)
    benchmark_discovery = {"endpoint_probe": endpoint_probe, "field_scan": field_scan}
    _write_json(settings.RAW_DIR / "openrouter_benchmarks.json", benchmark_discovery)

    logger.info("Benchmark discovery conclusion: %s", field_scan["conclusion"])

    # Confirmed (2026-09-08 live run): OpenRouter embeds a `benchmarks` block
    # directly on each model object - Artificial Analysis composite indices
    # (intelligence/coding/agentic) plus separately-provenanced Design Arena
    # rankings. See src/normalize.py's extract_benchmark_entries_from_raw_models
    # docstring and README "The benchmark question" for the full writeup on
    # why these are NOT GPQA Diamond / IFBench / AA-Omniscience.
    raw_benchmark_entries: list[dict] = normalize.extract_benchmark_entries_from_raw_models(raw_models)

    # ---- 3. Normalize models ---------------------------------------------
    logger.info("Normalizing %d models...", len(raw_models))
    normalized_models = [normalize.normalize_model(m, retrieved_at) for m in raw_models]
    model_index_result = normalize.build_model_index(normalized_models)

    _write_json(settings.NORMALIZED_DIR / "models.json", normalized_models)
    _write_csv(settings.NORMALIZED_DIR / "models.csv", normalized_models, {"raw"})

    # ---- 4. Normalize benchmarks (empty until a source is confirmed) -----
    normalized_benchmarks = [
        normalize.normalize_benchmark_record(
            model_ref=entry["model_ref"],
            benchmark_name=entry["benchmark_name"],
            raw_record=entry["raw_record"],
            model_index=model_index_result["index"],
            retrieved_at=retrieved_at,
            benchmark_source=entry.get("benchmark_source", "Artificial Analysis"),
            source_platform=entry.get("source_platform", "OpenRouter"),
        )
        for entry in raw_benchmark_entries
    ]
    _write_json(settings.NORMALIZED_DIR / "benchmarks.json", normalized_benchmarks)
    _write_json(settings.NORMALIZED_DIR / "model_benchmarks.json", normalized_benchmarks)
    _write_csv(
        settings.NORMALIZED_DIR / "model_benchmarks.csv",
        normalized_benchmarks,
        {"raw_data"},
        [
            "model_id", "raw_model_ref", "matched", "benchmark_name", "score",
            "score_unit", "benchmark_source", "source_platform", "retrieved_at",
            "benchmark_version", "benchmark_timestamp",
        ],
    )

    # ---- 5. Categorize + consistency checks -------------------------------
    categories_config = load_benchmark_categories()
    warnings = consistency_warnings(normalized_benchmarks)

    # ---- 6. Coverage + data quality reports --------------------------------
    coverage_report = build_coverage_report(normalized_models, normalized_benchmarks, categories_config)
    quality_report = build_data_quality_report(raw_models, normalized_models, normalized_benchmarks, warnings)

    if model_index_result["collisions"]:
        quality_report["issues"].append({
            "severity": "warning",
            "issue": "model_id_index_collision",
            "count": len(model_index_result["collisions"]),
            "detail": f"Loosened identifiers collided across multiple model IDs: {model_index_result['collisions']}",
        })

    _write_json(settings.ANALYSIS_DIR / "coverage_report.json", coverage_report)
    _write_json(settings.ANALYSIS_DIR / "data_quality_report.json", quality_report)
    _write_json(settings.ANALYSIS_DIR / "benchmark_discovery.json", benchmark_discovery)

    human_report = render_human_readable_report(coverage_report, quality_report)
    (settings.ANALYSIS_DIR / "report.txt").write_text(human_report)
    logger.info("\n" + human_report)

    # ---- 7. Sample inspection (Section 20-G) -------------------------------
    sample_inspection = _sample_inspection(normalized_models, model_index_result["index"])
    _write_json(settings.ANALYSIS_DIR / "sample_inspection.json", sample_inspection)
    for entry in sample_inspection:
        logger.info("Sample inspection - %s: %s", entry["model_id"], entry["status"])

    # ---- 8. Snapshot --------------------------------------------------------
    if snapshot:
        _write_snapshot(run_date, fetch_result.raw_json, benchmark_discovery, normalized_models,
                         normalized_benchmarks, coverage_report, quality_report)

    return {
        "retrieved_at": retrieved_at,
        "model_count": len(raw_models),
        "coverage_report": coverage_report,
        "data_quality_report": quality_report,
        "sample_inspection": sample_inspection,
    }


def _sample_inspection(normalized_models: list[dict], model_index: dict) -> list[dict]:
    results = []
    watchlist = list(settings.WATCHLIST_MODEL_IDS)
    by_id = {m["model_id"]: m for m in normalized_models if m.get("model_id")}

    for model_id in watchlist:
        if model_id in by_id:
            results.append({"model_id": model_id, "status": "present", "note": "found in raw inventory"})
        else:
            results.append({"model_id": model_id, "status": "MISSING",
                             "note": "not found in this run's OpenRouter inventory - "
                                     "check whether it was renamed, deprecated, or the fetch is incomplete"})

    for substring in settings.WATCHLIST_MODEL_NAME_SUBSTRINGS:
        matches = [m["model_id"] for m in normalized_models
                   if m.get("model_name") and substring.lower() in m["model_name"].lower()]
        results.append({
            "model_id": f"name-contains:{substring}",
            "status": "present" if matches else "MISSING",
            "note": f"matched: {matches}" if matches else "no model name contained this substring",
        })

    return results


def _write_snapshot(run_date, raw_models_payload, benchmark_discovery, normalized_models,
                     normalized_benchmarks, coverage_report, quality_report) -> None:
    snap_dir = settings.SNAPSHOTS_DIR / run_date
    suffix = 1
    original = snap_dir
    while snap_dir.exists():
        suffix += 1
        snap_dir = Path(f"{original}-{suffix}")
    snap_dir.mkdir(parents=True, exist_ok=True)

    _write_json(snap_dir / "openrouter_models.json", raw_models_payload)
    _write_json(snap_dir / "openrouter_benchmarks.json", benchmark_discovery)
    _write_json(snap_dir / "model_benchmarks.json", normalized_benchmarks)
    _write_json(snap_dir / "coverage_report.json", coverage_report)
    _write_json(snap_dir / "data_quality_report.json", quality_report)
    logger.info("Snapshot written to %s", snap_dir)
