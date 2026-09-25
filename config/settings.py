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

# ---------------------------------------------------------------------------
# Image & video generation catalogs (separate pipeline)
#
# These are DIFFERENT, dedicated, documented OpenRouter endpoints:
#   GET /api/v1/images/models
#   GET /api/v1/videos/models
#
# They are deliberately NOT fetched by the chat pipeline. Two reasons,
# confirmed against a live response (2026-09-17):
#   1. Their pricing is SKU/unit based, not token based - real cost for these
#      models cannot be expressed as pricing.prompt / pricing.completion at
#      all. See src/normalize_media.py.
#   2. Their catalogs are far smaller than the chat one, so the chat
#      pipeline's MIN_EXPECTED_MODEL_COUNT floor does not apply (below).
#
# The generic catalog can also be filtered with
# /api/v1/models?output_modalities=image|video, but that returns these same
# models with pricing mostly zeroed out and no pricing_skus, so the dedicated
# endpoints above are the primary source.
# ---------------------------------------------------------------------------
IMAGES_MODELS_ENDPOINT = "/api/v1/images/models"
VIDEOS_MODELS_ENDPOINT = "/api/v1/videos/models"

# Per-model record for image models. Live image models carry NO pricing on the
# list endpoint - cost lives only in the per-model endpoints record:
#   GET /api/v1/images/models/{id}/endpoints
#     -> endpoints[].pricing[] = {billable, unit, cost_usd, variant?}
IMAGE_MODEL_ENDPOINTS_PATH_TEMPLATE = "/api/v1/images/models/{model_id}/endpoints"

# Distinct from MIN_EXPECTED_MODEL_COUNT above on purpose: that floor (100) is
# for the chat catalog. The image and video catalogs are an order of magnitude
# smaller - observed live counts on 2026-09-17 were ~54 image models and ~29
# video models - so reusing 100 would falsely fail every run. These floors are
# set conservatively low to still catch a truly broken/empty/truncated
# response. Raise them only after confirming a new floor is genuinely correct
# for the live catalog.
MIN_EXPECTED_IMAGE_MODEL_COUNT = 10
MIN_EXPECTED_VIDEO_MODEL_COUNT = 10

# ---------------------------------------------------------------------------
# Media pipeline output paths
#
# Every one of these is a NEW file. Nothing here overlaps with a path the chat
# pipeline writes, so run_pipeline.py's outputs are never touched or reshaped.
# ---------------------------------------------------------------------------
MEDIA_RAW_IMAGE_MODELS_PATH = RAW_DIR / "openrouter_image_models.json"
MEDIA_RAW_VIDEO_MODELS_PATH = RAW_DIR / "openrouter_video_models.json"
MEDIA_RAW_IMAGE_ENDPOINTS_PATH = RAW_DIR / "openrouter_image_model_endpoints.json"

MEDIA_IMAGE_MODELS_JSON_PATH = NORMALIZED_DIR / "image_models.json"
MEDIA_IMAGE_MODELS_CSV_PATH = NORMALIZED_DIR / "image_models.csv"
MEDIA_VIDEO_MODELS_JSON_PATH = NORMALIZED_DIR / "video_models.json"
MEDIA_VIDEO_MODELS_CSV_PATH = NORMALIZED_DIR / "video_models.csv"
MEDIA_BENCHMARKS_JSON_PATH = NORMALIZED_DIR / "media_benchmarks.json"
MEDIA_BENCHMARKS_CSV_PATH = NORMALIZED_DIR / "media_benchmarks.csv"

MEDIA_COVERAGE_REPORT_PATH = ANALYSIS_DIR / "media_coverage_report.json"
MEDIA_DATA_QUALITY_REPORT_PATH = ANALYSIS_DIR / "media_data_quality_report.json"

# The image dashboard's weekly "Model of the week" history. A SEPARATE file from
# the chat pipeline's data/analysis/weekly_picks.json: the two dashboards lock
# picks from different rankings over different models, so a shared file would let
# one pipeline's lock silently overwrite the other's record.
MEDIA_WEEKLY_PICKS_PATH = ANALYSIS_DIR / "media_weekly_picks.json"

# Media snapshots live in their OWN top-level directory, NOT under
# SNAPSHOTS_DIR.
#
# This is load-bearing, not tidiness. dashboard/refresh.py decides whether
# today's chat snapshot already exists with snapshot_exists_for(), which does
# nothing more than look for a directory directly under SNAPSHOTS_DIR named
# `<date>` or `<date>-N`. Writing media snapshots to data/snapshots/<date>/media/
# therefore created a directory literally named `<date>`, so a media run on a
# given UTC day before that day's chat refresh made refresh.py pass
# --no-snapshot to the chat pipeline - silently losing the day's real chat
# snapshot. data/media_snapshots/ cannot match that pattern, so the two
# pipelines cannot interfere. dashboard/refresh.py, snapshot_exists_for() and
# SNAPSHOTS_DIR are deliberately left untouched.
MEDIA_SNAPSHOTS_DIR = DATA_DIR / "media_snapshots"

