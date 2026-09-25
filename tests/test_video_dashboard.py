"""Behaviour tests for the VIDEO dashboard builder.

These exercise the payload and its metric rules against small fixtures - the same
style as tests/test_image_dashboard.py - so they need no network and no real
pipeline run.

The template-level half of the "missing rate never renders as $0.00" rule is
asserted in tests/test_video_dashboard_invariants.py, because it is a property of
the template rather than of the data.
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_DIR = PROJECT_ROOT / "dashboard"
for _path in (str(PROJECT_ROOT), str(DASHBOARD_DIR)):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import build_video_dashboard as builder  # noqa: E402
import video_picks  # noqa: E402
from src import video_coverage  # noqa: E402

RETRIEVED_AT = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()


def model(model_id, *, rate=None, basis=None, units=None, durations=None,
          audio=None, name=None, provider="acme", pricing_unit=None):
    """One normalized catalogue record, shaped like the video pipeline's output."""
    return {
        "model_id": model_id,
        "model_name": name or model_id,
        "provider": provider,
        "model_type": "video",
        "has_any_price": pricing_unit is not None or bool(units),
        "has_valid_pricing": rate is not None,
        "pricing_unit": pricing_unit or ("usd_per_second" if rate is not None else None),
        "comparable_price": rate,
        "comparable_price_basis": basis,
        "pricing": {"unit_families": units or [], "raw_pricing_skus": {}},
        "supported_resolutions": ["720p"] if durations else None,
        "supported_durations": durations,
        "supported_aspect_ratios": None,
        "supported_sizes": None,
        "supported_frame_images": None,
        "generate_audio": audio,
        "seed": None,
        "upscale_factor": None,
        "retrieved_at": RETRIEVED_AT,
    }


def prompt_row(model_id, slug, passed, total, cost, *, width=1280, height=720, seconds=None):
    return {
        "model_id": model_id,
        "model_name": model_id,
        "model_type": "video",
        "media_type": "video",
        "prompt_slug": slug,
        "prompt_name": slug.replace("-", " ").title(),
        "matched": True,
        "checks_passed": passed,
        "checks_total": total,
        "pass_rate": passed / total if total else None,
        "cost_usd": cost,
        "generation_seconds": seconds,
        "output_width": width,
        "output_height": height,
        "duration_ms": int(seconds * 1000) if seconds is not None else None,
        "retrieved_at": RETRIEVED_AT,
        "raw_data": {"checks_source": "aria-label", "duplicate_conflicts": False},
    }


def strong(model_id, *, rate=0.10, checks=5, prompts=10, cost=1.0):
    """A model with enough evidence to clear the gate (>= 8 prompts, >= 40 checks)."""
    return [prompt_row(model_id, f"p{i}", checks, checks, cost) for i in range(prompts)]


def install(tmp_path, monkeypatch, models, rows, *, coverage=None, quality=None):
    """Point the builder at fixtures instead of the real pipeline outputs."""
    models_path = tmp_path / "video_dashboard_models.json"
    rows_path = tmp_path / "video_dashboard_prompt_benchmarks.json"
    coverage_path = tmp_path / "video_dashboard_coverage_report.json"
    quality_path = tmp_path / "video_dashboard_data_quality_report.json"
    picks_path = tmp_path / "video_dashboard_weekly_picks.json"

    for path, payload in (
        (models_path, models),
        (rows_path, rows),
        (coverage_path, coverage if coverage is not None else {}),
        (quality_path, quality if quality is not None else {}),
    ):
        path.write_text(json.dumps(payload), encoding="utf-8")

    monkeypatch.setattr(builder, "VIDEO_MODELS_PATH", models_path)
    monkeypatch.setattr(builder, "PROMPT_BENCHMARKS_PATH", rows_path)
    monkeypatch.setattr(builder, "COVERAGE_PATH", coverage_path)
    monkeypatch.setattr(builder, "QUALITY_PATH", quality_path)
    monkeypatch.setattr(builder, "WEEKLY_PICKS_PATH", picks_path)
    monkeypatch.setattr(video_picks.settings, "VIDEO_DASHBOARD_WEEKLY_PICKS_PATH", picks_path)
    return models_path


