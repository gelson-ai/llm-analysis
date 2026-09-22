"""Tests for dashboard/build_image_dashboard.py.

These exercise the builder's metric logic and its build path only - no network,
no pipeline run, no real data files. Every input is written into tmp_path and
the builder's module-level path constants are monkeypatched, the same way
tests/test_media_snapshot_paths.py monkeypatches config.settings paths.

The metric definitions these lock down are deliberate, so they are asserted
against hand-computed numbers rather than "whatever the code does today":

    pass rate = sum(checks_passed) / sum(checks_total)   (flat, NOT mean of rates)
    price     = mean cost_usd of those same rows         (NOT catalogue pricing)
    value     = pass rate / price
"""
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_DIR = PROJECT_ROOT / "dashboard"
if str(DASHBOARD_DIR) not in sys.path:
    sys.path.insert(0, str(DASHBOARD_DIR))

import build_image_dashboard as builder  # noqa: E402  (needs the path insert above)
import media_picks  # noqa: E402  (its OWN weekly picks module - not the chat one)
import weekly_picks  # noqa: E402  (read-only here, to prove the two weeks agree)

# Anchored relative to the real clock on purpose. check_freshness() compares the
# fixture timestamp against datetime.now(), so a frozen calendar date silently
# turns this suite red once that date is older than the window under assertion
# (72h) - which is exactly what happened on 2026-09-20, breaking the pytest gate
# that publish.yml runs before every deploy. One hour old is comfortably inside a
# 72h window and stays that way. The stale case is still covered explicitly by the
# 30-day offset in test_stale_data_refuses_to_build_and_writes_nothing.
RETRIEVED_AT = datetime.now(timezone.utc) - timedelta(hours=1)


def iso(moment: datetime) -> str:
    return moment.isoformat()


# ---------------------------------------------------------------------------
# fixture builders (mirror the real normalized shapes)
# ---------------------------------------------------------------------------
def model(model_id, name=None, provider=None, has_rate=True, pricing_unit="usd_per_image",
          comparable_price=0.1, pricing_lines=None, retrieved_at=None, **extra):
    lines = pricing_lines
    if lines is None:
        lines = [{
            "billable": "output_image", "unit": "image", "unit_family": "usd_per_image",
            "variant": None, "raw_cost_usd": 0.1, "usd_amount": 0.1,
            "is_negative": False, "is_parseable": True,
        }] if has_rate else []
    record = {
        "model_id": model_id,
        "model_name": name or model_id,
        "provider": provider or model_id.split("/")[0],
        "has_any_price": bool(lines),
        "has_valid_pricing": has_rate,
        "pricing_unit": pricing_unit if has_rate else None,
        "comparable_price": comparable_price if has_rate else None,
        "comparable_price_basis": "plain_output_image_rate" if has_rate else None,
        "supported_resolutions": None,
        "supported_aspect_ratios": None,
        "supported_sizes": None,
        "supported_durations": None,
        "endpoint_pricing": {
            "model_id": model_id,
            "endpoint_count": 1 if lines else 0,
            "endpoints": [{
                "provider_name": "Test Provider",
                "provider_slug": "test",
                "pricing_lines": lines,
                "has_any_price": bool(lines),
            }] if lines else [],
        },
        "retrieved_at": retrieved_at or iso(RETRIEVED_AT),
    }
    record.update(extra)
    return record


def prompt_row(model_id, passed, total, cost, media_type="image", slug="full-glass",
               name=None, cost_present=True, retrieved_at=None):
    return {
        "model_id": model_id,
        "raw_model_slug": f"{model_id}-20260101",
        "model_name": name or model_id,
        "matched": True,
        "media_type": media_type,
        "prompt_slug": slug,
        "prompt_name": slug.replace("-", " ").title(),
        "benchmark_name": f"Media Prompt Benchmark: {media_type}/{slug}",
        "benchmark_source": "OpenRouter Media Benchmarks",
        "source_platform": "OpenRouter",
        "source_url": f"https://openrouter.ai/benchmarks/media/{media_type}s/{slug}",
        "checks_passed": passed,
        "checks_total": total,
        "pass_rate": (passed / total) if total else None,
        "correctness_label": f"{passed}/{total}",
        "cost_usd": cost if cost_present else None,
        "generation_seconds": 8.5,
        "retrieved_at": retrieved_at or iso(RETRIEVED_AT),
    }


