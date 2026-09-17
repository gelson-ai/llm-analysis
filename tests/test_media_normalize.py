"""Normalization tests for the media (image/video) catalogs.

Offline only, using the curated fixtures in tests/fixtures/. Fixtures are
defined here rather than in conftest.py so no pre-existing test file is touched.

The SKU cases below are the vocabulary families actually observed in live
/api/v1/videos/models responses on 2026-09-17, not invented shapes.
"""
import json
from pathlib import Path

import pytest

from src import normalize, normalize_media

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    with open(FIXTURES_DIR / name) as f:
        return json.load(f)


@pytest.fixture
def sample_image_models_response():
    return _load("media_image_models_response.json")


@pytest.fixture
def sample_raw_image_models(sample_image_models_response):
    return sample_image_models_response["data"]


@pytest.fixture
def sample_video_models_response():
    return _load("media_video_models_response.json")


@pytest.fixture
def sample_raw_video_models(sample_video_models_response):
    return sample_video_models_response["data"]


@pytest.fixture
def sample_image_endpoints_response():
    return _load("media_image_endpoints_response.json")


@pytest.fixture
def sample_design_arena_models():
    return _load("media_models_with_design_arena.json")["data"]


def _model(raw_models, model_id):
    return next(m for m in raw_models if m["id"] == model_id)


# ---------------------------------------------------------------------------
# normalize_media_pricing - unit inference
# ---------------------------------------------------------------------------
def test_pricing_cents_per_second_is_converted_to_usd():
    """Live value: black-forest-labs/flux-video-edit -> {"cents_per_second_output": "3"}"""
    pricing = normalize_media.normalize_media_pricing({"cents_per_second_output": "3"}, "video")

    sku = pricing["skus"][0]
    assert sku["currency"] == "cents"
    assert sku["quantity"] == "second"
    assert sku["unit_family"] == "cents_per_second"
    assert sku["amount"] == 3.0
    assert sku["usd_amount"] == pytest.approx(0.03)  # divided by CENTS_PER_DOLLAR
    assert sku["conversion_applied"] is True
    assert pricing["unit_conversions_applied"] == ["cents_per_second_output"]
    assert pricing["comparable_price"]["usd"] == pytest.approx(0.03)


def test_pricing_usd_per_second_is_passed_through():
    """Live value: alibaba/wan-3.0 -> {"duration_seconds_720p": "0.1"}"""
    pricing = normalize_media.normalize_media_pricing({"duration_seconds_720p": "0.1"}, "video")

    sku = pricing["skus"][0]
    assert sku["currency"] == "usd"
    assert sku["usd_amount"] == pytest.approx(0.1)
    assert sku["conversion_applied"] is False
    assert sku["resolution_variant"] == "720p"


def test_pricing_records_currency_assumption_for_bare_keys():
    """A key with no explicit currency marker is assumed USD - and that
    assumption is recorded, never silent."""
    pricing = normalize_media.normalize_media_pricing({"duration_seconds": "0.08"}, "video")

    assert pricing["skus"][0]["currency_assumed"] is True
    assert pricing["currency_assumptions"] == ["duration_seconds"]


def test_pricing_explicit_cents_key_is_not_reported_as_assumed():
    pricing = normalize_media.normalize_media_pricing({"cents_per_second_output": "3"}, "video")
    assert pricing["skus"][0]["currency_assumed"] is False
    assert pricing["currency_assumptions"] == []


def test_pricing_video_token_family_is_recognised_but_not_a_per_second_rate():
    """Live value: bytedance/seedance-2.0 -> {"video_tokens": "0.000007", ...}"""
    pricing = normalize_media.normalize_media_pricing(
        {"video_tokens": "0.000007", "video_tokens_4k": "0.000004", "video_tokens_without_audio": "0.000007"},
        "video",
    )

    assert {s["quantity"] for s in pricing["skus"]} == {"video_token"}
    assert pricing["has_any_price"] is True
    # Priced, but NOT in a unit comparable with a per-second rate.
    assert pricing["comparable_price"]["usd"] is None
    assert sorted(pricing["unit_families"]) == ["usd_per_video_token"]