# ---------------------------------------------------------------------------
# the missing-catalogue-rate rule
# ---------------------------------------------------------------------------
def test_a_model_with_no_catalogue_rate_is_never_given_a_zero_rate():
    """The rule the hero card, tables and tooltips depend on: a missing rate is
    None (rendered as an em dash with a reason), never 0.0."""
    rows = builder.build_model_rows(
        [model("seedance/x", units=["usd_per_video_token"], durations=[5, 10])],
        video_coverage.pool_prompt_benchmarks(strong("seedance/x")),
    )
    row = rows[0]

    assert row["has_catalog_rate"] is False
    assert row["comparable_price"] is None
    assert row["cost_per_5s_clip"] is None
    assert row["rate_absence_label"] == "per-token pricing only"


@pytest.mark.parametrize("units,expected", [
    (["usd_per_video_token"], "per-token pricing only"),
    (["cents_per_megapixel_second"], "per-megapixel-second pricing only"),
    (["cents_per_generation"], "minimum-charge pricing only"),
    (["cents_per_image"], "input-image pricing only"),
    ([], "no published rate"),
])
def test_the_rate_absence_label_names_the_pricing_scheme(units, expected):
    assert builder.rate_absence_label(model("m", units=units)) == expected


def test_a_published_rate_produces_no_absence_label_and_a_5s_derivation():
    rate = builder.rate_absence_label(model("m", rate=0.03))

    assert rate is None

    rows = builder.build_model_rows(
        [model("m", rate=0.03, basis="plain_rate", units=["usd_per_second"], durations=[5])],
        video_coverage.pool_prompt_benchmarks(strong("m")),
    )
    assert rows[0]["has_catalog_rate"] is True
    assert rows[0]["comparable_price"] == pytest.approx(0.03)
    assert rows[0]["cost_per_5s_clip"] == pytest.approx(0.15)
    assert rows[0]["rate_absence_label"] is None


def test_a_model_with_no_rate_is_still_ranked_on_observed_cost():
    """The Seedance rule: value uses the observed benchmark cost, so a missing
    catalogue rate must not remove a model from the ranking."""
    rows = builder.build_model_rows(
        [model("seedance/x", units=["usd_per_video_token"], durations=[5])],
        video_coverage.pool_prompt_benchmarks(strong("seedance/x", cost=0.25)),
    )

    assert rows[0]["value"] is not None
    assert rows[0]["rankable"] is True
    assert rows[0]["value_rank"] == 1
    assert rows[0]["comparable_price"] is None


# ---------------------------------------------------------------------------
# the evidence gate
# ---------------------------------------------------------------------------
def test_the_evidence_gate_blocks_a_rank_but_keeps_the_value():
    thin = [prompt_row("thin/y", "p0", 5, 5, 0.10), prompt_row("thin/y", "p1", 5, 5, 0.10)]
    rows = builder.build_model_rows(
        [model("thin/y")], video_coverage.pool_prompt_benchmarks(thin)
    )
    row = rows[0]

    assert row["prompts"] == 2
    assert row["evidence_checks"] == 10
    assert row["value"] is not None, "the value is real and stays visible"
    assert row["evidence_gate_passed"] is False
    assert row["rankable"] is False
    assert row["value_rank"] is None, "but it must not take a rank"


def test_the_gate_needs_both_prompts_and_checks():
    assert video_coverage.meets_evidence_gate(8, 40) is True
    assert video_coverage.meets_evidence_gate(7, 400) is False
    assert video_coverage.meets_evidence_gate(40, 39) is False
    assert video_coverage.meets_evidence_gate(None, 40) is False


def test_ranking_skips_a_gated_out_model_and_leaves_a_gap_free_sequence():
    rows = builder.build_model_rows(
        [model("strong/a"), model("thin/y")],
        video_coverage.pool_prompt_benchmarks(
            strong("strong/a", cost=1.0) + [prompt_row("thin/y", "p0", 5, 5, 0.01)]
        ),
    )
    ranks = {row["model_id"]: row["value_rank"] for row in rows}

    # thin/y has a much better value but too little evidence, so strong/a is #1
    # and thin/y takes no rank - the sequence stays 1..N with no gaps.
    assert ranks == {"strong/a": 1, "thin/y": None}


