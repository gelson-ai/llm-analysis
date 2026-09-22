"""Tests for dashboard/media_picks.py - the image dashboard's Model of the Week.

These concentrate on the behaviours that would produce a WRONG pick silently
rather than an error, because those are the ones nobody would notice on a page:

  * a week that is not Monday..Sunday Asia/Manila (a pick "locked for the week"
    on the wrong boundary is wrong in a way that looks right);
  * a tie broken differently from the ranking the page prints beside the pick;
  * a later refresh OVERWRITING the locked pick instead of recording a revision,
    which would quietly rewrite history to make today's leader always look like
    it was the pick all along;
  * the media module sharing the chat pipeline's history file.

The rows used here are the shape build_image_dashboard.build_model_rows() emits.
No network, no real files: history paths are passed explicitly into tmp_path.
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_DIR = PROJECT_ROOT / "dashboard"
if str(DASHBOARD_DIR) not in sys.path:
    sys.path.insert(0, str(DASHBOARD_DIR))

import media_picks  # noqa: E402  (needs the path insert above)
import weekly_picks  # noqa: E402  (only to prove the history files differ)

MANILA = timezone(timedelta(hours=8))


def row(model_id, value, **extra):
    """One row in the shape the builder emits, reduced to what the pick uses."""
    record = {
        "model_id": model_id,
        "model_name": extra.get("model_name") or model_id,
        "provider": model_id.split("/")[0],
        "has_catalog_rate": True,
        "benchmarked": True,
        "design_arena": False,
        "pass_rate": 0.8,
        "avg_cost_usd": 0.10,
        "prompts": 2,
        "output_resolutions": ["1024x1024"],
        "duration_ms_mean": 8000.0,
        "value": value,
    }
    record.update(extra)
    return record


def payload(rows, retrieved_at="2026-09-24T02:00:00+00:00"):
    return {"rows": rows, "data_retrieved_at": retrieved_at}


def moment(iso_or_dt):
    if isinstance(iso_or_dt, str):
        return datetime.fromisoformat(iso_or_dt)
    return iso_or_dt


THURSDAY = "2026-09-24T02:00:00+00:00"        # Thu 24 Sep 2026, 10:00 PHT
NEXT_MONDAY = "2026-09-28T02:00:00+00:00"     # Mon 28 Sep 2026, 10:00 PHT


# ---------------------------------------------------------------------------
# the week
# ---------------------------------------------------------------------------
def test_the_week_runs_monday_to_sunday_with_the_displayed_range():
    view = media_picks.embed_view({}, datetime.fromisoformat(THURSDAY))

    assert view["week_label"] == "Mon 21 Sep - Sun 27 Sep 2026"
    assert view["week_key"] == "2026-09-21_2026-09-27"
    assert view["iso_week"] == "2026-W39"
    assert view["timezone"] == "Asia/Manila"


def test_the_week_turns_over_at_midnight_in_manila_not_utc():
    """Both of these are the same Sunday in UTC, but one minute apart in Manila
    they are different weeks. Using UTC here would misfile every pick made in
    the first eight hours of a Monday."""
    sunday_night = "2026-09-27T15:59:00+00:00"  # Sun 23:59 PHT
    monday_morning = "2026-09-27T16:00:00+00:00"  # Mon 00:00 PHT

    assert media_picks.embed_view({}, moment(sunday_night))["week_key"] == "2026-09-21_2026-09-27"
    assert media_picks.embed_view({}, moment(monday_morning))["week_key"] == "2026-09-28_2026-10-04"


def test_a_naive_timestamp_is_read_as_utc_rather_than_crashing():
    naive = datetime(2026, 9, 24, 2, 0, 0)
    assert media_picks.embed_view({}, naive)["week_key"] == "2026-09-21_2026-09-27"


# ---------------------------------------------------------------------------
# choosing the pick
# ---------------------------------------------------------------------------
def test_the_first_refresh_of_the_week_locks_the_pick():
    _, summary = media_picks.record_pick(
        media_picks.empty_history(),
        payload([row("a/low", 5.0), row("b/high", 20.0), row("c/mid", 12.0)]),
        now=datetime.fromisoformat(THURSDAY),
        source="refresh_media",
    )

    assert summary["state"] == "locked"
    assert summary["pick"]["model_id"] == "b/high"
    assert summary["metric"] == "value"


def test_the_locked_pick_keeps_the_evidence_it_rested_on():
    history, _ = media_picks.record_pick(
        media_picks.empty_history(),
        payload([row("b/high", 20.0, design_arena=True, output_resolutions=["2048x2048"])]),
        now=datetime.fromisoformat(THURSDAY),
    )
    entry = history["weeks"]["2026-09-21_2026-09-27"]["picks"]["value"]

    assert entry["model_id"] == "b/high"
    assert entry["value"] == pytest.approx(20.0)
    assert entry["pass_rate"] == pytest.approx(0.8)
    assert entry["avg_cost_usd"] == pytest.approx(0.10)
    assert entry["design_arena"] is True
    assert entry["output_resolutions"] == ["2048x2048"]
    assert entry["duration_ms_mean"] == pytest.approx(8000.0)
    assert entry["data_retrieved_at"] is not None
    assert entry["recorded_label"].endswith("PHT")
    assert entry["locked_by"] == "refresh_media"
    assert entry["revisions"] == []


def test_the_runners_up_are_recorded_so_a_later_reader_can_see_the_margin():
    history, _ = media_picks.record_pick(
        media_picks.empty_history(),
        payload([row("a/one", 3.0), row("b/two", 20.0), row("c/three", 12.0), row("d/four", 1.0)]),
        now=datetime.fromisoformat(THURSDAY),
    )
    contenders = history["weeks"]["2026-09-21_2026-09-27"]["picks"]["value"]["contenders"]

    assert [c["model_id"] for c in contenders] == ["b/two", "c/three", "a/one"]
    assert [c["rank"] for c in contenders] == [1, 2, 3]
    assert "d/four" not in [c["model_id"] for c in contenders]


def test_models_with_no_defined_value_are_not_pickable():
    """value is None when the pass rate or the benchmark cost is missing. Those
    models are not rankable on the page either, so they cannot be the week's
    winner."""
    history, summary = media_picks.record_pick(
        media_picks.empty_history(),
        payload([
            row("unrated/priced", None, pass_rate=None),
            row("unrated/free", None, avg_cost_usd=0.0),
            row("rated/ok", 4.0),
        ]),
        now=datetime.fromisoformat(THURSDAY),
    )

    assert summary["state"] == "locked"
    assert summary["pick"]["model_id"] == "rated/ok"


def test_a_week_with_nothing_rankable_records_no_pick_at_all():
    """An empty pick is honest; a placeholder model name would not be."""
    history, summary = media_picks.record_pick(
        media_picks.empty_history(),
        payload([row("unrated/priced", None, pass_rate=None)]),
        now=datetime.fromisoformat(THURSDAY),
    )

    assert summary["state"] == "no-eligible-models"
    assert history["weeks"]["2026-09-21_2026-09-27"]["picks"] == {}
    assert media_picks.embed_view(history, moment(THURSDAY))["picks"] == {}


def test_an_empty_payload_records_no_pick_rather_than_raising():
    history, summary = media_picks.record_pick(
        media_picks.empty_history(), payload([]), now=datetime.fromisoformat(THURSDAY)
    )
    assert summary["state"] == "no-eligible-models"
    assert media_picks.embed_view(history, moment(THURSDAY))["picks"] == {}


def test_a_tie_breaks_on_model_id_ascending_like_the_pages_own_ranking():
    """build_image_dashboard ranks with key=(-value, model_id). The pick has to
    use the identical tie-break or it could name a model that the page itself
    prints as #2."""
    history, _ = media_picks.record_pick(
        media_picks.empty_history(),
        payload([row("zeta/tie", 7.0), row("alpha/tie", 7.0)]),
        now=datetime.fromisoformat(THURSDAY),
        )
    entry = history["weeks"]["2026-09-21_2026-09-27"]["picks"]["value"]

    assert entry["model_id"] == "alpha/tie"