def arena_row(model_id, category, elo, win_rate, source="Design Arena", retrieved_at=None):
    return {
        "model_id": model_id,
        "raw_model_ref": model_id,
        "matched": True,
        "benchmark_name": f"Design Arena: models/{category}",
        "score": win_rate,
        "score_unit": "percentage_0_100",
        "benchmark_source": source,
        "source_platform": "OpenRouter",
        "retrieved_at": retrieved_at or iso(RETRIEVED_AT),
        "benchmark_version": None,
        "benchmark_timestamp": None,
        "raw_data": {"score": win_rate, "arena": "models", "category": category,
                     "elo": elo, "win_rate": win_rate, "rank": 2},
        "source_endpoint": "/api/v1/models?output_modalities=image",
    }


def default_dataset():
    """Five catalogue models covering all four coverage buckets.

    alpha/one     priced, benchmarked, two prompts judged at 4 and 5 checks
    beta/two      priced, benchmarked, two prompts judged at 5 checks each
    gamma/three   priced, NOT benchmarked          -> unrated
    delta/four    NO catalogue rate, benchmarked   -> ranked without a rate
    epsilon/five  priced, NOT benchmarked, has Design Arena
    """
    models = [
        model("alpha/one", name="Alpha One", has_rate=True),
        model("beta/two", name="Beta Two", has_rate=True),
        model("gamma/three", name="Gamma Three", has_rate=True),
        model("delta/four", name="Delta Four", has_rate=False, comparable_price=None),
        model("epsilon/five", name="Epsilon Five", has_rate=True),
    ]
    prompt_rows = [
        # alpha: 3/9 checks, mean cost (0.05 + 0.01) / 2 = 0.03
        prompt_row("alpha/one", 2, 5, 0.05, slug="full-glass", name="Alpha Short"),
        prompt_row("alpha/one", 1, 4, 0.01, slug="traffic-light", name="Alpha Short"),
        # beta: 9/10 checks, mean cost 0.10
        prompt_row("beta/two", 5, 5, 0.10, slug="full-glass"),
        prompt_row("beta/two", 4, 5, 0.10, slug="traffic-light"),
        # delta: 4/5 checks, mean cost 0.02 (no catalogue rate at all)
        prompt_row("delta/four", 4, 5, 0.02, slug="full-glass"),
        # a video row that must never be picked up by this builder
        prompt_row("video/model", 3, 5, 0.50, media_type="video", slug="walk-out"),
    ]
    arena_rows = [
        arena_row("alpha/one", "image", 1300, 60.0),
        arena_row("alpha/one", "logo", 1250, 55.0),
        arena_row("epsilon/five", "graphicdesign", 1200, 52.0),
        # A different benchmark family must be ignored, never merged in.
        arena_row("beta/two", "image", 999, 99.0, source="Artificial Analysis"),
    ]
    return models, prompt_rows, arena_rows


def install(tmp_path, monkeypatch, models=None, prompt_rows=None, arena_rows=None,
            snapshots=None, coverage=None, quality=None):
    models, prompt_rows, arena_rows = (
        default_dataset() if models is None else (models, prompt_rows or [], arena_rows or [])
    )
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    def dump(filename, payload):
        path = data_dir / filename
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    paths = {
        "IMAGE_MODELS_PATH": dump("image_models.json", models),
        "PROMPT_BENCHMARKS_PATH": dump("media_prompt_benchmarks.json", list(prompt_rows)),
        "DESIGN_ARENA_PATH": dump("media_benchmarks.json", list(arena_rows)),
        "COVERAGE_PATH": dump(
            "media_coverage_report.json",
            coverage if coverage is not None else {"model_inventory": {"image": {"total_models": len(models)}}},
        ),
        "QUALITY_PATH": dump(
            "media_data_quality_report.json",
            quality if quality is not None else {"issue_count": 0, "error_count": 0, "issues": []},
        ),
        "SNAPSHOTS_DIR": snapshots if snapshots is not None else (data_dir / "media_snapshots"),
        # Deliberately absent unless a test writes it: the payload's "weekly"
        # block must degrade to an empty week rather than fail the build, and
        # pointing this at tmp_path keeps the suite independent of whatever
        # history happens to exist on the machine running it.
        "WEEKLY_PICKS_PATH": data_dir / "media_weekly_picks.json",
    }
    for key, value in paths.items():
        monkeypatch.setattr(builder, key, value)
    return paths


def install_template(tmp_path, monkeypatch,
                     template="<html><body><script>const PAYLOAD=__DATA__;</script>"
                              "<script>__SHELL_JS__</script></body></html>"):
    template_path = tmp_path / "image_template.html"
    template_path.write_text(template, encoding="utf-8")
    output_path = tmp_path / "image_model_analysis.html"
    monkeypatch.setattr(builder, "TEMPLATE_PATH", template_path)
    monkeypatch.setattr(builder, "OUTPUT_PATH", output_path)
    return template_path, output_path