# ---------------------------------------------------------------------------
# capability fields (nullable - must never be invented)
# ---------------------------------------------------------------------------
def test_capability_fields_keep_their_unknowns():
    rows = builder.build_model_rows(
        [model("m", durations=[4, 8, 12], audio=None)],
        video_coverage.pool_prompt_benchmarks(strong("m")),
    )
    row = rows[0]

    assert row["max_duration_seconds"] == 12
    assert row["generate_audio"] is None, "unpublished audio support must stay unknown, not become False"


def test_audio_and_duration_survive_each_state():
    rows = builder.build_model_rows(
        [model("yes", durations=[5], audio=True), model("no", durations=[5], audio=False),
         model("weird", durations=None, audio=None)],
        video_coverage.pool_prompt_benchmarks(
            strong("yes") + strong("no") + strong("weird")
        ),
    )
    by_id = {row["model_id"]: row for row in rows}

    assert by_id["yes"]["generate_audio"] is True
    assert by_id["no"]["generate_audio"] is False
    assert by_id["weird"]["generate_audio"] is None
    assert by_id["weird"]["max_duration_seconds"] is None


# ---------------------------------------------------------------------------
# coverage and payload assembly
# ---------------------------------------------------------------------------
def test_coverage_separates_gated_out_from_unrated(tmp_path, monkeypatch):
    install(
        tmp_path, monkeypatch,
        [model("priced/a", rate=0.1, durations=[5]), model("unpriced/b", units=["usd_per_video_token"]),
         model("unrated/c", rate=0.2, durations=[5])],
        strong("priced/a") + strong("unpriced/b"),
    )
    payload = builder.build_payload()
    coverage = payload["coverage"]

    assert coverage["catalog_models"] == 3
    assert coverage["priced_models"] == 2
    assert coverage["benchmarked_models"] == 2
    assert coverage["rankable_models"] == 2
    assert coverage["unrated_models"] == 1
    assert coverage["unrated_ids"] == ["unrated/c"]
    assert coverage["benchmarked_unpriced_models"] == 1
    assert coverage["benchmarked_unpriced_ids"] == ["unpriced/b"]
    assert coverage["gated_out_models"] == 0


def test_every_catalogue_model_appears_exactly_once(tmp_path, monkeypatch):
    install(
        tmp_path, monkeypatch,
        [model("a", rate=0.1, durations=[5]), model("b", units=["usd_per_video_token"]),
         model("c", rate=0.2, durations=[5])],
        strong("a") + strong("b"),
    )
    payload = builder.build_payload()
    ids = [row["model_id"] for row in payload["rows"]]

    assert sorted(ids) == ["a", "b", "c"]
    assert len(ids) == len(set(ids))


def test_budget_requires_the_evidence_gate():
    rows = builder.build_model_rows(
        [model("cheap/x", rate=0.01, durations=[5]), model("thin/y")],
        video_coverage.pool_prompt_benchmarks(
            strong("cheap/x", cost=0.9) + [prompt_row("thin/y", "p0", 5, 5, 0.001)]
        ),
    )
    budget = builder.budget_models(rows)

    # cheap/x passes 100% and is eligible; thin/y is far cheaper but has one
    # prompt, so it is not recommended to anyone.
    assert [row["model_id"] for row in budget] == ["cheap/x"]


def test_prompt_coverage_reports_every_prompt_and_model():
    rows = [
        prompt_row("a", "traffic-light", 5, 5, 0.5),
        prompt_row("a", "walk-out", 4, 5, 0.4),
        prompt_row("b", "traffic-light", 3, 5, 0.3),
    ]
    panel = builder.build_prompt_coverage(rows, builder.build_model_rows(
        [model("a"), model("b")], video_coverage.pool_prompt_benchmarks(rows)
    ))

    assert [p["slug"] for p in panel["prompts"]] == ["traffic-light", "walk-out"]
    assert panel["prompt_count"] == 2
    assert panel["model_count"] == 2
    assert panel["row_count"] == 3

    model_a = next(m for m in panel["models"] if m["model_id"] == "a")
    assert model_a["prompt_count"] == 2
    assert model_a["cells"]["traffic-light"]["checks_passed"] == 5


