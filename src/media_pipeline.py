"""
Orchestration for the media (image- and video-generation) catalogs.

Self-contained companion to src/pipeline.py. It runs alongside the chat
pipeline, never inside it:

  - it writes only NEW files (see the MEDIA_* path constants in
    config/settings.py) - nothing the chat pipeline produces is overwritten,
    merged into, or reshaped;
  - it is not invoked by dashboard/refresh.py or run_weekly.bat, so the weekly
    dashboard refresh path is untouched;
  - it is runnable entirely on its own: `python run_media_pipeline.py`.

Shape mirrors src/pipeline.py deliberately (fetch -> normalize -> write
raw/normalized/analysis -> optional snapshot), so the two pipelines read alike,
and it reuses that module's JSON/CSV writers rather than reimplementing them.

Fail-loud contract (same as the chat pipeline): if any catalog fetch fails -
empty, malformed, or below its configured floor - OpenRouterAPIError propagates
and NOTHING is written to data/normalized/. All three catalogs are fetched
before any file is written, so a failure on a later one can never leave an
earlier one's output behind as a half-run.

Per-model image endpoint pricing is the one deliberate exception to
raise-everything: it is an enrichment fan-out of one request per image model, so
a single dead record logs a warning and is counted in the quality report
instead of destroying the run. Failures are reported, never swallowed.
"""
from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path
from typing import Optional

from config import settings
from src import normalize, normalize_media
from src.media_benchmark_scraper import (
    MediaBenchmarkParseError,
    discover_prompt_slugs,
    normalize_prompt_benchmark_row,
    parse_prompt_page,
    prompt_name_from_slug,
    strip_release_date_suffix,
)
from src.media_coverage import build_media_coverage_report, build_media_data_quality_report
from src.openrouter_client import (
    OpenRouterAPIError,
    fetch_image_model_endpoints,
    fetch_image_models,
    fetch_image_models_filtered,
    fetch_page,
    fetch_video_models,
)

# Reused from the chat pipeline so both pipelines serialize JSON/CSV
# identically. Importing this module does not change src/pipeline.py's behavior.
from src.pipeline import _now_iso, _write_csv, _write_json

logger = logging.getLogger("media_pipeline")

MEDIA_BENCHMARK_CSV_FIELDS = [
    "model_id", "raw_model_ref", "matched", "benchmark_name", "score",
    "score_unit", "benchmark_source", "source_platform", "source_endpoint",
    "retrieved_at", "benchmark_version", "benchmark_timestamp",
]


MEDIA_PROMPT_BENCHMARK_CSV_FIELDS = [
    "model_id", "raw_model_slug", "model_name", "matched", "media_type",
    "prompt_slug", "prompt_name", "benchmark_name", "benchmark_source",
    "source_platform", "source_url", "checks_passed", "checks_total",
    "pass_rate", "correctness_label", "cost_usd", "generation_seconds",
    "retrieved_at",
]


def _empty_endpoint_stats() -> dict:
    return {
        "enabled": False,
        "requested": 0,
        "succeeded": 0,
        "failed": 0,
        "failed_model_ids": [],
        "models_with_any_price_from_endpoints": 0,
        "models_with_no_price_from_endpoints": [],
        "delay_seconds_between_requests": settings.MEDIA_IMAGE_ENDPOINTS_DELAY_SECONDS,
    }


