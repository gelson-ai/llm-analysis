"""
HTTP client for the OpenRouter API.

Responsibilities (Section 15 of the spec):
  - request with timeout, retries, and backoff for transient failures
  - validate the response shape before handing it back
  - fail loudly on malformed or suspiciously small responses instead of
    silently returning a partial dataset
  - log every request/response outcome
"""
from __future__ import annotations

import logging
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import requests

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
from config import settings

logger = logging.getLogger("openrouter_client")


class OpenRouterAPIError(Exception):
    """Raised when the API cannot be reached or returns something we must not
    silently accept (malformed JSON, missing expected top-level shape, or an
    inventory that looks suspiciously incomplete)."""


@dataclass
class FetchResult:
    endpoint: str
    status_code: Optional[int]
    ok: bool
    raw_json: Optional[Any] = None
    error: Optional[str] = None
    attempts: int = 0
    elapsed_seconds: float = 0.0


@dataclass
class TextFetchResult:
    """Result of fetching a non-JSON (HTML) page.

    OpenRouter's media benchmark pages are server-rendered HTML with no JSON
    equivalent, so they need a text fetch rather than the JSON one above. Kept
    as a separate type so a caller can never mistake markup for parsed data."""
    url: str
    status_code: Optional[int]
    ok: bool
    text: Optional[str] = None
    error: Optional[str] = None
    attempts: int = 0
    elapsed_seconds: float = 0.0


def _request_with_retries(
    url: str,
    *,
    max_retries: int = settings.HTTP_MAX_RETRIES,
    timeout: int = settings.HTTP_TIMEOUT_SECONDS,
    backoff: float = settings.HTTP_RETRY_BACKOFF_SECONDS,
) -> FetchResult:
    last_error = None
    start = time.monotonic()
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(
                url,
                timeout=timeout,
                headers={"Accept": "application/json", "User-Agent": "llm-analysis-pipeline/0.1"},
            )
        except requests.exceptions.RequestException as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            logger.warning("Request to %s failed (attempt %d/%d): %s", url, attempt, max_retries, last_error)
            if attempt < max_retries:
                time.sleep(backoff * attempt)
                continue
            return FetchResult(endpoint=url, status_code=None, ok=False, error=last_error, attempts=attempt,
                                elapsed_seconds=time.monotonic() - start)

        elapsed = time.monotonic() - start

        # Transient server-side failure -> retry
        if resp.status_code >= 500 and attempt < max_retries:
            logger.warning("Server error %s from %s (attempt %d/%d), retrying", resp.status_code, url, attempt, max_retries)
            time.sleep(backoff * attempt)
            continue

        if resp.status_code != 200:
            return FetchResult(
                endpoint=url,
                status_code=resp.status_code,
                ok=False,
                error=f"HTTP {resp.status_code}: {resp.text[:500]}",
                attempts=attempt,
                elapsed_seconds=elapsed,
            )

        try:
            payload = resp.json()
        except ValueError as exc:
            return FetchResult(
                endpoint=url,
                status_code=resp.status_code,
                ok=False,
                error=f"Response was not valid JSON: {exc}",
                attempts=attempt,
                elapsed_seconds=elapsed,
            )

        return FetchResult(
            endpoint=url, status_code=resp.status_code, ok=True, raw_json=payload,
            attempts=attempt, elapsed_seconds=elapsed,
        )

    return FetchResult(endpoint=url, status_code=None, ok=False, error=last_error, attempts=max_retries,
                        elapsed_seconds=time.monotonic() - start)


def fetch_models() -> FetchResult:
    """Fetch the full model inventory from GET /api/v1/models.

    Validates:
      - top-level shape is {"data": [...]}  (OpenRouter's documented envelope)
      - the list is non-empty and at least MIN_EXPECTED_MODEL_COUNT long

    Does NOT filter, transform, or drop any model. Raises OpenRouterAPIError
    on anything that would otherwise produce a silently-partial dataset;
    callers should let that propagate rather than catching and continuing.
    """
    url = settings.OPENROUTER_BASE_URL + settings.MODELS_ENDPOINT
    result = _request_with_retries(url)

    if not result.ok:
        raise OpenRouterAPIError(f"Failed to fetch model inventory from {url}: {result.error}")

    payload = result.raw_json
    if not isinstance(payload, dict) or "data" not in payload:
        raise OpenRouterAPIError(
            f"Unexpected response shape from {url}: top-level object missing 'data' key. "
            f"Got keys: {list(payload.keys()) if isinstance(payload, dict) else type(payload)}"
        )

    models = payload["data"]
    if not isinstance(models, list):
        raise OpenRouterAPIError(f"'data' field from {url} was not a list (got {type(models)})")

    if len(models) == 0:
        raise OpenRouterAPIError(f"{url} returned zero models - refusing to treat this as a valid inventory")

    if len(models) < settings.MIN_EXPECTED_MODEL_COUNT:
        raise OpenRouterAPIError(
            f"{url} returned only {len(models)} models, fewer than the configured "
            f"MIN_EXPECTED_MODEL_COUNT={settings.MIN_EXPECTED_MODEL_COUNT}. This looks like a partial "
            f"response (pagination? rate limiting? API change?) rather than the complete inventory. "
            f"Refusing to silently proceed - raise MIN_EXPECTED_MODEL_COUNT only after confirming this "
            f"count is actually correct."
        )

    # Note on pagination: as of the last confirmed schema, GET /api/v1/models
    # returns the entire inventory in one response (no `next_cursor` / `page`
    # fields in the documented envelope). We still check for common pagination
    # markers here and log loudly if we find one, since silently ignoring
    # pagination would violate the "no partial dataset" rule.
    pagination_markers = [k for k in payload.keys() if k not in ("data",)]
    if pagination_markers:
        logger.warning(
            "Response from %s has extra top-level keys beyond 'data': %s. "
            "These may indicate pagination metadata that this client does not yet handle - "
            "inspect data/raw/openrouter_models.json manually before trusting completeness.",
            url, pagination_markers,
        )

    logger.info("Fetched %d models from %s in %.2fs (%d attempt(s))", len(models), url, result.elapsed_seconds, result.attempts)
    return result