def row_for(payload, model_id):
    return next(row for row in payload["rows"] if row["model_id"] == model_id)


# ---------------------------------------------------------------------------
# performance: one flat pool of checks
# ---------------------------------------------------------------------------
def test_pass_rate_pools_every_check_instead_of_averaging_row_rates():
    """alpha/one: 3 of 9 checks, judged as 2/5 and 1/4.

    The flat aggregate is 3/9 = 0.3333. The unweighted mean of the row rates
    would be (0.4 + 0.25) / 2 = 0.325 - a different number, so this pins down
    which one the builder is required to use.
    """
    performance = builder.aggregate_performance([
        prompt_row("alpha/one", 2, 5, 0.05),
        prompt_row("alpha/one", 1, 4, 0.01),
    ])
    pool = performance["alpha/one"]
    assert pool["checks_passed"] == 3
    assert pool["checks_total"] == 9
    assert pool["pass_rate"] == pytest.approx(3 / 9)
    assert pool["pass_rate"] != pytest.approx((2 / 5 + 1 / 4) / 2)
    assert pool["prompts"] == 2
    assert pool["evidence_checks"] == 9


def test_rows_without_both_check_counts_are_left_out_of_the_pool():
    performance = builder.aggregate_performance([
        prompt_row("alpha/one", 2, 5, 0.05),
        {**prompt_row("alpha/one", 3, 5, 0.05), "checks_total": None},
    ])
    pool = performance["alpha/one"]
    assert (pool["checks_passed"], pool["checks_total"]) == (2, 5)
    assert pool["prompts"] == 1


def test_only_image_rows_are_considered():
    rows = [prompt_row("alpha/one", 2, 5, 0.05), prompt_row("video/model", 3, 5, 0.5, media_type="video")]
    assert [row["model_id"] for row in builder.image_benchmark_rows(rows)] == ["alpha/one"]


# ---------------------------------------------------------------------------
# price: mean of the benchmark rows' own cost
# ---------------------------------------------------------------------------
def test_cost_is_the_mean_of_row_costs_ignoring_missing_values():
    performance = builder.aggregate_performance([
        prompt_row("alpha/one", 2, 5, 0.05),
        prompt_row("alpha/one", 1, 4, 0.01),
    ])
    assert performance["alpha/one"]["avg_cost_usd"] == pytest.approx(0.03)
    assert performance["alpha/one"]["cost_rows"] == 2

    with_missing = builder.aggregate_performance([
        prompt_row("beta/two", 5, 5, 0.10),
        prompt_row("beta/two", 4, 5, 0.0, cost_present=False),
    ])
    assert with_missing["beta/two"]["avg_cost_usd"] == pytest.approx(0.10)
    assert with_missing["beta/two"]["cost_rows"] == 1


def test_cost_is_undefined_when_no_row_carries_one():
    performance = builder.aggregate_performance([prompt_row("alpha/one", 2, 5, 0.0, cost_present=False)])
    assert performance["alpha/one"]["avg_cost_usd"] is None


# ---------------------------------------------------------------------------
# value
# ---------------------------------------------------------------------------
def test_value_score_is_pass_rate_divided_by_benchmark_cost():
    assert builder.value_score(0.9, 0.10) == pytest.approx(9.0)


@pytest.mark.parametrize("pass_rate,cost", [
    (None, 0.10),    # no benchmark rows
    (0.9, None),     # benchmarked but no usable cost
    (0.9, 0.0),      # free: division would be undefined, not infinite
])
def test_value_score_is_undefined_rather_than_guessed(pass_rate, cost):
    assert builder.value_score(pass_rate, cost) is None


def test_value_ranks_only_rankable_models_and_leaves_the_rest_null(tmp_path, monkeypatch):
    install(tmp_path, monkeypatch)
    payload = builder.build_payload()

    ranks = {row["model_id"]: row["value_rank"] for row in payload["rows"]}
    # delta is cheapest at 0.8/0.02 = 40, alpha 11.1, beta 9.0
    assert ranks == {
        "delta/four": 1,
        "alpha/one": 2,
        "beta/two": 3,
        "gamma/three": None,
        "epsilon/five": None,
    }


