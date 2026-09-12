#!/usr/bin/env python3
"""
Refresh the OpenRouter data and rebuild the dashboard - one command.

Usage:
    python dashboard/refresh.py [--skip-fetch] [--no-snapshot]
                               [--max-age-hours 72] [--json]

Steps, in order:
  1. run run_pipeline.py (network access required) unless --skip-fetch;
     if it fails, abort WITHOUT rebuilding, so the last good dashboard stays
     published rather than being replaced by a half-updated one;
  2. lock this week's Model of the Week for any metric that isn't locked yet;
  3. run build_dashboard.py to republish the HTML.

This is deliberately a standalone CLI rather than logic buried inside the web
server, so the exact same command can later be invoked by a GitHub Action with
no changes. dashboard/serve.py does nothing but execute it.

Exit codes:
    0 - refresh completed, dashboard republished
    1 - refresh failed; nothing was republished
    2 - another refresh is already running
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
sys.path.insert(0, str(DASHBOARD_DIR))

import build_dashboard  # noqa: E402  (sibling module, needs the path above)
import weekly_picks  # noqa: E402

PROJECT_ROOT = build_dashboard.PROJECT_ROOT
DASHBOARD_PATH = build_dashboard.OUTPUT_PATH
SNAPSHOTS_DIR = PROJECT_ROOT / "data" / "snapshots"
LOCK_PATH = PROJECT_ROOT / "data" / "analysis" / ".refresh.lock"

# A refresh normally takes seconds. If the lock file is older than this it can
# only be left over from a process that died, so it is safe to take over.
LOCK_STALE_SECONDS = 1800


class RefreshLock:
    """Cross-process guard so two refreshes cannot interleave.

    Uses O_EXCL creation, so the check and the claim are one atomic step.
    """

    def __init__(self, path: Path = LOCK_PATH, stale_after: int = LOCK_STALE_SECONDS):
        self.path = Path(path)
        self.stale_after = stale_after
        self.acquired = False

    def _write_owner(self) -> None:
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump({"pid": os.getpid(), "started_at": datetime.now(timezone.utc).isoformat()}, handle)

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for attempt in (1, 2):
            try:
                handle = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if attempt == 2:
                    return False
                try:
                    age = time.time() - self.path.stat().st_mtime
                except OSError:
                    age = 0
                if age < self.stale_after:
                    return False
                # Stale lock from a dead process - clear it and retry once.
                try:
                    self.path.unlink()
                except OSError:
                    return False
                continue
            else:
                os.close(handle)
                self._write_owner()
                self.acquired = True
                return True
        return False

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            self.path.unlink()
        except OSError:
            pass
        self.acquired = False


def utc_today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def snapshot_exists_for(date_str: str) -> bool:
    """The pipeline names snapshots <UTC date> then <UTC date>-2, -3, ..."""
    if not SNAPSHOTS_DIR.exists():
        return False
    prefix = date_str + "-"
    for entry in SNAPSHOTS_DIR.iterdir():
        if entry.is_dir() and (entry.name == date_str or entry.name.startswith(prefix)):
            return True
    return False


def run_refresh(
    skip_fetch: bool = False,
    no_snapshot: bool = False,
    max_age_hours: float = 72.0,
    source: str = "refresh",
    on_step=None,
) -> dict:
    """Run the full refresh. Returns a structured result; never raises for
    expected failures (network down, build refused, concurrent run)."""
    started = time.time()
    result: dict = {
        "ok": False,
        "steps": [],
        "started_at": datetime.now(timezone.utc).isoformat(),
        "dashboard": str(DASHBOARD_PATH),
    }

    def step(name: str, **extra) -> None:
        entry = {"step": name, "at": datetime.now(timezone.utc).isoformat(), **extra}
        result["steps"].append(entry)
        if on_step:
            on_step(entry)

    lock = RefreshLock()
    if not lock.acquire():
        result["error"] = "refresh_already_running"
        result["message"] = "Another refresh is already in progress."
        result["duration_seconds"] = round(time.time() - started, 2)
        result["finished_at"] = datetime.now(timezone.utc).isoformat()
        return result

    try:
        # ---- 1. Fetch -----------------------------------------------------
        if skip_fetch:
            step("fetch", skipped=True, reason="--skip-fetch")
        else:
            wanted_snapshot = not no_snapshot
            already_snapshotted = snapshot_exists_for(utc_today())
            if already_snapshotted:
                # One snapshot per day instead of one per click. The README's
                # "every run snapshots" principle is relaxed here on purpose:
                # on-demand refreshes would otherwise grow data/snapshots/
                # without bound.
                wanted_snapshot = False

            cmd = [sys.executable, str(PROJECT_ROOT / "run_pipeline.py")]
            if not wanted_snapshot:
                cmd.append("--no-snapshot")

            proc = subprocess.run(
                cmd, cwd=str(PROJECT_ROOT), capture_output=True, text=True,
            )
            step("fetch", exit_code=proc.returncode, snapshot=wanted_snapshot)
            if proc.returncode != 0:
                result["error"] = "pipeline_failed"
                result["message"] = (
                    "Data fetch failed, so the dashboard was left untouched. "
                    "The previously published snapshot is still being served."
                )
                result["detail"] = (proc.stderr or proc.stdout or "").strip()[-1500:]
                return result

        # ---- 2. Lock this week's picks -------------------------------------
        payload = build_dashboard.build_payload()
        if not payload.get("rows"):
            result["error"] = "no_rows"
            result["message"] = "No models were found after joining, so nothing was recorded or rebuilt."
            return result

        history = weekly_picks.load_history()
        history, summary = weekly_picks.record_weekly_picks(history, payload, source=source)
        weekly_picks.save_history(history)
        step("weekly_picks", summary=summary.get("metrics"), week=summary.get("week_key"))
        result["weekly"] = summary

        # ---- 3. Rebuild ----------------------------------------------------
        build_cmd = [
            sys.executable,
            str(DASHBOARD_DIR / "build_dashboard.py"),
            "--max-age-hours", str(max_age_hours),
        ]
        build = subprocess.run(build_cmd, cwd=str(PROJECT_ROOT), capture_output=True, text=True)
        step("build", exit_code=build.returncode)
        if build.returncode != 0:
            result["error"] = "build_failed"
            result["message"] = (
                "The dashboard rebuild was refused, so the previous HTML is still in place."
            )
            result["detail"] = (build.stderr or build.stdout or "").strip()[-1500:]
            return result

        result["ok"] = True
        result["retrieved_at"] = payload.get("data_retrieved_at")
        result["model_count"] = len(payload.get("rows") or [])
        return result
    finally:
        result["duration_seconds"] = round(time.time() - started, 2)
        result["finished_at"] = datetime.now(timezone.utc).isoformat()
        lock.release()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--skip-fetch", action="store_true",
                        help="Rebuild and record from the data already on disk (no network)")
    parser.add_argument("--no-snapshot", action="store_true",
                        help="Never write a timestamped snapshot for this run")
    parser.add_argument("--max-age-hours", type=float, default=72.0,
                        help="Freshness window enforced by the dashboard build (default 72)")
    parser.add_argument("--json", action="store_true",
                        help="Print the result as a single JSON object (used by dashboard/serve.py)")
    args = parser.parse_args()

    result = run_refresh(
        skip_fetch=args.skip_fetch,
        no_snapshot=args.no_snapshot,
        max_age_hours=args.max_age_hours,
    )

    if args.json:
        print(json.dumps(result, default=str))
    else:
        for entry in result["steps"]:
            detail = ", ".join(f"{k}={v}" for k, v in entry.items() if k not in ("step", "at"))
            print(f"  - {entry['step']}{(': ' + detail) if detail else ''}")
        if result["ok"]:
            print(
                f"OK: refreshed {result.get('model_count')} models "
                f"({result.get('duration_seconds')}s); data retrieved_at={result.get('retrieved_at')}"
            )
            weekly = result.get("weekly") or {}
            if weekly:
                print(f"Week {weekly.get('week_key')}: {weekly.get('metrics')}")
        else:
            print(f"FAILED: {result.get('message') or result.get('error')}", file=sys.stderr)
            if result.get("detail"):
                print(result["detail"], file=sys.stderr)

    if result["ok"]:
        return 0
    if result.get("error") == "refresh_already_running":
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
