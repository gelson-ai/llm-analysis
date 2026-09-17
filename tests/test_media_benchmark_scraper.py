"""Tests for the media prompt-benchmark scraper.

Offline only. The HTML fixture is REAL markup captured from
https://openrouter.ai/benchmarks/media/videos/traffic-light on 2026-09-17
(three models, each with both rendered copies of its row), because the whole
risk in this module is markup drift - a hand-written fixture would prove
nothing.

Fixtures are defined here rather than in conftest.py so no pre-existing test
file is touched.
"""
from pathlib import Path

import pytest

from src import media_benchmark_scraper as scraper

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def traffic_light_html():
    return (FIXTURES_DIR / "media_benchmark_page_videos_traffic_light.html").read_text(encoding="utf-8")


def _rows_by_slug(rows):
    return {row["raw_model_slug"]: row for row in rows}


# ---------------------------------------------------------------------------
# Row parsing, against real markup
# ---------------------------------------------------------------------------
def test_parse_real_page_yields_one_row_per_model(traffic_light_html):
    rows = scraper.parse_prompt_page(traffic_light_html)

    assert len(rows) == 3
    assert all(row["checks_source"] == "aria-label" for row in rows)


def test_parse_extracts_all_three_measurements(traffic_light_html):
    rows = _rows_by_slug(scraper.parse_prompt_page(traffic_light_html))

    happyhorse = rows["alibaba/happyhorse-1.1-20260624"]
    assert happyhorse["checks_passed"] == 5
    assert happyhorse["checks_total"] == 5
    assert happyhorse["cost_usd"] == pytest.approx(0.79)
    assert happyhorse["generation_seconds"] == pytest.approx(134.9)
    assert happyhorse["model_name"] == "HappyHorse 1.1"


def test_parse_deduplicates_the_two_rendered_copies(traffic_light_html):
    """Every row is rendered twice on the live page; both copies must collapse
    into one row, and their agreement is recorded."""
    rows = scraper.parse_prompt_page(traffic_light_html)

    assert all(row["duplicate_blocks"] == 2 for row in rows)
    assert not any(row["conflicting_values"] for row in rows)


def test_parse_ignores_non_model_links(traffic_light_html):
    """Nav/footer links such as /benchmarks/media/videos/<slug> must not be
    mistaken for models."""
    slugs = {row["raw_model_slug"] for row in scraper.parse_prompt_page(traffic_light_html)}

    assert not any(slug.startswith("benchmarks/") for slug in slugs)
    assert all("/" in slug for slug in slugs)


def test_parse_returns_empty_for_a_page_without_rows():
    assert scraper.parse_prompt_page("<html><body><p>no results</p></body></html>") == []


def test_parse_clips_cost_and_time_to_that_row():
    """The three metrics must come from the SAME row, not be pooled across the
    page - pooling was the obvious failure mode for this markup."""
    html = """
    <li href="/a/one-20260101"><span aria-label="5 of 5 checks passed">5/5</span>$1.50<span>10.0s</span></li>
    <li href="/b/two-20260102"><span aria-label="1 of 5 checks passed">1/5</span>$9.99<span>99.9s</span></li>
    """
    rows = _rows_by_slug(scraper.parse_prompt_page(html))

    assert rows["b/two-20260102"]["cost_usd"] == pytest.approx(9.99)
    assert rows["b/two-20260102"]["generation_seconds"] == pytest.approx(99.9)
    assert rows["b/two-20260102"]["checks_passed"] == 1
    assert rows["a/one-20260101"]["cost_usd"] == pytest.approx(1.50)


# ---------------------------------------------------------------------------
# Degradation paths - each must be loud or explicit, never silent
# ---------------------------------------------------------------------------
def test_parse_falls_back_to_visible_text_when_aria_label_is_missing(traffic_light_html):
    """The aria-label is the preferred contract; the visible 'N/M' badge is the
    documented fallback, and which one was used is recorded on every row."""
    stripped = traffic_light_html.replace('aria-label="5 of 5 checks passed"', 'data-x="gone"')
    rows = scraper.parse_prompt_page(stripped)

    assert rows, "fallback should still yield rows"
    assert any(row["checks_source"] == "visible_text" for row in rows)


def test_parse_flags_disagreeing_duplicate_copies():
    """If the two copies of a row disagree, keep the higher pass count but set
    conflicting_values so the caller can report it rather than silently pick."""
    html = """
    <li href="/a/one-20260101"><span aria-label="2 of 5 checks passed">2/5</span>$1.00<span>5.0s</span></li>
    <li href="/a/one-20260101"><span aria-label="5 of 5 checks passed">5/5</span>$1.00<span>5.0s</span></li>
    """
    rows = scraper.parse_prompt_page(html)

    assert len(rows) == 1
    assert rows[0]["conflicting_values"] is True
    assert rows[0]["checks_passed"] == 5
    assert rows[0]["duplicate_blocks"] == 2


