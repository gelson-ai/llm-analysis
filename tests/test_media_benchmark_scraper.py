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


@pytest.fixture
def full_glass_html():
    """Real IMAGE benchmark page markup, captured 2026-09-22, including a
    trimmed excerpt of the page's embedded per-asset payload."""
    return (FIXTURES_DIR / "media_benchmark_page_images_full_glass.html").read_text(encoding="utf-8")


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
# Additive enrichment from the embedded per-asset payload
# ---------------------------------------------------------------------------
def test_asset_payload_yields_resolution_and_duration(full_glass_html):
    """The rendered markup carries NO output resolution, and its generation time
    is a rounded badge. Both come from the page's embedded payload, which is
    located BY KEY rather than by position - the RSC envelope around the objects
    is not a contract."""
    assets = scraper.parse_asset_payload(full_glass_html)

    assert assets, "the fixture's payload excerpt must parse"
    entry = assets["black-forest-labs/flux.2-flex"]
    assert entry["output_width"] == 1024
    assert entry["output_height"] == 1024
    assert entry["duration_ms"] == 10624
    assert entry["asset_media_type"] == "image/jpeg"
    assert entry["thumbnail_url"].endswith("thumb-0.webp")
    assert entry["asset_count"] == 1


def test_payload_keys_are_date_stripped_so_markup_and_payload_agree(full_glass_html):
    """The markup always links the DATED slug while an image payload uses the
    undated one; comparing stripped forms is what lets the two join at all. This
    joins on a model whose markup slug really does carry a date."""
    assets = scraper.parse_asset_payload(full_glass_html)
    rows = _rows_by_slug(scraper.parse_prompt_page(full_glass_html))
    row = rows["black-forest-labs/flux.2-flex"]

    assert row["raw_model_slug"] == "black-forest-labs/flux.2-flex"
    assert scraper.strip_release_date_suffix(row["raw_model_slug"]) in assets


def test_rows_gain_the_payload_fields_without_losing_the_markup_values(full_glass_html):
    """The two sources are JOINED, not substituted: the judged pass counts stay
    authoritative from the markup (score.checks is empty on some pages, so a
    payload-only parser would lose them), and the markup's own cost and time are
    untouched."""
    row = _rows_by_slug(scraper.parse_prompt_page(full_glass_html))["black-forest-labs/flux.2-max"]

    assert row["checks_source"] == "aria-label"
    assert row["checks_passed"] == 3 and row["checks_total"] == 4
    assert row["cost_usd"] == pytest.approx(0.07)
    assert row["generation_seconds"] == pytest.approx(16.5)
    # ... and the payload fields on top.
    assert (row["output_width"], row["output_height"]) == (1024, 1024)
    assert row["duration_ms"] == 16462
    assert row["asset_media_type"] == "image/jpeg"
    assert row["asset_count"] == 1


def test_the_two_time_sources_agree_within_the_badge_rounding(full_glass_html):
    """The visible badge and the payload measure the same thing, so they are a
    free cross-check on each other. Worth keeping: if they ever diverge by more
    than the badge's rounding, one of the two parsers has started reading a
    different number."""
    for row in scraper.parse_prompt_page(full_glass_html):
        assert row["duration_ms"] is not None and row["generation_seconds"] is not None
        assert row["duration_ms"] / 1000 == pytest.approx(row["generation_seconds"], abs=0.1)


def test_payload_enrichment_is_optional_not_required(traffic_light_html):
    """The saved video fixture has no RSC payload (it was trimmed to markup), so
    this asserts the documented contract: markup-only pages still parse, and the
    payload fields are simply absent rather than fabricated."""
    rows = scraper.parse_prompt_page(traffic_light_html)

    assert len(rows) == 3
    assert "output_width" not in rows[0]
    assert rows[0]["checks_passed"] == 5


def test_a_payload_with_rows_but_no_parseable_asset_raises():
    """Loud, not silent. If the payload lists costUsd but nothing can be read
    out of it, that can only mean the payload shape changed - and continuing
    would quietly drop resolution and generation time from every page."""
    broken = ('<script>self.__next_f.push([1,"{&quot;costUsd&quot;:1}"])</script>'
              .replace("&quot;", '\\"'))
    with pytest.raises(scraper.MediaBenchmarkParseError):
        scraper.parse_asset_payload(broken)


def test_a_page_with_no_payload_at_all_returns_nothing_quietly():
    """Absent is legitimate (an older page, or a trimmed fixture); broken is not."""
    assert scraper.parse_asset_payload("<html><body><li>x</li></body></html>") == {}


# ---------------------------------------------------------------------------
# Telling an unjudged page apart from a markup change
# ---------------------------------------------------------------------------
def test_page_publishes_judged_checks_distinguishes_the_two_empty_results(full_glass_html):
    """Both cases yield zero parsed rows, and the caller's correct reaction to
    each is opposite: skip an unjudged page, but raise on a page that publishes
    checks we failed to read."""
    assert scraper.page_publishes_judged_checks(full_glass_html) is True

    # Real shape of /benchmarks/media/images/portraits (live 2026-09-22): result
    # rows and assets, but the string "checks" nowhere in a 1.6 MB document.
    unjudged = ('<html><body>' + '<li href="/a/one"><img src="/x.webp"></li>' * 3 + '</body></html>')
    assert scraper.page_publishes_judged_checks(unjudged) is False
    assert scraper.parse_prompt_page(unjudged) == []

    # The fallback contract counts too - a page using the visible badge is still
    # publishing judgements, so an empty parse there IS a defect.
    assert scraper.page_publishes_judged_checks('<li><span>2/5</span></li>') is True


def test_a_skipped_page_is_reported_not_just_logged():
    """The pipeline skips an unjudged page rather than failing, which is only
    acceptable because the omission is recorded in both reports."""
    from src import media_coverage

    stats = {
        "rows": 867,
        "pages_fetched": 28,
        "models_matched_to_inventory": 66,
        "pages_skipped": [{"page": "/benchmarks/media/images/portraits",
                           "reason": "no_judged_checks", "row_blocks": 192}],
    }

    coverage = media_coverage._prompt_benchmark_coverage(stats)
    assert coverage["pages_skipped_count"] == 1
    assert coverage["pages_skipped"][0]["page"].endswith("portraits")

    report = media_coverage.build_media_data_quality_report({}, {}, [], prompt_benchmark_stats=stats)
    issues = [i for i in report["issues"] if i["issue"] == "prompt_benchmark_page_skipped"]
    assert len(issues) == 1
    assert issues[0]["severity"] == "info"
    assert "no judged pass count" in issues[0]["detail"]


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
