from src.normalize import (
    build_model_index,
    canonical_candidates,
    match_benchmark_model_id,
    normalize_model,
)


def _index(sample_raw_models):
    normalized = [normalize_model(m, "2026-09-07T00:00:00Z") for m in sample_raw_models]
    return build_model_index(normalized)


def test_exact_openrouter_model_id_match(sample_raw_models):
    idx = _index(sample_raw_models)
    matched = match_benchmark_model_id("qwen/qwen3-8b", idx["index"])
    assert matched == "qwen/qwen3-8b"


def test_model_permaslug_match(sample_raw_models):
    idx = _index(sample_raw_models)
    matched = match_benchmark_model_id("anthropic/claude-3.5-sonnet-20241022", idx["index"])
    assert matched == "anthropic/claude-3.5-sonnet"


def test_base_slug_fallback_strips_free_suffix():
    candidates = canonical_candidates("qwen/qwen3-8b:free")
    assert "qwen/qwen3-8b:free" in candidates
    assert "qwen/qwen3-8b" in candidates


def test_unmatched_benchmark_model_returns_none(sample_raw_models):
    idx = _index(sample_raw_models)
    matched = match_benchmark_model_id("totally/unknown-model-xyz", idx["index"])
    assert matched is None


def test_solar_pro_4_is_indexed_by_exact_id(sample_raw_models):
    idx = _index(sample_raw_models)
    assert match_benchmark_model_id("upstage/solar-pro-4", idx["index"]) == "upstage/solar-pro-4"