def test_pricing_megapixel_second_is_not_mistaken_for_a_plain_second_rate():
    """Live value: black-forest-labs/flux-video-upscale."""
    pricing = normalize_media.normalize_media_pricing(
        {"cents_per_megapixel_second_precise": "7.5", "cents_per_megapixel_second_creative": "10.5"},
        "video",
    )

    sku = pricing["skus"][0]
    assert sku["quantity"] == "megapixel_second"
    assert sku["usd_amount"] == pytest.approx(0.075)
    assert sku["operation"] == "precise"
    assert pricing["comparable_price"]["usd"] is None


def test_pricing_per_generation_minimum_is_flagged_and_excluded():
    """Live value: runway/aleph-2 -> {"cents_per_second_output": "28",
    "minimum_cents_per_generation": "56"}"""
    pricing = normalize_media.normalize_media_pricing(
        {"cents_per_second_output": "28", "minimum_cents_per_generation": "56"}, "video"
    )

    minimum = next(s for s in pricing["skus"] if s["sku"] == "minimum_cents_per_generation")
    assert minimum["is_minimum"] is True
    assert minimum["quantity"] == "generation"
    assert minimum["usd_amount"] == pytest.approx(0.56)

    # The minimum must never be chosen as the headline per-second rate.
    assert pricing["comparable_price"]["basis_sku"] == "cents_per_second_output"
    assert pricing["comparable_price"]["usd"] == pytest.approx(0.28)


def test_pricing_audio_variants_are_captured():
    """Live value: google/veo-3.1."""
    pricing = normalize_media.normalize_media_pricing(
        {
            "duration_seconds_with_audio": "0.40",
            "duration_seconds_without_audio": "0.20",
            "duration_seconds_with_audio_4k": "0.60",
            "duration_seconds_without_audio_4k": "0.40",
        },
        "video",
    )

    by_sku = {s["sku"]: s for s in pricing["skus"]}
    assert by_sku["duration_seconds_with_audio"]["audio_variant"] == "with_audio"
    assert by_sku["duration_seconds_without_audio"]["audio_variant"] == "without_audio"
    assert by_sku["duration_seconds_with_audio_4k"]["resolution_variant"] == "4k"
    # Both audio rows are unqualified by resolution, so both are comparable -
    # the cheaper baseline is chosen, deterministically and visibly.
    assert pricing["comparable_price"]["basis"] == "audio_qualified_rate"
    assert pricing["comparable_price"]["usd"] == pytest.approx(0.20)


def test_pricing_operation_qualified_tiers_are_captured():
    """Live value: alibaba/wan-2.6."""
    pricing = normalize_media.normalize_media_pricing(
        {
            "text_to_video_duration_seconds_720p": "0.08",
            "image_to_video_duration_seconds_720p": "0.10",
        },
        "video",
    )

    operations = {s["operation"] for s in pricing["skus"]}
    assert operations == {"text_to_video", "image_to_video"}
    assert pricing["comparable_price"]["basis_sku"] == "text_to_video_duration_seconds_720p"


def test_pricing_prefers_720p_over_a_cheaper_lower_tier():
    """Ranking on the cheapest published tier would compare a 480p price
    against another model's 720p price, so 720p wins by preference."""
    pricing = normalize_media.normalize_media_pricing(
        {"duration_seconds_480p": "0.05", "duration_seconds_720p": "0.1", "duration_seconds_1080p": "0.2"},
        "video",
    )

    assert pricing["comparable_price"]["usd"] == pytest.approx(0.1)
    assert pricing["comparable_price"]["basis"] == "720p_tier"
    assert pricing["usd_per_second_by_resolution"] == {
        "480p": pytest.approx(0.05),
        "720p": pytest.approx(0.1),
        "1080p": pytest.approx(0.2),
    }


def test_pricing_plain_rate_beats_a_tiered_rate():
    pricing = normalize_media.normalize_media_pricing(
        {"duration_seconds": "0.13", "duration_seconds_720p": "0.08"}, "video"
    )
    assert pricing["comparable_price"]["basis_sku"] == "duration_seconds"
    assert pricing["comparable_price"]["basis"] == "plain_rate"