def test_parse_tolerates_a_missing_cost_or_time():
    html = '<li href="/a/one-20260101"><span aria-label="3 of 5 checks passed">3/5</span></li>'
    rows = scraper.parse_prompt_page(html)

    assert rows[0]["cost_usd"] is None
    assert rows[0]["generation_seconds"] is None
    assert rows[0]["checks_passed"] == 3


# ---------------------------------------------------------------------------
# Prompt discovery
# ---------------------------------------------------------------------------
def test_discover_prompt_slugs_reads_the_plural_path():
    """Regression: the URL path is plural (/media/videos) while the media_type
    key is singular, which silently discovered nothing."""
    html = ('<a href="/benchmarks/media/videos/traffic-light">x</a>'
            '<a href="/benchmarks/media/videos/five-candles">y</a>'
            '<a href="/benchmarks/media/images/full-glass">z</a>')

    assert scraper.discover_prompt_slugs(html, "video") == ["traffic-light", "five-candles"]
    assert scraper.discover_prompt_slugs(html, "image") == ["full-glass"]


def test_discover_prompt_slugs_deduplicates_and_preserves_order():
    html = ('<a href="/benchmarks/media/images/full-glass">1</a>'
            '<a href="/benchmarks/media/images/mirror">2</a>'
            '<a href="/benchmarks/media/images/full-glass">3</a>')

    assert scraper.discover_prompt_slugs(html, "image") == ["full-glass", "mirror"]


def test_discover_prompt_slugs_returns_empty_for_unknown_media_type():
    assert scraper.discover_prompt_slugs("<html></html>", "audio") == []


# ---------------------------------------------------------------------------
# Normalized record shape
# ---------------------------------------------------------------------------
def test_normalize_row_builds_a_comparable_record():
    row = {"raw_model_slug": "alibaba/wan-3.0-20260824", "model_name": "Wan 3.0",
           "checks_passed": 4, "checks_total": 5, "cost_usd": 0.68,
           "generation_seconds": 345.6, "checks_source": "aria-label",
           "duplicate_blocks": 2, "conflicting_values": False}

    record = scraper.normalize_prompt_benchmark_row(
        row, media_type="video", prompt_slug="traffic-light", prompt_name="Traffic Light",
        source_url="https://openrouter.ai/benchmarks/media/videos/traffic-light",
        retrieved_at="2026-09-17T00:00:00Z", model_id="alibaba/wan-3.0",
    )

    assert record["model_id"] == "alibaba/wan-3.0"
    assert record["matched"] is True
    assert record["benchmark_name"] == "Media Prompt Benchmark: video/traffic-light"
    assert record["correctness_label"] == "4/5"
    assert record["pass_rate"] == pytest.approx(0.8)
    assert record["cost_usd"] == pytest.approx(0.68)
    assert record["generation_seconds"] == pytest.approx(345.6)
    assert record["benchmark_source"] == scraper.BENCHMARK_SOURCE
    assert record["raw_data"]["checks_source"] == "aria-label"


def test_normalize_row_keeps_unmatched_rows_with_a_null_model_id():
    """An unmatched row is still written, flagged - never silently dropped."""
    record = scraper.normalize_prompt_benchmark_row(
        {"raw_model_slug": "x/y-20260101", "checks_passed": 1, "checks_total": 5},
        media_type="image", prompt_slug="mirror", prompt_name="Mirror",
        source_url="u", retrieved_at="t", model_id=None,
    )

    assert record["model_id"] is None
    assert record["matched"] is False
    assert record["correctness_label"] == "1/5"


def test_normalize_row_handles_missing_counts_without_inventing_a_rate():
    record = scraper.normalize_prompt_benchmark_row(
        {"raw_model_slug": "x/y-20260101", "checks_passed": None, "checks_total": None},
        media_type="image", prompt_slug="mirror", prompt_name="Mirror",
        source_url="u", retrieved_at="t", model_id=None,
    )

    assert record["pass_rate"] is None
    assert record["correctness_label"] is None


# ---------------------------------------------------------------------------
# Slug matching helper
# ---------------------------------------------------------------------------
def test_strip_release_date_suffix():
    assert scraper.strip_release_date_suffix("alibaba/wan-3.0-20260824") == "alibaba/wan-3.0"
    assert scraper.strip_release_date_suffix("openai/gpt-image-2") == "openai/gpt-image-2"
    assert scraper.strip_release_date_suffix("") == ""


def test_prompt_name_from_slug():
    assert scraper.prompt_name_from_slug("traffic-light") == "Traffic Light"
    assert scraper.prompt_name_from_slug("full-glass") == "Full Glass"