# Which keys in a `pricing_skus` map are denominated in cents rather than USD.
# OpenRouter mixes both in the same object (e.g. "cents_per_second_output":
# "3" alongside "duration_seconds_720p": "0.08"), so this conversion is an
# explicit, documented assumption - every conversion is recorded on the parsed
# SKU and listed in the media data-quality report, never applied silently.
CENTS_PER_DOLLAR = 100
CENT_DENOMINATED_SKU_PREFIX = "cents_"

# Politeness delay between the per-model image endpoints requests. The image
# pricing fan-out is one request per image model (dozens per run), so it is
# serialised with a small gap rather than fired off at once.
MEDIA_IMAGE_ENDPOINTS_DELAY_SECONDS = 0.25

# The generic catalog, filtered by output modality.
#
# This IS fetched by the media pipeline - not for inventory or pricing, but as
# the only source of Design Arena scores for image models. Confirmed live
# (2026-09-17): the dedicated /api/v1/images/models endpoint publishes no
# `benchmarks` field at all, while 13 of the 54 records here carry a full
# `benchmarks.design_arena` block. The dedicated endpoints stay the source of
# truth for inventory and pricing.
#
# The video variant is kept for documentation only: it is NOT fetched, because
# it publishes neither design_arena nor usable pricing (every pricing value in
# it is zeroed).
IMAGE_MODELS_FILTER_ENDPOINT = "/api/v1/models?output_modalities=image"
VIDEO_MODELS_FILTER_ENDPOINT = "/api/v1/models?output_modalities=video"

# ---------------------------------------------------------------------------
# OpenRouter media prompt benchmarks (HTML, not an API)
#
# These are OpenRouter's own prompt-by-prompt evaluations:
#   https://openrouter.ai/benchmarks/media/images   (15 prompt pages)
#   https://openrouter.ai/benchmarks/media/videos   (12 prompt pages)
#
# Each prompt page publishes, per model: a judged pass count ("N of M checks
# passed"), the actual cost in USD, and the generation time in seconds. There is
# NO public JSON API for this: the pages are server-rendered HTML, and the
# obvious JSON routes (/api/v1/benchmarks, /api/v1/videos/benchmarks) return
# 401 "No cookie auth credentials found" - they exist but are session-gated.
# So the data is public but only reachable by parsing rendered markup.
#
# That makes the extraction markup-dependent, which is why:
#   - parsing prefers the accessibility contract (aria-label="N of M checks
#     passed"), falling back to the visible "N/M" text;
#   - a page that yields zero rows is a HARD ERROR, not an empty result, and
#     each prompt count has a floor below which the run fails loudly;
#   - the extracted rows are preserved verbatim under data/raw/ together with a
#     sha256 + byte size per page, so markup drift is detectable.
# ---------------------------------------------------------------------------
MEDIA_BENCHMARK_INDEX_ENDPOINTS = {
    "image": "/benchmarks/media/images",
    "video": "/benchmarks/media/videos",
}
MEDIA_BENCHMARK_BASE_URL = OPENROUTER_BASE_URL

# Observed live 2026-09-17: 15 image prompts, 12 video prompts. Floors are set
# below that on purpose - they exist to catch a broken index parse, not to pin
# the exact current count (OpenRouter adds prompts over time). Raise only after
# confirming a new floor is genuinely right.
MIN_EXPECTED_IMAGE_PROMPT_COUNT = 5
MIN_EXPECTED_VIDEO_PROMPT_COUNT = 5

# Politeness delay between prompt-page requests (27 pages per run).
MEDIA_BENCHMARK_PAGE_DELAY_SECONDS = 0.25

# Row-level sanity.
#
# A per-page floor is deliberately NOT used. Prompt difficulty genuinely varies
# how many models a page covers: measured live 2026-09-17, 14 of the 15 image
# prompts had 42 rows each but `composite-refs` (the multi-reference prompt) had
# just 3, and video `walk-out` had 14 against 24 for the rest. A per-page floor
# would fail on that legitimate variance.
#
# Instead:
#   - a page parsing to ZERO rows is a hard error (markup break or empty page);
#   - the TOTAL across all pages must clear this floor, which catches a partial
#     break that a zero-row check would miss.
# Observed total: 867 rows. This floor is a crash detector, not a target.
MIN_EXPECTED_TOTAL_PROMPT_BENCHMARK_ROWS = 200

MEDIA_RAW_PROMPT_BENCHMARK_ROWS_PATH = RAW_DIR / "openrouter_media_prompt_benchmark_rows.json"
MEDIA_PROMPT_BENCHMARKS_JSON_PATH = NORMALIZED_DIR / "media_prompt_benchmarks.json"
MEDIA_PROMPT_BENCHMARKS_CSV_PATH = NORMALIZED_DIR / "media_prompt_benchmarks.csv"