def test_pricing_by_resolution_map_excludes_audio_and_operation_qualified_rows():
    pricing = normalize_media.normalize_media_pricing(
        {
            "duration_seconds_720p": "0.08",
            "duration_seconds_with_audio_720p": "0.10",
            "text_to_video_duration_seconds_720p": "0.09",
        },
        "video",
    )
    assert pricing["usd_per_second_by_resolution"] == {"720p": pytest.approx(0.08)}


def test_pricing_image_comparable_uses_per_image_dimension():
    pricing = normalize_media.normalize_media_pricing({"reference_images": "0.04"}, "image")
    assert pricing["skus"][0]["quantity"] == "image"
    assert pricing["comparable_price"]["unit"] == "usd_per_image"
    assert pricing["comparable_price"]["usd"] == pytest.approx(0.04)


# ---------------------------------------------------------------------------
# normalize_media_pricing - missing / malformed / hostile input
# ---------------------------------------------------------------------------
def test_pricing_missing_pricing_skus_yields_no_price():
    pricing = normalize_media.normalize_media_pricing(None, "video")

    assert pricing["sku_count"] == 0
    assert pricing["has_any_price"] is False
    assert pricing["comparable_price"] == {"unit": None, "usd": None, "basis": None, "basis_sku": None}


def test_pricing_empty_dict_yields_no_price():
    pricing = normalize_media.normalize_media_pricing({}, "video")
    assert pricing["has_any_price"] is False
    assert pricing["resolved_sku_count"] == 0


def test_pricing_non_dict_yields_no_price_rather_than_raising():
    for bad in [[], "not-a-dict", 42, [{"per-video-second": "0.5"}]]:
        pricing = normalize_media.normalize_media_pricing(bad, "video")
        assert pricing["has_any_price"] is False
        assert pricing["raw_pricing_skus"] == bad  # preserved verbatim


def test_pricing_malformed_values_are_none_and_never_fabricated():
    pricing = normalize_media.normalize_media_pricing(
        {"duration_seconds_720p": "not-a-number", "video_tokens": None, "cents_per_second_output": ""},
        "video",
    )

    assert pricing["has_any_price"] is False
    assert all(sku["usd_amount"] is None for sku in pricing["skus"])
    assert all(sku["is_parseable"] is False for sku in pricing["skus"])
    # Nothing was dropped - every key is still reported as unresolved.
    assert sorted(pricing["unresolved_sku_keys"]) == [
        "cents_per_second_output", "duration_seconds_720p", "video_tokens"
    ]


def test_pricing_partially_malformed_keeps_the_good_value():
    pricing = normalize_media.normalize_media_pricing(
        {"duration_seconds": "0.08", "duration_seconds_720p": "not-a-number"}, "video"
    )

    assert pricing["resolved_sku_count"] == 1
    assert pricing["has_any_price"] is True
    assert pricing["unresolved_sku_keys"] == ["duration_seconds_720p"]
    assert pricing["comparable_price"]["usd"] == pytest.approx(0.08)


def test_pricing_negative_value_is_treated_as_missing_and_flagged():
    pricing = normalize_media.normalize_media_pricing({"duration_seconds": "-1"}, "video")

    sku = pricing["skus"][0]
    assert sku["is_negative"] is True
    assert sku["usd_amount"] is None
    assert sku["amount"] is None
    assert pricing["has_any_price"] is False


def test_pricing_bool_is_not_coerced_to_a_number():
    """True/False are ints in Python; they must not silently become 1.0/0.0."""
    pricing = normalize_media.normalize_media_pricing({"duration_seconds": True}, "video")
    assert pricing["skus"][0]["usd_amount"] is None


