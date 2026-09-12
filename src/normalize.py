"""
Normalization layer (Sections 6, 7, 11, 14).

Design principles enforced here:
  - never drop a model for lacking pricing or benchmarks (Section 4)
  - preserve the raw object alongside the normalized one
  - never fabricate benchmark_version / timestamps - use None when absent
  - keep a mapping layer that preserves the original OpenRouter ID
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from config import settings

MTOK = settings.MTOK


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------
def _to_float_or_none(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f


def normalize_pricing(raw_pricing: Optional[dict]) -> dict:
    """Convert OpenRouter's per-token string prices into floats, and derive
    per-MTok prices. Keeps every other pricing field (web_search,
    input_cache_read, input_cache_write, overrides, ...) untouched under
    `other_pricing_fields` so nothing is lost (Section 13 - caching).

    OpenRouter uses "-1" as a sentinel for its own auto-router / dynamic
    meta-models (e.g. openrouter/auto, openrouter/fusion) whose real price
    is determined per-request rather than fixed. We surface that explicitly
    via `is_dynamic_pricing` rather than treating -1 as a literal (invalid)
    negative price.
    """
    raw_pricing = raw_pricing or {}

    input_price_per_token = _to_float_or_none(raw_pricing.get("prompt"))
    output_price_per_token = _to_float_or_none(raw_pricing.get("completion"))

    is_dynamic_pricing = (input_price_per_token == -1) or (output_price_per_token == -1)
    if is_dynamic_pricing:
        input_price_per_token = None
        output_price_per_token = None
    else:
        # Any other negative value is genuinely invalid (not a known sentinel)
        # - treat as missing rather than silently computing a negative cost.
        if input_price_per_token is not None and input_price_per_token < 0:
            input_price_per_token = None
        if output_price_per_token is not None and output_price_per_token < 0:
            output_price_per_token = None

    known_keys = {"prompt", "completion"}
    other_fields = {k: v for k, v in raw_pricing.items() if k not in known_keys}

    # Fields that look like cache pricing - surfaced explicitly per Section 13
    # so a later pass can decide whether to fold them into the cost formula.
    cache_related = {
        k: v for k, v in other_fields.items()
        if "cache" in k.lower()
    }

    return {
        "input_price_per_token": input_price_per_token,
        "output_price_per_token": output_price_per_token,
        "input_price_per_mtok": input_price_per_token * MTOK if input_price_per_token is not None else None,
        "output_price_per_mtok": output_price_per_token * MTOK if output_price_per_token is not None else None,
        "has_valid_pricing": input_price_per_token is not None and output_price_per_token is not None,
        "is_dynamic_pricing": is_dynamic_pricing,
        "cache_related_fields": cache_related,
        "other_pricing_fields": other_fields,
    }


def blended_price_per_mtok(
    input_price_per_mtok: Optional[float],
    output_price_per_mtok: Optional[float],
    input_weight: float = settings.INPUT_TOKEN_WEIGHT,
    output_weight: float = settings.OUTPUT_TOKEN_WEIGHT,
) -> Optional[float]:
    """(input_weight * input + output_weight * output) / (input_weight + output_weight).

    Returns None if either price is missing - never fabricates a cost for a
    model with unknown pricing.
    """
    if input_price_per_mtok is None or output_price_per_mtok is None:
        return None
    total_weight = input_weight + output_weight
    return (input_weight * input_price_per_mtok + output_weight * output_price_per_mtok) / total_weight


# ---------------------------------------------------------------------------
# Model normalization
# ---------------------------------------------------------------------------
def normalize_model(raw_model: dict, retrieved_at: str) -> dict:
    """Build a normalized model record. Never raises on missing optional
    fields; only requires that the model have an `id`."""
    model_id = raw_model.get("id")
    if not model_id:
        # Caller (data-quality pass) is responsible for flagging this; we
        # still return a record so the model isn't silently dropped.
        model_id = None

    pricing = normalize_pricing(raw_model.get("pricing"))
    provider = model_id.split("/", 1)[0] if model_id and "/" in model_id else None

    return {
        "model_id": model_id,
        "canonical_slug": raw_model.get("canonical_slug"),
        "model_name": raw_model.get("name"),
        "provider": provider,
        "input_price_per_token": pricing["input_price_per_token"],
        "output_price_per_token": pricing["output_price_per_token"],
        "input_price_per_mtok": pricing["input_price_per_mtok"],
        "output_price_per_mtok": pricing["output_price_per_mtok"],
        "has_valid_pricing": pricing["has_valid_pricing"],
        "is_dynamic_pricing": pricing["is_dynamic_pricing"],
        "cache_related_pricing_fields": pricing["cache_related_fields"],
        "other_pricing_fields": pricing["other_pricing_fields"],
        "context_length": raw_model.get("context_length"),
        "architecture": raw_model.get("architecture"),
        "top_provider": raw_model.get("top_provider"),
        "reasoning": raw_model.get("reasoning"),
        "created": raw_model.get("created"),
        "raw_source": "openrouter:/api/v1/models",
        "retrieved_at": retrieved_at,
        "raw": raw_model,
    }


# ---------------------------------------------------------------------------
# Model ID matching / mapping layer (Section 14)
# ---------------------------------------------------------------------------
_VERSION_SUFFIX_RE = re.compile(r"(:free|:beta|:extended|:nitro|:online)$", re.IGNORECASE)


def canonical_candidates(model_id: str) -> list[str]:
    """Return, in priority order, the identifiers a benchmark record might
    plausibly use to reference this model: the exact OpenRouter id first
    (never discard this), then looser fallbacks."""
    if not model_id:
        return []
    candidates = [model_id]

    base = _VERSION_SUFFIX_RE.sub("", model_id)
    if base != model_id:
        candidates.append(base)

    if "/" in base:
        candidates.append(base.split("/", 1)[1])  # slug without vendor prefix

    return candidates


def build_model_index(normalized_models: list[dict]) -> dict:
    """Build a lookup index from every plausible identifier variant back to
    the canonical OpenRouter model_id, so benchmark matching (Section 14)
    never needs to guess twice. Detects and reports collisions rather than
    silently letting the last write win."""
    index: dict[str, str] = {}
    collisions: dict[str, list[str]] = {}

    for m in normalized_models:
        model_id = m.get("model_id")
        if not model_id:
            continue
        identifiers = set(canonical_candidates(model_id))
        if m.get("canonical_slug"):
            identifiers.add(m["canonical_slug"])

        for ident in identifiers:
            if ident in index and index[ident] != model_id:
                collisions.setdefault(ident, [index[ident]]).append(model_id)
                continue  # keep first-seen mapping; flagged in collisions
            index[ident] = model_id

    return {"index": index, "collisions": collisions}


def match_benchmark_model_id(raw_benchmark_model_ref: str, model_index: dict) -> Optional[str]:
    """Resolve a benchmark record's model reference to a canonical
    OpenRouter model_id using the index built by build_model_index.
    Returns None (never guesses) if nothing matches."""
    if not raw_benchmark_model_ref:
        return None
    if raw_benchmark_model_ref in model_index:
        return model_index[raw_benchmark_model_ref]
    for candidate in canonical_candidates(raw_benchmark_model_ref):
        if candidate in model_index:
            return model_index[candidate]
    return None


# ---------------------------------------------------------------------------
# Benchmark normalization (Section 7)
# ---------------------------------------------------------------------------
def normalize_score(raw_score: Any) -> tuple[Optional[float], str]:
    """Best-effort classification of a raw score's representation.

    Returns (numeric_value, score_unit) where score_unit is one of:
    "fraction_0_1", "percentage_0_100", "raw", "unknown".
    Does not rescale the value - callers must not compare scores with
    different units without explicit conversion.
    """
    value = _to_float_or_none(raw_score)
    if value is None:
        return None, "unknown"
    if 0.0 <= value <= 1.0:
        return value, "fraction_0_1"
    if 1.0 < value <= 100.0:
        return value, "percentage_0_100"
    return value, "raw"


def normalize_benchmark_record(
    model_ref: str,
    benchmark_name: str,
    raw_record: dict,
    *,
    model_index: dict,
    retrieved_at: str,
    benchmark_source: str = "Artificial Analysis",
    source_platform: str = "OpenRouter",
) -> dict:
    matched_model_id = match_benchmark_model_id(model_ref, model_index)
    score, score_unit = normalize_score(raw_record.get("score") if isinstance(raw_record, dict) else raw_record)

    return {
        "model_id": matched_model_id,
        "raw_model_ref": model_ref,
        "matched": matched_model_id is not None,
        "benchmark_name": benchmark_name,
        "score": score,
        "score_unit": score_unit,
        "benchmark_source": benchmark_source,
        "source_platform": source_platform,
        "retrieved_at": retrieved_at,
        "benchmark_version": (raw_record.get("version") if isinstance(raw_record, dict) else None) or None,
        "benchmark_timestamp": (raw_record.get("timestamp") if isinstance(raw_record, dict) else None) or None,
        "raw_data": raw_record,
    }


# ---------------------------------------------------------------------------
# Real benchmark extraction, confirmed against a live OpenRouter response
# (2026-09-08). See README "The benchmark question" for the full writeup.
#
# Confirmed shape of GET /api/v1/models -> data[i]["benchmarks"]:
#   {
#     "artificial_analysis": {
#       "intelligence_index": <float>,   # blended AA composite - NOT the same
#                                         # thing as GPQA Diamond / IFBench /
#                                         # AA-Omniscience individually
#       "coding_index": <float>,         # coding - out of scope for this product
#       "agentic_index": <float>         # agentic tool-use - not one of our 3 categories
#     },
#     "design_arena": [
#       {"arena": "models"|"agents", "category": "<slug>", "elo": <int>,
#        "rank": <int>, "win_rate": <float 0-100>},
#       ...
#     ]
#   }
#
# Neither block is GPQA Diamond, IFBench, or AA-Omniscience. OpenRouter's
# public model-listing endpoint does not expose per-benchmark breakdowns -
# only these blended/derived scores. design_arena is a DIFFERENT benchmark
# provider (Design Arena, not Artificial Analysis) bundled in the same
# response - we tag it with its own benchmark_source so it is never mixed
# with Artificial Analysis scores.
# ---------------------------------------------------------------------------
AA_INDEX_FIELDS = {
    "intelligence_index": "AA Intelligence Index",
    "coding_index": "AA Coding Index",
    "agentic_index": "AA Agentic Index",
}


def extract_benchmark_entries_from_raw_models(raw_models: list[dict]) -> list[dict]:
    """Flatten every model's `benchmarks` block into (model_ref, benchmark_name,
    raw_record, benchmark_source, source_platform) entries ready for
    normalize_benchmark_record. Returns [] entries for models with no
    benchmarks block at all - never fabricates one."""
    entries: list[dict] = []

    for model in raw_models:
        model_id = model.get("id")
        benchmarks = model.get("benchmarks")
        if not isinstance(benchmarks, dict):
            continue

        aa = benchmarks.get("artificial_analysis")
        if isinstance(aa, dict):
            for field_key, benchmark_name in AA_INDEX_FIELDS.items():
                if field_key in aa and aa[field_key] is not None:
                    entries.append({
                        "model_ref": model_id,
                        "benchmark_name": benchmark_name,
                        "raw_record": {"score": aa[field_key], "field": field_key},
                        "benchmark_source": "Artificial Analysis",
                        "source_platform": "OpenRouter",
                    })

        design_arena = benchmarks.get("design_arena")
        if isinstance(design_arena, list):
            for row in design_arena:
                if not isinstance(row, dict):
                    continue
                arena = row.get("arena", "unknown-arena")
                category = row.get("category", "unknown-category")
                entries.append({
                    "model_ref": model_id,
                    "benchmark_name": f"Design Arena: {arena}/{category}",
                    "raw_record": {"score": row.get("win_rate"), **row},
                    "benchmark_source": "Design Arena",
                    "source_platform": "OpenRouter",
                })

    return entries
