"""
Parser for OpenRouter's media prompt benchmarks.

Source: https://openrouter.ai/benchmarks/media/images (15 prompts)
        https://openrouter.ai/benchmarks/media/videos (12 prompts)

Each prompt page publishes, per model, three measurements:

  - a judged pass count, e.g. "5 of 5 checks passed"
  - the actual cost of that generation, in USD
  - the generation time, in seconds

There is NO public JSON API for this. Confirmed live 2026-09-17:

  - the pages are server-rendered HTML (no __NEXT_DATA__, no /api/ reference in
    the document at all - the only JSON is schema.org ItemList metadata, which
    carries names and ranks but no scores);
  - /api/v1/benchmarks and /api/v1/videos/benchmarks return
    401 "No cookie auth credentials found" - those routes exist but are
    session-gated, so they are not usable without a logged-in cookie.

Extraction therefore parses markup, and is written defensively because of it:

  - it anchors on the accessibility contract (aria-label="N of M checks
    passed") and only falls back to the visible "N/M" text;
  - it anchors on `href="/<vendor>/<model>-<YYYYMMDD>"` for the model identity,
    which is the canonical slug the model catalogs also use;
  - every observed page renders each row TWICE (two identical <li> blocks), so
    rows are de-duplicated - and if the two copies ever DISAGREE, that is
    reported rather than silently resolved;
  - a page that parses to zero rows raises, because the only way that happens is
    a markup change, and silently returning nothing would look exactly like
    "these models have no benchmark data".
"""
from __future__ import annotations

import html as html_lib
import re
from typing import Optional

BENCHMARK_SOURCE = "OpenRouter Media Benchmarks"

# Media type -> index page path. Kept here rather than read from settings so the
# parser stays usable on a saved page without the config package loaded.
INDEX_PATHS = {
    "image": "/benchmarks/media/images",
    "video": "/benchmarks/media/videos",
}


class MediaBenchmarkParseError(Exception):
    """Raised when a benchmark page parses to something we must not accept
    silently (zero rows, or no model rows at all)."""


_ROW_SPLIT_RE = re.compile(r"(?=<li )")

# The accessibility contract. Preferred over the visible text because it is
# semantically meaningful markup rather than styling.
_CHECKS_ARIA_RE = re.compile(r'aria-label="(\d+)\s+of\s+(\d+)\s+checks?\s+passed"', re.IGNORECASE)
# Fallback: the visible "N/M" badge.
_CHECKS_VISIBLE_RE = re.compile(r">(\d{1,3})\s*/\s*(\d{1,3})\s*<")

# Model identity: the canonical slug, e.g. /alibaba/happyhorse-1.1-20260624
_MODEL_HREF_RE = re.compile(r'href="/([a-z0-9][a-z0-9._\-]*/[a-z0-9][a-z0-9._\-]*)"')

_COST_RE = re.compile(r"\$([0-9]+(?:\.[0-9]+)?)")
_TIME_RE = re.compile(r">([0-9]+(?:\.[0-9]+)?)s<")

# Paths that are never a model slug, so they can't be mistaken for one.
_NON_MODEL_PREFIXES = (
    "benchmarks", "models", "providers", "pricing", "docs", "rankings",
    "chat", "apps", "discover", "collections", "about", "blog", "careers",
    "privacy", "terms", "support", "data", "brand", "developers", "labs",
    "business", "enterprise", "works-with-openrouter", "settings", "login",
    "signup", "images", "static", "api",
)

_RELEASE_DATE_SUFFIX_RE = re.compile(r"-\d{8}$")


def discover_prompt_slugs(index_html: str, media_type: str) -> list[str]:
    """Return the prompt slugs linked from a benchmark index page, in page order.

    The index page renders one prompt's results but links to every prompt, so
    this is how the full set is discovered without hardcoding it.
    """
    # Note the plural in the URL path ("/media/images", "/media/videos") vs the
    # singular media_type key - derive the prefix from INDEX_PATHS rather than
    # rebuilding it from media_type, which silently matched nothing.
    base = INDEX_PATHS.get(media_type)
    if not base:
        return []
    prefix = base + "/"
    slugs: list[str] = []
    for match in re.finditer(rf'href="{re.escape(prefix)}([a-z0-9\-]+)"', index_html):
        slug = match.group(1)
        if slug not in slugs:
            slugs.append(slug)
    return slugs


def _clean_text(raw: str) -> str:
    return html_lib.unescape(raw).strip()