def test_pricing_unknown_key_is_reported_not_guessed():
    pricing = normalize_media.normalize_media_pricing({"some_future_pricing_unit": "0.5"}, "video")

    sku = pricing["skus"][0]
    assert sku["is_parseable"] is True
    assert sku["quantity"] == "unknown"
    assert sku["unit_family"] == "unknown"
    assert sku["usd_amount"] is None  # no multiplier guessed for an unknown unit
    assert pricing["has_any_price"] is False
    assert pricing["raw_pricing_skus"] == {"some_future_pricing_unit": "0.5"}


def test_pricing_raw_map_is_preserved_verbatim():
    raw = {"duration_seconds_720p": "0.08", "cents_per_second_output": "3"}
    pricing = normalize_media.normalize_media_pricing(raw, "video")
    assert pricing["raw_pricing_skus"] == raw
    assert pricing["raw_pricing_skus"] is raw


# ---------------------------------------------------------------------------
# normalize_image_endpoint_pricing
# ---------------------------------------------------------------------------
def test_image_endpoint_pricing_per_image(sample_image_endpoints_response):
    pricing = normalize_media.normalize_image_endpoint_pricing(
        sample_image_endpoints_response["recraft/recraft-v4"]
    )

    assert pricing["model_id"] == "recraft/recraft-v4"
    assert pricing["endpoint_count"] == 1
    assert pricing["has_any_price"] is True
    assert pricing["comparable_price"]["unit"] == "usd_per_image"
    assert pricing["comparable_price"]["usd"] == pytest.approx(0.04)
    assert pricing["comparable_price"]["basis"] == "plain_output_image_rate"


def test_image_endpoint_pricing_token_priced_model(sample_image_endpoints_response):
    pricing = normalize_media.normalize_image_endpoint_pricing(
        sample_image_endpoints_response["openai/gpt-image-2"]
    )

    # Falls through image -> megapixel -> token, and the reported unit says so.
    assert pricing["comparable_price"]["unit"] == "usd_per_token"
    assert pricing["comparable_price"]["basis"] == "plain_output_token_rate"
    assert pricing["comparable_price"]["usd"] == pytest.approx(3e-05)
    # Input lines are preserved too, not discarded.
    billables = {line["billable"] for line in pricing["endpoints"][0]["pricing_lines"]}
    assert "input_image" in billables and "output_image" in billables


def test_image_endpoint_pricing_per_megapixel(sample_image_endpoints_response):
    pricing = normalize_media.normalize_image_endpoint_pricing(
        sample_image_endpoints_response["black-forest-labs/flux.2-max"]
    )

    assert pricing["comparable_price"]["unit"] == "usd_per_megapixel"
    assert pricing["usd_per_megapixel_by_variant"] == {"default": pytest.approx(0.07)}


def test_image_endpoint_pricing_tiered_variants_pick_cheapest_tier():
    payload = {
        "id": "example-vendor/tiered-image",
        "endpoints": [
            {
                "provider_name": "Example",
                "provider_slug": "example",
                "pricing": [
                    {"billable": "output_image", "unit": "image", "cost_usd": 0.19, "variant": "4k"},
                    {"billable": "output_image", "unit": "image", "cost_usd": 0.12, "variant": "2k"},
                ],
            }
        ],
    }
    pricing = normalize_media.normalize_image_endpoint_pricing(payload)

    assert pricing["comparable_price"]["basis"] == "cheapest_variant_tier"
    assert pricing["comparable_price"]["usd"] == pytest.approx(0.12)
    assert pricing["usd_per_image_by_variant"] == {"4k": pytest.approx(0.19), "2k": pytest.approx(0.12)}


def test_image_endpoint_pricing_multiple_providers_all_preserved():
    payload = {
        "id": "example-vendor/multi-provider",
        "endpoints": [
            {"provider_name": "A", "provider_slug": "a",
             "pricing": [{"billable": "output_image", "unit": "image", "cost_usd": 0.04}]},
            {"provider_name": "B", "provider_slug": "b",
             "pricing": [{"billable": "output_image", "unit": "image", "cost_usd": 0.06}]},
            {"provider_name": "C", "provider_slug": "c", "pricing": []},
        ],
    }
    pricing = normalize_media.normalize_image_endpoint_pricing(payload)

    assert pricing["endpoint_count"] == 3
    assert [e["provider_slug"] for e in pricing["endpoints"]] == ["a", "b", "c"]
    assert pricing["providers_without_pricing"] == ["c"]
    assert pricing["comparable_price"]["usd"] == pytest.approx(0.04)  # first-seen wins


