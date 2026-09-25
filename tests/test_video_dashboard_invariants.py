"""Cross-file invariants for the third (video-generation) dashboard page.

Same style as tests/test_image_dashboard_invariants.py: these lock down promises
that only hold ACROSS files, and that a later edit could quietly break.

  * the video template's four <style> blocks are still a verbatim copy of the chat
    template's, so the three pages cannot drift into different products;
  * the template on disk is still exactly what derive_video_template.py produces
    from the image template - a hand-edit that is not reflected back into the
    derivation would be silently overwritten by the next run, so it is a failure
    rather than a surprise;
  * a missing catalogue rate renders as an em dash WITH a reason, never as $0.00,
    in every place a rate is shown;
  * there is no Design Arena / preference-survey remnant, because no video
    preference benchmark exists;
  * two builds of the same data are identical once `generated_at` is masked.
"""
import json
import re
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_DIR = PROJECT_ROOT / "dashboard"
for _path in (str(PROJECT_ROOT), str(DASHBOARD_DIR)):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import build_video_dashboard as builder  # noqa: E402
import derive_video_template as derivation  # noqa: E402
import page_shell  # noqa: E402

CHAT_TEMPLATE_PATH = DASHBOARD_DIR / "template.html"
IMAGE_TEMPLATE_PATH = DASHBOARD_DIR / "image_template.html"
VIDEO_TEMPLATE_PATH = DASHBOARD_DIR / "video_template.html"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def style_blocks(html: str) -> list[str]:
    return re.findall(r"<style>.*?</style>", html, re.S)


# ---------------------------------------------------------------------------
# design parity
# ---------------------------------------------------------------------------
def test_video_template_styles_are_a_verbatim_copy_of_the_chat_template():
    chat_blocks = style_blocks(read(CHAT_TEMPLATE_PATH))
    video_blocks = style_blocks(read(VIDEO_TEMPLATE_PATH))

    assert len(chat_blocks) == 4, "the chat template's style blocks moved; update this deliberately"
    assert video_blocks == chat_blocks, (
        "the video template's <style> blocks must be an unmodified, same-order copy of the chat "
        "template's - the visual result depends on the whole cascade, and the image template's "
        "copy is identical too"
    )


def test_the_video_template_is_still_exactly_its_derivation():
    """The video template is DERIVED from the image template. A hand-edit that is
    not reflected back into derive_video_template.py would be silently reverted
    the next time anyone runs it, so the file on disk must match its derivation."""
    expected = derivation.derive(read(IMAGE_TEMPLATE_PATH))
    assert read(VIDEO_TEMPLATE_PATH) == expected, (
        "video_template.html has drifted from derive_video_template.py - either re-run the script "
        "or add the change to its substitution list"
    )


def test_the_derivation_refuses_a_template_whose_anchors_moved():
    """The script's whole safety property: it must fail loudly rather than
    produce a page with a section quietly missing."""
    with pytest.raises(SystemExit):
        derivation.derive("<html><body>no anchors here</body></html>")


# ---------------------------------------------------------------------------
# shell wiring
# ---------------------------------------------------------------------------
def test_the_video_template_scopes_one_shell_and_an_empty_tab_container():
    html = read(VIDEO_TEMPLATE_PATH)

    assert html.count("__SHELL_JS__") == 1
    assert html.count("<script>") == 2, "the page carries exactly a payload script and the shell"
    match = re.search(r'<nav class="tabs" id="navTabs"[^>]*>(.*?)</nav>', html, re.S)
    assert match, "the video template is missing the shared tab container"
    assert match.group(1).strip() == "", "tabs must come from nav_tabs.NAV_TABS, never hardcoded"


def test_the_video_template_runs_the_shell_before_the_payload():
    """Same ordering as the image template, so the theme applies before first
    paint - and the same reason the shell must never read PAYLOAD. In the
    TEMPLATE the shell is still the __SHELL_JS__ placeholder; the injected
    `const NAV_TABS=` form only exists in the built page."""
    html = read(VIDEO_TEMPLATE_PATH)

    assert html.index("__SHELL_JS__") < html.index("__DATA__"), (
        "the shell must run before the payload on this page"
    )
    assert "PAYLOAD" not in read(page_shell.SHELL_JS_SOURCE)


def test_the_committed_page_is_not_stale_relative_to_its_template():
    html = read(builder.OUTPUT_PATH)

    assert "__DATA__" not in html, "the committed video page still holds an unsubstituted payload"
    assert "const NAV_TABS=[" in html
    assert html.index("const NAV_TABS=[") < html.rindex("NAV_TABS"), (
        "the tab DATA must be injected before the shell code that reads it"
    )


# ---------------------------------------------------------------------------
# the missing-rate rule (the user-visible half)
# ---------------------------------------------------------------------------
def test_no_place_renders_a_missing_rate_as_zero():
    """A model that publishes only per-token rates has NO per-second price. That
    is a different statement from "it costs nothing", so every rate render must
    sit behind a null guard."""
    html = read(VIDEO_TEMPLATE_PATH)

    money_calls = len(re.findall(r"money\(row\.comparable_price\)", html))
    guards = len(re.findall(r"comparable_price==null", html))

    assert money_calls >= 1, "the catalogue rate is never rendered"
    assert guards >= money_calls, (
        f"{money_calls} rate render(s) but only {guards} null guard(s) - an unguarded call is how a "
        f"missing rate becomes $0.00"
    )