# ---------------------------------------------------------------------------
# scatter point sizing
# ---------------------------------------------------------------------------
def test_point_radius_is_monotonic_clamped_and_undefined_without_evidence():
    low = builder.point_radius(59, 59, 64)
    middle = builder.point_radius(61, 59, 64)
    high = builder.point_radius(64, 59, 64)
    assert low == builder.POINT_RADIUS_MIN
    assert high == builder.POINT_RADIUS_MAX
    assert low < middle < high
    # Clamped, so an out-of-range value can never overflow the plot.
    assert builder.point_radius(1000, 59, 64) == builder.POINT_RADIUS_MAX
    assert builder.point_radius(0, 59, 64) == builder.POINT_RADIUS_MIN
    # A single-point domain must not divide by zero.
    assert builder.point_radius(59, 59, 59) == pytest.approx(
        (builder.POINT_RADIUS_MIN + builder.POINT_RADIUS_MAX) / 2
    )
    assert builder.point_radius(None, 59, 64) is None


def test_point_radius_uses_attempted_checks_rather_than_prompt_count(tmp_path, monkeypatch):
    install(tmp_path, monkeypatch)
    payload = builder.build_payload()
    by_checks = {row["model_id"]: row for row in payload["rows"] if row["evidence_checks"]}
    biggest = max(by_checks.values(), key=lambda row: row["evidence_checks"])
    smallest = min(by_checks.values(), key=lambda row: row["evidence_checks"])
    assert biggest["point_radius"] == builder.POINT_RADIUS_MAX
    assert smallest["point_radius"] == builder.POINT_RADIUS_MIN
    # alpha and beta both appear on two prompts but attempted different numbers
    # of checks, so prompt count alone could not separate them.
    alpha, beta = row_for(payload, "alpha/one"), row_for(payload, "beta/two")
    assert alpha["prompts"] == beta["prompts"] == 2
    assert alpha["evidence_checks"] != beta["evidence_checks"]
    assert alpha["point_radius"] != beta["point_radius"]


# ---------------------------------------------------------------------------
# coverage: never drop a model
# ---------------------------------------------------------------------------
def test_priced_model_without_benchmark_rows_is_unrated_but_still_present(tmp_path, monkeypatch):
    install(tmp_path, monkeypatch)
    payload = builder.build_payload()

    assert len(payload["rows"]) == 5, "every catalogue model must survive the join"
    unrated = row_for(payload, "gamma/three")
    assert unrated["has_catalog_rate"] is True
    assert unrated["benchmarked"] is False
    assert unrated["value"] is None
    assert unrated["value_rank"] is None
    assert unrated["pass_rate"] is None
    assert unrated["avg_cost_usd"] is None
    assert unrated["point_radius"] is None
    assert "gamma/three" in payload["coverage"]["unrated_ids"]


def test_coverage_counts_the_four_questions_separately(tmp_path, monkeypatch):
    install(tmp_path, monkeypatch)
    coverage = builder.build_payload()["coverage"]

    assert coverage["catalog_models"] == 5
    assert coverage["priced_models"] == 4          # delta/four publishes no rate
    assert coverage["benchmarked_models"] == 3
    assert coverage["rankable_models"] == 3
    assert coverage["unrated_models"] == 2
    assert coverage["unrated_ids"] == ["gamma/three", "epsilon/five"]
    assert coverage["unrated_but_priced_models"] == 2
    assert coverage["benchmarked_unpriced_models"] == 1
    assert coverage["benchmarked_unpriced_ids"] == ["delta/four"]
    assert coverage["benchmarked_unrankable_ids"] == []
    assert coverage["design_arena_models"] == 2
    # delta/four was benchmarked on one prompt, alpha and beta on two each.
    assert coverage["prompt_count_min"] == 1
    assert coverage["prompt_count_max"] == 2


def test_model_benchmarked_without_any_cost_is_listed_as_unrankable(tmp_path, monkeypatch):
    models = [model("alpha/one")]
    rows = [prompt_row("alpha/one", 2, 5, 0.0, cost_present=False)]
    install(tmp_path, monkeypatch, models=models, prompt_rows=rows, arena_rows=[])
    payload = builder.build_payload()

    assert payload["coverage"]["benchmarked_models"] == 1
    assert payload["coverage"]["rankable_models"] == 0
    assert payload["coverage"]["benchmarked_unrankable_ids"] == ["alpha/one"]
    assert row_for(payload, "alpha/one")["value"] is None


def test_payload_reports_every_catalogue_model_exactly_once(tmp_path, monkeypatch):
    install(tmp_path, monkeypatch)
    ids = [row["model_id"] for row in builder.build_payload()["rows"]]
    assert ids == ["alpha/one", "beta/two", "gamma/three", "delta/four", "epsilon/five"]
    assert len(ids) == len(set(ids))