def test_image_endpoint_pricing_empty_or_missing_payload():
    for payload in [{}, None, {"id": "x"}, {"id": "x", "endpoints": "nope"}, []]:
        pricing = normalize_media.normalize_image_endpoint_pricing(payload)
        assert pricing["has_any_price"] is False
        assert pricing["comparable_price"]["usd"] is None
        assert pricing["comparable_price"]["unit"] is None


def test_image_endpoint_pricing_malformed_cost_counts_unparseable():
    payload = {
        "id": "example-vendor/bad-cost",
        "endpoints": [{"provider_slug": "a", "pricing": [
            {"billable": "output_image", "unit": "image", "cost_usd": "not-a-number"},
            {"billable": "output_image", "unit": "image", "cost_usd": 0.05},
        ]}],
    }
    pricing = normalize_media.normalize_image_endpoint_pricing(payload)

    assert pricing["unparseable_pricing_line_count"] == 1
    assert pricing["resolved_pricing_line_count"] == 1
    assert pricing["comparable_price"]["usd"] == pytest.approx(0.05)


def test_image_endpoint_pricing_negative_cost_is_not_reported_as_a_price():
    payload = {
        "id": "example-vendor/negative",
        "endpoints": [{"provider_slug": "a", "pricing": [
            {"billable": "output_image", "unit": "image", "cost_usd": -5},
        ]}],
    }
    pricing = normalize_media.normalize_image_endpoint_pricing(payload)

    assert pricing["has_any_price"] is False
    assert pricing["endpoints"][0]["pricing_lines"][0]["is_negative"] is True
    assert pricing["providers_without_pricing"] == ["a"]


# ---------------------------------------------------------------------------
# normalize_media_model
# ---------------------------------------------------------------------------
def test_normalize_image_model_preserves_raw_and_extracts_core_fields(sample_raw_image_models):
    raw = _model(sample_raw_image_models, "openai/gpt-image-2.5-sunburst")
    normalized = normalize_media.normalize_media_model(raw, "image", "2026-09-17T00:00:00Z")

    assert normalized["model_id"] == "openai/gpt-image-2.5-sunburst"
    assert normalized["provider"] == "openai"
    assert normalized["model_type"] == "image"
    assert normalized["model_name"] == raw["name"]
    assert normalized["architecture"]["output_modalities"] == ["image"]
    assert normalized["architecture"]["synthesized"] is False
    assert normalized["supports_streaming"] is True
    assert normalized["endpoints_url"].endswith("/endpoints")
    assert normalized["raw"] == raw  # raw object preserved verbatim
    assert normalized["raw_source"] == "openrouter:/api/v1/images/models"
    assert normalized["retrieved_at"] == "2026-09-17T00:00:00Z"


def test_normalize_video_model_synthesizes_missing_architecture(sample_raw_video_models):
    """Video models carry no `architecture` key at all (confirmed live), so the
    output modality is derived from the catalog and the record says so."""
    raw = _model(sample_raw_video_models, "alibaba/wan-3.0")
    assert "architecture" not in raw

    normalized = normalize_media.normalize_media_model(raw, "video", "2026-09-17T00:00:00Z")

    assert normalized["architecture"]["synthesized"] is True
    assert normalized["output_modalities"] == ["video"]
    assert normalized["input_modalities"] is None
    assert normalized["raw_source"] == "openrouter:/api/v1/videos/models"


def test_normalize_media_model_parses_pricing_skus_from_the_raw_model(sample_raw_video_models):
    """The model record must price itself from its own `pricing_skus` - a caller
    that forgets to pass `pricing=` must not silently produce an unpriced model."""
    raw = _model(sample_raw_video_models, "alibaba/wan-3.0")
    normalized = normalize_media.normalize_media_model(raw, "video", "2026-09-17T00:00:00Z")

    assert normalized["has_valid_pricing"] is True
    assert normalized["comparable_price"] == pytest.approx(0.1)
    assert normalized["pricing_unit"] == "usd_per_second"
    assert normalized["pricing"]["raw_pricing_skus"] == raw["pricing_skus"]


