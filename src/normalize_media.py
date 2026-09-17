"""
Normalization for OpenRouter's image- and video-generation model catalogs
(GET /api/v1/images/models and GET /api/v1/videos/models).

Why this is a separate module from src/normalize.py
---------------------------------------------------
The chat catalog's pricing is token based: `pricing.prompt` / `pricing.completion`,
per-token strings that src.normalize.normalize_pricing() converts to per-MTok
figures. Neither media catalog works that way:

  - Video models carry a `pricing_skus` map whose values are strings in MIXED
    units and vocabularies. Confirmed live (2026-09-17), keys include
    `duration_seconds_720p` (USD per second), `cents_per_second_output` (CENTS
    per second), `video_tokens` (USD per video token),
    `cents_per_megapixel_second_precise`, `reference_images`,
    `minimum_cents_per_generation`, and operation/audio-qualified variants such
    as `text_to_video_duration_seconds_480p` and
    `duration_seconds_without_audio_4k`. The published docs example
    (`per-video-second`) does NOT occur in live data, so this module never
    assumes a fixed key list.
  - Image models carry NO pricing at all on the list endpoint. Their cost lives
    only in the per-model endpoints record, as
    `endpoints[].pricing[] = {billable, unit, cost_usd, variant?}`.

Folding that into normalize.py would mean rewriting a well-tested, load-bearing
token-pricing path to satisfy two small side catalogs. Instead this module
reuses normalize.py's *contract* (never drop a model, never fabricate a price,
preserve the raw object) without touching its code.

Design principles enforced here (same as src/normalize.py):
  - never drop a model for lacking pricing
  - preserve the full raw object alongside the normalized one
  - never fabricate a price: absent/unparseable -> None
  - every unit inference is recorded explicitly rather than applied silently
"""
from __future__ import annotations

import re
from typing import Any, Optional

from config import settings
from src import normalize

IMAGE_MODEL_TYPE = "image"
VIDEO_MODEL_TYPE = "video"

RAW_SOURCE_BY_MODEL_TYPE = {
    IMAGE_MODEL_TYPE: "openrouter:/api/v1/images/models",
    VIDEO_MODEL_TYPE: "openrouter:/api/v1/videos/models",
}

DESIGN_ARENA_BENCHMARK_SOURCE = "Design Arena"

# ---------------------------------------------------------------------------
# Unit inference
#
# The vocabulary below was derived from the keys actually present in live
# /api/v1/videos/models responses on 2026-09-17. Anything that does not match
# is reported as "unknown" with usd_amount=None - it is never guessed at.
# ---------------------------------------------------------------------------
_QUANTITY_MEGAPIXEL_SECOND = "megapixel_second"
_QUANTITY_SECOND = "second"
_QUANTITY_VIDEO_TOKEN = "video_token"
_QUANTITY_IMAGE = "image"
_QUANTITY_GENERATION = "generation"
_QUANTITY_UNKNOWN = "unknown"

_RESOLUTION_RE = re.compile(r"(?:^|_)(512|1k|2k|4k|8k|\d{3,4}p)(?=_|$)")

# Longest/most specific first - "megapixel_second" contains "second", and
# "minimum_cents_per_generation" must not be read as a per-second rate.
_QUANTITY_MATCHERS = (
    ("megapixel_second", _QUANTITY_MEGAPIXEL_SECOND),
    ("generation", _QUANTITY_GENERATION),
    ("token", _QUANTITY_VIDEO_TOKEN),
    ("image", _QUANTITY_IMAGE),
    ("second", _QUANTITY_SECOND),
)

_OPERATION_MATCHERS = (
    ("video_continuation", "video_continuation"),
    ("text_to_video", "text_to_video"),
    ("image_to_video", "image_to_video"),
    ("image_input", "image_input"),
    ("precise", "precise"),
    ("creative", "creative"),
    ("reference", "reference_image"),
)

# Billable line units seen (and documented) on the per-model image endpoints
# record. `unit` there is an independent, explicit field, so no inference is
# needed for image endpoint pricing - only for pricing_skus keys.
_IMAGE_ENDPOINT_UNIT_TO_FAMILY = {
    "image": "usd_per_image",
    "megapixel": "usd_per_megapixel",
    "token": "usd_per_token",
}