# ---------------------------------------------------------------------------
# design arena
# ---------------------------------------------------------------------------
def test_design_arena_panel_pivots_by_category_and_ignores_other_sources(tmp_path, monkeypatch):
    install(tmp_path, monkeypatch)
    arena = builder.build_payload()["design_arena"]

    assert [category["key"] for category in arena["categories"]] == ["graphicdesign", "image", "logo"]
    assert [category["label"] for category in arena["categories"]] == ["Graphic design", "Image", "Logo"]
    assert [record["model_id"] for record in arena["models"]] == ["alpha/one", "epsilon/five"]
    assert arena["model_count"] == 2
    assert arena["row_count"] == 3

    alpha = next(record for record in arena["models"] if record["model_id"] == "alpha/one")
    assert alpha["categories"]["image"] == {"elo": 1300, "win_rate": 60.0, "score_unit": "percentage_0_100", "rank": 2}
    assert alpha["categories"]["logo"]["elo"] == 1250
    assert "graphicdesign" not in alpha["categories"]

    assert "beta/two" not in {record["model_id"] for record in arena["models"]}, (
        "a row from another benchmark family must not enter the Design Arena panel"
    )


def test_design_arena_is_never_blended_into_the_value_score(tmp_path, monkeypatch):
    """epsilon/five has Design Arena scores but no benchmark rows, so it must
    stay unrated - a preference score is not a capability score."""
    install(tmp_path, monkeypatch)
    payload = builder.build_payload()

    epsilon = row_for(payload, "epsilon/five")
    assert epsilon["design_arena"] is True
    assert epsilon["value"] is None
    assert epsilon["value_rank"] is None
    assert "epsilon/five" in payload["coverage"]["unrated_ids"]


# ---------------------------------------------------------------------------
# budget picks
# ---------------------------------------------------------------------------
def test_budget_selection_respects_the_threshold_and_sorts_by_cost(tmp_path, monkeypatch):
    install(tmp_path, monkeypatch)
    rows = builder.build_payload()["rows"]

    picks = builder.budget_models(rows, min_pass_rate=0.5, top_n=5)
    # beta 90%, delta 80% and alpha 33% - alpha is below the cut.
    assert [row["model_id"] for row in picks] == ["delta/four", "beta/two"]

    cheapest_first = builder.budget_models(rows, min_pass_rate=0.7, top_n=1)
    assert [row["model_id"] for row in cheapest_first] == ["delta/four"]


def test_budget_selection_is_empty_when_nothing_clears_the_threshold(tmp_path, monkeypatch):
    install(tmp_path, monkeypatch)
    rows = builder.build_payload()["rows"]
    assert builder.budget_models(rows, min_pass_rate=0.99, top_n=5) == []


def test_budget_uses_the_documented_default_threshold(tmp_path, monkeypatch):
    install(tmp_path, monkeypatch)
    payload = builder.build_payload()
    assert payload["budget"]["min_pass_rate"] == builder.BUDGET_MIN_PASS_RATE
    assert payload["budget"]["top_n"] == builder.BUDGET_TOP_N
    assert payload["budget"]["model_ids"] == [row["model_id"] for row in builder.budget_models(payload["rows"])]
    assert set(payload["budget"]["model_ids"]).issubset(
        {row["model_id"] for row in payload["rows"] if row["value"] is not None}
    )


# ---------------------------------------------------------------------------
# nothing fabricated
# ---------------------------------------------------------------------------
def test_payload_carries_no_artificial_analysis_latency_or_uptime_data(tmp_path, monkeypatch):
    install(tmp_path, monkeypatch)
    payload = builder.build_payload()

    # The disclaimer prose is allowed to *name* what it disclaims. No
    # data-bearing part of the payload may carry any of it.
    data_sections = {key: value for key, value in payload.items() if key != "constants"}
    serialized = json.dumps(data_sections).lower()
    for absent in ("artificial analysis", "aa intelligence", "aa coding", "aa agentic",
                   "uptime", "latency", "p50", "p95"):
        assert absent not in serialized, f"{absent!r} must not appear in any data section of the payload"

    # ...and the provenance note must explicitly state the absence, so the
    # page can never be read as implying Artificial Analysis coverage.
    assert "artificial analysis" in payload["constants"]["provenance_note"].lower()
    assert "never folded into the value score" in payload["constants"]["provenance_note"]

    for row in payload["rows"]:
        assert "benchmark_source" not in row
    assert payload["design_arena"]["source"] == "Design Arena"