def test_normalize_image_model_has_no_pricing_but_is_not_dropped(sample_raw_image_models):
    raw = _model(sample_raw_image_models, "openai/gpt-image-2.5-sunburst")
    normalized = normalize_media.normalize_media_model(raw, "image", "2026-09-17T00:00:00Z")

    assert normalized["pricing"]["sku_count"] == 0
    assert normalized["has_any_price"] is False
    assert normalized["has_valid_pricing"] is False
    assert normalized["comparable_price"] is None
    assert normalized["model_id"] == "openai/gpt-image-2.5-sunburst"  # present, not dropped


def test_normalize_image_model_uses_endpoint_pricing_when_supplied(sample_raw_image_models, sample_image_endpoints_response):
    raw = _model(sample_raw_image_models, "meta/muse-image")
    raw = dict(raw, id="recraft/recraft-v4")  # reuse the per-image fixture's pricing
    endpoint_pricing = normalize_media.normalize_image_endpoint_pricing(
        sample_image_endpoints_response["recraft/recraft-v4"]
    )

    normalized = normalize_media.normalize_media_model(
        raw, "image", "2026-09-17T00:00:00Z", endpoint_pricing=endpoint_pricing
    )

    assert normalized["has_valid_pricing"] is True
    assert normalized["comparable_price"] == pytest.approx(0.04)
    assert normalized["pricing_unit"] == "usd_per_image"
    assert normalized["pricing"]["has_any_price"] is False  # list endpoint still had none


def test_normalize_media_model_accepts_both_supported_sizes_spellings():
    base = {"id": "example-vendor/x", "name": "X", "created": 1}
    with_plural = normalize_media.normalize_media_model(
        dict(base, supported_sizes=["1280x720"]), "video", "2026-09-17T00:00:00Z"
    )
    with_singular = normalize_media.normalize_media_model(
        dict(base, supported_size=["1280x720"]), "video", "2026-09-17T00:00:00Z"
    )
    assert with_plural["supported_sizes"] == ["1280x720"]
    assert with_singular["supported_sizes"] == ["1280x720"]


def test_normalize_media_model_missing_id_is_not_dropped():
    normalized = normalize_media.normalize_media_model({"name": "No ID Media"}, "video", "2026-09-17T00:00:00Z")

    assert normalized["model_id"] is None
    assert normalized["provider"] is None
    assert normalized["model_name"] == "No ID Media"


def test_normalize_media_model_provider_convention_matches_chat_pipeline(sample_raw_video_models):
    raw = _model(sample_raw_video_models, "black-forest-labs/flux-video-edit")
    normalized = normalize_media.normalize_media_model(raw, "video", "2026-09-17T00:00:00Z")
    assert normalized["provider"] == "black-forest-labs"


def test_normalize_media_model_video_without_comparable_rate_is_priced_but_not_rankable(sample_raw_video_models):
    raw = _model(sample_raw_video_models, "bytedance/seedance-2.0")
    normalized = normalize_media.normalize_media_model(raw, "video", "2026-09-17T00:00:00Z")

    assert normalized["has_any_price"] is True
    assert normalized["has_valid_pricing"] is False
    assert normalized["comparable_price"] is None
    assert normalized["pricing_unit"] is None


def test_normalize_media_model_keeps_supported_durations_and_frame_images(sample_raw_video_models):
    raw = _model(sample_raw_video_models, "google/veo-3.1")
    normalized = normalize_media.normalize_media_model(raw, "video", "2026-09-17T00:00:00Z")

    assert normalized["supported_durations"] == [4, 6, 8]
    assert normalized["supported_frame_images"] == ["first_frame", "last_frame"]
    assert normalized["generate_audio"] is True


