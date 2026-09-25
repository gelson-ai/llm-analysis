#!/usr/bin/env python3
"""
Weekly "Model of the Week" for the VIDEO dashboard.

The video counterpart of dashboard/media_picks.py (image) and
dashboard/weekly_picks.py (chat), and deliberately a separate module with its own
history file: the three dashboards lock picks from different rankings over
different model sets, so one shared history file would let any pipeline overwrite
another's record - the same reasoning that keeps their lock files separate.

It reuses weekly_picks' GENERIC pieces - week boundaries, the Manila week label,
the timestamp label, history load/save with an explicit path - rather than
re-implementing the timezone handling, which is the part most likely to be subtly
wrong. It does NOT reuse that module's ranking, which is chat-specific.

Semantics are identical to the other two, because they are what makes a pick mean
anything: a week runs Monday 00:00:00 to Sunday 23:59:59 Asia/Manila, the pick is
LOCKED on the first refresh of that week, and a later refresh whose leader differs
appends a revision and is reported as "current leader today" rather than
overwriting the locked pick.

Ranking eligibility adds the video dashboard's minimum-evidence gate
(>= 8 prompts AND >= 40 attempted checks) on top of "has a value score", because
a value drawn from a single 5-check prompt must not take the week. The gate lives
in src/video_coverage.py and is imported rather than restated, so the pick and the
rank the page prints beside it cannot disagree.

Pure logic - no network, and no file writes at import time.
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

DASHBOARD_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = DASHBOARD_DIR.parent
for _path in (str(PROJECT_ROOT), str(DASHBOARD_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from config import settings  # noqa: E402  (needs the path insert above)
import weekly_picks  # noqa: E402  (generic week/history helpers only)
from src.video_coverage import (  # noqa: E402
    MIN_CHECKS_FOR_RANKING,
    MIN_PROMPTS_FOR_RANKING,
    meets_evidence_gate,
)

METRIC = "value"
METRIC_LABEL = "Value (pass rate / benchmark cost per clip)"
CONTENDERS_KEPT = 3


def history_path() -> Path:
    """This dashboard's OWN history file, from settings like every other input."""
    return settings.VIDEO_DASHBOARD_WEEKLY_PICKS_PATH


def empty_history() -> dict:
    return weekly_picks.empty_history()


def load_history(path: Optional[Path | str] = None) -> dict:
    return weekly_picks.load_history(path or history_path())


def save_history(history: dict, path: Optional[Path | str] = None) -> Path:
    return weekly_picks.save_history(history, path or history_path())


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------
def week_context(moment: datetime) -> dict:
    """The identity of the week containing ``moment``, derived in ONE place.

    record_pick and embed_view both need this, and if they derived it separately
    they could disagree about which week the page is describing - the lock would
    land in one week and the hero would label another.
    """
    week_start, week_end = weekly_picks.week_bounds(moment)
    iso_year, iso_week = week_start.isocalendar()[:2]
    return {
        "start": week_start,
        "end": week_end,
        "key": weekly_picks.week_key(week_start, week_end),
        "label": weekly_picks.format_week_label(week_start, week_end),
        "iso_week": "%04d-W%02d" % (iso_year, iso_week),
    }


def eligible(row: dict) -> bool:
    """Whether a row may be ranked at all.

    `rankable` is set by the builder as "has a value score AND clears the
    minimum-evidence gate", so this mirrors the value_rank the page prints.
    """
    return bool(row.get("rankable"))


def ranked_rows(rows: Optional[list[dict]]) -> list[tuple[float, dict]]:
    """Eligible models ranked by value, best first.

    The tie-break is the dashboard's own - model id ascending - so the locked
    pick can never disagree with the rank printed beside it.
    """
    scored: list[tuple[float, dict]] = [
        (row["value"], row) for row in (rows or [])
        if row.get("value") is not None and eligible(row)
    ]
    scored.sort(key=lambda pair: (-pair[0], pair[1].get("model_id") or ""))
    return scored


def leader_for(rows: Optional[list[dict]]) -> Optional[tuple[float, dict]]:
    ranked = ranked_rows(rows)
    return ranked[0] if ranked else None