def parse_prompt_page(page_html: str) -> list[dict]:
    """Parse one prompt page into raw rows, one per (model) after de-duplication.

    Returns a list of dicts with the fields exactly as found on the page plus
    `duplicate_blocks` (how many identical copies were seen) and
    `conflicting_values` (True if the copies disagreed - reported, never hidden).
    """
    candidates: list[dict] = []

    for block in _ROW_SPLIT_RE.split(page_html):
        checks = _CHECKS_ARIA_RE.search(block)
        checks_source = "aria-label"
        if not checks:
            checks = _CHECKS_VISIBLE_RE.search(block)
            checks_source = "visible_text"
        if not checks:
            continue  # not a results row

        model_match = None
        for href in _MODEL_HREF_RE.finditer(block):
            candidate = href.group(1)
            if candidate.split("/", 1)[0] in _NON_MODEL_PREFIXES:
                continue
            model_match = candidate
            break
        if not model_match:
            continue

        cost = _COST_RE.search(block)
        timing = _TIME_RE.search(block)

        candidates.append({
            "raw_model_slug": model_match,
            "checks_passed": int(checks.group(1)),
            "checks_total": int(checks.group(2)),
            "checks_source": checks_source,
            "cost_usd": float(cost.group(1)) if cost else None,
            "generation_seconds": float(timing.group(1)) if timing else None,
            # The visible model name (the anchor text next to the slug).
            "model_name": _extract_anchor_text(block, model_match),
        })

    if not candidates:
        return []

    # Each row is rendered twice. Collapse on the model slug, and surface any
    # disagreement between copies rather than quietly picking one.
    merged: dict[str, dict] = {}
    for row in candidates:
        slug = row["raw_model_slug"]
        if slug not in merged:
            merged[slug] = {**row, "duplicate_blocks": 1, "conflicting_values": False}
            continue
        existing = merged[slug]
        existing["duplicate_blocks"] += 1
        for field in ("checks_passed", "checks_total", "cost_usd", "generation_seconds"):
            if row[field] != existing[field]:
                existing["conflicting_values"] = True
                # Keep the highest pass count so a transient render glitch does
                # not understate a model, but the conflict is flagged above.
                if field == "checks_passed" and (row[field] or 0) > (existing[field] or 0):
                    existing[field] = row[field]

    return list(merged.values())


def _extract_anchor_text(block: str, model_slug: str) -> Optional[str]:
    match = re.search(rf'href="/{re.escape(model_slug)}"[^>]*>([^<]{{1,80}})<', block)
    return _clean_text(match.group(1)) if match else None


def strip_release_date_suffix(model_slug: str) -> str:
    """`alibaba/happyhorse-1.1-20260624` -> `alibaba/happyhorse-1.1`.

    Benchmark pages always link the dated canonical slug. Video models publish
    that same canonical_slug in the catalog (so they match directly), but the
    dedicated image catalog does not, so image models need this fallback.
    """
    return _RELEASE_DATE_SUFFIX_RE.sub("", model_slug or "")


def normalize_prompt_benchmark_row(
    row: dict,
    *,
    media_type: str,
    prompt_slug: str,
    prompt_name: str,
    source_url: str,
    retrieved_at: str,
    model_id: Optional[str] = None,
) -> dict:
    """Turn a parsed page row into a normalized benchmark record.

    `model_id` is the resolved catalog model, or None when the row's slug could
    not be matched - an unmatched row is still written, flagged, so it is never
    silently dropped.
    """
    passed = row.get("checks_passed")
    total = row.get("checks_total")
    pass_rate = (passed / total) if (passed is not None and total) else None

    return {
        "model_id": model_id,
        "raw_model_slug": row.get("raw_model_slug"),
        "model_name": row.get("model_name"),
        "matched": model_id is not None,
        "media_type": media_type,
        "prompt_slug": prompt_slug,
        "prompt_name": prompt_name,
        "benchmark_name": f"Media Prompt Benchmark: {media_type}/{prompt_slug}",
        "benchmark_source": BENCHMARK_SOURCE,
        "source_platform": "OpenRouter",
        "source_url": source_url,
        "checks_passed": passed,
        "checks_total": total,
        "pass_rate": pass_rate,
        "correctness_label": f"{passed}/{total}" if (passed is not None and total) else None,
        "cost_usd": row.get("cost_usd"),
        "generation_seconds": row.get("generation_seconds"),
        "retrieved_at": retrieved_at,
        # `checks_source` records whether the accessibility label or the visible
        # text was used, so a silent markup change is visible in the output.
        "raw_data": {
            "checks_source": row.get("checks_source"),
            "duplicate_blocks": row.get("duplicate_blocks"),
            "conflicting_values": row.get("conflicting_values"),
        },
    }


def prompt_name_from_slug(slug: str) -> str:
    """`traffic-light` -> `Traffic Light` (the page's own heading text)."""
    return " ".join(part.capitalize() for part in (slug or "").split("-"))