def test_the_rate_renderers_name_the_scheme_instead_of_a_bare_dash():
    html = read(VIDEO_TEMPLATE_PATH)

    assert "rate_absence_label" in html, "the reason for a missing rate is never shown"
    # The reference table and the unrated table both fall back to a phrase rather
    # than an empty cell.
    assert 'rate_absence_label||"no published rate"' in html


def test_the_absence_labels_live_in_the_builder_not_the_template():
    """Which schemes exist is a fact about the data, so it is derived in Python
    from the units each model publishes - the template only supplies a fallback.
    A hardcoded scheme list in the template would go stale silently."""
    template = read(VIDEO_TEMPLATE_PATH)

    for label in ("per-token pricing only", "per-megapixel-second pricing only",
                  "minimum-charge pricing only", "input-image pricing only"):
        assert label in builder.RATE_ABSENCE_LABELS_AS_TEXT, (
            f"the builder no longer derives {label!r}"
        )
        assert label not in template, (
            f"{label!r} is hardcoded in the template; it must come from the payload"
        )


def test_the_hero_card_carries_the_clip_length_caveat():
    """The caveat that qualifies the headline pick must be ON the card, not only
    in the provenance panel."""
    html = read(VIDEO_TEMPLATE_PATH)
    hero = html[html.index('byId("heroArenaFlag")'):]
    hero = hero[:hero.index("setText(\"heroNote\"")]

    assert "Clip length is not published" in hero, (
        "the hero card must state that clip length is unpublished, because its cost figure is "
        "an observed per-clip cost"
    )


def test_the_gated_out_model_still_shows_its_value_without_a_rank():
    """Ranking is gated, but the value itself is real and stays visible."""
    template = read(VIDEO_TEMPLATE_PATH)
    assert "evidence_min_prompts" in template
    assert "not rankable" in template


# ---------------------------------------------------------------------------
# no preference/arena remnants
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path", [VIDEO_TEMPLATE_PATH, builder.OUTPUT_PATH])
def test_no_design_arena_remnant_survives(path):
    html = read(path)

    for banned in ("Design Arena", "design_arena", "arenaCell", "ARENA.models"):
        assert banned not in html, (
            f"{path.name} still refers to {banned!r}. No video preference benchmark exists, so "
            f"the section must not reappear even as dead code."
        )


def test_the_video_sections_that_replaced_it_exist():
    html = read(VIDEO_TEMPLATE_PATH)

    assert 'id="promptCoverageTitle"' in html
    assert 'id="promptCoverageBody"' in html
    assert 'id="videoScatter"' in html
    assert "imageScatter" not in html


# ---------------------------------------------------------------------------
# determinism
# ---------------------------------------------------------------------------
def test_two_builds_agree_once_the_build_timestamp_is_masked(tmp_path, monkeypatch):
    """`generated_at` is wall-clock on purpose (parity with the image page), so
    determinism is asserted with that one field masked. Everything else - the
    rows, the rankings, the coverage - must be byte-identical across builds."""
    import datetime as dt

    models = [{
        "model_id": "m/one", "model_name": "One", "provider": "acme", "model_type": "video",
        "has_valid_pricing": True, "pricing_unit": "usd_per_second", "comparable_price": 0.1,
        "comparable_price_basis": "plain_rate", "pricing": {"unit_families": ["usd_per_second"]},
        "supported_durations": [5], "generate_audio": True,
        "retrieved_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }]
    rows = [{
        "model_id": "m/one", "model_name": "One", "media_type": "video", "matched": True,
        "prompt_slug": f"p{i}", "prompt_name": f"P{i}", "checks_passed": 5, "checks_total": 5,
        "cost_usd": 1.0, "output_width": 1280, "output_height": 720, "duration_ms": 1000,
        "generation_seconds": 1.0, "retrieved_at": models[0]["retrieved_at"],
    } for i in range(10)]

    for name, payload in (("models.json", models), ("rows.json", rows),
                          ("coverage.json", {}), ("quality.json", {}), ("picks.json", {})):
        (tmp_path / name).write_text(json.dumps(payload), encoding="utf-8")

    monkeypatch.setattr(builder, "VIDEO_MODELS_PATH", tmp_path / "models.json")
    monkeypatch.setattr(builder, "PROMPT_BENCHMARKS_PATH", tmp_path / "rows.json")
    monkeypatch.setattr(builder, "COVERAGE_PATH", tmp_path / "coverage.json")
    monkeypatch.setattr(builder, "QUALITY_PATH", tmp_path / "quality.json")
    monkeypatch.setattr(builder, "WEEKLY_PICKS_PATH", tmp_path / "picks.json")
    import video_picks
    monkeypatch.setattr(video_picks.settings, "VIDEO_DASHBOARD_WEEKLY_PICKS_PATH", tmp_path / "picks.json")

    def masked(payload):
        return {key: value for key, value in payload.items() if key != "generated_at"}

    first = json.dumps(masked(builder.build_payload()), sort_keys=True)
    second = json.dumps(masked(builder.build_payload()), sort_keys=True)

    assert first == second
    assert builder.build_payload()["generated_at"] is not None
