"""
Coverage and data-quality reporting for the media (image/video) catalogs.

Deliberately NOT the same shape as src/coverage.py's reports. Those are built
around the chat product's questions - priority Artificial Analysis benchmarks,
"models with enough benchmark data to rank", reasoning/knowledge/instruction
coverage - none of which apply to image and video generation models. Forcing
this data into that shape would produce a report full of zeros that means
nothing.

What these reports do share with src/coverage.py is the *spirit*:
  - coverage is reported as separate facts ("model exists" / "has a price" /
    "has a benchmark") rather than collapsed into one number;
  - the data-quality report fails loudly - anything unparsed, missing or
    unexpected is listed with a severity, never silently dropped;
  - every unit assumption this pipeline makes is stated in the output.
"""
from __future__ import annotations

from collections import Counter
from typing import Optional

from config import settings
from src.normalize_media import DESIGN_ARENA_BENCHMARK_SOURCE


def _price_range(values: list[float]) -> Optional[dict]:
    if not values:
        return None
    return {"min": min(values), "max": max(values), "count": len(values)}


def _summarize_type(
    normalized_models: list[dict],
    benchmark_model_ids: set[str],
) -> dict:
    total = len(normalized_models)
    priced = [m for m in normalized_models if m.get("has_valid_pricing")]
    with_any_price = [m for m in normalized_models if m.get("has_any_price")]
    prices = [m["comparable_price"] for m in priced if m.get("comparable_price") is not None]
    with_arena = sum(1 for m in normalized_models if m.get("model_id") in benchmark_model_ids)

    unit_families = sorted({
        family
        for m in normalized_models
        for family in ((m.get("pricing") or {}).get("unit_families") or [])
    })

    return {
        "total_models": total,
        "models_with_resolved_price": len(priced),
        "models_without_resolved_price": total - len(priced),
        "price_resolution_rate": round(100 * len(priced) / total, 2) if total else 0.0,
        # "has a price" and "has a price we can compare across models" are two
        # different facts - a model priced only in video tokens falls in the
        # first bucket but not the second, and that must stay visible.
        "models_with_any_price_data": len(with_any_price),
        "models_with_price_data_but_no_comparable_rate": len(with_any_price) - len(priced),
        "models_with_design_arena": with_arena,
        "models_without_design_arena": total - with_arena,
        "unit_families_present": unit_families,
        "comparable_price_units_used": sorted({m.get("pricing_unit") for m in priced if m.get("pricing_unit")}),
        "comparable_price_basis_used": sorted({m.get("comparable_price_basis") for m in priced if m.get("comparable_price_basis")}),
        "comparable_price_range": _price_range(prices),
        "provider_count": len({m.get("provider") for m in normalized_models if m.get("provider")}),
    }