def test_metadata_the_image_catalogue_does_not_publish_is_carried_as_null(tmp_path, monkeypatch):
    install(tmp_path, monkeypatch)
    row = row_for(builder.build_payload(), "alpha/one")
    assert row["resolutions"] is None
    assert row["aspect_ratios"] is None
    assert row["sizes"] is None
    assert row["durations"] is None


def test_design_arena_summary_picks_one_winner_per_category_and_reports_its_margin():
    panel = builder.build_design_arena_panel([
        arena_row("alpha/one", "logo", elo=1200, win_rate=55.0),
        arena_row("beta/two", "logo", elo=1250, win_rate=58.0),
        # Same Elo as beta/two, higher win rate - the documented tie-break.
        arena_row("gamma/three", "logo", elo=1250, win_rate=61.0),
        arena_row("alpha/one", "image", elo=1100, win_rate=50.0),
    ])
    summary = {entry["category"]: entry for entry in builder.build_design_arena_summary(panel)}

    assert set(summary) == {"image", "logo"}

    logo = summary["logo"]
    assert logo["model_id"] == "gamma/three", "higher win rate must break an Elo tie"
    assert logo["elo"] == 1250
    assert logo["win_rate"] == 61.0
    assert logo["models_with_a_score"] == 3
    assert logo["elo_spread"] == 50
    assert logo["lead_over_second"] == 0, "a tie is a 0-Elo lead, not a missing value"

    # A category with a single scored model has no second place to lead.
    assert summary["image"]["lead_over_second"] is None
    assert summary["image"]["models_with_a_score"] == 1


def test_design_arena_summary_skips_categories_nobody_was_scored_in():
    """A category with no scores must produce no row, rather than a row of
    blanks that reads like a result somebody achieved."""
    panel = builder.build_design_arena_panel([arena_row("alpha/one", "logo", elo=1200, win_rate=55.0)])
    panel["categories"].append({"key": "graphicdesign", "label": "Graphic design"})

    summary = builder.build_design_arena_summary(panel)
    assert [entry["category"] for entry in summary] == ["logo"]


def test_design_arena_summary_is_deterministic_for_identical_scores():
    """Two models with identical Elo, win rate and rank must resolve the same way
    every run, or the published leader could flip without any data changing."""
    rows = [arena_row("zeta/last", "logo", elo=1300, win_rate=60.0),
            arena_row("alpha/first", "logo", elo=1300, win_rate=60.0)]
    first = builder.build_design_arena_summary(builder.build_design_arena_panel(rows))
    second = builder.build_design_arena_summary(builder.build_design_arena_panel(list(reversed(rows))))
    assert first[0]["model_id"] == second[0]["model_id"] == "alpha/first"


def test_design_arena_summary_ignores_other_benchmark_sources():
    panel = builder.build_design_arena_panel([
        arena_row("alpha/one", "logo", elo=1200, win_rate=55.0),
        arena_row("beta/two", "logo", elo=1900, win_rate=90.0, source="Some Other Arena"),
    ])
    summary = builder.build_design_arena_summary(panel)
    assert summary[0]["model_id"] == "alpha/one"
    assert summary[0]["models_with_a_score"] == 1


# ---------------------------------------------------------------------------
# freshness gate + build path
# ---------------------------------------------------------------------------
def test_stale_data_refuses_to_build_and_writes_nothing(tmp_path, monkeypatch, capsys):
    old = iso(RETRIEVED_AT - timedelta(days=30))
    install(tmp_path, monkeypatch,
            models=[model("alpha/one", retrieved_at=old)],
            prompt_rows=[prompt_row("alpha/one", 2, 5, 0.05, retrieved_at=old)],
            arena_rows=[])
    _, output_path = install_template(tmp_path, monkeypatch)

    ok, message = builder.check_freshness(72)
    assert ok is False
    assert "freshness window" in message

    monkeypatch.setattr(sys, "argv", ["build_image_dashboard.py"])
    assert builder.main() == 1
    assert not output_path.exists()
    assert "STALE" in capsys.readouterr().err


def test_missing_image_models_refuses_to_build(tmp_path, monkeypatch):
    install(tmp_path, monkeypatch)
    monkeypatch.setattr(builder, "IMAGE_MODELS_PATH", tmp_path / "nope.json")
    ok, message = builder.check_freshness(72)
    assert ok is False
    assert "does not exist" in message