def _fetch_image_endpoint_pricing(raw_image_models: list[dict]) -> tuple[dict, dict, dict]:
    """Fetch and parse the per-model endpoints record for every image model.

    Returns (parsed_by_model_id, raw_by_model_id, stats). Never raises for an
    individual model - see the module docstring - but every failure is recorded
    in `stats` and surfaced in the data-quality report.
    """
    parsed: dict[str, dict] = {}
    raw_by_model_id: dict[str, dict] = {}
    failed_model_ids: list[str] = []
    without_price: list[str] = []
    requests_made = 0

    for raw_model in raw_image_models:
        model_id = raw_model.get("id") if isinstance(raw_model, dict) else None
        if not model_id:
            continue

        # Serialise with a small gap: this is dozens of requests per run.
        if requests_made:
            time.sleep(settings.MEDIA_IMAGE_ENDPOINTS_DELAY_SECONDS)
        requests_made += 1

        result = fetch_image_model_endpoints(model_id)
        raw_by_model_id[model_id] = {
            "endpoint": result.endpoint,
            "status_code": result.status_code,
            "ok": result.ok,
            "error": result.error,
            "attempts": result.attempts,
            "payload": result.raw_json if result.ok else None,
        }

        if not result.ok:
            failed_model_ids.append(model_id)
            continue

        model_pricing = normalize_media.normalize_image_endpoint_pricing(result.raw_json)
        parsed[model_id] = model_pricing
        if not model_pricing["has_any_price"]:
            without_price.append(model_id)

    stats = {
        "enabled": True,
        "requested": requests_made,
        "succeeded": requests_made - len(failed_model_ids),
        "failed": len(failed_model_ids),
        "failed_model_ids": sorted(failed_model_ids),
        "models_with_any_price_from_endpoints": sum(1 for p in parsed.values() if p["has_any_price"]),
        "models_with_no_price_from_endpoints": sorted(without_price),
        "delay_seconds_between_requests": settings.MEDIA_IMAGE_ENDPOINTS_DELAY_SECONDS,
    }
    logger.info(
        "Image endpoint pricing: %d/%d record(s) fetched, %d failed, %d model(s) with a usable price",
        stats["succeeded"], stats["requested"], stats["failed"],
        stats["models_with_any_price_from_endpoints"],
    )
    return parsed, raw_by_model_id, stats