def _compact(ratio: float, row: dict) -> dict:
    """The fields worth keeping for one contender, recorded rather than
    recomputed, so a past week's record still means what it meant then."""
    return {
        "model_id": row.get("model_id"),
        "model_name": row.get("model_name"),
        "provider": row.get("provider"),
        "value": round(ratio, 4),
        "pass_rate": row.get("pass_rate"),
        "avg_cost_usd": row.get("avg_cost_usd"),
        "prompts": row.get("prompts"),
        "checks_total": row.get("checks_total"),
    }


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------
def record_pick(
    history: Optional[dict],
    payload: dict,
    now: Optional[datetime] = None,
    source: str = "refresh_video",
) -> tuple[dict, dict]:
    """Lock this week's pick, or note that the leader has moved.

    Returns ``(history, summary)``. ``history`` is mutated in place and also
    returned. ``summary["state"]`` is one of ``locked`` (recorded now),
    ``unchanged``, ``revised`` (a different #1 was seen but the locked pick
    stands) or ``no-eligible-models``.
    """
    moment = weekly_picks.as_utc(now)
    rows = payload.get("rows") or []
    data_retrieved_at = payload.get("data_retrieved_at")

    week_info = week_context(moment)
    key = week_info["key"]

    history = history if isinstance(history, dict) and history else weekly_picks.empty_history()
    history["updated_at"] = moment.isoformat()

    weeks = history.setdefault("weeks", {})
    week = weeks.get(key)
    if week is None:
        week = {
            "week_start": week_info["start"].isoformat(),
            "week_end": week_info["end"].isoformat(),
            "week_label": week_info["label"],
            "iso_week": week_info["iso_week"],
            "picks": {},
        }
        weeks[key] = week

    summary: dict = {"week_key": key, "week_label": week["week_label"], "metric": METRIC}

    ranked = ranked_rows(rows)
    if not ranked:
        summary["state"] = "no-eligible-models"
        return history, summary

    ratio, leader = ranked[0]
    entry = week["picks"].get(METRIC)

    if entry is None:
        entry = {
            "metric": METRIC,
            "metric_label": METRIC_LABEL,
            "model_id": leader.get("model_id"),
            "model_name": leader.get("model_name"),
            "provider": leader.get("provider"),
            "value": round(ratio, 4),
            "pass_rate": leader.get("pass_rate"),
            "avg_cost_usd": leader.get("avg_cost_usd"),
            "prompts": leader.get("prompts"),
            "checks_total": leader.get("checks_total"),
            # The evidence the pick rested on, frozen with it - including the
            # fields that qualify the headline: the output resolutions actually
            # generated, whether the catalogue publishes a rate at all, and (for
            # the hero card's caveat) that clip length is not published.
            "output_resolutions": leader.get("output_resolutions") or [],
            "has_catalog_rate": bool(leader.get("has_catalog_rate")),
            "comparable_price": leader.get("comparable_price"),
            "comparable_price_basis": leader.get("comparable_price_basis"),
            "mean_generation_seconds": leader.get("mean_generation_seconds"),
            "recorded_at": moment.isoformat(),
            "recorded_label": weekly_picks.format_local_moment(moment),
            "data_retrieved_at": data_retrieved_at,
            "locked_by": source,
            "contenders": [
                {"rank": rank, **_compact(value, row)}
                for rank, (value, row) in enumerate(ranked[:CONTENDERS_KEPT], start=1)
            ],
            "revisions": [],
        }
        week["picks"][METRIC] = entry
        summary["state"] = "locked"
    elif entry.get("model_id") == leader.get("model_id"):
        summary["state"] = "unchanged"
    else:
        revisions = entry.setdefault("revisions", [])
        already_logged = bool(revisions) and revisions[-1].get("new_leader_model_id") == leader.get("model_id")
        if not already_logged:
            revisions.append({
                "at": moment.isoformat(),
                "at_label": weekly_picks.format_local_moment(moment),
                "new_leader_model_id": leader.get("model_id"),
                "new_leader_model_name": leader.get("model_name"),
                "new_leader_value": round(ratio, 4),
                "locked_model_id": entry.get("model_id"),
                "locked_model_name": entry.get("model_name"),
            })
            summary["state"] = "revised"
        else:
            # Same challenger seen again: already on record, nothing new to log.
            summary["state"] = "unchanged"

    entry["current_leader"] = _compact(ratio, leader)
    entry["current_leader_differs"] = entry.get("model_id") != leader.get("model_id")
    entry["revision_count"] = len(entry.get("revisions", []))
    summary["pick"] = _compact(ratio, leader)
    return history, summary


# ---------------------------------------------------------------------------
# Dashboard embedding
# ---------------------------------------------------------------------------
def embed_view(history: Optional[dict], now: Optional[datetime] = None) -> dict:
    """The slice of history the video dashboard embeds in its payload.

    Deliberately small: the browser needs the current week's locked pick and
    enough context to explain it. The full history stays in
    data/analysis/video_dashboard_weekly_picks.json.
    """
    moment = weekly_picks.as_utc(now)
    week_info = week_context(moment)
    key = week_info["key"]

    weeks = (history or {}).get("weeks") or {}
    week = weeks.get(key) or {}
    entry = (week.get("picks") or {}).get(METRIC) or {}

    picks: dict[str, dict] = {}
    if entry:
        picks[METRIC] = {
            "model_id": entry.get("model_id"),
            "model_name": entry.get("model_name"),
            "provider": entry.get("provider"),
            "value": entry.get("value"),
            "pass_rate": entry.get("pass_rate"),
            "avg_cost_usd": entry.get("avg_cost_usd"),
            "prompts": entry.get("prompts"),
            "checks_total": entry.get("checks_total"),
            "output_resolutions": entry.get("output_resolutions") or [],
            "has_catalog_rate": bool(entry.get("has_catalog_rate")),
            "comparable_price": entry.get("comparable_price"),
            "comparable_price_basis": entry.get("comparable_price_basis"),
            "mean_generation_seconds": entry.get("mean_generation_seconds"),
            "recorded_label": entry.get("recorded_label"),
            "locked_by": entry.get("locked_by"),
            "revision_count": entry.get("revision_count", 0),
            "current_leader": entry.get("current_leader"),
            "current_leader_differs": bool(entry.get("current_leader_differs")),
        }

    return {
        "timezone": weekly_picks.TIMEZONE_NAME,
        "week_key": key,
        # A stored record is authoritative for its own week (it may have been
        # written by an older label format); otherwise the range is still shown,
        # because the hero must render its week before the first lock.
        "week_label": week.get("week_label") or week_info["label"],
        "iso_week": week.get("iso_week") or week_info["iso_week"],
        "picks": picks,
        "history_weeks": len(weeks),
        "history_updated_at": (history or {}).get("updated_at"),
        # Carried in the payload so the hero card can name the gate in its own
        # words without the template hardcoding numbers the module owns.
        "evidence_gate": {
            "min_prompts": MIN_PROMPTS_FOR_RANKING,
            "min_checks": MIN_CHECKS_FOR_RANKING,
        },
    }