def test_payload_mirrors_the_image_page_sections(tmp_path, monkeypatch):
    """The video page is meant to stay structurally parallel to the image page,
    so the section names must match even where the contents differ."""
    install(tmp_path, monkeypatch, [model("a", rate=0.1, durations=[5])], strong("a"))
    payload = builder.build_payload()

    for key in ("rows", "coverage", "budget", "trend", "weekly", "constants",
                "sources", "data_retrieved_at", "benchmarks_retrieved_at", "generated_at"):
        assert key in payload, f"the payload lost the {key} section"

    assert "prompt_coverage" in payload
    assert "design_arena" not in payload, (
        "there is no video preference or arena benchmark - the section must not reappear"
    )

    assert payload["constants"]["evidence_min_prompts"] == video_coverage.MIN_PROMPTS_FOR_RANKING
    assert payload["constants"]["evidence_min_checks"] == video_coverage.MIN_CHECKS_FOR_RANKING
    assert payload["constants"]["value_definition"].startswith("pass rate")


def test_the_build_timestamp_is_wall_clock_and_data_stamps_come_from_the_snapshot(tmp_path, monkeypatch):
    """`generated_at` is deliberately the build time (parity with the image page),
    while the data stamps come from the snapshot - which is why the determinism
    check in the invariants compares builds with generated_at masked."""
    install(tmp_path, monkeypatch, [model("a", rate=0.1, durations=[5])], strong("a"))
    payload = builder.build_payload()

    assert payload["data_retrieved_at"] == RETRIEVED_AT
    assert payload["benchmarks_retrieved_at"] == RETRIEVED_AT
    assert payload["generated_at"] != RETRIEVED_AT
    assert datetime.fromisoformat(payload["generated_at"]).tzinfo is not None


# ---------------------------------------------------------------------------
# file contracts
# ---------------------------------------------------------------------------
def test_the_builder_owns_exactly_one_output_and_no_status_sidecar():
    assert builder.OUTPUT_PATH.parent == DASHBOARD_DIR
    assert builder.OUTPUT_PATH.name == "video_model_analysis.html"
    assert builder.TEMPLATE_PATH.name == "video_template.html"
    assert not hasattr(builder, "STATUS_PATH"), (
        "the media precedent has no status sidecar and the shell reads "
        "refresh_status.json for per-target state"
    )


def test_the_builder_reads_only_video_dashboard_inputs():
    """A cross-read would let a video rebuild depend on - or disturb - the image
    page's data."""
    # Every input path; the template and output are the builder's own files in
    # dashboard/, asserted separately below.
    data_paths = {
        name: value for name, value in vars(builder).items()
        if name.endswith("_PATH") and name.isupper()
        and name not in ("TEMPLATE_PATH", "OUTPUT_PATH")
    }
    assert data_paths, "the builder's input paths are missing"

    for name, value in data_paths.items():
        text = str(value)
        assert "video_dashboard" in text or "video_snapshots" in text, (
            f"{name} points outside the video dashboard's own data: {text}"
        )

    assert builder.TEMPLATE_PATH.parent == DASHBOARD_DIR
    assert builder.OUTPUT_PATH.parent == DASHBOARD_DIR


def test_stale_data_refuses_to_build_and_writes_nothing(tmp_path, monkeypatch):
    install(tmp_path, monkeypatch, [model("a", rate=0.1, durations=[5])], strong("a"))
    builder.VIDEO_MODELS_PATH.write_text(json.dumps([
        {**model("a", rate=0.1, durations=[5]),
         "retrieved_at": (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()}
    ]), encoding="utf-8")

    fresh, message = builder.check_freshness(192.0)

    assert fresh is False
    assert "older than the 192.0-hour freshness window" in message


def test_freshness_reports_missing_and_empty_inputs_distinctly(tmp_path, monkeypatch):
    missing_path = tmp_path / "does-not-exist.json"
    monkeypatch.setattr(builder, "VIDEO_MODELS_PATH", missing_path)

    missing, message = builder.check_freshness(192.0)
    assert missing is False and "does not exist" in message

    empty_path = tmp_path / "empty.json"
    empty_path.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(builder, "VIDEO_MODELS_PATH", empty_path)

    empty, message = builder.check_freshness(192.0)
    assert empty is False and "empty" in message