def run(snapshot: bool = True, with_image_pricing: bool = True) -> dict:
    """Run the media pipeline. Returns a summary dict.

    Raises OpenRouterAPIError (uncaught, on purpose) if either catalog cannot be
    reliably fetched - no partial dataset is written to data/normalized/.
    """
    retrieved_at = _now_iso()
    run_date = retrieved_at[:10]

    # ---- 1. Fetch all catalogs (nothing written until every one succeeds) -
    logger.info("Fetching OpenRouter image + video model catalogs...")
    image_result = fetch_image_models()
    video_result = fetch_video_models()
    raw_image_models = image_result.raw_json["data"]
    raw_video_models = video_result.raw_json["data"]

    # Third source, for benchmarks only: the dedicated image endpoint publishes
    # no `benchmarks` block, but the generic catalog filtered to image output
    # does carry design_arena scores for a subset of the same models.
    logger.info("Fetching filtered generic catalog (Design Arena source)...")
    filtered_result = fetch_image_models_filtered()
    raw_filtered_image_models = filtered_result.raw_json["data"]

    _write_json(settings.MEDIA_RAW_IMAGE_MODELS_PATH, image_result.raw_json)
    _write_json(settings.MEDIA_RAW_VIDEO_MODELS_PATH, video_result.raw_json)

    # ---- 2. Image pricing fan-out -----------------------------------------
    # The image list endpoint publishes no pricing at all; cost lives only in
    # the per-model endpoints record. Video models need no equivalent step -
    # their pricing_skus arrives inline.
    if with_image_pricing:
        logger.info("Fetching per-model endpoint pricing for %d image models...", len(raw_image_models))
        endpoint_pricing_by_model, endpoint_raw_map, endpoint_stats = _fetch_image_endpoint_pricing(raw_image_models)
        _write_json(settings.MEDIA_RAW_IMAGE_ENDPOINTS_PATH, endpoint_raw_map)
    else:
        logger.info("Skipping per-model image endpoint pricing (--skip-image-pricing)")
        endpoint_pricing_by_model, endpoint_raw_map, endpoint_stats = {}, {}, _empty_endpoint_stats()
        # Deliberately do NOT write the raw file here. Writing an empty object
        # would overwrite the previous run's captured records with nothing,
        # destroying the raw evidence for a run that never happened.
        logger.info(
            "Left %s untouched (previous run's records preserved)",
            settings.MEDIA_RAW_IMAGE_ENDPOINTS_PATH,
        )

    # ---- 3. Normalize ------------------------------------------------------
    logger.info("Normalizing %d image and %d video models...", len(raw_image_models), len(raw_video_models))
    image_models = [
        normalize_media.normalize_media_model(
            m, normalize_media.IMAGE_MODEL_TYPE, retrieved_at,
            endpoint_pricing=endpoint_pricing_by_model.get(m.get("id")),
        )
        for m in raw_image_models
    ]
    video_models = [
        normalize_media.normalize_media_model(m, normalize_media.VIDEO_MODEL_TYPE, retrieved_at)
        for m in raw_video_models
    ]

    _write_json(settings.MEDIA_IMAGE_MODELS_JSON_PATH, image_models)
    _write_csv(settings.MEDIA_IMAGE_MODELS_CSV_PATH, image_models, {"raw"})
    _write_json(settings.MEDIA_VIDEO_MODELS_JSON_PATH, video_models)
    _write_csv(settings.MEDIA_VIDEO_MODELS_CSV_PATH, video_models, {"raw"})

    # ---- 4. Design Arena benchmarks ----------------------------------------
    # Three sources are scanned; the filtered generic catalog is the only one
    # that actually publishes design_arena today. Duplicates across overlapping
    # sources are suppressed and counted, never dropped silently.
    raw_arena_entries, dropped_arena_entries = normalize_media.collect_design_arena_entries([
        (settings.IMAGES_MODELS_ENDPOINT, raw_image_models),
        (settings.VIDEOS_MODELS_ENDPOINT, raw_video_models),
        (settings.IMAGE_MODELS_FILTER_ENDPOINT, raw_filtered_image_models),
    ])
    media_model_index = normalize_media.build_media_model_index(
        image_models + video_models,
        # The filtered catalog is the only image source publishing canonical_slug,
        # which is what benchmark page links use.
        extra_catalog_models=raw_filtered_image_models,
    )
    media_benchmarks = [
        normalize.normalize_benchmark_record(
            model_ref=entry["model_ref"],
            benchmark_name=entry["benchmark_name"],
            raw_record=entry["raw_record"],
            model_index=media_model_index["index"],
            retrieved_at=retrieved_at,
            benchmark_source=entry.get("benchmark_source", normalize_media.DESIGN_ARENA_BENCHMARK_SOURCE),
            source_platform=entry.get("source_platform", "OpenRouter"),
        )
        | {"source_endpoint": entry.get("source_endpoint")}
        for entry in raw_arena_entries
    ]
    logger.info(
        "Extracted %d Design Arena benchmark record(s) from media models across %d source(s)%s",
        len(media_benchmarks), 3,
        f" ({len(dropped_arena_entries)} duplicate(s) suppressed)" if dropped_arena_entries else "",
    )

    _write_json(settings.MEDIA_BENCHMARKS_JSON_PATH, media_benchmarks)
    _write_csv(
        settings.MEDIA_BENCHMARKS_CSV_PATH,
        media_benchmarks,
        {"raw_data"},
        MEDIA_BENCHMARK_CSV_FIELDS,
    )

    # ---- 4b. Prompt benchmarks (scraped HTML pages) -------------------------
    prompt_benchmarks, prompt_bundle = _fetch_prompt_benchmarks(
        retrieved_at, media_model_index["index"]
    )
    prompt_stats = prompt_bundle["stats"]

    # The extracted rows are preserved verbatim; the full page HTML is not, since
    # 27 pages is several MB per run and this repo tracks data/**. A sha256 +
    # byte size per page is stored instead, which is what actually matters for
    # spotting markup drift, and every page is re-parseable from the URL.
    _write_json(settings.MEDIA_RAW_PROMPT_BENCHMARK_ROWS_PATH, {
        "retrieved_at": retrieved_at,
        "rows": prompt_bundle["raw_rows"],
        "page_fingerprints": prompt_stats["page_fingerprints"],
        "note": (
            "Verbatim parsed rows (not normalized). Full page HTML is deliberately not "
            "committed - see page_fingerprints for a sha256/byte-size drift check."
        ),
    })
    _write_json(settings.MEDIA_PROMPT_BENCHMARKS_JSON_PATH, prompt_benchmarks)
    _write_csv(
        settings.MEDIA_PROMPT_BENCHMARKS_CSV_PATH,
        prompt_benchmarks,
        {"raw_data"},
        MEDIA_PROMPT_BENCHMARK_CSV_FIELDS,
    )

    # ---- 5. Coverage + data quality ---------------------------------------
    coverage_report = build_media_coverage_report(
        image_models, video_models, media_benchmarks, endpoint_stats, retrieved_at,
        filtered_image_raw_models=raw_filtered_image_models,
        dropped_duplicate_benchmarks=dropped_arena_entries,
        prompt_benchmark_stats=prompt_stats,
    )
    quality_report = build_media_data_quality_report(
        {
            normalize_media.IMAGE_MODEL_TYPE: raw_image_models,
            normalize_media.VIDEO_MODEL_TYPE: raw_video_models,
        },
        {
            normalize_media.IMAGE_MODEL_TYPE: image_models,
            normalize_media.VIDEO_MODEL_TYPE: video_models,
        },
        media_benchmarks,
        endpoint_stats,
        dropped_duplicate_benchmarks=dropped_arena_entries,
        benchmark_sources_scanned=[
            settings.IMAGES_MODELS_ENDPOINT,
            settings.VIDEOS_MODELS_ENDPOINT,
            settings.IMAGE_MODELS_FILTER_ENDPOINT,
        ],
        prompt_benchmark_stats=prompt_stats,
    )
    if media_model_index["collisions"]:
        quality_report["issues"].append({
            "severity": "warning",
            "issue": "model_id_index_collision",
            "count": len(media_model_index["collisions"]),
            "detail": f"Loosened identifiers collided across multiple media model IDs: "
                      f"{media_model_index['collisions']}",
        })
        quality_report["issue_count"] = len(quality_report["issues"])
        quality_report["warning_count"] = sum(
            1 for i in quality_report["issues"] if i.get("severity") == "warning"
        )

    _write_json(settings.MEDIA_COVERAGE_REPORT_PATH, coverage_report)
    _write_json(settings.MEDIA_DATA_QUALITY_REPORT_PATH, quality_report)

    logger.info(
        "Media coverage: %d image / %d video models, %d with a resolved price, "
        "%d with Design Arena rows. Data quality: %d error(s), %d warning(s), %d info item(s).",
        coverage_report["model_inventory"]["image"]["total_models"],
        coverage_report["model_inventory"]["video"]["total_models"],
        coverage_report["model_inventory"]["total_with_resolved_price"],
        coverage_report["model_inventory"]["total_with_design_arena"],
        quality_report["error_count"], quality_report["warning_count"], quality_report["info_count"],
    )

    # ---- 6. Snapshot -------------------------------------------------------
    if snapshot:
        _write_media_snapshot(
            run_date,
            image_result.raw_json,
            video_result.raw_json,
            endpoint_raw_map,
            image_models,
            video_models,
            media_benchmarks,
            coverage_report,
            quality_report,
            prompt_benchmarks,
            prompt_bundle["raw_rows"],
        )

    return {
        "retrieved_at": retrieved_at,
        "image_model_count": len(raw_image_models),
        "video_model_count": len(raw_video_models),
        "prompt_benchmark_row_count": len(prompt_benchmarks),
        "coverage_report": coverage_report,
        "data_quality_report": quality_report,
    }


