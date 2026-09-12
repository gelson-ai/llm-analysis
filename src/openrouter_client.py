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


def probe_endpoint(path: str) -> FetchResult:
    """Probe a candidate endpoint for benchmark data without raising on
    failure - used purely for discovery/investigation (Section 3)."""
    url = settings.OPENROUTER_BASE_URL + path
    try:
        return _request_with_retries(url, max_retries=1)
    except Exception as exc:  # pragma: no cover - defensive
        return FetchResult(endpoint=url, status_code=None, ok=False, error=str(exc))