def test_empty_catalogue_refuses_to_build(tmp_path, monkeypatch, capsys):
    install(tmp_path, monkeypatch, models=[], prompt_rows=[], arena_rows=[])
    _, output_path = install_template(tmp_path, monkeypatch)

    monkeypatch.setattr(sys, "argv", ["build_image_dashboard.py"])
    assert builder.main() == 1
    assert not output_path.exists()
    assert "empty" in capsys.readouterr().err


def test_freshness_reports_the_age_of_the_media_snapshot(tmp_path, monkeypatch):
    install(tmp_path, monkeypatch)
    ok, message = builder.check_freshness(72)
    assert ok is True
    assert "retrieved_at=" in message


def test_render_substitutes_the_data_placeholder_with_parseable_json(tmp_path, monkeypatch):
    install(tmp_path, monkeypatch)
    _, output_path = install_template(
        tmp_path, monkeypatch,
        template="<html><body><script>const PAYLOAD=__DATA__;</script>"
                 "<script>__SHELL_JS__</script></body></html>",
    )
    payload = builder.build_payload()
    written = builder.render(payload)

    assert written == output_path
    html = output_path.read_text(encoding="utf-8")
    assert "__DATA__" not in html
    injected = html.split("const PAYLOAD=", 1)[1].split(";</script>", 1)[0]
    parsed = json.loads(injected)
    assert parsed["coverage"]["catalog_models"] == 5
    assert parsed["rows"][0]["model_id"] == "alpha/one"


def test_render_leaves_no_temporary_file_behind(tmp_path, monkeypatch):
    install(tmp_path, monkeypatch)
    _, output_path = install_template(tmp_path, monkeypatch)
    builder.render(builder.build_payload())

    assert output_path.exists()
    assert list(output_path.parent.glob("*.tmp")) == []


def test_main_writes_the_dashboard_and_reports_the_coverage_buckets(tmp_path, monkeypatch, capsys):
    install(tmp_path, monkeypatch)
    _, output_path = install_template(tmp_path, monkeypatch)

    monkeypatch.setattr(sys, "argv", ["build_image_dashboard.py"])
    assert builder.main() == 0

    output = capsys.readouterr().out
    assert output_path.exists()
    assert "52 catalogue" not in output  # this fixture has five models, not the real catalogue
    assert "5 catalogue / 4 with a catalogue rate / 3 benchmarked / 3 rankable / 2 unrated" in output


def test_payload_reports_the_timestamps_of_each_input(tmp_path, monkeypatch):
    install(tmp_path, monkeypatch)
    payload = builder.build_payload()
    assert payload["data_retrieved_at"] == iso(RETRIEVED_AT)
    assert payload["benchmarks_retrieved_at"] == iso(RETRIEVED_AT)
    assert payload["design_arena_retrieved_at"] == iso(RETRIEVED_AT)
    assert payload["generated_at"] is not None


# ---------------------------------------------------------------------------
# weekly "Model of the week"
#
# The pick is LOCKED by refresh_media.py before the build, so these tests are
# about the builder *carrying* the locked record (and degrading safely when
# there is none) - not about choosing it. The choice itself is tested in
# tests/test_media_picks.py.
# ---------------------------------------------------------------------------
def test_a_week_with_no_history_still_renders_its_date_range(tmp_path, monkeypatch):
    """The hero shows the week range from the first refresh of the week, before
    any pick has been locked. An empty range would leave the card looking
    broken for the whole first week."""
    install(tmp_path, monkeypatch)  # WEEKLY_PICKS_PATH is absent inside tmp_path
    weekly = builder.build_payload()["weekly"]

    assert weekly["picks"] == {}
    assert weekly["week_key"]
    assert weekly["iso_week"]
    assert re.fullmatch(r"Mon \d{1,2} \w{3} - Sun \d{1,2} \w{3} \d{4}", weekly["week_label"])


def test_the_week_range_is_the_monday_to_sunday_manila_week_being_rendered(tmp_path, monkeypatch):
    install(tmp_path, monkeypatch)
    weekly = builder.build_payload()["weekly"]

    monday, sunday = weekly_picks.week_bounds(datetime.now(timezone.utc))
    assert weekly["week_key"] == weekly_picks.week_key(monday, sunday)
    assert weekly["timezone"] == "Asia/Manila"
    # Monday 21 Sep - Sunday 27 Sep 2026: the two day numbers are 6 days apart.
    day_numbers = [int(part) for part in re.findall(r"\d{1,2}(?= \w{3})", weekly["week_label"])]
    assert len(day_numbers) == 2
    assert (sunday - monday).days == 6