def _fetch_prompt_benchmarks(retrieved_at: str, model_index: dict) -> tuple[list[dict], dict]:
    """Scrape every OpenRouter media prompt-benchmark page.

    Returns (normalized_records, stats). Raises OpenRouterAPIError for a page
    that cannot be fetched and MediaBenchmarkParseError for a page that parses
    to nothing - see the scraper module for why an empty parse must be fatal.
    """
    pages: dict[str, dict] = {}
    raw_rows: list[dict] = []
    normalized: list[dict] = []
    fingerprints: dict[str, dict] = {}
    prompt_counts: dict[str, int] = {}
    conflicts = 0

    floors = {
        "image": settings.MIN_EXPECTED_IMAGE_PROMPT_COUNT,
        "video": settings.MIN_EXPECTED_VIDEO_PROMPT_COUNT,
    }

    for media_type, index_path in settings.MEDIA_BENCHMARK_INDEX_ENDPOINTS.items():
        index_result = fetch_page(index_path)
        if not index_result.ok:
            raise OpenRouterAPIError(
                f"Failed to fetch media benchmark index page {index_path}: {index_result.error}"
            )

        slugs = discover_prompt_slugs(index_result.text or "", media_type)
        floor = floors.get(media_type, 1)
        if len(slugs) < floor:
            raise MediaBenchmarkParseError(
                f"Only {len(slugs)} {media_type} prompt(s) discovered on {index_path}, below the "
                f"configured floor of {floor}. The index page markup has probably changed shape - "
                f"refusing to write a partial benchmark set."
            )
        logger.info("Discovered %d %s prompt benchmark page(s)", len(slugs), media_type)

        for position, slug in enumerate(slugs):
            page_path = f"{index_path}/{slug}"
            if position:
                time.sleep(settings.MEDIA_BENCHMARK_PAGE_DELAY_SECONDS)

            page = fetch_page(page_path)
            if not page.ok:
                raise OpenRouterAPIError(f"Failed to fetch benchmark page {page_path}: {page.error}")

            page_html = page.text or ""
            fingerprints[page_path] = {
                "sha256": hashlib.sha256(page_html.encode("utf-8")).hexdigest(),
                "bytes": len(page_html.encode("utf-8")),
                "attempts": page.attempts,
            }

            rows = parse_prompt_page(page_html)
            if not rows:
                raise MediaBenchmarkParseError(
                    f"{page_path} parsed to zero model rows. Results ARE published for every prompt, so "
                    f"an empty parse means the page markup changed (or the page failed to render). "
                    f"Refusing to treat this as 'no benchmark data for these models'."
                )

            prompt_name = prompt_name_from_slug(slug)
            prompt_counts[slug] = len(rows)

            for row in rows:
                conflicts += 1 if row.get("conflicting_values") else 0
                raw_rows.append({**row, "media_type": media_type, "prompt_slug": slug,
                                 "source_url": page.url})
                normalized.append(normalize_prompt_benchmark_row(
                    row,
                    media_type=media_type,
                    prompt_slug=slug,
                    prompt_name=prompt_name,
                    source_url=page.url,
                    retrieved_at=retrieved_at,
                    model_id=_resolve_benchmark_model_id(row["raw_model_slug"], model_index),
                ))

    pages["page_fingerprints"] = fingerprints
    pages["rows_per_prompt"] = prompt_counts

    unmatched = sorted({r["raw_model_slug"] for r in normalized if not r["matched"]})

    if len(normalized) < settings.MIN_EXPECTED_TOTAL_PROMPT_BENCHMARK_ROWS:
        raise MediaBenchmarkParseError(
            f"Only {len(normalized)} prompt-benchmark row(s) parsed across "
            f"{len(fingerprints)} page(s), below the floor of "
            f"{settings.MIN_EXPECTED_TOTAL_PROMPT_BENCHMARK_ROWS}. Individual prompts legitimately vary in "
            f"how many models they cover, but a total this low means the parsing broke somewhere - "
            f"refusing to write a partial benchmark set. Rows per prompt: {prompt_counts}"
        )

    checks_sources: dict[str, int] = {}
    for record in normalized:
        source = (record.get("raw_data") or {}).get("checks_source") or "unknown"
        checks_sources[source] = checks_sources.get(source, 0) + 1

    stats = {
        "pages_fetched": len(fingerprints),
        "rows": len(normalized),
        "rows_per_prompt": prompt_counts,
        "distinct_models_in_benchmarks": len({r["raw_model_slug"] for r in normalized}),
        "models_matched_to_inventory": len({r["model_id"] for r in normalized if r["model_id"]}),
        "unmatched_model_slugs": unmatched,
        "conflicting_row_values": conflicts,
        "checks_source_counts": checks_sources,
        "rows_missing_cost": sum(1 for r in normalized if r.get("cost_usd") is None),
        "rows_missing_generation_time": sum(1 for r in normalized if r.get("generation_seconds") is None),
        "page_fingerprints": fingerprints,
    }
    logger.info(
        "Prompt benchmarks: %d row(s) across %d page(s); %d model(s) matched to the catalog, "
        "%d unmatched, %d conflicting row(s)",
        stats["rows"], stats["pages_fetched"], stats["models_matched_to_inventory"],
        len(unmatched), conflicts,
    )
    return normalized, {"stats": stats, "raw_rows": raw_rows}


