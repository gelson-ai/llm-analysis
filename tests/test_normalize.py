from src.normalize import (
    extract_benchmark_entries_from_raw_models,
    normalize_model,
    normalize_pricing,
    normalize_score,
)


def test_normalize_model_valid_pricing(sample_raw_models):
    qwen = next(m for m in sample_raw_models if m["id"] == "qwen/qwen3-8b")
    normalized = normalize_model(qwen, "2026-09-07T00:00:00Z")

    assert normalized["model_id"] == "qwen/qwen3-8b"
    assert normalized["provider"] == "qwen"
    assert normalized["has_valid_pricing"] is True
    assert normalized["input_price_per_mtok"] == 0.00000006 * 1_000_000
    assert normalized["output_price_per_mtok"] == 0.00000012 * 1_000_000
    assert normalized["context_length"] == 40960
    assert normalized["raw"] == qwen  # raw object preserved


def test_normalize_model_missing_pricing(sample_raw_models):
    m = next(x for x in sample_raw_models if x["id"] == "some-vendor/no-pricing-model")
    normalized = normalize_model(m, "2026-09-07T00:00:00Z")

    assert normalized["has_valid_pricing"] is False
    assert normalized["input_price_per_token"] is None
    assert normalized["output_price_per_token"] is None
    # model is NOT discarded just because pricing is missing/malformed
    assert normalized["model_id"] == "some-vendor/no-pricing-model"


def test_normalize_model_missing_context_length(sample_raw_models):
    m = next(x for x in sample_raw_models if x["id"] == "some-vendor/missing-context-length")
    normalized = normalize_model(m, "2026-09-07T00:00:00Z")

    assert normalized["context_length"] is None
    assert normalized["has_valid_pricing"] is True  # pricing was fine


def test_normalize_model_missing_id_is_not_dropped():
    raw = {"name": "No ID Model", "pricing": {"prompt": "0.000001", "completion": "0.000002"}}
    normalized = normalize_model(raw, "2026-09-07T00:00:00Z")
    assert normalized["model_id"] is None
    assert normalized["model_name"] == "No ID Model"


def test_normalize_pricing_malformed_prompt_price():
    result = normalize_pricing({"prompt": "not-a-number", "completion": "0.000002"})
    assert result["input_price_per_token"] is None
    assert result["output_price_per_token"] == 0.000002
    assert result["has_valid_pricing"] is False


def test_normalize_pricing_preserves_cache_fields():
    result = normalize_pricing({
        "prompt": "0.000003", "completion": "0.000015",
        "input_cache_read": "0.0000003", "input_cache_write": "0.00000375",
    })
    assert "input_cache_read" in result["cache_related_fields"]
    assert "input_cache_write" in result["cache_related_fields"]


def test_normalize_score_fraction():
    value, unit = normalize_score(0.87)
    assert value == 0.87
    assert unit == "fraction_0_1"


def test_normalize_score_percentage():
    value, unit = normalize_score(87.0)
    assert value == 87.0
    assert unit == "percentage_0_100"


def test_normalize_score_unknown_format():
    value, unit = normalize_score("not-a-score")
    assert value is None
    assert unit == "unknown"


def test_normalize_score_raw_scale():
    value, unit = normalize_score(250)
    assert value == 250
    assert unit == "raw"


def test_dynamic_pricing_sentinel_treated_as_missing(sample_raw_models):
    router = next(m for m in sample_raw_models if m["id"] == "openrouter/auto")
    normalized = normalize_model(router, "2026-09-07T00:00:00Z")

    assert normalized["is_dynamic_pricing"] is True
    assert normalized["has_valid_pricing"] is False
    assert normalized["input_price_per_token"] is None
    assert normalized["output_price_per_token"] is None


def test_extract_benchmark_entries_from_raw_models(sample_raw_models):
    entries = extract_benchmark_entries_from_raw_models(sample_raw_models)
    qwen_entries = [e for e in entries if e["model_ref"] == "qwen/qwen3-8b"]

    names = {e["benchmark_name"] for e in qwen_entries}
    assert "AA Intelligence Index" in names
    assert "AA Coding Index" in names
    assert "AA Agentic Index" in names
    assert any(n.startswith("Design Arena: ") for n in names)

    intel = next(e for e in qwen_entries if e["benchmark_name"] == "AA Intelligence Index")
    assert intel["raw_record"]["score"] == 46.9
    assert intel["benchmark_source"] == "Artificial Analysis"

    design = next(e for e in qwen_entries if e["benchmark_name"].startswith("Design Arena: "))
    assert design["benchmark_source"] == "Design Arena"
    assert design["raw_record"]["score"] == 41.2  # win_rate


def test_extract_benchmark_entries_skips_models_without_benchmarks(sample_raw_models):
    entries = extract_benchmark_entries_from_raw_models(sample_raw_models)
    refs = {e["model_ref"] for e in entries}
    assert "anthropic/claude-3.5-sonnet" not in refs