def build_media_coverage_report(
    image_models: list[dict],
    video_models: list[dict],
    media_benchmarks: list[dict],
    endpoint_fetch_stats: Optional[dict] = None,
    retrieved_at: Optional[str] = None,
    filtered_image_raw_models: Optional[list[dict]] = None,
    dropped_duplicate_benchmarks: Optional[list[dict]] = None,
    prompt_benchmark_stats: Optional[dict] = None,
) -> dict:
    """Basic coverage stats for the media catalogs.

    Fields are intentionally limited to what this data can actually support:
    how many models of each type, how many have a resolved price, how many have
    Design Arena scores.
    """
    endpoint_fetch_stats = endpoint_fetch_stats or {}
    dropped_duplicate_benchmarks = dropped_duplicate_benchmarks or []
    arena_model_ids = {
        b["model_id"] for b in media_benchmarks
        if b.get("model_id") and b.get("benchmark_source") == DESIGN_ARENA_BENCHMARK_SOURCE
    }

    image_summary = _summarize_type(image_models, arena_model_ids)
    video_summary = _summarize_type(video_models, arena_model_ids)

    per_benchmark: dict[tuple, set] = {}
    for b in media_benchmarks:
        key = (b.get("benchmark_name"), b.get("source_endpoint"))
        per_benchmark.setdefault(key, set()).add(b.get("model_id"))

    total_models = len(image_models) + len(video_models)
    benchmark_coverage = [
        {
            "benchmark_name": name,
            "benchmark_source": DESIGN_ARENA_BENCHMARK_SOURCE,
            "source_endpoint": source_endpoint,
            "number_of_models": len(model_ids),
            "percentage_of_media_inventory": round(100 * len(model_ids) / total_models, 2) if total_models else 0.0,
        }
        for (name, source_endpoint), model_ids in per_benchmark.items()
    ]
    benchmark_coverage.sort(key=lambda row: (-row["number_of_models"], row["benchmark_name"]))

    # Cross-check the two image sources against each other. They are expected to
    # overlap heavily but not necessarily match exactly (the dedicated catalog
    # and the generic filtered view are not guaranteed to be the same snapshot),
    # so any divergence is surfaced rather than assumed away.
    inventory_cross_check = None
    if filtered_image_raw_models is not None:
        # `image_models` are NORMALIZED records (key `model_id`); the filtered
        # list is still raw API objects (key `id`). Do not mix the two.
        dedicated_ids = {m.get("model_id") for m in image_models if m.get("model_id")}
        filtered_ids = {m.get("id") for m in filtered_image_raw_models if isinstance(m, dict) and m.get("id")}
        inventory_cross_check = {
            "dedicated_image_catalog": len(dedicated_ids),
            "filtered_generic_catalog": len(filtered_ids),
            "in_both": len(dedicated_ids & filtered_ids),
            "only_in_dedicated_catalog": sorted(dedicated_ids - filtered_ids),
            "only_in_filtered_catalog": sorted(filtered_ids - dedicated_ids),
        }

    all_models = image_models + video_models
    cents_converted = sorted({
        sku
        for m in all_models
        for sku in ((m.get("pricing") or {}).get("unit_conversions_applied") or [])
    })
    currency_assumed = sorted({
        sku
        for m in all_models
        for sku in ((m.get("pricing") or {}).get("currency_assumptions") or [])
    })

    return {
        "retrieved_at": retrieved_at,
        "model_inventory": {
            "image": image_summary,
            "video": video_summary,
            "total_models": total_models,
            "total_with_resolved_price": (
                image_summary["models_with_resolved_price"] + video_summary["models_with_resolved_price"]
            ),
            "total_with_design_arena": sum(1 for m in all_models if m.get("model_id") in arena_model_ids),
        },
        "image_endpoint_pricing": endpoint_fetch_stats or {
            "enabled": False,
            "requested": 0,
            "succeeded": 0,
            "failed": 0,
            "failed_model_ids": [],
            "models_with_any_price_from_endpoints": 0,
        },
        "image_inventory_cross_check": inventory_cross_check,
        "benchmark_coverage": benchmark_coverage,
        "prompt_benchmark_coverage": _prompt_benchmark_coverage(prompt_benchmark_stats),
        "benchmark_records_by_source_endpoint": _benchmark_counts_by_endpoint(media_benchmarks),
        "benchmark_duplicates_suppressed": len(dropped_duplicate_benchmarks),
        "pricing_unit_assumptions": {
            "cents_converted_to_usd_sku_keys": cents_converted,
            "bare_keys_assumed_usd_sku_keys": currency_assumed,
            "note": (
                "OpenRouter mixes denominations inside a single pricing_skus object "
                "(e.g. 'cents_per_second_output': '3' next to 'duration_seconds_720p': '0.08'). "
                "Keys starting with 'cents_' are divided by 100; keys with no explicit currency "
                "marker are treated as USD. Both lists above name exactly which keys each rule "
                "was applied to, so no converted figure is ever silently mixed with a USD one."
            ),
        },
        "benchmark_note": (
            "Design Arena scores are read from the generic catalog filtered by output modality "
            "(/api/v1/models?output_modalities=image), because the dedicated /api/v1/images/models "
            "endpoint publishes no `benchmarks` block at all. Video models publish no Design Arena "
            "scores on any endpoint. `source_endpoint` on each benchmark record says where its row "
            "came from."
        ),
    }


