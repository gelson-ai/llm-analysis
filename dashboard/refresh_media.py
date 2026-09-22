#!/usr/bin/env python3
"""
Refresh the OpenRouter media (image/video) data and rebuild the image dashboard.

Usage:
    python dashboard/refresh_media.py [--skip-fetch] [--no-snapshot]
                                      [--max-age-hours 192] [--json]

Deliberately a SIBLING of dashboard/refresh.py rather than a mode of it. The
media pipeline must never be able to break the chat dashboard, so the two are
separate processes with separate locks, separate status files and separate exit
codes. Something else orchestrates them - dashboard/refresh_all.py locally, a
workflow step in CI - and nothing merges their code paths.

Steps, in order:
  1. run run_media_pipeline.py (network access; ~90s: 52 per-model image
     endpoint records + 27 benchmark prompt pages) unless --skip-fetch. If it
     fails, abort WITHOUT rebuilding, so the last good image dashboard stays
     published rather than being replaced by a half-updated one;
  2. run build_image_dashboard.py to republish the HTML;
  3. write dashboard/media_status.json.

Every path here is the media pipeline's own. Nothing writes to status.json,
data/snapshots/ or any other chat-side surface.

Exit codes:
    0 - refresh completed, image dashboard republished
    1 - refresh failed; nothing was republished
    2 - another media refresh is already running
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
import build_image_dashboard  # noqa: E402
from refresh_lock import RefreshLock  # noqa: E402

MEDIA_PIPELINE_SCRIPT = PROJECT_ROOT / "run_media_pipeline.py"
BUILD_SCRIPT = DASHBOARD_DIR / "build_image_dashboard.py"

# This pipeline's OWN lock. It must never be the chat pipeline's
# data/analysis/.refresh.lock: the whole point is that a stuck media run cannot
# block, or be blocked by, a chat refresh.
LOCK_PATH = settings.ANALYSIS_DIR / ".media_refresh.lock"

# This pipeline's OWN status sidecar. Named for the media pipeline, written from
# the same payload the HTML was built from, and NOT staged into _site (the
# deployed Worker reads the repository copy; nothing in the browser fetches it
# directly). build_image_dashboard.py deliberately has no STATUS_PATH of its own
# - an invariant test asserts it writes exactly one file - so the orchestrating
# script writes this instead.
STATUS_PATH = DASHBOARD_DIR / "media_status.json"


def utc_today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def media_snapshot_exists_for(date_str: str) -> bool:
    """Whether a media snapshot for this UTC date already exists.

    Mirrors the chat pipeline's one-snapshot-per-day relaxation, but scans the
    MEDIA snapshot root only. It must never look at data/snapshots/: the chat
    pipeline's refresh.py::snapshot_exists_for() treats any directory directly
    under data/snapshots/ named <date> or <date>-N as "today's chat snapshot
    exists", so a media run writing there would silently suppress the chat
    snapshot for the day. tests/test_media_snapshot_paths.py locks that down.
    """
    root = settings.MEDIA_SNAPSHOTS_DIR
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
    max_age_hours: float = build_image_dashboard.DEFAULT_MAX_AGE_HOURS,
    on_step=None,
) -> dict:
    """Run the media refresh. Returns a structured result; never raises for
    expected failures (network down, build refused, concurrent run)."""
    started = time.time()
    result: dict = {
        "ok": False,
        "target": "media",
        "steps": [],
        "started_at": datetime.now(timezone.utc).isoformat(),
        "dashboard": str(build_image_dashboard.OUTPUT_PATH),
    }

    def step(name: str, **extra) -> None:
        entry = {"step": name, "at": datetime.now(timezone.utc).isoformat(), **extra}
        result["steps"].append(entry)
        if on_step:
            on_step(entry)

    lock = RefreshLock(LOCK_PATH)
    if not lock.acquire():
        result["error"] = "refresh_already_running"
        result["message"] = "Another media refresh is already in progress."
        result["duration_seconds"] = round(time.time() - started, 2)
        result["finished_at"] = datetime.now(timezone.utc).isoformat()
        return result

    try:
        # ---- 1. Fetch -----------------------------------------------------
        if skip_fetch:
            step("fetch", skipped=True, reason="--skip-fetch")
        else:
            wanted_snapshot = not no_snapshot
            if wanted_snapshot and media_snapshot_exists_for(utc_today()):
                # One snapshot per day instead of one per click, matching the
                # chat pipeline: a refresh button repeated all afternoon would
                # otherwise grow data/media_snapshots/ without bound.
                wanted_snapshot = False

            cmd = [sys.executable, str(MEDIA_PIPELINE_SCRIPT)]
            if not wanted_snapshot:
                cmd.append("--no-snapshot")

            proc = subprocess.run(cmd, cwd=str(PROJECT_ROOT), capture_output=True, text=True)
            step("fetch", exit_code=proc.returncode, snapshot=wanted_snapshot)
            if proc.returncode != 0:
                result["error"] = "media_pipeline_failed"
                result["message"] = (
                    "Media data fetch failed, so the image dashboard was left untouched. "
                    "The previously published snapshot is still being served."
                )
                result["detail"] = (proc.stderr or proc.stdout or "").strip()[-1500:]
                return result

        # ---- 2. Rebuild ----------------------------------------------------
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
                "The image dashboard rebuild was refused, so the previous HTML is still in place."
            )
            result["detail"] = (build.stderr or build.stdout or "").strip()[-1500:]
            return result

        # ---- 3. Stamp this pipeline's own status sidecar -------------------
        # Read back the payload the build just used, so the status can never
        # disagree with the published HTML about which snapshot it describes.
        payload = build_image_dashboard.build_payload()
        status = {
            "version": 1,
            "pipeline": "media",
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
                        help="Rebuild from the media data already on disk (no network)")
    parser.add_argument("--no-snapshot", action="store_true",
                        help="Never write a timestamped media snapshot for this run")
    parser.add_argument("--max-age-hours", type=float,
                        default=build_image_dashboard.DEFAULT_MAX_AGE_HOURS,
                        help="Freshness window enforced by the image build "
                             f"(default {build_image_dashboard.DEFAULT_MAX_AGE_HOURS:g})")
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
            print(f"OK: media data refreshed and {build_image_dashboard.OUTPUT_PATH.name} rebuilt.")
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