# ---------------------------------------------------------------------------
# revising a locked week
# ---------------------------------------------------------------------------
def test_the_same_leader_again_is_unchanged_and_does_not_touch_the_record():
    history, _ = media_picks.record_pick(
        media_picks.empty_history(), payload([row("b/high", 20.0)]),
        now=datetime.fromisoformat(THURSDAY),
    )
    entry = history["weeks"]["2026-09-21_2026-09-27"]["picks"]["value"]
    before = json.dumps(entry, sort_keys=True)

    history, summary = media_picks.record_pick(
        history, payload([row("b/high", 22.0)]), now=datetime.fromisoformat(THURSDAY)
    )
    after = history["weeks"]["2026-09-21_2026-09-27"]["picks"]["value"]

    assert summary["state"] == "unchanged"
    assert entry["revisions"] == []
    assert entry["revision_count"] == 0
    # The locked value is the one it was locked with, not today's reading.
    assert after["value"] == pytest.approx(20.0)
    # ...but the live comparison against today's data is still refreshed.
    assert after["current_leader"]["value"] == pytest.approx(22.0)
    assert after["current_leader_differs"] is False
    assert json.dumps({k: v for k, v in after.items() if k != "current_leader"}, sort_keys=True) == \
        json.dumps({k: v for k, v in json.loads(before).items() if k != "current_leader"}, sort_keys=True)