# ---------------------------------------------------------------------------
# Video dashboard (its own pipeline, its own paths)
#
# The video page has its OWN fetch/normalize/report chain rather than sharing the
# media pipeline's, for the same reason the media pipeline does not share the
# chat one: refresh_all.py reports per target, and a failure in one pipeline must
# never skip or corrupt another. Sharing a path would also mean the video refresh
# overwriting the media pipeline's inputs, which are embedded verbatim in the
# published image page.
#
# So every name below is deliberately NOT an existing MEDIA_* name. The media
# pipeline keeps writing data/normalized/video_models.json for the image page's
# coverage block; this dashboard reads and writes only its own files.
#
# Inputs are REUSED, not duplicated - the endpoints already exist and are fetched
# with the same client helpers:
#   VIDEOS_MODELS_ENDPOINT                        -> /api/v1/videos/models
#   MEDIA_BENCHMARK_INDEX_ENDPOINTS["video"]      -> /benchmarks/media/videos
#   MIN_EXPECTED_VIDEO_MODEL_COUNT                -> catalog floor
#   MIN_EXPECTED_VIDEO_PROMPT_COUNT               -> prompt-page floor
#   MIN_EXPECTED_TOTAL_PROMPT_BENCHMARK_ROWS      -> total-row floor
#   MEDIA_BENCHMARK_PAGE_DELAY_SECONDS            -> page politeness gap
#
# No per-model endpoint fan-out is needed here: unlike image models, video models
# publish their `pricing_skus` on the list endpoint itself.
#
# VIDEO_DASHBOARD_SNAPSHOTS_DIR is a sibling of SNAPSHOTS_DIR and
# MEDIA_SNAPSHOTS_DIR, never nested inside either.
# dashboard/refresh.py::snapshot_exists_for() treats any directory directly under
# SNAPSHOTS_DIR named `<date>` or `<date>-N` as "today's chat snapshot already
# exists", so a video snapshot written under a date-named directory there would
# silently suppress the day's chat snapshot. data/video_snapshots/ cannot match
# that pattern. Locked down by tests/test_video_snapshot_paths.py, which calls the
# real snapshot_exists_for().
# ---------------------------------------------------------------------------
VIDEO_DASHBOARD_RAW_MODELS_PATH = RAW_DIR / "video_dashboard_models.json"
VIDEO_DASHBOARD_MODELS_JSON_PATH = NORMALIZED_DIR / "video_dashboard_models.json"
VIDEO_DASHBOARD_MODELS_CSV_PATH = NORMALIZED_DIR / "video_dashboard_models.csv"

VIDEO_DASHBOARD_RAW_PROMPT_ROWS_PATH = RAW_DIR / "video_dashboard_prompt_benchmark_rows.json"
VIDEO_DASHBOARD_PROMPT_BENCHMARKS_JSON_PATH = NORMALIZED_DIR / "video_dashboard_prompt_benchmarks.json"
VIDEO_DASHBOARD_PROMPT_BENCHMARKS_CSV_PATH = NORMALIZED_DIR / "video_dashboard_prompt_benchmarks.csv"

VIDEO_DASHBOARD_COVERAGE_REPORT_PATH = ANALYSIS_DIR / "video_dashboard_coverage_report.json"
VIDEO_DASHBOARD_DATA_QUALITY_REPORT_PATH = ANALYSIS_DIR / "video_dashboard_data_quality_report.json"

# The video dashboard's weekly "Model of the week" history. A SEPARATE file from
# the chat pipeline's weekly_picks.json and the image dashboard's
# media_weekly_picks.json: the three lock picks from different rankings over
# different models, so a shared file would let one pipeline's lock silently
# overwrite another's record.
VIDEO_DASHBOARD_WEEKLY_PICKS_PATH = ANALYSIS_DIR / "video_dashboard_weekly_picks.json"

VIDEO_DASHBOARD_SNAPSHOTS_DIR = DATA_DIR / "video_snapshots"

# Total-row crash detector for the video prompt-benchmark scrape. The shared
# MIN_EXPECTED_TOTAL_PROMPT_BENCHMARK_ROWS (200) is deliberately NOT reused: that
# floor is for the COMBINED image+video total (observed 867), while this pipeline
# scrapes only the 12 video prompts (observed 276 rows on 2026-09-22). A floor of
# 150 leaves room for a prompt legitimately disappearing without turning a normal
# run red, and still catches a partial parse. Like the media pipeline there is no
# per-page floor - per-prompt row counts genuinely vary (14 to 24).
MIN_EXPECTED_TOTAL_VIDEO_PROMPT_BENCHMARK_ROWS = 150
