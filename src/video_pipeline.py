"""Orchestration for the VIDEO-generation model dashboard.

Third sibling of src/pipeline.py (chat models) and src/media_pipeline.py (image
catalogue + the image page's benchmark data). It exists because the video
dashboard needs its own fetch/refresh target: refresh_all.py reports per target,
and a failure in one pipeline must never skip or corrupt another.

Everything it writes is defined by the VIDEO_DASHBOARD_* constants in
config/settings.py. No MEDIA_* path and no chat path is ever touched, which
matters because the published image page embeds the media coverage report
verbatim - a video run must not be able to change the image page's bytes.

It reuses only helpers that are already shared and pure, and modifies none of
them:

  * src/openrouter_client.fetch_video_models / fetch_page  (transport);
  * src/media_benchmark_scraper                            (pure HTML parser);
  * src/normalize_media.normalize_media_model / normalize_media_pricing;
  * src/normalize.match_benchmark_model_id;
  * src/pipeline._now_iso / _write_json / _write_csv       (identical output).

One deliberate difference from the media pipeline: there is NO per-model endpoint
fan-out. Image models publish no pricing on their list endpoint, so the media
pipeline needs one extra request per image model; video models publish their
`pricing_skus` inline, so this pipeline needs one catalogue request plus the
prompt-benchmark pages.

Fail-loud contract (same as the other two pipelines): if the catalogue or the
prompt-benchmark index cannot be fetched - or the catalogue is below its floor -
OpenRouterAPIError propagates and nothing is written to data/normalized/.
"""
from __future__ import annotations

import hashlib
import logging
import time
from typing import Optional

from config import settings
from src import normalize, normalize_media
from src.media_benchmark_scraper import (
    BENCHMARK_SOURCE,
    MediaBenchmarkParseError,
    discover_prompt_slugs,
    normalize_prompt_benchmark_row,
    page_publishes_judged_checks,
    parse_prompt_page,
    prompt_name_from_slug,
    strip_release_date_suffix,
)
from src.openrouter_client import OpenRouterAPIError, fetch_page, fetch_video_models
from src.pipeline import _now_iso, _write_csv, _write_json
from src.video_coverage import (
    build_video_coverage_report,
    build_video_data_quality_report,
    pool_prompt_benchmarks,
)

logger = logging.getLogger("video_pipeline")

# Mirrors the media pipeline's prompt-benchmark CSV contract so the two pipelines'
# outputs read alike. Declared explicitly (not derived from the rows) so a page
# that stops publishing a field cannot silently change the CSV schema.
VIDEO_PROMPT_BENCHMARK_CSV_FIELDS = [
    "model_id", "raw_model_slug", "model_name", "matched", "media_type",
    "prompt_slug", "prompt_name", "benchmark_name", "benchmark_source",
    "source_platform", "source_url", "checks_passed", "checks_total",
    "pass_rate", "correctness_label", "cost_usd", "generation_seconds",
    "asset_count", "asset_url", "thumbnail_url", "asset_media_type",
    "output_width", "output_height", "duration_ms",
    "retrieved_at",
]

# The single media type this pipeline handles. Kept as a name rather than a bare
# string so a future video-adjacent page (e.g. video editing) is a deliberate edit.
VIDEO_MEDIA_TYPE = normalize_media.VIDEO_MODEL_TYPE


