#!/usr/bin/env python3
"""
Refresh the OpenRouter video-model data and rebuild the video dashboard.

Usage:
    python dashboard/refresh_video.py [--skip-fetch] [--no-snapshot]
                                      [--max-age-hours 192] [--json]

Deliberately a SIBLING of dashboard/refresh.py (chat) and
dashboard/refresh_media.py (image) rather than a mode of either. The video
pipeline must never be able to break the other two dashboards, so the three are
separate processes with separate locks, separate status files and separate exit
codes. dashboard/refresh_all.py orchestrates them locally and a workflow step
does so in CI; nothing merges their code paths.

Steps, in order:
  1. run run_video_pipeline.py (network access; ~20s: one catalogue request plus
     12 benchmark prompt pages) unless --skip-fetch. If it fails, abort WITHOUT
     rebuilding, so the last good video dashboard stays published rather than
     being replaced by a half-updated one;
  2. lock this week's Model of the Week;
  3. run build_video_dashboard.py to republish the HTML;
  4. write dashboard/video_status.json.

Every path here is the video dashboard's own. Nothing writes to status.json,
media_status.json, data/snapshots/ or data/media_snapshots/ - the media
pipeline's outputs are embedded verbatim in the published image page, so a video
run must not be able to touch them at all.

Exit codes:
    0 - refresh completed, video dashboard republished
    1 - refresh failed; nothing was republished
    2 - another video refresh is already running
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

DASHBOARD_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = DASHBOARD_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(DASHBOARD_DIR) not in sys.path:
    sys.path.insert(0, str(DASHBOARD_DIR))

from config import settings  # noqa: E402  (needs the path insert above)
import build_video_dashboard  # noqa: E402
import video_picks  # noqa: E402  (this dashboard's OWN weekly picks)
from refresh_lock import RefreshLock  # noqa: E402

VIDEO_PIPELINE_SCRIPT = PROJECT_ROOT / "run_video_pipeline.py"
BUILD_SCRIPT = DASHBOARD_DIR / "build_video_dashboard.py"

# This pipeline's OWN lock, in the same directory as the other two but a
# different file: the whole point is that a stuck video run cannot block, or be
# blocked by, a chat or image refresh.
LOCK_PATH = settings.ANALYSIS_DIR / ".video_refresh.lock"

# This dashboard's OWN status sidecar, written from the same payload the HTML was
# built from. Deliberately NOT staged into _site: the deployed Worker reads the
# repository copy via the Contents API, and nothing in the browser fetches it
# directly. build_video_dashboard.py has no STATUS_PATH of its own - an invariant
# test asserts it writes exactly one file - so this script writes it instead.
STATUS_PATH = DASHBOARD_DIR / "video_status.json"


def utc_today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def video_snapshot_exists_for(date_str: str) -> bool:
    """Whether a video snapshot for this UTC date already exists.

    Mirrors the one-snapshot-per-day relaxation the other two pipelines use, but
    scans the VIDEO snapshot root only. It must never look at data/snapshots/ or
    data/media_snapshots/: refresh.py::snapshot_exists_for() treats any directory
    directly under data/snapshots/ named <date> or <date>-N as "today's chat
    snapshot exists", so a video run writing there would silently suppress the
    day's chat snapshot. tests/test_video_snapshot_paths.py locks that down.
    """
    root = settings.VIDEO_DASHBOARD_SNAPSHOTS_DIR
    if not root.exists():
        return False
    prefix = date_str + "-"
    for entry in root.iterdir():
        if entry.is_dir() and (entry.name == date_str or entry.name.startswith(prefix)):
            return True
    return False


def run_refresh(
    skip_fetch: bool = False,
    no_snapshot: bool = False,
    max_age_hours: float = build_video_dashboard.DEFAULT_MAX_AGE_HOURS,
    on_step=None,
) -> dict:
    """Run the video refresh. Returns a structured result; never raises for
    expected failures (network down, build refused, concurrent run)."""
    started = time.time()
    result: dict = {
        "ok": False,
        "target": "video",
        "steps": [],
        "started_at": datetime.now(timezone.utc).isoformat(),
        "dashboard": str(build_video_dashboard.OUTPUT_PATH),
    }

    def step(name: str, **extra) -> None:
        entry = {"step": name, "at": datetime.now(timezone.utc).isoformat(), **extra}
        result["steps"].append(entry)
        if on_step:
            on_step(entry)

    lock = RefreshLock(LOCK_PATH)
    if not lock.acquire():
        result["error"] = "refresh_already_running"
        result["message"] = "Another video refresh is already in progress."
        result["duration_seconds"] = round(time.time() - started, 2)
        result["finished_at"] = datetime.now(timezone.utc).isoformat()
        return result

    try:
        # ---- 1. Fetch -----------------------------------------------------
        if skip_fetch:
            step("fetch", skipped=True, reason="--skip-fetch")
        else:
            wanted_snapshot = not no_snapshot
            if wanted_snapshot and video_snapshot_exists_for(utc_today()):
                # One snapshot per day instead of one per click, matching the
                # other two pipelines: a refresh button repeated all afternoon
                # would otherwise grow data/video_snapshots/ without bound.
                wanted_snapshot = False

            cmd = [sys.executable, str(VIDEO_PIPELINE_SCRIPT)]
            if not wanted_snapshot:
                cmd.append("--no-snapshot")

            proc = subprocess.run(cmd, cwd=str(PROJECT_ROOT), capture_output=True, text=True)
            step("fetch", exit_code=proc.returncode, snapshot=wanted_snapshot)
            if proc.returncode != 0:
                result["error"] = "video_pipeline_failed"
                result["message"] = (
                    "Video data fetch failed, so the video dashboard was left untouched. "
                    "The previously published snapshot is still being served."
                )
                result["detail"] = (proc.stderr or proc.stdout or "").strip()[-1500:]
                return result

        # ---- 2. Lock this week's Model of the Week -------------------------
        # BEFORE the rebuild, deliberately: recording the pick after the build
        # would mean this week's pick did not appear on the page until the NEXT
        # refresh, so the page would render last week's state for a whole week.
        #
        # The payload is built once here and reused below, so one object decides
        # what gets locked AND what the status sidecar reports.
        payload = build_video_dashboard.build_payload()

        history = video_picks.load_history()
        history, pick_summary = video_picks.record_pick(history, payload)
        video_picks.save_history(history)
        step("weekly_pick", state=pick_summary.get("state"), week=pick_summary.get("week_key"))

        # ---- 3. Rebuild ----------------------------------------------------
        build_cmd = [
            sys.executable,
            str(BUILD_SCRIPT),
            "--max-age-hours", str(max_age_hours),
        ]
        build = subprocess.run(build_cmd, cwd=str(PROJECT_ROOT), capture_output=True, text=True)
        step("build", exit_code=build.returncode)
        if build.returncode != 0:
            result["error"] = "build_failed"
            result["message"] = (
                "The video dashboard rebuild was refused, so the previous HTML is still in place."
            )
            result["detail"] = (build.stderr or build.stdout or "").strip()[-1500:]
            return result

        # ---- 4. Stamp this dashboard's own status sidecar -------------------
        status = {
            "version": 1,
            "pipeline": "video",
            "retrieved_at": payload.get("data_retrieved_at"),
            "generated_at": payload.get("generated_at"),
            "model_count": len(payload.get("rows") or []),
        }
        tmp_status = STATUS_PATH.with_name(STATUS_PATH.name + ".tmp")
        with open(tmp_status, "w", encoding="utf-8") as handle:
            json.dump(status, handle, indent=2)
        os.replace(tmp_status, STATUS_PATH)
        step("status", path=str(STATUS_PATH))

        result["ok"] = True
        result["retrieved_at"] = status["retrieved_at"]
        result["model_count"] = status["model_count"]
        result["status_path"] = str(STATUS_PATH)
        return result
    finally:
        result["duration_seconds"] = round(time.time() - started, 2)
        result["finished_at"] = datetime.now(timezone.utc).isoformat()
        lock.release()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--skip-fetch", action="store_true",
                        help="Rebuild from the video data already on disk (no network)")
    parser.add_argument("--no-snapshot", action="store_true",
                        help="Never write a timestamped video snapshot for this run")
    parser.add_argument("--max-age-hours", type=float,
                        default=build_video_dashboard.DEFAULT_MAX_AGE_HOURS,
                        help="Freshness window enforced by the video build "
                             f"(default {build_video_dashboard.DEFAULT_MAX_AGE_HOURS:g})")
    parser.add_argument("--json", action="store_true",
                        help="Print the result as a single JSON object (used by dashboard/refresh_all.py)")
    args = parser.parse_args()

    result = run_refresh(
        skip_fetch=args.skip_fetch,
        no_snapshot=args.no_snapshot,
        max_age_hours=args.max_age_hours,
    )

    if args.json:
        print(json.dumps(result, default=str))
    else:
        if result["ok"]:
            print(f"OK: video data refreshed and {build_video_dashboard.OUTPUT_PATH.name} rebuilt.")
        else:
            print(f"FAILED: {result.get('message', result.get('error'))}", file=sys.stderr)
            detail = result.get("detail")
            if detail:
                print(detail, file=sys.stderr)

    if result.get("error") == "refresh_already_running":
        return 2
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
