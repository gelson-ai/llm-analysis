"""Tests for weekly Model-of-the-Week tracking.

These exercise dashboard/weekly_picks.py only - pure logic, no network, no
pipeline run, no real data files (tmp_path is used for the round-trip tests).
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "dashboard"))

import weekly_picks  # noqa: E402  (needs the path insert above)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def row(model_id, name, price, intelligence=None, coding=None, agentic=None, provider="acme"):
    """One positional row in the exact order build_dashboard.build_payload emits."""
    return [model_id, name, provider, price, price, price, 128000,
            intelligence, coding, agentic, True, False]


def payload(rows, retrieved_at="2026-09-11T02:00:00+00:00"):
    return {"rows": rows, "data_retrieved_at": retrieved_at}


MONDAY = datetime(2026, 9, 7, 2, 0, tzinfo=timezone.utc)
TUESDAY = datetime(2026, 9, 8, 2, 0, tzinfo=timezone.utc)
THURSDAY = datetime(2026, 9, 10, 2, 0, tzinfo=timezone.utc)
FRIDAY = datetime(2026, 9, 11, 2, 0, tzinfo=timezone.utc)
NEXT_MONDAY = datetime(2026, 9, 14, 1, 0, tzinfo=timezone.utc)


def record(history, rows, when):
    return weekly_picks.record_weekly_picks(history, payload(rows), now=when)


# ---------------------------------------------------------------------------
# week boundaries (the part most likely to be wrong)
# ---------------------------------------------------------------------------
def test_week_bounds_are_monday_to_sunday():
    start, end = weekly_picks.week_bounds(FRIDAY)
    assert (start.isoformat(), end.isoformat()) == ("2026-09-07", "2026-09-13")


def test_sunday_2359_manila_is_still_the_same_week():
    # 23:59 Sunday in Manila == 15:59 Sunday UTC
    moment = datetime(2026, 9, 13, 15, 59, tzinfo=timezone.utc)
    start, end = weekly_picks.week_bounds(moment)
    assert start.isoformat() == "2026-09-07"
    assert end.isoformat() == "2026-09-13"


def test_monday_0000_manila_rolls_into_the_new_week():
    # 00:00 Monday in Manila == 16:00 Sunday UTC - the UTC date is still Sunday
    moment = datetime(2026, 9, 13, 16, 0, tzinfo=timezone.utc)
    start, end = weekly_picks.week_bounds(moment)
    assert (start.isoformat(), end.isoformat()) == ("2026-09-14", "2026-09-20")


def test_sunday_morning_utc_run_stays_in_the_manila_week():
    # 01:00 UTC Sunday is 09:00 Sunday in Manila
    start, _ = weekly_picks.week_bounds(datetime(2026, 9, 13, 1, 0, tzinfo=timezone.utc))
    assert start.isoformat() == "2026-09-07"


def test_naive_datetimes_are_treated_as_utc():
    start, _ = weekly_picks.week_bounds(datetime(2026, 9, 11, 2, 0))
    assert start.isoformat() == "2026-09-07"


def test_week_label_is_human_readable():
    start, end = weekly_picks.week_bounds(FRIDAY)
    assert weekly_picks.format_week_label(start, end) == "Mon 7 Sep - Sun 13 Sep 2026"


def test_week_key_uses_explicit_dates():
    start, end = weekly_picks.week_bounds(FRIDAY)
    assert weekly_picks.week_key(start, end) == "2026-09-07_2026-09-13"


# ---------------------------------------------------------------------------
# ranking
# ---------------------------------------------------------------------------
def test_value_ratio_excludes_free_unpriced_and_unscored():
    assert weekly_picks.value_ratio(None, 1.0) is None
    assert weekly_picks.value_ratio(50, None) is None
    assert weekly_picks.value_ratio(50, 0) is None
    assert weekly_picks.value_ratio(50, 2) == 25


def test_models_without_the_selected_metric_are_not_eligible():
    ranked = weekly_picks.ranked_rows(
        [row("a/model", "A", 1.0, intelligence=None), row("b/model", "B", 1.0, intelligence=30)],
        "intelligence",
    )
    assert [item[1]["model_id"] for item in ranked] == ["b/model"]


def test_ranking_prefers_higher_score_on_equal_value():
    ranked = weekly_picks.ranked_rows(
        [row("a/model", "A", 1.0, intelligence=10), row("b/model", "B", 2.0, intelligence=20)],
        "intelligence",
    )
    # both have value 10.0; the higher score wins
    assert [item[1]["model_id"] for item in ranked] == ["b/model", "a/model"]


def test_ranking_is_deterministic_on_full_ties():
    ranked = weekly_picks.ranked_rows(
        [row("zz/model", "Z", 2.0, intelligence=20), row("aa/model", "A", 2.0, intelligence=20)],
        "intelligence",
    )
    assert [item[1]["model_id"] for item in ranked] == ["aa/model", "zz/model"]


def test_rows_may_be_dicts_as_well_as_positional_lists():
    dict_row = {
        "model_id": "d/model", "model_name": "D", "provider": "p",
        "blended_price": 1.0, "intelligence": 40, "context_length": 4096,
    }
    history, summary = weekly_picks.record_weekly_picks(
        weekly_picks.empty_history(), {"rows": [dict_row]}, now=TUESDAY,
    )
    entry = history["weeks"][summary["week_key"]]["picks"]["intelligence"]
    assert entry["model_id"] == "d/model"


# ---------------------------------------------------------------------------
# locking behaviour
# ---------------------------------------------------------------------------
def test_first_refresh_of_the_week_locks_the_pick():
    history, summary = record(weekly_picks.empty_history(),
                              [row("x/model", "X", 1.0, intelligence=50)], TUESDAY)
    assert summary["metrics"]["intelligence"] == "locked"
    entry = history["weeks"][summary["week_key"]]["picks"]["intelligence"]
    assert entry["model_id"] == "x/model"
    assert entry["revisions"] == []
    assert entry["revision_count"] == 0
    assert entry["locked_by"] == "refresh"
    assert entry["contenders"][0]["model_id"] == "x/model"


def test_later_refresh_reports_a_new_leader_without_changing_the_pick():
    history, first = record(weekly_picks.empty_history(),
                            [row("x/model", "X", 1.0, intelligence=50)], TUESDAY)
    key = first["week_key"]

    history, second = record(history,
                             [row("x/model", "X", 1.0, intelligence=50),
                              row("y/model", "Y", 0.5, intelligence=50)], THURSDAY)

    assert second["metrics"]["intelligence"] == "revised"
    entry = history["weeks"][key]["picks"]["intelligence"]
    assert entry["model_id"] == "x/model"          # the locked pick is untouched
    assert entry["current_leader_differs"] is True
    assert entry["current_leader"]["model_id"] == "y/model"
    assert entry["revision_count"] == 1
    assert entry["revisions"][0]["new_leader_model_id"] == "y/model"
    assert entry["revisions"][0]["locked_model_id"] == "x/model"


def test_repeated_refresh_does_not_duplicate_the_revision():
    history, first = record(weekly_picks.empty_history(),
                            [row("x/model", "X", 1.0, intelligence=50)], TUESDAY)
    key = first["week_key"]
    rows = [row("x/model", "X", 1.0, intelligence=50), row("y/model", "Y", 0.5, intelligence=50)]

    history, _ = record(history, rows, THURSDAY)
    history, third = record(history, rows, FRIDAY)

    assert third["metrics"]["intelligence"] == "unchanged"
    assert history["weeks"][key]["picks"]["intelligence"]["revision_count"] == 1


def test_unchanged_refresh_stays_locked_and_quiet():
    history, first = record(weekly_picks.empty_history(),
                            [row("x/model", "X", 1.0, intelligence=50)], TUESDAY)
    history, second = record(history, [row("x/model", "X", 1.0, intelligence=50)], FRIDAY)
    assert second["metrics"]["intelligence"] == "unchanged"
    assert history["weeks"][first["week_key"]]["picks"]["intelligence"]["revision_count"] == 0


def test_new_week_locks_the_moved_leader():
    history, first = record(weekly_picks.empty_history(),
                            [row("x/model", "X", 1.0, intelligence=50)], TUESDAY)
    rows = [row("x/model", "X", 1.0, intelligence=50), row("y/model", "Y", 0.5, intelligence=50)]
    history, _ = record(history, rows, THURSDAY)

    history, second = record(history, rows, NEXT_MONDAY)

    assert second["week_key"] == "2026-09-14_2026-09-20"
    assert second["metrics"]["intelligence"] == "locked"
    assert history["weeks"]["2026-09-14_2026-09-20"]["picks"]["intelligence"]["model_id"] == "y/model"
    # the previous week's record is preserved as history
    assert history["weeks"][first["week_key"]]["picks"]["intelligence"]["model_id"] == "x/model"
    assert len(history["weeks"]) == 2


def test_metrics_are_tracked_independently():
    rows = [row("x/model", "X", 1.0, intelligence=50, coding=10, agentic=5),
            row("y/model", "Y", 1.0, intelligence=10, coding=50, agentic=5)]
    history, summary = record(weekly_picks.empty_history(), rows, TUESDAY)
    picks = history["weeks"][summary["week_key"]]["picks"]
    assert picks["intelligence"]["model_id"] == "x/model"
    assert picks["coding"]["model_id"] == "y/model"
    assert set(summary["metrics"]) == {"intelligence", "coding", "agentic"}


def test_metric_with_no_eligible_models_is_reported_not_recorded():
    history, summary = record(weekly_picks.empty_history(),
                              [row("x/model", "X", 1.0, intelligence=50)], TUESDAY)
    assert summary["metrics"]["coding"] == "no-eligible-models"
    assert "coding" not in history["weeks"][summary["week_key"]]["picks"]


def test_free_models_never_become_the_pick():
    history, summary = record(weekly_picks.empty_history(),
                              [row("free/model", "Free", 0.0, intelligence=90),
                               row("paid/model", "Paid", 1.0, intelligence=10)], TUESDAY)
    entry = history["weeks"][summary["week_key"]]["picks"]["intelligence"]
    assert entry["model_id"] == "paid/model"


# ---------------------------------------------------------------------------
# persistence and embedding
# ---------------------------------------------------------------------------
def test_history_round_trips_through_disk(tmp_path):
    history, summary = record(weekly_picks.empty_history(),
                              [row("x/model", "X", 1.0, intelligence=50)], TUESDAY)
    path = tmp_path / "weekly_picks.json"
    weekly_picks.save_history(history, path)

    reloaded = weekly_picks.load_history(path)
    assert reloaded["weeks"][summary["week_key"]]["picks"]["intelligence"]["model_id"] == "x/model"
    assert reloaded["timezone"] == "Asia/Manila"


def test_corrupt_history_degrades_to_empty_instead_of_raising(tmp_path):
    path = tmp_path / "weekly_picks.json"
    path.write_text("{ not valid json", encoding="utf-8")
    history = weekly_picks.load_history(path)
    assert history["weeks"] == {}


def test_missing_history_file_degrades_to_empty(tmp_path):
    assert weekly_picks.load_history(tmp_path / "nope.json")["weeks"] == {}


def test_recording_into_a_corrupt_history_still_works(tmp_path):
    history, summary = record(weekly_picks.load_history(tmp_path / "nope.json"),
                              [row("x/model", "X", 1.0, intelligence=50)], TUESDAY)
    assert summary["metrics"]["intelligence"] == "locked"


def test_embed_view_exposes_the_current_week_only():
    history, _ = record(weekly_picks.empty_history(),
                        [row("x/model", "X", 1.0, intelligence=50)], TUESDAY)
    view = weekly_picks.embed_view(history, now=FRIDAY)

    assert view["timezone"] == "Asia/Manila"
    assert view["week_key"] == "2026-09-07_2026-09-13"
    assert view["week_label"] == "Mon 7 Sep - Sun 13 Sep 2026"
    assert view["history_weeks"] == 1
    assert view["picks"]["intelligence"]["model_id"] == "x/model"
    assert view["picks"]["intelligence"]["current_leader_differs"] is False
    # the dashboard never needs the full revision log, just the count
    assert "revisions" not in view["picks"]["intelligence"]


def test_embed_view_is_empty_but_valid_without_history():
    view = weekly_picks.embed_view(None, now=FRIDAY)
    assert view["picks"] == {}
    assert view["week_label"] == "Mon 7 Sep - Sun 13 Sep 2026"
