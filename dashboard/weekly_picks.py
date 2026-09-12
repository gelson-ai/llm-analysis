#!/usr/bin/env python3
"""
Weekly "Model of the Week" tracking.

The dashboard's headline recommendation is the best-value model (the selected
Artificial Analysis composite index divided by fixed blended price). That
ranking already existed, but it had no memory: it recomputed on every metric
toggle and nothing was ever recorded, so nothing could be compared week over
week.

This module gives it a memory. A week runs Monday 00:00:00 through Sunday
23:59:59 in Asia/Manila, and the week's pick is LOCKED on the first refresh of
that week:

  - the first refresh of a week records the current #1 for each metric;
  - later refreshes in the same week never overwrite that pick;
  - if a later refresh produces a different #1, the change is appended to
    ``revisions`` so mid-week movement is visible rather than silently lost;
  - the new #1 normally becomes next week's pick, because next Monday's first
    refresh locks from then-current data.

Asia/Manila is implemented as a fixed UTC+8 offset rather than through
``zoneinfo``: the Philippines has had no daylight saving since 1978, and
``zoneinfo`` would need the extra ``tzdata`` package on Windows. This keeps the
project dependency-free.

Pure logic only - no network calls, and no file writes at import time. Every
function takes its inputs as arguments so it can be unit tested without running
the pipeline. ``dashboard/refresh.py`` wires it to the real data.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HISTORY_PATH = PROJECT_ROOT / "data" / "analysis" / "weekly_picks.json"

HISTORY_VERSION = 1
TIMEZONE_NAME = "Asia/Manila"
MANILA_TZ = timezone(timedelta(hours=8), TIMEZONE_NAME)

METRICS: tuple[str, ...] = ("intelligence", "coding", "agentic")
METRIC_LABELS = {
    "intelligence": "AA Intelligence Index",
    "coding": "AA Coding Index",
    "agentic": "AA Agentic Index",
}

# build_dashboard.build_payload() emits each model as a positional list, kept
# terse on purpose (428 models x 12 columns are embedded in a single HTML file).
ROW_FIELDS = (
    "model_id",
    "model_name",
    "provider",
    "blended_price",
    "input_price",
    "output_price",
    "context_length",
    "intelligence",
    "coding",
    "agentic",
    "has_price",
    "is_dynamic",
)

CONTENDERS_KEPT = 3

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------
def _fmt_day(value: date) -> str:
    """Deterministic across platforms - strftime('%b') is locale dependent."""
    return f"{_WEEKDAYS[value.weekday()]} {value.day} {_MONTHS[value.month - 1]}"


def format_week_label(week_start: date, week_end: date) -> str:
    """e.g. 'Mon 7 Sep - Sun 13 Sep 2026'."""
    return (
        f"{_fmt_day(week_start)} - {_fmt_day(week_end)} {week_end.year}"
    )


def _fmt_local_moment(moment: datetime) -> str:
    local = moment.astimezone(MANILA_TZ)
    return f"{_fmt_day(local.date())} {local.year}, {local.strftime('%H:%M')} PHT"


def _as_utc(moment: Optional[datetime]) -> datetime:
    moment = moment or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment


# ---------------------------------------------------------------------------
# Week boundaries
# ---------------------------------------------------------------------------
def week_bounds(moment: Optional[datetime] = None) -> tuple[date, date]:
    """Return (Monday, Sunday) of the Manila week containing ``moment``.

    Returns calendar dates in Manila local terms. Note that a Manila day can
    start on the previous UTC date, which is exactly why this conversion
    happens in one place instead of being duplicated at call sites.
    """
    local = _as_utc(moment).astimezone(MANILA_TZ)
    monday = local.date() - timedelta(days=local.weekday())
    return monday, monday + timedelta(days=6)


def week_key(week_start: date, week_end: date) -> str:
    """History key - explicit dates rather than an ISO week number, so the
    stored record says exactly which days it covers."""
    return f"{week_start.isoformat()}_{week_end.isoformat()}"


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------
def as_row_dicts(rows: Any) -> list[dict]:
    """Accept either the positional lists build_payload() emits or dicts."""
    out: list[dict] = []
    for row in rows or []:
        if isinstance(row, dict):
            out.append(row)
        else:
            out.append(dict(zip(ROW_FIELDS, row)))
    return out


def value_ratio(score: Optional[float], price: Optional[float]) -> Optional[float]:
    """Selected index divided by blended price per million tokens.

    Free (price == 0), unpriced and unscored models are excluded - the same
    rule the live dashboard uses, so the recorded pick can never disagree with
    what a viewer sees.
    """
    if score is None or price is None:
        return None
    if price <= 0:
        return None
    return score / price


def ranked_rows(rows: Any, metric: str) -> list[tuple[float, dict]]:
    """Rank eligible models by value ratio, best first.

    Ties are broken deterministically (higher score, then cheaper, then model
    id) so a lock never depends on incidental dict/API ordering.
    """
    scored: list[tuple[float, dict]] = []
    for row in as_row_dicts(rows):
        ratio = value_ratio(row.get(metric), row.get("blended_price"))
        if ratio is None:
            continue
        scored.append((ratio, row))

    def sort_key(item: tuple[float, dict]):
        ratio, row = item
        price = row.get("blended_price")
        return (
            -ratio,
            -(row.get(metric) or 0.0),
            price if price is not None else float("inf"),
            row.get("model_id") or "",
        )

    scored.sort(key=sort_key)
    return scored


def leader_for(rows: Any, metric: str) -> Optional[tuple[float, dict]]:
    ranked = ranked_rows(rows, metric)
    return ranked[0] if ranked else None


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------
def empty_history() -> dict:
    return {
        "version": HISTORY_VERSION,
        "timezone": TIMEZONE_NAME,
        "updated_at": None,
        "weeks": {},
    }


def load_history(path: Optional[Path | str] = None) -> dict:
    """Never raises: a missing or corrupt history simply starts empty, because
    losing the file must not stop the dashboard from being rebuilt."""
    target = Path(path) if path else HISTORY_PATH
    if not target.exists():
        return empty_history()
    try:
        with open(target, encoding="utf-8") as handle:
            data = json.load(handle)
    except (ValueError, OSError):
        return empty_history()
    if not isinstance(data, dict) or not isinstance(data.get("weeks"), dict):
        return empty_history()
    data.setdefault("version", HISTORY_VERSION)
    data.setdefault("timezone", TIMEZONE_NAME)
    data.setdefault("updated_at", None)
    return data


def save_history(history: dict, path: Optional[Path | str] = None) -> Path:
    """Write via a temp file + replace so a crash mid-write cannot leave a
    truncated history behind."""
    target = Path(path) if path else HISTORY_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(history, handle, indent=2, default=str)
        handle.write("\n")
    tmp.replace(target)
    return target


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------
def _contenders(ranked: list[tuple[float, dict]], metric: str) -> list[dict]:
    out = []
    for rank, (ratio, row) in enumerate(ranked[:CONTENDERS_KEPT], start=1):
        out.append({
            "rank": rank,
            "model_id": row.get("model_id"),
            "model_name": row.get("model_name"),
            "provider": row.get("provider"),
            "score": row.get(metric),
            "blended_price": row.get("blended_price"),
            "value": round(ratio, 4),
        })
    return out


def _same_model(entry: dict, row: dict) -> bool:
    return entry.get("model_id") == row.get("model_id")


def _compact_leader(ratio: float, row: dict, metric: str) -> dict:
    return {
        "model_id": row.get("model_id"),
        "model_name": row.get("model_name"),
        "provider": row.get("provider"),
        "score": row.get(metric),
        "blended_price": row.get("blended_price"),
        "value": round(ratio, 4),
    }


def record_weekly_picks(
    history: Optional[dict],
    payload: dict,
    now: Optional[datetime] = None,
    source: str = "refresh",
) -> tuple[dict, dict]:
    """Lock this week's pick per metric, or note that the leader has moved.

    Returns ``(history, summary)``. ``history`` is mutated in place and also
    returned for convenience. The summary maps each metric to one of:
    ``locked`` (recorded now), ``unchanged``, ``revised`` (a different #1 was
    seen but the locked pick stands) or ``no-eligible-models``.
    """
    moment = _as_utc(now)
    rows = as_row_dicts(payload.get("rows", []))
    data_retrieved_at = payload.get("data_retrieved_at")

    week_start, week_end = week_bounds(moment)
    key = week_key(week_start, week_end)

    history = history if isinstance(history, dict) and history else empty_history()
    history.setdefault("version", HISTORY_VERSION)
    history["timezone"] = TIMEZONE_NAME
    history["updated_at"] = moment.isoformat()

    weeks = history.setdefault("weeks", {})
    week = weeks.get(key)
    if week is None:
        week = {
            "week_start": week_start.isoformat(),
            "week_end": week_end.isoformat(),
            "week_label": format_week_label(week_start, week_end),
            "iso_week": "%04d-W%02d" % (week_start.isocalendar()[0], week_start.isocalendar()[1]),
            "picks": {},
        }
        weeks[key] = week

    summary: dict[str, str] = {}

    for metric in METRICS:
        ranked = ranked_rows(rows, metric)
        if not ranked:
            summary[metric] = "no-eligible-models"
            continue

        ratio, leader = ranked[0]
        entry = week["picks"].get(metric)

        if entry is None:
            entry = {
                "metric": metric,
                "metric_label": METRIC_LABELS[metric],
                "model_id": leader.get("model_id"),
                "model_name": leader.get("model_name"),
                "provider": leader.get("provider"),
                "score": leader.get(metric),
                "blended_price": leader.get("blended_price"),
                "value": round(ratio, 4),
                "context_length": leader.get("context_length"),
                "recorded_at": moment.isoformat(),
                "recorded_local": moment.astimezone(MANILA_TZ).isoformat(),
                "recorded_label": _fmt_local_moment(moment),
                "data_retrieved_at": data_retrieved_at,
                "locked_by": source,
                "contenders": _contenders(ranked, metric),
                "revisions": [],
            }
            week["picks"][metric] = entry
            summary[metric] = "locked"
        elif _same_model(entry, leader):
            summary[metric] = "unchanged"
        else:
            revisions = entry.setdefault("revisions", [])
            already_logged = bool(revisions) and revisions[-1].get("new_leader_model_id") == leader.get("model_id")
            if not already_logged:
                revisions.append({
                    "at": moment.isoformat(),
                    "at_label": _fmt_local_moment(moment),
                    "new_leader_model_id": leader.get("model_id"),
                    "new_leader_model_name": leader.get("model_name"),
                    "new_leader_value": round(ratio, 4),
                    "locked_model_id": entry.get("model_id"),
                    "locked_model_name": entry.get("model_name"),
                })
                summary[metric] = "revised"
            else:
                summary[metric] = "unchanged"

        entry["current_leader"] = _compact_leader(ratio, leader, metric)
        entry["current_leader_differs"] = not _same_model(entry, leader)
        entry["revision_count"] = len(entry.get("revisions", []))

    return history, {
        "week_key": key,
        "week_label": week["week_label"],
        "iso_week": week.get("iso_week"),
        "metrics": summary,
    }


# ---------------------------------------------------------------------------
# Dashboard embedding
# ---------------------------------------------------------------------------
def embed_view(history: Optional[dict], now: Optional[datetime] = None) -> dict:
    """The slice of history the dashboard embeds in its payload.

    Deliberately small: the browser only needs the current week's locked pick
    per metric plus enough context to explain it. The full history stays in
    data/analysis/weekly_picks.json.
    """
    moment = _as_utc(now)
    week_start, week_end = week_bounds(moment)
    key = week_key(week_start, week_end)

    weeks = (history or {}).get("weeks") or {}
    week = weeks.get(key) or {}

    picks: dict[str, dict] = {}
    for metric, entry in (week.get("picks") or {}).items():
        picks[metric] = {
            "model_id": entry.get("model_id"),
            "model_name": entry.get("model_name"),
            "provider": entry.get("provider"),
            "score": entry.get("score"),
            "blended_price": entry.get("blended_price"),
            "value": entry.get("value"),
            "context_length": entry.get("context_length"),
            "recorded_label": entry.get("recorded_label"),
            "locked_by": entry.get("locked_by"),
            "revision_count": entry.get("revision_count", 0),
            "current_leader": entry.get("current_leader"),
            "current_leader_differs": bool(entry.get("current_leader_differs")),
        }

    return {
        "timezone": TIMEZONE_NAME,
        "week_key": key,
        "week_label": week.get("week_label") or format_week_label(week_start, week_end),
        "iso_week": week.get("iso_week"),
        "picks": picks,
        "history_weeks": len(weeks),
        "history_updated_at": (history or {}).get("updated_at"),
    }