def test_a_changed_leader_is_recorded_as_a_revision_not_an_overwrite():
    history, _ = media_picks.record_pick(
        media_picks.empty_history(), payload([row("a/old", 20.0), row("b/new", 5.0)]),
        now=datetime.fromisoformat(THURSDAY),
    )
    history, summary = media_picks.record_pick(
        history, payload([row("a/old", 6.0), row("b/new", 30.0)]),
        now=datetime.fromisoformat(THURSDAY),
    )
    entry = history["weeks"]["2026-09-21_2026-09-27"]["picks"]["value"]

    assert summary["state"] == "revised"
    # The week's pick is still what was locked. History is not rewritten.
    assert entry["model_id"] == "a/old"
    assert entry["value"] == pytest.approx(20.0)
    # The movement is visible instead of silently applied.
    assert entry["current_leader_differs"] is True
    assert entry["current_leader"]["model_id"] == "b/new"
    assert entry["revision_count"] == 1
    revision = entry["revisions"][0]
    assert revision["new_leader_model_id"] == "b/new"
    assert revision["locked_model_id"] == "a/old"
    assert revision["new_leader_value"] == pytest.approx(30.0)
    assert revision["at_label"].endswith("PHT")


def test_the_same_challenger_is_only_logged_once():
    """Persisting a changed leader across many refreshes must not append the
    same revision on every run - the log would become unreadable and the count
    meaningless."""
    history, _ = media_picks.record_pick(
        media_picks.empty_history(), payload([row("a/old", 20.0), row("b/new", 5.0)]),
        now=datetime.fromisoformat(THURSDAY),
    )
    history, first = media_picks.record_pick(
        history, payload([row("a/old", 6.0), row("b/new", 30.0)]),
        now=datetime.fromisoformat(THURSDAY),
    )
    history, second = media_picks.record_pick(
        history, payload([row("a/old", 7.0), row("b/new", 31.0)]),
        now=datetime.fromisoformat(THURSDAY),
    )
    history, third = media_picks.record_pick(
        history, payload([row("a/old", 8.0), row("b/new", 32.0)]),
        now=datetime.fromisoformat(THURSDAY),
    )
    entry = history["weeks"]["2026-09-21_2026-09-27"]["picks"]["value"]

    assert (first["state"], second["state"], third["state"]) == ("revised", "unchanged", "unchanged")
    assert entry["revision_count"] == 1
    assert entry["current_leader"]["value"] == pytest.approx(32.0)


def test_a_second_challenger_after_the_first_is_a_second_revision():
    history, _ = media_picks.record_pick(
        media_picks.empty_history(), payload([row("a/old", 20.0)]),
        now=datetime.fromisoformat(THURSDAY),
    )
    history, _ = media_picks.record_pick(
        history, payload([row("a/old", 5.0), row("b/second", 9.0)]),
        now=datetime.fromisoformat(THURSDAY),
    )
    history, summary = media_picks.record_pick(
        history, payload([row("a/old", 5.0), row("c/third", 11.0)]),
        now=datetime.fromisoformat(THURSDAY),
    )
    entry = history["weeks"]["2026-09-21_2026-09-27"]["picks"]["value"]

    assert summary["state"] == "revised"
    assert [r["new_leader_model_id"] for r in entry["revisions"]] == ["b/second", "c/third"]
    assert entry["revision_count"] == 2
    assert entry["model_id"] == "a/old"