def run(snapshot: bool = True) -> dict:
    """Run the video pipeline. Returns a summary dict.

    Raises OpenRouterAPIError if the catalogue cannot be reliably fetched, and
    MediaBenchmarkParseError if a benchmark page's markup no longer parses.
    Neither is caught here, on purpose: this is a fail-loud pipeline, and nothing
    has been written to data/normalized/ at the point either can be raised.
    """
    retrieved_at = _now_iso()
    run_date = retrieved_at[:10]

    # ---- 1. Fetch the catalogue (nothing written until it succeeds) --------
    logger.info("Fetching the OpenRouter video model catalogue...")
    catalogue_result = fetch_video_models()
    raw_video_models = catalogue_result.raw_json["data"]
    _write_json(settings.VIDEO_DASHBOARD_RAW_MODELS_PATH, catalogue_result.raw_json)

    # ---- 2. Normalize -----------------------------------------------------
    logger.info("Normalizing %d video model(s)...", len(raw_video_models))
    video_models = [
        normalize_media.normalize_media_model(m, VIDEO_MEDIA_TYPE, retrieved_at)
        for m in raw_video_models
    ]
    model_index = normalize_media.build_media_model_index(video_models)["index"]

    _write_json(settings.VIDEO_DASHBOARD_MODELS_JSON_PATH, video_models)
    _write_csv(settings.VIDEO_DASHBOARD_MODELS_CSV_PATH, video_models, {"raw"})

    # ---- 3. Prompt benchmarks (scraped HTML, video pages only) -------------
    prompt_benchmarks, prompt_bundle = _fetch_video_prompt_benchmarks(retrieved_at, model_index)
    prompt_stats = prompt_bundle["stats"]

    # Rows are preserved verbatim; the full page HTML deliberately is not (it is
    # several MB per run and this repo tracks data/**). A sha256 + byte size per
    # page is what actually detects markup drift, and every page stays
    # re-parseable from its URL.
    _write_json(settings.VIDEO_DASHBOARD_RAW_PROMPT_ROWS_PATH, {
        "retrieved_at": retrieved_at,
        "rows": prompt_bundle["raw_rows"],
        "page_fingerprints": prompt_stats["page_fingerprints"],
        "note": (
            "Verbatim parsed rows (not normalized). Full page HTML is deliberately not "
            "committed - see page_fingerprints for a sha256/byte-size drift check."
        ),
    })
    _write_json(settings.VIDEO_DASHBOARD_PROMPT_BENCHMARKS_JSON_PATH, prompt_benchmarks)
    _write_csv(
        settings.VIDEO_DASHBOARD_PROMPT_BENCHMARKS_CSV_PATH,
        prompt_benchmarks,
        {"raw_data"},
        VIDEO_PROMPT_BENCHMARK_CSV_FIELDS,
    )

    # ---- 4. Coverage + data quality ---------------------------------------
    # One pool feeds both reports and, later, the dashboard builder's rows, so the
    # numbers reported by a run and the numbers shown on the page are the same
    # computation.
    pooled = pool_prompt_benchmarks(prompt_benchmarks, VIDEO_MEDIA_TYPE)

    coverage_report = build_video_coverage_report(
        video_models, prompt_benchmarks, prompt_stats, retrieved_at, pooled=pooled,
    )
    quality_report = build_video_data_quality_report(
        catalogue_result.raw_json, video_models, prompt_benchmarks, prompt_stats, pooled=pooled,
    )

    _write_json(settings.VIDEO_DASHBOARD_COVERAGE_REPORT_PATH, coverage_report)
    _write_json(settings.VIDEO_DASHBOARD_DATA_QUALITY_REPORT_PATH, quality_report)

    inventory = coverage_report["model_inventory"]
    logger.info(
        "Video coverage: %d model(s), %d with a resolved rate, %d benchmarked, %d unrated. "
        "Data quality: %d error(s), %d warning(s), %d info item(s).",
        inventory["total_models"], inventory["models_with_resolved_price"],
        inventory["models_with_benchmark_rows"], coverage_report["unrated"]["count"],
        quality_report["error_count"], quality_report["warning_count"], quality_report["info_count"],
    )

    # ---- 5. Snapshot ------------------------------------------------------
    if snapshot:
        _write_video_snapshot(
            run_date,
            catalogue_result.raw_json,
            video_models,
            prompt_benchmarks,
            prompt_bundle["raw_rows"],
            coverage_report,
            quality_report,
        )

    return {
        "retrieved_at": retrieved_at,
        "video_model_count": len(video_models),
        "prompt_benchmark_row_count": len(prompt_benchmarks),
        "pooled_model_count": len(pooled),
        "pool": pooled,
        "coverage_report": coverage_report,
        "data_quality_report": quality_report,
    }