def test_payload_carries_the_locked_pick_from_the_media_history_file(tmp_path, monkeypatch):
    paths = install(tmp_path, monkeypatch)
    history, summary = media_picks.record_pick(
        media_picks.empty_history(), builder.build_payload(), source="test"
    )
    media_picks.save_history(history, paths["WEEKLY_PICKS_PATH"])

    weekly = builder.build_payload()["weekly"]
    pick = weekly["picks"]["value"]
    assert summary["state"] == "locked"
    assert pick["model_id"] == summary["pick"]["model_id"]
    assert pick["locked_by"] == "test"
    assert pick["recorded_label"]
    assert weekly["history_weeks"] == 1


def test_the_locked_pick_is_the_rank_one_model_the_page_prints(tmp_path, monkeypatch):
    """The two rankings are computed in different modules. If they ever
    disagreed, the page would print a #1 and label a different model as the
    week's winner."""
    install(tmp_path, monkeypatch)
    payload = builder.build_payload()
    _, summary = media_picks.record_pick(media_picks.empty_history(), payload, source="test")

    rank_one = next(row for row in payload["rows"] if row["value_rank"] == 1)
    assert summary["pick"]["model_id"] == rank_one["model_id"]


def test_a_tie_is_broken_the_same_way_by_both_rankings(tmp_path, monkeypatch):
    """Equal value is broken by model id ascending. Two models at exactly the
    same pass rate and cost is the case where a divergent tie-break would show
    up as a pick that is not the page's #1."""
    models = [model("zeta/tie", name="Zeta Tie"), model("alpha/tie", name="Alpha Tie")]
    prompts = [prompt_row("zeta/tie", 5, 5, 0.10), prompt_row("alpha/tie", 5, 5, 0.10)]
    install(tmp_path, monkeypatch, models=models, prompt_rows=prompts, arena_rows=[])

    payload = builder.build_payload()
    rows = {row["model_id"]: row for row in payload["rows"]}
    assert rows["zeta/tie"]["value"] == rows["alpha/tie"]["value"]  # a real tie
    assert rows["alpha/tie"]["value_rank"] == 1

    _, summary = media_picks.record_pick(media_picks.empty_history(), payload, source="test")
    assert summary["pick"]["model_id"] == "alpha/tie"


def test_a_corrupt_history_is_ignored_rather_than_failing_the_build(tmp_path, monkeypatch):
    """Losing the history must not stop the dashboard being rebuilt - the same
    promise load_history() makes for the chat side."""
    paths = install(tmp_path, monkeypatch)
    paths["WEEKLY_PICKS_PATH"].write_text("{not json", encoding="utf-8")

    weekly = builder.build_payload()["weekly"]
    assert weekly["picks"] == {}
    assert weekly["history_weeks"] == 0


# ---------------------------------------------------------------------------
# trend placeholder
# ---------------------------------------------------------------------------
def test_trend_state_counts_only_dated_snapshot_directories(tmp_path, monkeypatch):
    snapshots_root = tmp_path / "media_snapshots"
    (snapshots_root / "2026-09-17").mkdir(parents=True)
    (snapshots_root / "2026-09-17" / "media_prompt_benchmarks.json").write_text("[]", encoding="utf-8")
    (snapshots_root / "2026-09-24").mkdir(parents=True)  # no rows file -> not a usable point
    (snapshots_root / "stray.txt").write_text("not a snapshot", encoding="utf-8")
    install(tmp_path, monkeypatch, snapshots=snapshots_root)

    trend = builder.trend_state()
    assert trend["snapshot_dates"] == ["2026-09-17"]
    assert trend["points"] == 1
    assert trend["min_points"] == builder.MIN_TREND_POINTS
    assert trend["ready"] is False


def test_trend_state_becomes_ready_with_two_snapshots(tmp_path, monkeypatch):
    snapshots_root = tmp_path / "media_snapshots"
    for date in ("2026-09-17", "2026-09-24"):
        (snapshots_root / date).mkdir(parents=True)
        (snapshots_root / date / "media_prompt_benchmarks.json").write_text("[]", encoding="utf-8")
    install(tmp_path, monkeypatch, snapshots=snapshots_root)

    assert builder.trend_state() == {
        "snapshot_dates": ["2026-09-17", "2026-09-24"],
        "points": 2,
        "min_points": builder.MIN_TREND_POINTS,
        "ready": True,
    }


def test_trend_state_is_empty_but_safe_when_no_snapshots_exist(tmp_path, monkeypatch):
    install(tmp_path, monkeypatch, snapshots=tmp_path / "missing")
    trend = builder.trend_state()
    assert trend["points"] == 0
    assert trend["ready"] is False