def _benchmark_counts_by_endpoint(media_benchmarks: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for b in media_benchmarks:
        key = b.get("source_endpoint") or "unknown"
        counts[key] = counts.get(key, 0) + 1
    return counts


def _prompt_benchmark_coverage(stats: Optional[dict]) -> dict:
    """Summarise the scraped prompt-benchmark set.

    Reported as separate facts rather than one headline number: how many pages
    were read, how many (model, prompt) rows came back, how many of those rows
    belong to models we actually hold in the catalog, and how many measurements
    came from the accessibility label rather than the visible text - the last
    one being the early warning that the markup contract is slipping.
    """
    if not stats:
        return {"enabled": False}

    checks_sources = stats.get("checks_source_counts") or {}
    skipped_pages = stats.get("pages_skipped") or []
    return {
        "enabled": True,
        "source": "https://openrouter.ai/benchmarks/media/{images,videos}",
        "benchmark_source": "OpenRouter Media Benchmarks",
        "pages_fetched": stats.get("pages_fetched"),
        # Fetched but NOT usable: a page publishing generated assets with no
        # judged checks anywhere. Kept separate from prompt_page_count so the
        # dataset size is never overstated.
        "pages_skipped": skipped_pages,
        "pages_skipped_count": len(skipped_pages),
        "prompt_page_count": len(stats.get("rows_per_prompt") or {}),
        "rows": stats.get("rows"),
        "rows_per_prompt": stats.get("rows_per_prompt"),
        "distinct_models_in_benchmarks": stats.get("distinct_models_in_benchmarks"),
        "models_matched_to_inventory": stats.get("models_matched_to_inventory"),
        "models_unmatched": len(stats.get("unmatched_model_slugs") or []),
        "unmatched_model_slugs": stats.get("unmatched_model_slugs"),
        "rows_missing_cost": stats.get("rows_missing_cost"),
        "rows_missing_generation_time": stats.get("rows_missing_generation_time"),
        "correctness_extraction_sources": checks_sources,
        "conflicting_row_values": stats.get("conflicting_row_values"),
        "note": (
            "Correctness is the count of judged checks passed for ONE prompt, not a global "
            "score - pass rates are only comparable within the same prompt_slug. Cost and "
            "generation_seconds are the observed values for that single generation."
        ),
    }


def build_media_data_quality_report(
    raw_models_by_type: dict[str, list[dict]],
    normalized_models_by_type: dict[str, list[dict]],
    media_benchmarks: list[dict],
    endpoint_fetch_stats: Optional[dict] = None,
    dropped_duplicate_benchmarks: Optional[list[dict]] = None,
    benchmark_sources_scanned: Optional[list[str]] = None,
    prompt_benchmark_stats: Optional[dict] = None,
) -> dict:
    """Same "fail loudly, never silently drop" contract as
    src.coverage.build_data_quality_report: the same envelope
    ({issue_count, error_count, warning_count, info_count, issues}) and the same
    per-issue shape ({severity, issue, count, detail}).
    """
    issues: list[dict] = []
    endpoint_fetch_stats = endpoint_fetch_stats or {}
    dropped_duplicate_benchmarks = dropped_duplicate_benchmarks or []
    benchmark_sources_scanned = benchmark_sources_scanned or []
    prompt_benchmark_stats = prompt_benchmark_stats or {}
    all_models = [m for models in normalized_models_by_type.values() for m in models]

    for model_type, raw_models in raw_models_by_type.items():
        # Missing / duplicate IDs
        missing_ids = [i for i, m in enumerate(raw_models) if isinstance(m, dict) and not m.get("id")]
        if missing_ids:
            issues.append({
                "severity": "error", "issue": "missing_model_id", "count": len(missing_ids),
                "detail": f"{len(missing_ids)} raw {model_type} model object(s) had no 'id' field "
                          f"(indices: {missing_ids[:20]})",
            })

        id_counts = Counter(m.get("id") for m in raw_models if isinstance(m, dict) and m.get("id"))
        dupes = {mid: count for mid, count in id_counts.items() if count > 1}
        if dupes:
            issues.append({
                "severity": "error", "issue": "duplicate_model_id", "count": len(dupes),
                "detail": f"Duplicate {model_type} model IDs found: {dupes}",
            })

    # ---- Pricing issues ----------------------------------------------------
    no_pricing_skus = [m["model_id"] for m in all_models if ((m.get("pricing") or {}).get("sku_count") or 0) == 0]
    if no_pricing_skus:
        issues.append({
            "severity": "info", "issue": "no_pricing_skus", "count": len(no_pricing_skus),
            "detail": f"{len(no_pricing_skus)} media model(s) publish no pricing_skus at all "
                      f"(expected for every image model - image pricing lives in the per-model "
                      f"endpoints record instead; sample: {no_pricing_skus[:10]})",
        })

    unparseable = [
        (m["model_id"], sku["sku"], sku["raw_value"])
        for m in all_models
        for sku in ((m.get("pricing") or {}).get("skus") or [])
        if not sku.get("is_parseable")
    ]
    if unparseable:
        issues.append({
            "severity": "warning", "issue": "unparseable_sku_value", "count": len(unparseable),
            "detail": f"{len(unparseable)} pricing_skus value(s) were not numeric and were kept as "
                      f"None rather than guessed: {unparseable[:10]}",
        })

    unrecognized = [
        sku["sku"]
        for m in all_models
        for sku in ((m.get("pricing") or {}).get("skus") or [])
        if sku.get("is_parseable") and sku.get("unit_family") == "unknown"
    ]
    if unrecognized:
        issues.append({
            "severity": "warning", "issue": "unrecognized_sku_unit", "count": len(unrecognized),
            "detail": f"pricing_skus key(s) parsed to a number but matched no known unit "
                      f"vocabulary, so no USD figure was derived: {sorted(set(unrecognized))}. "
                      f"Extend the vocabulary in src/normalize_media.py after inspecting these.",
        })

    negative = [
        (m["model_id"], sku["sku"], sku["raw_value"])
        for m in all_models
        for sku in ((m.get("pricing") or {}).get("skus") or [])
        if sku.get("is_negative")
    ]
    negative += [
        (m["model_id"], f"{line.get('billable')}/{line.get('unit')}", line.get("raw_cost_usd"))
        for m in all_models
        for endpoint in ((m.get("endpoint_pricing") or {}).get("endpoints") or [])
        for line in endpoint.get("pricing_lines", [])
        if line.get("is_negative")
    ]
    if negative:
        issues.append({
            "severity": "error", "issue": "negative_price", "count": len(negative),
            "detail": f"Genuinely negative price(s) found and treated as missing, not as a literal "
                      f"negative cost: {negative[:10]}",
        })

    unparseable_image_lines = [
        m["model_id"] for m in all_models
        if ((m.get("endpoint_pricing") or {}).get("unparseable_pricing_line_count") or 0) > 0
    ]
    if unparseable_image_lines:
        issues.append({
            "severity": "warning", "issue": "unparseable_image_pricing_line", "count": len(unparseable_image_lines),
            "detail": f"{len(unparseable_image_lines)} image model(s) had a pricing line with a "
                      f"non-numeric cost_usd: {unparseable_image_lines[:10]}",
        })

    no_comparable = [
        m["model_id"] for m in all_models
        if m.get("has_any_price") and not m.get("has_valid_pricing")
    ]
    if no_comparable:
        issues.append({
            "severity": "info", "issue": "price_data_without_comparable_rate", "count": len(no_comparable),
            "detail": f"{len(no_comparable)} media model(s) publish a price but not in a unit that can be "
                      f"compared across models (e.g. priced only per video token, per megapixel-second, or "
                      f"with a per-generation minimum): {no_comparable}. They are NOT unpriced - see "
                      f"'has_any_price' vs 'has_valid_pricing' on each record.",
        })

    # ---- Endpoint fan-out --------------------------------------------------
    if endpoint_fetch_stats.get("enabled"):
        failed = endpoint_fetch_stats.get("failed") or 0
        if failed:
            issues.append({
                "severity": "warning", "issue": "image_endpoint_fetch_failed", "count": failed,
                "detail": f"{failed} of {endpoint_fetch_stats.get('requested')} per-model image endpoints "
                          f"record(s) could not be fetched, so those models have no image price this run: "
                          f"{(endpoint_fetch_stats.get('failed_model_ids') or [])[:10]}",
            })
        missing_price = endpoint_fetch_stats.get("models_with_no_price_from_endpoints") or []
        if missing_price:
            issues.append({
                "severity": "info", "issue": "image_endpoint_pricing_missing", "count": len(missing_price),
                "detail": f"{len(missing_price)} image model(s) returned an endpoints record with no usable "
                          f"price: {missing_price[:10]}",
            })

    # ---- Benchmark issues --------------------------------------------------
    missing_benchmark_ref = [b for b in media_benchmarks if not b.get("raw_model_ref")]
    if missing_benchmark_ref:
        issues.append({
            "severity": "error", "issue": "missing_benchmark_model_id", "count": len(missing_benchmark_ref),
            "detail": f"{len(missing_benchmark_ref)} media benchmark record(s) had no model reference at all",
        })

    unmatched = [b for b in media_benchmarks if b.get("raw_model_ref") and not b.get("matched")]
    if unmatched:
        issues.append({
            "severity": "warning", "issue": "unmatched_benchmark_model_id", "count": len(unmatched),
            "detail": f"{len(unmatched)} media benchmark record(s) referenced a model that could not be "
                      f"matched: {sorted({b['raw_model_ref'] for b in unmatched})[:10]}",
        })

    bench_dupes = {
        key: count for key, count in Counter(
            (b.get("model_id"), b.get("benchmark_name"), b.get("benchmark_version"))
            for b in media_benchmarks if b.get("model_id")
        ).items() if count > 1
    }
    if bench_dupes:
        issues.append({
            "severity": "info", "issue": "duplicate_benchmark_record", "count": len(bench_dupes),
            "detail": f"{len(bench_dupes)} (model, benchmark, version) combination(s) appear more than once",
        })

    if dropped_duplicate_benchmarks:
        issues.append({
            "severity": "info", "issue": "duplicate_benchmark_suppressed", "count": len(dropped_duplicate_benchmarks),
            "detail": f"{len(dropped_duplicate_benchmarks)} Design Arena row(s) appeared in more than one scanned "
                      f"source and were suppressed (first occurrence kept, keyed on model + benchmark name): "
                      f"{[(d.get('model_ref'), d.get('benchmark_name'), d.get('source_endpoint')) for d in dropped_duplicate_benchmarks[:5]]}",
        })

    # ---- Prompt benchmarks (scraped) ---------------------------------------
    if prompt_benchmark_stats.get("rows") is not None:
        issues.append({
            "severity": "info", "issue": "prompt_benchmarks_collected",
            "count": prompt_benchmark_stats["rows"],
            "detail": f"{prompt_benchmark_stats['rows']} prompt-benchmark row(s) parsed from "
                      f"{prompt_benchmark_stats.get('pages_fetched')} page(s); "
                      f"{prompt_benchmark_stats.get('models_matched_to_inventory')} distinct model(s) matched "
                      f"to the catalog. Correctness values are per-prompt judged pass counts, not a global score.",
        })

        # A skipped page is a real gap in the dataset, so it is reported rather
        # than only logged: its models contributed no scores AND no costs.
        skipped_pages = prompt_benchmark_stats.get("pages_skipped") or []
        for skipped in skipped_pages:
            issues.append({
                "severity": "info", "issue": "prompt_benchmark_page_skipped",
                "count": 1,
                "detail": f"{skipped.get('page')} was skipped: {skipped.get('reason')}. "
                          f"The page publishes {skipped.get('row_blocks')} result-row block(s) of generated "
                          f"assets but no judged pass count anywhere, so there is nothing to score. Its rows "
                          f"are excluded from both the pass-rate and the cost averages, which keeps this "
                          f"dataset's rule that price and performance come from the same rows.",
            })

        unmatched_slugs = prompt_benchmark_stats.get("unmatched_model_slugs") or []
        if unmatched_slugs:
            issues.append({
                "severity": "warning", "issue": "unmatched_prompt_benchmark_model", "count": len(unmatched_slugs),
                "detail": f"{len(unmatched_slugs)} benchmark page model slug(s) could not be matched to a model "
                          f"in either media catalog: {unmatched_slugs[:10]}. Their rows were still written, with "
                          f"model_id=null.",
            })

        fallback_rows = (prompt_benchmark_stats.get("checks_source_counts") or {}).get("visible_text", 0)
        if fallback_rows:
            issues.append({
                "severity": "warning", "issue": "prompt_benchmark_aria_label_fallback", "count": fallback_rows,
                "detail": f"{fallback_rows} row(s) fell back to the visible 'N/M' text because the "
                          f"aria-label='N of M checks passed' attribute was absent. The accessibility markup "
                          f"has probably changed - verify the parsed pass counts against the live page.",
            })

        conflicts = prompt_benchmark_stats.get("conflicting_row_values") or 0
        if conflicts:
            issues.append({
                "severity": "warning", "issue": "prompt_benchmark_conflicting_row_values", "count": conflicts,
                "detail": f"{conflicts} row(s) had two rendered copies that disagreed on their values. The "
                          f"higher pass count was kept; inspect the raw rows before trusting these figures.",
            })

    # ---- Expected absences / documented assumptions ------------------------
    design_arena_rows = [b for b in media_benchmarks if b.get("benchmark_source") == DESIGN_ARENA_BENCHMARK_SOURCE]
    if design_arena_rows:
        by_endpoint = {}
        for row in design_arena_rows:
            key = row.get("source_endpoint") or "unknown"
            by_endpoint[key] = by_endpoint.get(key, 0) + 1
        issues.append({
            "severity": "info", "issue": "design_arena_collected", "count": len(design_arena_rows),
            "detail": f"{len(design_arena_rows)} Design Arena row(s) collected for "
                      f"{len({b.get('model_id') for b in design_arena_rows})} media model(s), by source: {by_endpoint}. "
                      f"Rows come from the generic catalog filtered by output modality, not from the dedicated "
                      f"image/video endpoints.",
        })
    else:
        issues.append({
            "severity": "info", "issue": "design_arena_absent", "count": 0,
            "detail": f"No design_arena rows were found in any of the {len(benchmark_sources_scanned)} scanned "
                      f"source(s): {benchmark_sources_scanned}. The filtered generic catalog "
                      f"({settings.IMAGE_MODELS_FILTER_ENDPOINT}) is known to publish design_arena for a subset of "
                      f"image models, so 0 rows here means OpenRouter has stopped publishing it or every model "
                      f"lost its rankings - worth verifying rather than assuming this absence is expected.",
        })

    synthesized = [m["model_id"] for m in all_models if (m.get("architecture") or {}).get("synthesized")]
    if synthesized:
        issues.append({
            "severity": "info", "issue": "architecture_synthesized", "count": len(synthesized),
            "detail": f"{len(synthesized)} media model(s) had no `architecture` block in the API response "
                      f"(expected for video models), so output modality was derived from the catalog they "
                      f"came from and `architecture.synthesized` is set on those records.",
        })

    cents_keys = sorted({
        sku for m in all_models for sku in ((m.get("pricing") or {}).get("unit_conversions_applied") or [])
    })
    if cents_keys:
        issues.append({
            "severity": "info", "issue": "unit_conversion_applied", "count": len(cents_keys),
            "detail": f"{len(cents_keys)} cent-denominated SKU key(s) were converted to USD by dividing by "
                      f"{100}: {cents_keys}",
        })

    assumed_keys = sorted({
        sku for m in all_models for sku in ((m.get("pricing") or {}).get("currency_assumptions") or [])
    })
    if assumed_keys:
        issues.append({
            "severity": "info", "issue": "currency_assumed_usd", "count": len(assumed_keys),
            "detail": f"{len(assumed_keys)} SKU key(s) carry no explicit currency marker and were assumed "
                      f"to be USD (all bare keys observed live are USD): {assumed_keys}",
        })

    return {
        "issue_count": len(issues),
        "error_count": sum(1 for i in issues if i.get("severity") == "error"),
        "warning_count": sum(1 for i in issues if i.get("severity") == "warning"),
        "info_count": sum(1 for i in issues if i.get("severity") == "info"),
        "issues": issues,
    }