def _fetch_catalog(*, endpoint_path: str, min_count: int, label: str) -> FetchResult:
    """Shared validated fetch for the dedicated media (image/video) catalogs.

    Applies the same validation ladder as fetch_models(), in the same order:
      - the response envelope is {"data": [...]}
      - `data` is a list
      - it is non-empty
      - it is at least `min_count` long

    `min_count` is passed in by the caller (which reads it off `settings` at
    call time) rather than defaulted here, because the media catalogs are an
    order of magnitude smaller than the chat inventory and need their own,
    much lower floors. Anything that would produce a silently-partial dataset
    raises OpenRouterAPIError instead.

    This is a private helper used only by the media fetchers - fetch_models()
    is deliberately left exactly as it was.
    """
    url = settings.OPENROUTER_BASE_URL + endpoint_path
    result = _request_with_retries(url)

    if not result.ok:
        raise OpenRouterAPIError(f"Failed to fetch {label} catalog from {url}: {result.error}")

    payload = result.raw_json
    if not isinstance(payload, dict) or "data" not in payload:
        raise OpenRouterAPIError(
            f"Unexpected response shape from {url}: top-level object missing 'data' key. "
            f"Got keys: {list(payload.keys()) if isinstance(payload, dict) else type(payload)}"
        )

    models = payload["data"]
    if not isinstance(models, list):
        raise OpenRouterAPIError(f"'data' field from {url} was not a list (got {type(models)})")

    if len(models) == 0:
        raise OpenRouterAPIError(
            f"{url} returned zero {label} models - refusing to treat this as a valid inventory"
        )

    if len(models) < min_count:
        raise OpenRouterAPIError(
            f"{url} returned only {len(models)} {label} models, fewer than the configured floor of "
            f"{min_count}. This looks like a partial response (pagination? rate limiting? API change?) "
            f"rather than the complete catalog. Refusing to silently proceed - lower the floor only "
            f"after confirming this count is actually correct."
        )

    # Same pagination-marker guard as the chat fetch: silently ignoring
    # pagination metadata would violate the "no partial dataset" rule.
    pagination_markers = [k for k in payload.keys() if k not in ("data",)]
    if pagination_markers:
        logger.warning(
            "Response from %s has extra top-level keys beyond 'data': %s. "
            "These may indicate pagination metadata that this client does not yet handle - "
            "inspect the raw response manually before trusting completeness.",
            url, pagination_markers,
        )

    logger.info(
        "Fetched %d %s models from %s in %.2fs (%d attempt(s))",
        len(models), label, url, result.elapsed_seconds, result.attempts,
    )
    return result


def fetch_image_models() -> FetchResult:
    """Fetch the image-generation catalog from GET /api/v1/images/models.

    Validates the envelope, rejects an empty list, and rejects anything below
    settings.MIN_EXPECTED_IMAGE_MODEL_COUNT (read at call time). Raises
    OpenRouterAPIError rather than returning a silently-partial catalog.

    Note: this endpoint returns NO pricing for image models. Cost lives in the
    per-model endpoints record - see fetch_image_model_endpoints(). No model is
    dropped for lacking pricing.
    """
    return _fetch_catalog(
        endpoint_path=settings.IMAGES_MODELS_ENDPOINT,
        min_count=settings.MIN_EXPECTED_IMAGE_MODEL_COUNT,
        label="image",
    )


def fetch_video_models() -> FetchResult:
    """Fetch the video-generation catalog from GET /api/v1/videos/models.

    Same validation contract as fetch_image_models(). Video models DO carry a
    `pricing_skus` map, but its values mix units and vocabularies (cents per
    second, USD per second, USD per video token, cents per megapixel-second,
    per-generation minimums...) - see src/normalize_media.py.
    """
    return _fetch_catalog(
        endpoint_path=settings.VIDEOS_MODELS_ENDPOINT,
        min_count=settings.MIN_EXPECTED_VIDEO_MODEL_COUNT,
        label="video",
    )