# ---------------------------------------------------------------------------
# Design Arena extraction
# ---------------------------------------------------------------------------
def test_design_arena_extraction_returns_only_design_arena_rows(sample_design_arena_models):
    entries = normalize_media.extract_design_arena_from_media_models(sample_design_arena_models)

    assert entries, "the synthetic fixture is supposed to contain design_arena rows"
    assert {e["benchmark_source"] for e in entries} == {"Design Arena"}
    assert all(e["benchmark_name"].startswith("Design Arena: ") for e in entries)


def test_design_arena_extraction_never_mixes_in_artificial_analysis(sample_design_arena_models):
    """The fixture's second model carries BOTH blocks. Artificial Analysis scores
    are chat-model metrics and must not leak into the media benchmark output."""
    entries = normalize_media.extract_design_arena_from_media_models(sample_design_arena_models)

    assert not any(e["benchmark_source"] == "Artificial Analysis" for e in entries)
    assert not any("AA " in e["benchmark_name"] for e in entries)
    assert "example-vendor/chat-indexed-media-model" in {e["model_ref"] for e in entries}


def test_design_arena_extraction_skips_models_without_benchmarks(sample_design_arena_models):
    entries = normalize_media.extract_design_arena_from_media_models(sample_design_arena_models)
    assert "example-vendor/no-benchmarks-media-model" not in {e["model_ref"] for e in entries}


def test_design_arena_extraction_returns_empty_for_real_media_catalogs(sample_raw_image_models, sample_raw_video_models):
    """Neither live media endpoint publishes a benchmarks block today, so this
    must degrade to an empty list rather than raise or invent rows."""
    assert normalize_media.extract_design_arena_from_media_models(sample_raw_image_models) == []
    assert normalize_media.extract_design_arena_from_media_models(sample_raw_video_models) == []
    assert normalize_media.extract_design_arena_from_media_models([]) == []


def test_design_arena_rows_normalize_into_benchmark_records(sample_design_arena_models):
    entries = normalize_media.extract_design_arena_from_media_models(sample_design_arena_models)
    normalized_models = [
        normalize_media.normalize_media_model(m, "image", "2026-09-17T00:00:00Z")
        for m in sample_design_arena_models
    ]
    index = normalize_media.build_media_model_index(normalized_models)

    records = [
        normalize.normalize_benchmark_record(
            model_ref=entry["model_ref"],
            benchmark_name=entry["benchmark_name"],
            raw_record=entry["raw_record"],
            model_index=index["index"],
            retrieved_at="2026-09-17T00:00:00Z",
            benchmark_source=entry["benchmark_source"],
            source_platform=entry["source_platform"],
        )
        for entry in entries
    ]

    assert records
    assert all(r["matched"] is True for r in records)
    assert all(r["model_id"] for r in records)
    assert {r["benchmark_source"] for r in records} == {"Design Arena"}
    logo = next(r for r in records if r["benchmark_name"].endswith("/logo"))
    assert logo["score"] == pytest.approx(44.7)  # win_rate
    assert logo["raw_data"]["elo"] == 1090


# ---------------------------------------------------------------------------
# Cross-checks against the chat pipeline
# ---------------------------------------------------------------------------
def test_media_index_uses_canonical_slug_like_the_chat_index(sample_raw_video_models):
    normalized_models = [
        normalize_media.normalize_media_model(m, "video", "2026-09-17T00:00:00Z")
        for m in sample_raw_video_models
    ]
    index = normalize_media.build_media_model_index(normalized_models)

    raw = _model(sample_raw_video_models, "black-forest-labs/flux-video-edit")
    assert raw["canonical_slug"] in index["index"]
    assert normalize.match_benchmark_model_id(raw["canonical_slug"], index["index"]) == raw["id"]


def test_media_normalizer_does_not_use_the_token_pricing_path():
    """Media pricing must never be forced through normalize_pricing(): the
    outputs are different shapes and normalize_pricing would return nonsense
    (no prompt/completion keys at all)."""
    raw = {"cents_per_second_output": "3"}
    assert normalize.normalize_pricing(raw)["has_valid_pricing"] is False
    assert normalize_media.normalize_media_pricing(raw, "video")["has_any_price"] is True