def _parse_amount(value: Any) -> Optional[float]:
    """Parse a raw price-ish value into a float, or None if it is not numeric.

    Accepts both the string form used by `pricing_skus` ("0.08") and the
    numeric form used by the per-model endpoints pricing (`cost_usd`). Bools are
    rejected (True/False are ints in Python and would silently become 1.0/0.0).
    Negative values ARE returned so the caller can flag them as invalid rather
    than losing the fact that they existed.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _infer_currency(key: str) -> tuple[str, bool]:
    """Return (currency, assumed) for a pricing_skus key.

    `assumed` is True when the key carries no explicit currency marker and we
    fell back to treating it as USD (all bare keys observed live are USD). That
    fallback is recorded on the parsed SKU and listed in the media
    data-quality report - it is never applied silently.
    """
    lowered = key.lower()
    if lowered.startswith(settings.CENT_DENOMINATED_SKU_PREFIX) or "cents_per" in lowered:
        return "cents", False
    if lowered.startswith("usd_") or "_usd_" in lowered:
        return "usd", False
    return "usd", True


def _infer_quantity(key: str) -> str:
    lowered = key.lower()
    for needle, quantity in _QUANTITY_MATCHERS:
        if needle in lowered:
            return quantity
    return _QUANTITY_UNKNOWN


def _infer_operation(key: str) -> Optional[str]:
    lowered = key.lower()
    for needle, operation in _OPERATION_MATCHERS:
        if needle in lowered:
            return operation
    return None


def _infer_resolution_variant(key: str) -> Optional[str]:
    match = _RESOLUTION_RE.search(key.lower())
    return match.group(1) if match else None


def _infer_audio_variant(key: str) -> Optional[str]:
    lowered = key.lower()
    if "without_audio" in lowered:
        return "without_audio"
    if "with_audio" in lowered:
        return "with_audio"
    return None


def _unit_family(currency: str, quantity: str) -> str:
    if quantity == _QUANTITY_UNKNOWN:
        return "unknown"
    return f"{currency}_per_{quantity}"


def _parse_sku(key: str, raw_value: Any) -> dict:
    """Turn one `pricing_skus` entry into a structured, self-describing record.

    Everything inferred (currency, quantity, the cents->USD conversion) is
    recorded on the record so a reader never has to re-derive it, and so the
    data-quality report can list exactly which assumptions were applied.
    """
    amount = _parse_amount(raw_value)
    currency, currency_assumed = _infer_currency(key)
    quantity = _infer_quantity(key)
    is_negative = amount is not None and amount < 0

    multiplier = (1.0 / settings.CENTS_PER_DOLLAR) if currency == "cents" else 1.0
    conversion_applied = currency == "cents" and amount is not None

    # A recognised currency is not enough to put a USD figure on a value: we
    # also have to know what it is *per*. For an unrecognised unit, "0.5" could
    # mean anything, so no usd_amount is derived - the raw number is still
    # preserved, and the quality report names the key so the vocabulary can be
    # extended deliberately rather than guessed at now.
    usd_amount: Optional[float] = None
    if amount is not None and not is_negative and quantity != _QUANTITY_UNKNOWN:
        usd_amount = amount * multiplier

    return {
        "sku": key,
        "raw_value": raw_value,
        "amount": None if is_negative else amount,
        "currency": currency if amount is not None else None,
        "quantity": quantity if amount is not None else _QUANTITY_UNKNOWN,
        "unit_family": _unit_family(currency, quantity) if (amount is not None and usd_amount is not None) else "unknown",
        "usd_amount": usd_amount,
        "conversion_applied": conversion_applied and usd_amount is not None,
        "currency_assumed": currency_assumed and amount is not None and usd_amount is not None,
        "resolution_variant": _infer_resolution_variant(key),
        "audio_variant": _infer_audio_variant(key),
        "operation": _infer_operation(key),
        "is_minimum": "minimum" in key.lower(),
        "is_negative": is_negative,
        "is_parseable": amount is not None,
    }


# ---------------------------------------------------------------------------
# Comparable-rate selection
#
# A "comparable" price is the single number a caller can rank models on without
# mixing units: USD per second for video, USD per image for image. Picking it
# requires a deterministic, documented preference order - recorded in `basis`
# so the choice is always visible, never implied.
# ---------------------------------------------------------------------------
def _preference_rank(sku: dict) -> Optional[tuple]:
    """Sort key for choosing a representative rate. Lower is better.

    0. no resolution / audio / operation modifier (the plain, headline rate)
    1. audio-qualified general rate (no resolution / operation modifier)
    2. the 720p tier with no operation modifier (the common default tier)
    3. the 720p tier with an operation modifier (e.g. text_to_video_..._720p)
    4. anything else - cheapest first

    The 720p tier is deliberately preferred over a cheaper 480p tier (tier 4):
    ranking on the lowest published tier would compare a 480p price against a
    720p price on another model, which is not like-for-like. Tiers 2/3 make the
    comparison basis consistent across models; the winner is always recorded in
    `basis_sku` so the choice is visible.
    """
    if sku.get("usd_amount") is None:
        return None
    has_resolution = sku.get("resolution_variant") is not None
    has_audio = sku.get("audio_variant") is not None
    has_operation = sku.get("operation") is not None
    is_minimum = bool(sku.get("is_minimum"))

    if not has_resolution and not has_audio and not has_operation and not is_minimum:
        tier = 0
    elif not has_resolution and not has_operation and not is_minimum:
        tier = 1
    elif sku.get("resolution_variant") == "720p" and not has_operation and not is_minimum:
        tier = 2
    elif sku.get("resolution_variant") == "720p" and not is_minimum:
        tier = 3
    else:
        tier = 4

    return (tier, sku["usd_amount"], sku["sku"])


def _basis_label(sku: dict) -> str:
    tier = _preference_rank(sku)[0]
    return {
        0: "plain_rate",
        1: "audio_qualified_rate",
        2: "720p_tier",
        3: "720p_tier_with_operation",
        4: "lowest_published_tier",
    }.get(tier, "unknown_basis")


def _select_comparable(skus: list[dict], quantity: str, unit: str) -> dict:
    """Pick the representative rate for a given quantity dimension."""
    candidates = [
        s for s in skus
        if s.get("quantity") == quantity
        and s.get("usd_amount") is not None
        and not s.get("is_minimum")
    ]
    ranked = sorted(
        (s for s in candidates if _preference_rank(s) is not None),
        key=_preference_rank,
    )
    if not ranked:
        # Report no unit at all rather than the unit we were hoping to find:
        # a caller that sees `unit: "usd_per_second"` would otherwise reasonably
        # conclude the model is priced per second, when in fact it has no
        # comparable rate and a different source may still supply one.
        return {"unit": None, "usd": None, "basis": None, "basis_sku": None}
    winner = ranked[0]
    return {
        "unit": unit,
        "usd": winner["usd_amount"],
        "basis": _basis_label(winner),
        "basis_sku": winner["sku"],
    }


def _usd_rate_map(skus: list[dict], quantity: str, *, exclude_modified: bool) -> dict:
    """Map resolution variant (or "default") -> USD rate, first seen wins.

    When `exclude_modified` is set, only SKUs with no audio/operation modifier
    are considered, so the resulting map is a clean like-for-like comparison
    across resolution tiers.
    """
    rates: dict[str, float] = {}
    for sku in skus:
        if sku.get("quantity") != quantity or sku.get("usd_amount") is None:
            continue
        if sku.get("is_minimum"):
            continue
        if exclude_modified and (sku.get("audio_variant") or sku.get("operation")):
            continue
        key = sku.get("resolution_variant") or "default"
        rates.setdefault(key, sku["usd_amount"])
    return rates


# ---------------------------------------------------------------------------
# pricing_skus
# ---------------------------------------------------------------------------
def normalize_media_pricing(raw_pricing_skus: Any, model_type: Optional[str] = None) -> dict:
    """Parse a `pricing_skus` map into structured rates.

    Never fabricates a price: a missing, empty, non-dict or non-numeric SKU
    yields `None` (same philosophy as normalize_pricing() in src/normalize.py).
    The raw map is preserved verbatim under `raw_pricing_skus` so nothing is
    lost even if this parser does not recognise a future key.

    `model_type` ("image"/"video"/None) only affects which dimension is treated
    as the headline comparable rate (USD per second for video, USD per image for
    image). It defaults to None so the function is still usable standalone.
    """
    raw_skus = raw_pricing_skus if isinstance(raw_pricing_skus, dict) else {}

    skus = [_parse_sku(key, value) for key, value in raw_skus.items()]

    resolved = [s for s in skus if s["usd_amount"] is not None]
    unresolved_keys = [s["sku"] for s in skus if s["usd_amount"] is None]

    if model_type == IMAGE_MODEL_TYPE:
        comparable = _select_comparable(skus, _QUANTITY_IMAGE, "usd_per_image")
    else:
        # Video (and unknown) models are priced per second of output.
        comparable = _select_comparable(skus, _QUANTITY_SECOND, "usd_per_second")

    unit_families = sorted({s["unit_family"] for s in skus if s["usd_amount"] is not None})

    return {
        "skus": skus,
        "raw_pricing_skus": raw_pricing_skus,
        "sku_count": len(skus),
        "resolved_sku_count": len(resolved),
        "unresolved_sku_keys": unresolved_keys,
        "has_any_price": bool(resolved),
        "comparable_price": comparable,
        # Only unmodified per-second rates, so a resolution-tier comparison is
        # like-for-like (no audio/operation-qualified rows mixed in).
        "usd_per_second_by_resolution": _usd_rate_map(skus, _QUANTITY_SECOND, exclude_modified=True),
        "usd_per_image_by_resolution": _usd_rate_map(skus, _QUANTITY_IMAGE, exclude_modified=True),
        "unit_families": unit_families,
        "unit_conversions_applied": sorted({s["sku"] for s in skus if s["conversion_applied"]}),
        "currency_assumptions": sorted({s["sku"] for s in skus if s["currency_assumed"]}),
    }


# ---------------------------------------------------------------------------
# Per-model image endpoints record - the only place image pricing is published.
#
# Confirmed live shape (GET /api/v1/images/models/{id}/endpoints):
#   {"id": "<model id>",
#    "endpoints": [{"provider_name": ..., "provider_slug": ..., "provider_tag": ...,
#                   "supported_parameters": {...}, "allowed_passthrough_parameters": [...],
#                   "supports_streaming": bool,
#                   "pricing": [{"billable": "output_image", "unit": "image",
#                                "cost_usd": 0.05, "variant": "2k"}]}]}
# ---------------------------------------------------------------------------
def _normalize_endpoint_pricing_line(raw_line: Any) -> dict:
    amount = _parse_amount(raw_line.get("cost_usd")) if isinstance(raw_line, dict) else None
    is_negative = amount is not None and amount < 0
    unit = raw_line.get("unit") if isinstance(raw_line, dict) else None
    return {
        "billable": raw_line.get("billable") if isinstance(raw_line, dict) else None,
        "unit": unit,
        "unit_family": _IMAGE_ENDPOINT_UNIT_TO_FAMILY.get(unit, "unknown") if unit else "unknown",
        "variant": raw_line.get("variant") if isinstance(raw_line, dict) else None,
        "raw_cost_usd": raw_line.get("cost_usd") if isinstance(raw_line, dict) else None,
        "usd_amount": None if (amount is None or is_negative) else amount,
        "is_negative": is_negative,
        "is_parseable": amount is not None,
    }


def _endpoint_rate_map(lines: list[dict], billable: str, unit: str) -> dict:
    rates: dict[str, float] = {}
    for line in lines:
        if line.get("billable") != billable or line.get("unit") != unit:
            continue
        if line.get("usd_amount") is None:
            continue
        rates.setdefault(line.get("variant") or "default", line["usd_amount"])
    return rates


def _select_endpoint_comparable(lines: list[dict]) -> dict:
    """Representative image price: a plain per-image output rate, else a
    resolution-tiered per-image rate (cheapest tier), else a megapixel rate.

    `basis_sku` records exactly which line was chosen.
    """
    def _pick(billable: str, unit: str, reason: str) -> Optional[dict]:
        exact = [
            line for line in lines
            if line.get("billable") == billable and line.get("unit") == unit
            and line.get("usd_amount") is not None and not line.get("variant")
        ]
        if exact:
            return {"unit": _IMAGE_ENDPOINT_UNIT_TO_FAMILY[unit], "usd": exact[0]["usd_amount"],
                    "basis": reason, "basis_sku": f"{billable}/{unit}"}
        tiered = sorted(
            (
                line for line in lines
                if line.get("billable") == billable and line.get("unit") == unit
                and line.get("usd_amount") is not None
            ),
            key=lambda line: (line["usd_amount"], str(line.get("variant"))),
        )
        if tiered:
            winner = tiered[0]
            return {"unit": _IMAGE_ENDPOINT_UNIT_TO_FAMILY[unit], "usd": winner["usd_amount"],
                    "basis": "cheapest_variant_tier", "basis_sku": f"{billable}/{unit}@{winner.get('variant')}"}
        return None

    return (
        _pick("output_image", "image", "plain_output_image_rate")
        or _pick("output_image", "megapixel", "plain_output_megapixel_rate")
        or _pick("output_image", "token", "plain_output_token_rate")
        or {"unit": None, "usd": None, "basis": None, "basis_sku": None}
    )


def normalize_image_endpoint_pricing(raw_endpoints_payload: Any) -> dict:
    """Parse one image model's per-endpoint record into pricing data.

    `cost_usd` is already USD with an explicit `unit`, so no currency inference
    is needed here - unlike pricing_skus. A model may be served by several
    providers, each with its own pricing lines; all are preserved.
    """
    payload = raw_endpoints_payload if isinstance(raw_endpoints_payload, dict) else {}
    raw_endpoints = payload.get("endpoints")
    raw_endpoints = raw_endpoints if isinstance(raw_endpoints, list) else []

    endpoints: list[dict] = []
    all_lines: list[dict] = []
    providers_without_pricing: list[str] = []
    unparseable_count = 0

    for raw_endpoint in raw_endpoints:
        if not isinstance(raw_endpoint, dict):
            continue
        raw_pricing = raw_endpoint.get("pricing")
        raw_pricing = raw_pricing if isinstance(raw_pricing, list) else []
        lines = [_normalize_endpoint_pricing_line(line) for line in raw_pricing if isinstance(line, dict)]
        unparseable_count += sum(1 for line in lines if not line["is_parseable"])
        all_lines.extend(lines)

        provider_slug = raw_endpoint.get("provider_slug")
        if not any(line["usd_amount"] is not None for line in lines):
            providers_without_pricing.append(provider_slug)

        endpoints.append({
            "provider_name": raw_endpoint.get("provider_name"),
            "provider_slug": provider_slug,
            "provider_tag": raw_endpoint.get("provider_tag"),
            "supports_streaming": raw_endpoint.get("supports_streaming"),
            "allowed_passthrough_parameters": raw_endpoint.get("allowed_passthrough_parameters"),
            "pricing_lines": lines,
            "has_any_price": any(line["usd_amount"] is not None for line in lines),
        })

    priced_lines = [line for line in all_lines if line["usd_amount"] is not None]

    return {
        "model_id": payload.get("id"),
        "endpoint_count": len(endpoints),
        "endpoints": endpoints,
        "pricing_line_count": len(all_lines),
        "resolved_pricing_line_count": len(priced_lines),
        "unparseable_pricing_line_count": unparseable_count,
        "has_any_price": bool(priced_lines),
        "comparable_price": _select_endpoint_comparable(all_lines),
        "usd_per_image_by_variant": _endpoint_rate_map(all_lines, "output_image", "image"),
        "usd_per_megapixel_by_variant": _endpoint_rate_map(all_lines, "output_image", "megapixel"),
        "usd_per_token_by_variant": _endpoint_rate_map(all_lines, "output_image", "token"),
        "providers_without_pricing": providers_without_pricing,
        "raw_endpoints_payload": raw_endpoints_payload,
    }


def _normalize_media_architecture(raw_model: dict, model_type: str) -> dict:
    """Image models carry an `architecture` block; video models carry none at
    all (confirmed live), only a free-text description. Synthesize the missing
    part from `model_type` rather than leaving modality information absent -
    and mark the synthesis so it is never mistaken for API-provided data."""
    architecture = raw_model.get("architecture")
    if isinstance(architecture, dict):
        return {
            "modality": architecture.get("modality"),
            "input_modalities": architecture.get("input_modalities"),
            "output_modalities": architecture.get("output_modalities"),
            "synthesized": False,
        }
    return {
        "modality": None,
        "input_modalities": None,
        "output_modalities": [model_type],
        "synthesized": True,
    }


def normalize_media_model(
    raw_model: dict,
    model_type: str,
    retrieved_at: str,
    *,
    pricing: Optional[dict] = None,
    endpoint_pricing: Optional[dict] = None,
) -> dict:
    """Build a normalized media model record.

    Never raises on missing optional fields; only `id` is expected. A model
    without an `id` is still returned (with model_id None) so it is not
    silently dropped - the data-quality pass flags it instead. The full raw
    object is preserved under `raw`, same as normalize_model().

    `pricing` is the parsed `pricing_skus` structure. When omitted it is parsed
    from the raw model's `pricing_skus` field, which is where video models carry
    their rates; image models have no `pricing_skus` at all and so get the
    no-price shape, with their cost arriving separately via `endpoint_pricing`.
    """
    model_id = raw_model.get("id")
    if pricing is None:
        pricing = normalize_media_pricing(raw_model.get("pricing_skus"), model_type)

    architecture = _normalize_media_architecture(raw_model, model_type)
    provider = model_id.split("/", 1)[0] if model_id and "/" in model_id else None

    # Video models publish `supported_sizes`; the docs sometimes name it
    # `supported_size`. Accept either, never guess between them.
    supported_sizes = raw_model.get("supported_sizes")
    if supported_sizes is None:
        supported_sizes = raw_model.get("supported_size")

    comparable_sku = pricing.get("comparable_price") or {}
    comparable_endpoint = (endpoint_pricing or {}).get("comparable_price") or {}

    return {
        "model_id": model_id,
        "canonical_slug": raw_model.get("canonical_slug"),
        "model_name": raw_model.get("name"),
        "provider": provider,
        "model_type": model_type,
        "description": raw_model.get("description"),
        "created": raw_model.get("created"),
        "hugging_face_id": raw_model.get("hugging_face_id"),
        "architecture": architecture,
        "input_modalities": architecture["input_modalities"],
        "output_modalities": architecture["output_modalities"],
        "supported_parameters": raw_model.get("supported_parameters"),
        "supports_streaming": raw_model.get("supports_streaming"),
        "endpoints_url": raw_model.get("endpoints"),
        "supported_resolutions": raw_model.get("supported_resolutions"),
        "supported_aspect_ratios": raw_model.get("supported_aspect_ratios"),
        "supported_sizes": supported_sizes,
        "supported_durations": raw_model.get("supported_durations"),
        "supported_frame_images": raw_model.get("supported_frame_images"),
        "generate_audio": raw_model.get("generate_audio"),
        "seed": raw_model.get("seed"),
        "upscale_factor": raw_model.get("upscale_factor"),
        "creativity": raw_model.get("creativity"),
        "allowed_passthrough_parameters": raw_model.get("allowed_passthrough_parameters"),
        "has_any_price": bool(pricing.get("has_any_price") or (endpoint_pricing or {}).get("has_any_price")),
        "has_valid_pricing": comparable_sku.get("usd") is not None or comparable_endpoint.get("usd") is not None,
        "pricing_unit": (
            comparable_sku.get("unit") if comparable_sku.get("usd") is not None
            else comparable_endpoint.get("unit")
        ),
        "comparable_price": comparable_sku.get("usd") if comparable_sku.get("usd") is not None else comparable_endpoint.get("usd"),
        "comparable_price_basis": comparable_sku.get("basis") if comparable_sku.get("usd") is not None else comparable_endpoint.get("basis"),
        "pricing": pricing,
        "endpoint_pricing": endpoint_pricing,
        "raw_source": RAW_SOURCE_BY_MODEL_TYPE.get(model_type),
        "retrieved_at": retrieved_at,
        "raw": raw_model,
    }


def collect_design_arena_entries(
    sources: list[tuple[str, list[dict]]],
) -> tuple[list[dict], list[dict]]:
    """Extract Design Arena entries from several raw model sources at once,
    tagging each with the endpoint it came from, then drop duplicates.

    Multiple sources are needed because they disagree about what they publish:
    the dedicated image catalog has no benchmarks block at all, while the
    generic catalog filtered to image output exposes design_arena for a subset
    of the same models.

    Overlapping sources mean the same (model, arena, category) row can arrive
    twice. First occurrence wins, and every suppressed duplicate is RETURNED
    rather than dropped silently, so the caller can report the count.

    Returns (kept_entries, dropped_duplicates).
    """
    collected: list[dict] = []
    for endpoint, raw_models in sources:
        for entry in extract_design_arena_from_media_models(raw_models or []):
            collected.append({**entry, "source_endpoint": endpoint})

    seen: set[tuple] = set()
    kept: list[dict] = []
    dropped: list[dict] = []
    for entry in collected:
        key = (entry.get("model_ref"), entry.get("benchmark_name"))
        if key in seen:
            dropped.append(entry)
            continue
        seen.add(key)
        kept.append(entry)
    return kept, dropped


def build_media_model_index(
    normalized_models: list[dict],
    extra_catalog_models: Optional[list[dict]] = None,
) -> dict:
    """Reuse the chat pipeline's identifier index so benchmark matching behaves
    identically for media models (including video models' `canonical_slug`, and
    the collision detection that comes with it).

    `extra_catalog_models` lets extra raw catalog records contribute identifiers
    without becoming normalized models. This is needed because the dedicated
    image catalog publishes NO `canonical_slug` while the generic filtered
    catalog DOES - and benchmark pages link the dated canonical slug (e.g.
    `meta/muse-image-1.0-eval-20260824` for catalog id `meta/muse-image`).
    Without those extra identifiers that row stays unmatched.
    """
    index_input = list(normalized_models)
    for raw_model in extra_catalog_models or []:
        if not isinstance(raw_model, dict) or not raw_model.get("id"):
            continue
        index_input.append({
            "model_id": raw_model["id"],
            "canonical_slug": raw_model.get("canonical_slug"),
        })
    return normalize.build_model_index(index_input)


# ---------------------------------------------------------------------------
# Benchmarks (Design Arena)
#
# Media models were NOT observed to carry a `benchmarks` block on either
# endpoint (checked live 2026-09-17, twice). The extraction is still
# implemented, defensively, by reusing the chat pipeline's existing extractor -
# so if OpenRouter starts publishing design_arena rows on these models, they are
# picked up with no new code, and today the function simply returns an empty
# list. Reusing the extractor (rather than copying it) also guarantees the
# benchmark shape can never drift between the two pipelines.
# ---------------------------------------------------------------------------
def extract_design_arena_from_media_models(raw_models: list[dict]) -> list[dict]:
    """Return only the Design Arena entries from media models' `benchmarks`
    blocks, using the exact extraction logic the chat pipeline already uses.

    The Artificial Analysis branch of that extractor is filtered out here: those
    composite indices are chat-model scores, and mixing them into the media
    pipeline's benchmark output would blur two different sources.
    """
    entries = normalize.extract_benchmark_entries_from_raw_models(raw_models)
    return [e for e in entries if e.get("benchmark_source") == DESIGN_ARENA_BENCHMARK_SOURCE]