def _fetch_video_prompt_benchmarks(retrieved_at: str, model_index: dict) -> tuple[list[dict], dict]:
    """Scrape the video prompt-benchmark pages.

    Mirrors the media pipeline's page handling (and its guards) but only walks the
    video index. Raises OpenRouterAPIError for a page that cannot be fetched and
    MediaBenchmarkParseError for a page that parses to nothing.
    """
    index_path = settings.MEDIA_BENCHMARK_INDEX_ENDPOINTS[VIDEO_MEDIA_TYPE]

    index_result = fetch_page(index_path)
    if not index_result.ok:
        raise OpenRouterAPIError(
            f"Failed to fetch the video benchmark index page {index_path}: {index_result.error}"
        )

    slugs = discover_prompt_slugs(index_result.text or "", VIDEO_MEDIA_TYPE)
    if len(slugs) < settings.MIN_EXPECTED_VIDEO_PROMPT_COUNT:
        raise MediaBenchmarkParseError(
            f"Only {len(slugs)} video prompt(s) discovered on {index_path}, below the configured "
            f"floor of {settings.MIN_EXPECTED_VIDEO_PROMPT_COUNT}. The index page markup has "
            f"probably changed shape - refusing to write a partial benchmark set."
        )
    logger.info("Discovered %d video prompt benchmark page(s)", len(slugs))

    raw_rows: list[dict] = []
    normalized: list[dict] = []
    fingerprints: dict[str, dict] = {}
    prompt_counts: dict[str, int] = {}
    skipped_pages: list[dict] = []
    conflicts = 0

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
        if not rows and not page_publishes_judged_checks(page_html):
            # A page publishing assets but no judged checks anywhere. Its rows
            # deliberately do NOT enter the dataset: price and performance must
            # come from the same rows, and a page with costs but no checks would
            # push prices into the averages with nothing to pair them with.
            logger.warning(
                "%s publishes no judged checks (no 'N of M checks passed' marker anywhere) - "
                "skipping its %d result-row block(s). It contributes no scores AND no costs.",
                page_path, page_html.count("<li "),
            )
            skipped_pages.append({
                "page": page_path,
                "reason": "no_judged_checks",
                "row_blocks": page_html.count("<li "),
            })
            continue
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
            raw_rows.append({**row, "media_type": VIDEO_MEDIA_TYPE, "prompt_slug": slug,
                             "source_url": page.url})
            normalized.append(normalize_prompt_benchmark_row(
                row,
                media_type=VIDEO_MEDIA_TYPE,
                prompt_slug=slug,
                prompt_name=prompt_name,
                source_url=page.url,
                retrieved_at=retrieved_at,
                model_id=_resolve_benchmark_model_id(row["raw_model_slug"], model_index),
            ))

    if len(normalized) < settings.MIN_EXPECTED_TOTAL_VIDEO_PROMPT_BENCHMARK_ROWS:
        raise MediaBenchmarkParseError(
            f"Only {len(normalized)} video prompt-benchmark row(s) parsed across {len(fingerprints)} "
            f"page(s), below the floor of {settings.MIN_EXPECTED_TOTAL_VIDEO_PROMPT_BENCHMARK_ROWS}. "
            f"Individual prompts legitimately vary in how many models they cover, but a total this "
            f"low means the parsing broke somewhere. Rows per prompt: {prompt_counts}"
        )

    unmatched = sorted({r["raw_model_slug"] for r in normalized if not r["matched"]})
    stats = {
        "pages_fetched": len(fingerprints),
        "pages_skipped": skipped_pages,
        "rows": len(normalized),
        "rows_per_prompt": prompt_counts,
        "distinct_models_in_benchmarks": len({r["raw_model_slug"] for r in normalized}),
        "models_matched_to_inventory": len({r["model_id"] for r in normalized if r["model_id"]}),
        "unmatched_model_slugs": unmatched,
        "conflicting_row_values": conflicts,
        "rows_missing_cost": sum(1 for r in normalized if r.get("cost_usd") is None),
        "rows_missing_generation_time": sum(1 for r in normalized if r.get("generation_seconds") is None),
        "page_fingerprints": fingerprints,
        "benchmark_source": BENCHMARK_SOURCE,
    }
    logger.info(
        "Video prompt benchmarks: %d row(s) across %d page(s); %d model(s) matched to the catalogue, "
        "%d unmatched slug(s), %d conflicting row(s)",
        stats["rows"], stats["pages_fetched"], stats["models_matched_to_inventory"],
        len(unmatched), conflicts,
    )
    return normalized, {"stats": stats, "raw_rows": raw_rows}


def _resolve_benchmark_model_id(raw_slug: str, model_index: dict) -> Optional[str]:
    """Match a benchmark page slug to a catalogue model.

    The slug as-is first (video models publish the dated canonical slug, which the
    index already contains), then the release-date-stripped form.
    """
    matched = normalize.match_benchmark_model_id(raw_slug, model_index)
    if matched:
        return matched
    stripped = strip_release_date_suffix(raw_slug)
    if stripped != raw_slug:
        return normalize.match_benchmark_model_id(stripped, model_index)
    return None


def _write_video_snapshot(
    run_date: str,
    raw_video_payload,
    video_models: list[dict],
    prompt_benchmarks: list[dict],
    prompt_raw_rows: list[dict],
    coverage_report: dict,
    quality_report: dict,
) -> None:
    """Write a video snapshot to <VIDEO_DASHBOARD_SNAPSHOTS_DIR>/<date>[/-N]/.

    The snapshot root is its own top-level directory, a sibling of
    data/snapshots/ (chat) and data/media_snapshots/ (image). That is
    load-bearing, not tidiness: dashboard/refresh.py::snapshot_exists_for()
    decides whether today's chat snapshot already exists by looking for a
    directory directly under data/snapshots/ named `<date>` or `<date>-N`, so a
    snapshot written there by another pipeline would make refresh.py pass
    --no-snapshot to the chat pipeline and silently lose the day's real chat
    snapshot. data/video_snapshots/ cannot match that pattern; the guarantee is
    locked down by tests/test_video_snapshot_paths.py.

    Suffixes roll over like the media pipeline's: the second run of a day writes
    `<date>-2`, never the same directory twice.
    """
    snapshot_dir = settings.VIDEO_DASHBOARD_SNAPSHOTS_DIR / run_date
    suffix = 1
    original = snapshot_dir
    while snapshot_dir.exists():
        suffix += 1
        snapshot_dir = original.with_name(f"{original.name}-{suffix}")
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    _write_json(snapshot_dir / "video_dashboard_models.json", video_models)
    _write_json(snapshot_dir / "video_dashboard_models_raw.json", raw_video_payload)
    _write_json(snapshot_dir / "video_dashboard_prompt_benchmarks.json", prompt_benchmarks)
    _write_json(snapshot_dir / "video_dashboard_prompt_benchmark_rows.json", prompt_raw_rows)
    _write_json(snapshot_dir / "video_dashboard_coverage_report.json", coverage_report)
    _write_json(snapshot_dir / "video_dashboard_data_quality_report.json", quality_report)
    logger.info("Wrote video snapshot to %s", snapshot_dir)
