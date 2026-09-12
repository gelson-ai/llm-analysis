"""
Central configuration for the OpenRouter data pipeline.

Everything here is meant to be a *documented assumption* per the project
spec, not a hardcoded truth. Change these values as new evidence comes in
from real pipeline runs (see data/analysis/coverage_report.json and
data/analysis/data_quality_report.json after each run).
"""

from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
NORMALIZED_DIR = DATA_DIR / "normalized"
SNAPSHOTS_DIR = DATA_DIR / "snapshots"
ANALYSIS_DIR = DATA_DIR / "analysis"
CONFIG_DIR = PROJECT_ROOT / "config"

BENCHMARK_CATEGORIES_PATH = CONFIG_DIR / "benchmark_categories.json"

# ---------------------------------------------------------------------------
# OpenRouter API
# ---------------------------------------------------------------------------
OPENROUTER_BASE_URL = "https://openrouter.ai"
MODELS_ENDPOINT = "/api/v1/models"

# Candidate endpoints that MIGHT expose benchmark/Artificial Analysis data.
# None of these are confirmed to exist or to return benchmark data - the
# discovery step (src/discover.py) probes them, records the raw HTTP
# response (status code + body, or error), and never assumes a 404/error
# means "no benchmark data exists". Add candidates here as you find real
# ones (e.g. by inspecting openrouter.ai/models network traffic in a
# browser) and re-run discovery.
CANDIDATE_BENCHMARK_ENDPOINTS = [
    "/api/v1/models",  # primary - benchmarks may simply be embedded here
    "/api/frontend/models",
    "/api/frontend/models/find",
    "/api/v1/benchmarks",
]

HTTP_TIMEOUT_SECONDS = 30
HTTP_MAX_RETRIES = 3
HTTP_RETRY_BACKOFF_SECONDS = 2.0

# If OpenRouter returns fewer than this many models, treat it as a
# suspiciously incomplete inventory and fail loudly rather than silently
# writing a partial dataset. Adjust once you know the real current count
# (OpenRouter has listed several hundred models historically).
MIN_EXPECTED_MODEL_COUNT = 100

# ---------------------------------------------------------------------------
# Cost model (Section 12) - configurable, NOT yet the final formula
# ---------------------------------------------------------------------------
INPUT_TOKEN_WEIGHT = 3
OUTPUT_TOKEN_WEIGHT = 1

# Alternative ratios available for later sensitivity analysis (input, output)
ALTERNATIVE_WEIGHT_RATIOS = [
    (1, 1),
    (3, 1),
    (5, 1),
    (10, 1),
]

MTOK = 1_000_000

# ---------------------------------------------------------------------------
# Benchmarks we specifically care about for this product (Section 8)
#
# CONFIRMED against a live run (2026-09-08): OpenRouter's /api/v1/models does
# NOT expose GPQA Diamond, IFBench, or AA-Omniscience individually. It embeds
# exactly three Artificial Analysis composite indices per model
# (intelligence/coding/agentic) plus a separately-provenanced "Design Arena"
# set of per-category Elo/win-rate rows. None of these are a clean match for
# "reasoning" / "general knowledge" / "instruction following" in isolation -
# AA's Intelligence Index is itself a blend across many underlying evals
# (including some coding and agentic tasks), so treating it as a pure
# reasoning-or-knowledge score would be an assumption this pipeline is
# supposed to avoid making silently. See README "The benchmark question".
# ---------------------------------------------------------------------------
PRIORITY_BENCHMARKS = [
    "AA Intelligence Index",
    "AA Coding Index",
    "AA Agentic Index",
]

# Kept for traceability - these were the originally-requested benchmarks;
# confirmed absent from OpenRouter's public model-listing response.
ORIGINALLY_REQUESTED_BENCHMARKS_NOT_FOUND = [
    "GPQA Diamond",
    "IFBench",
    "AA-Omniscience",
]

# Models flagged in the spec as historically unreliable to extract /
# match - watch these closely in the sample-inspection step.
WATCHLIST_MODEL_IDS = [
    "qwen/qwen3-8b",
]
WATCHLIST_MODEL_NAME_SUBSTRINGS = [
    "solar pro 4",
]