def _resolve_benchmark_model_id(raw_slug: str, model_index: dict) -> Optional[str]:
    """Match a benchmark page slug to a catalog model.

    Tries the slug as-is first (video models publish the dated canonical slug,
    which the index already contains), then the release-date-stripped form, which
    is what the dedicated image catalog needs because it publishes no
    canonical_slug.
    """
    matched = normalize.match_benchmark_model_id(raw_slug, model_index)
    if matched:
        return matched
    stripped = strip_release_date_suffix(raw_slug)
    if stripped != raw_slug:
        return normalize.match_benchmark_model_id(stripped, model_index)
    return None


def _write_media_snapshot(
    run_date: str,
    raw_image_payload,
    raw_video_payload,
    endpoint_raw_map,
    image_models: list[dict],
    video_models: list[dict],
    media_benchmarks: list[dict],
    coverage_report: dict,
    quality_report: dict,
    prompt_benchmarks: list[dict],
    prompt_raw_rows: list[dict],
) -> None:
    """Write a media snapshot under <SNAPSHOTS_DIR>/<date>/media[/-N]/.

    Its own subfolder and its own suffix loop, so it can never collide with the
    chat pipeline's snapshot files.

    Note: creating the <date> folder here is visible to
    dashboard/refresh.py's snapshot_exists_for(), which only inspects top-level
    directories and uses "does today's folder exist?" to decide whether to pass
    --no-snapshot to the chat pipeline. So a media run that lands before the
    day's first chat run will make that day's chat refresh skip its snapshot.
    That is logged explicitly below rather than left to be discovered.
    """
    day_dir = settings.SNAPSHOTS_DIR / run_date
    day_dir_existed = day_dir.exists()

    media_dir = day_dir / settings.MEDIA_SNAPSHOT_SUBDIR
    suffix = 1
    original = media_dir
    while media_dir.exists():
        suffix += 1
        media_dir = Path(f"{original}-{suffix}")
    media_dir.mkdir(parents=True, exist_ok=True)

    if not day_dir_existed:
        logger.warning(
            "Created today's snapshot folder %s for media data. dashboard/refresh.py treats the "
            "existence of this folder as 'a snapshot already exists for today' and will pass "
            "--no-snapshot to the chat pipeline, so the chat snapshot for %s may be skipped if the "
            "weekly refresh has not run yet today.",
            day_dir, run_date,
        )

    _write_json(media_dir / "openrouter_image_models.json", raw_image_payload)
    _write_json(media_dir / "openrouter_video_models.json", raw_video_payload)
    _write_json(media_dir / "openrouter_image_model_endpoints.json", endpoint_raw_map)
    _write_json(media_dir / "image_models.json", image_models)
    _write_json(media_dir / "video_models.json", video_models)
    _write_json(media_dir / "media_benchmarks.json", media_benchmarks)
    _write_json(media_dir / "media_prompt_benchmarks.json", prompt_benchmarks)
    _write_json(media_dir / "openrouter_media_prompt_benchmark_rows.json", prompt_raw_rows)
    _write_json(media_dir / "media_coverage_report.json", coverage_report)
    _write_json(media_dir / "media_data_quality_report.json", quality_report)
    logger.info("Media snapshot written to %s", media_dir)