def fetch_image_models_filtered() -> FetchResult:
    """Fetch the generic catalog filtered to image-output models, i.e.
    GET /api/v1/models?output_modalities=image.

    Used ONLY as the source of Design Arena benchmark scores for image models:
    the dedicated /api/v1/images/models endpoint publishes no benchmarks block
    at all, while this filtered view exposes `benchmarks.design_arena` for a
    subset of the same models (confirmed live 2026-09-17).

    Validated the same way as the other catalogs, and deliberately fatal on
    failure: a silent failure here would present "no Design Arena scores" as if
    it were a fact about the data, when it would actually be a fact about the
    fetch. Inventory and pricing still come from the dedicated endpoints.
    """
    return _fetch_catalog(
        endpoint_path=settings.IMAGE_MODELS_FILTER_ENDPOINT,
        min_count=settings.MIN_EXPECTED_IMAGE_MODEL_COUNT,
        label="filtered image",
    )


def fetch_image_model_endpoints(model_id: str) -> FetchResult:
    """Fetch one image model's per-endpoint record, which is the ONLY place
    image pricing is published.

    Deliberately does NOT raise on failure (unlike the catalog fetchers): this
    is a per-model enrichment call run once per image model, so one dead
    endpoint record must not destroy an otherwise-good run. Callers are
    expected to count and report failures - never to swallow them.
    """
    url = settings.OPENROUTER_BASE_URL + settings.IMAGE_MODEL_ENDPOINTS_PATH_TEMPLATE.format(
        model_id=model_id
    )
    result = _request_with_retries(url, max_retries=2)
    if not result.ok:
        logger.warning("Failed to fetch endpoint pricing for image model %s: %s", model_id, result.error)
    return result


def fetch_page(path: str) -> TextFetchResult:
    """Fetch a non-JSON (HTML) OpenRouter page, with the same retry/backoff
    policy as the JSON fetchers.

    Deliberately does NOT raise: whether a failed page is fatal depends on the
    caller (a benchmark prompt page yielding no rows must fail loudly, but the
    failure is reported and validated by the scraper, which knows what was
    expected rather than the transport layer guessing).
    """
    url = path if path.startswith("http") else settings.MEDIA_BENCHMARK_BASE_URL + path
    last_error = None
    start = time.monotonic()

    for attempt in range(1, settings.HTTP_MAX_RETRIES + 1):
        try:
            resp = requests.get(
                url,
                timeout=settings.HTTP_TIMEOUT_SECONDS,
                headers={
                    "Accept": "text/html,application/xhtml+xml",
                    "User-Agent": "llm-analysis-pipeline/0.1 (+media benchmarks)",
                },
            )
        except requests.exceptions.RequestException as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            logger.warning("Page request to %s failed (attempt %d/%d): %s",
                           url, attempt, settings.HTTP_MAX_RETRIES, last_error)
            if attempt < settings.HTTP_MAX_RETRIES:
                time.sleep(settings.HTTP_RETRY_BACKOFF_SECONDS * attempt)
                continue
            return TextFetchResult(url=url, status_code=None, ok=False, error=last_error,
                                   attempts=attempt, elapsed_seconds=time.monotonic() - start)

        elapsed = time.monotonic() - start

        if resp.status_code >= 500 and attempt < settings.HTTP_MAX_RETRIES:
            logger.warning("Server error %s from %s (attempt %d/%d), retrying",
                           resp.status_code, url, attempt, settings.HTTP_MAX_RETRIES)
            time.sleep(settings.HTTP_RETRY_BACKOFF_SECONDS * attempt)
            continue

        if resp.status_code != 200:
            return TextFetchResult(url=url, status_code=resp.status_code, ok=False,
                                   error=f"HTTP {resp.status_code}: {resp.text[:200]}",
                                   attempts=attempt, elapsed_seconds=elapsed)

        logger.info("Fetched page %s (%d chars) in %.2fs", url, len(resp.text), elapsed)
        return TextFetchResult(url=url, status_code=resp.status_code, ok=True, text=resp.text,
                               attempts=attempt, elapsed_seconds=elapsed)

    return TextFetchResult(url=url, status_code=None, ok=False, error=last_error,
                           attempts=settings.HTTP_MAX_RETRIES, elapsed_seconds=time.monotonic() - start)


def probe_endpoint(path: str) -> FetchResult:
    """Probe a candidate endpoint for benchmark data without raising on
    failure - used purely for discovery/investigation (Section 3)."""
    url = settings.OPENROUTER_BASE_URL + path
    try:
        return _request_with_retries(url, max_retries=1)
    except Exception as exc:  # pragma: no cover - defensive
        return FetchResult(endpoint=url, status_code=None, ok=False, error=str(exc))