# ---------------------------------------------------------------------------
# weeks accumulate
# ---------------------------------------------------------------------------
def test_each_week_locks_its_own_pick_and_keeps_the_earlier_weeks():
    history, _ = media_picks.record_pick(
        media_picks.empty_history(), payload([row("sep/first", 20.0)]),
        now=datetime.fromisoformat(THURSDAY),
    )
    history, _ = media_picks.record_pick(
        history, payload([row("oct/second", 30.0)]),
        now=datetime.fromisoformat(NEXT_MONDAY),
    )

    assert sorted(history["weeks"]) == ["2026-09-21_2026-09-27", "2026-09-28_2026-10-04"]
    assert history["weeks"]["2026-09-21_2026-09-27"]["picks"]["value"]["model_id"] == "sep/first"
    assert history["weeks"]["2026-09-28_2026-10-04"]["picks"]["value"]["model_id"] == "oct/second"

    # The embedded view is only ever the week being rendered.
    view = media_picks.embed_view(history, moment(NEXT_MONDAY))
    assert view["history_weeks"] == 2
    assert list(view["picks"]) == ["value"]
    assert view["picks"]["value"]["model_id"] == "oct/second"
    assert view["week_label"] == "Mon 28 Sep - Sun 4 Oct 2026"


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------
def test_the_history_round_trips_through_disk(tmp_path):
    path = tmp_path / "media_weekly_picks.json"
    history, _ = media_picks.record_pick(
        media_picks.empty_history(), payload([row("b/high", 20.0)]),
        now=datetime.fromisoformat(THURSDAY), source="refresh_media",
    )
    media_picks.save_history(history, path)

    reloaded = media_picks.load_history(path)
    assert media_picks.embed_view(reloaded, moment(THURSDAY))["picks"]["value"]["model_id"] == "b/high"
    assert reloaded["weeks"]["2026-09-21_2026-09-27"]["picks"]["value"]["locked_by"] == "refresh_media"


def test_a_missing_or_corrupt_history_starts_empty_instead_of_raising(tmp_path):
    missing = tmp_path / "nope.json"
    assert media_picks.load_history(missing)["weeks"] == {}

    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json at all", encoding="utf-8")
    assert media_picks.load_history(corrupt)["weeks"] == {}

    # A list where an object is expected is the same failure mode.
    wrong_shape = tmp_path / "wrong.json"
    wrong_shape.write_text("[1, 2, 3]", encoding="utf-8")
    assert media_picks.load_history(wrong_shape)["weeks"] == {}


def test_saving_leaves_no_temporary_file_behind(tmp_path):
    path = tmp_path / "media_weekly_picks.json"
    media_picks.save_history(media_picks.empty_history(), path)

    assert path.exists()
    assert list(tmp_path.glob("*.tmp")) == []


def test_the_media_history_is_a_separate_file_from_the_chat_one():
    """A shared file would let either pipeline's lock overwrite the other's
    record: they lock different picks over different model sets."""
    assert media_picks.history_path() == media_picks.history_path()
    assert media_picks.history_path() != weekly_picks.HISTORY_PATH
    assert media_picks.history_path().name == "media_weekly_picks.json"
    assert media_picks.history_path().parent == weekly_picks.HISTORY_PATH.parent


def test_the_module_never_reaches_into_the_chat_pipelines_ranking():
    """It reuses only the generic week/history helpers. Reusing the chat
    ranking (Artificial Analysis indices, blended token price) would be a
    silent category error, so the module is asserted not to call it."""
    source = (DASHBOARD_DIR / "media_picks.py").read_text(encoding="utf-8")
    for chat_specific in ("value_ratio", "record_weekly_picks", "METRICS", "as_row_dicts"):
        assert chat_specific not in source, f"media_picks must not use weekly_picks.{chat_specific}"


def test_every_generic_helper_it_does_reuse_is_public():
    """The week/history helpers are shared, so they must be public API - a
    private one could be renamed out from under this module."""
    for name in ("week_bounds", "week_key", "format_week_label", "empty_history",
                 "load_history", "save_history", "format_local_moment", "as_utc"):
        assert not name.startswith("_")
        assert callable(getattr(weekly_picks, name))


def test_the_embedded_view_is_json_serialisable():
    """It is baked straight into the page's payload - a datetime or a Path here
    would break the build, not just this dashboard."""
    history, _ = media_picks.record_pick(
        media_picks.empty_history(), payload([row("b/high", 20.0)]),
        now=datetime.fromisoformat(THURSDAY),
    )
    view = media_picks.embed_view(history, moment(THURSDAY))
    assert json.loads(json.dumps(view))["picks"]["value"]["model_id"] == "b/high"
