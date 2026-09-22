#!/usr/bin/env python3
"""
Run BOTH data refreshes and report each one's outcome independently.

Usage:
    python dashboard/refresh_all.py [--targets all|chat|media] [--skip-fetch]
                                    [--no-snapshot] [--json]

The two pipelines stay independent. This module only spawns them as separate
processes, each with its own lock, its own status file and its own exit code -
nothing here merges their code paths, so the media pipeline can never break the
chat one. That separation is a deliberate earlier decision, not an accident of
how this file happens to be written.

Both targets always run: a failure in one does NOT skip the other, which is what
lets the button report per-dashboard success/failure on a single click.

Exit codes:
    0 - every requested target succeeded
    1 - at least one requested target failed
    2 - at least one requested target was already running (and none failed)
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

# Imported by name rather than reached through `subprocess.`: tests replace the
# `subprocess` module reference in this module to fake child processes, and an
# attribute lookup on that replacement would break the failure path.
from subprocess import TimeoutExpired

DASHBOARD_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = DASHBOARD_DIR.parent
sys.path.insert(0, str(DASHBOARD_DIR))

# Each target is a standalone CLI. refresh.py (chat) predates this module and is
# a frozen surface - it is invoked exactly as it always was, never modified.
TARGET_SCRIPTS = {
    "chat": DASHBOARD_DIR / "refresh.py",
    "media": DASHBOARD_DIR / "refresh_media.py",
}
TARGETS = tuple(TARGET_SCRIPTS)

# Deliberately different windows. The chat pipeline's 72h default matches its
# weekly cadence; the media pipeline's is 192h because media data is published
# on the same weekly beat but its own build has always defaulted to 192. Forcing
# one window on both would make a media rebuild refuse data that is legitimately
# fresh for its own cadence.
DEFAULT_MAX_AGE_HOURS = {"chat": 72.0, "media": 192.0}

# Per-target ceiling. dashboard/serve.py's own --timeout-seconds must exceed
# 2x this, or it would kill the whole unified refresh mid-pipeline.
DEFAULT_TIMEOUT_SECONDS = 600.0

# Per-target last-run outcome, for the deployed Worker: it cannot see this
# process's in-memory state, so it reads the result off this file instead.
STATUS_PATH = DASHBOARD_DIR / "refresh_status.json"


def _parse_result(stdout: str, stderr: str) -> dict:
    """Each refresh script prints one JSON object as its last line with --json."""
    for line in reversed((stdout or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    return {
        "ok": False,
        "error": "unparseable_output",
        "message": "The refresh process did not return a result.",
        "detail": ((stderr or "") + (stdout or ""))[-1500:],
    }


def run_target(name: str, args: argparse.Namespace, windows: dict[str, float]) -> dict:
    """Run one pipeline. Never raises: a failure must not stop the other one."""
    command = [sys.executable, str(TARGET_SCRIPTS[name]), "--json",
               "--max-age-hours", str(windows[name])]
    if args.skip_fetch:
        command.append("--skip-fetch")
    if args.no_snapshot:
        command.append("--no-snapshot")

    started = datetime.now(timezone.utc).isoformat()
    started_epoch = time.time()
    try:
        proc = subprocess.run(
            command, cwd=str(PROJECT_ROOT), capture_output=True, text=True,
            timeout=args.timeout_seconds,
        )
        result = _parse_result(proc.stdout, proc.stderr)
    except TimeoutExpired:
        result = {
            "ok": False,
            "error": "timeout",
            "message": f"The {name} refresh did not finish within {args.timeout_seconds:g}s.",
        }
    except Exception as exc:  # noqa: BLE001 - a crash must not strand the run
        result = {
            "ok": False,
            "error": "runner_error",
            "message": f"The {name} refresh could not be started: {exc}",
        }

    result.setdefault("ok", False)
    result.setdefault("target", name)
    if result.get("error") == "refresh_already_running":
        result["state"] = "already_running"
    else:
        result["state"] = "ok" if result["ok"] else "failed"
    result.setdefault("started_at", started)
    result["duration_seconds"] = round(time.time() - started_epoch, 2)
    return result


def run_all(targets: tuple[str, ...] = TARGETS,
            skip_fetch: bool = False,
            no_snapshot: bool = False,
            windows: dict[str, float] | None = None,
            timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> dict:
    """Run each requested target in turn. Returns the aggregate result."""
    resolved_windows = dict(DEFAULT_MAX_AGE_HOURS)
    resolved_windows.update(windows or {})
    args = argparse.Namespace(
        targets=targets, skip_fetch=skip_fetch, no_snapshot=no_snapshot,
        timeout_seconds=timeout_seconds,
    )
    started = time.time()
    results: dict[str, dict] = {}
    for name in targets:
        results[name] = run_target(name, args, resolved_windows)

    requested = {name: results[name] for name in targets}
    failed = [name for name, res in requested.items() if res["state"] == "failed"]
    running = [name for name, res in requested.items() if res["state"] == "already_running"]

    aggregate = {
        "ok": not failed and not running,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "duration_seconds": round(time.time() - started, 2),
        "targets": requested,
        "failed_targets": failed,
        "already_running_targets": running,
    }
    if failed:
        aggregate["error"] = "target_failed"
        aggregate["message"] = _summarise(requested)
    elif running:
        aggregate["error"] = "refresh_already_running"
        aggregate["message"] = "A refresh for one of the dashboards is already running."
    else:
        aggregate["message"] = _summarise(requested)
    return aggregate


def _summarise(results: dict[str, dict]) -> str:
    """One human line naming which dashboard succeeded and which did not."""
    labels = {"chat": "Chat", "media": "Image"}
    parts = []
    for name, res in results.items():
        label = labels.get(name, name)
        if res["state"] == "ok":
            parts.append(f"{label}: updated.")
        elif res["state"] == "already_running":
            parts.append(f"{label}: a refresh was already running.")
        else:
            reason = res.get("message") or res.get("error") or "failed"
            parts.append(f"{label}: failed — {reason}")
    return " ".join(parts)


def write_status(aggregate: dict) -> Path:
    """Persist the per-target outcome for readers that outlive this process."""
    status = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "state": "failed" if aggregate.get("failed_targets") else
                 ("running" if aggregate.get("already_running_targets") else "ok"),
        "duration_seconds": aggregate.get("duration_seconds"),
        "targets": aggregate["targets"],
    }
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATUS_PATH.with_name(STATUS_PATH.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(status, handle, indent=2)
    os.replace(tmp, STATUS_PATH)
    return STATUS_PATH


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--targets", choices=("all",) + TARGETS, default="all",
                        help="Which dashboards to refresh (default all)")
    parser.add_argument("--skip-fetch", action="store_true",
                        help="Rebuild from the data already on disk (no network, either pipeline)")
    parser.add_argument("--no-snapshot", action="store_true",
                        help="Never write a timestamped snapshot for either pipeline")
    parser.add_argument("--chat-max-age-hours", type=float,
                        default=DEFAULT_MAX_AGE_HOURS["chat"],
                        help="Freshness window for the chat build "
                             f"(default {DEFAULT_MAX_AGE_HOURS['chat']:g})")
    parser.add_argument("--media-max-age-hours", type=float,
                        default=DEFAULT_MAX_AGE_HOURS["media"],
                        help="Freshness window for the image build "
                             f"(default {DEFAULT_MAX_AGE_HOURS['media']:g})")
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS,
                        help=f"Per-target ceiling (default {DEFAULT_TIMEOUT_SECONDS:g})")
    parser.add_argument("--json", action="store_true",
                        help="Print the aggregate result as a single JSON object")
    args = parser.parse_args()

    targets = TARGETS if args.targets == "all" else (args.targets,)
    aggregate = run_all(
        targets=targets,
        skip_fetch=args.skip_fetch,
        no_snapshot=args.no_snapshot,
        windows={"chat": args.chat_max_age_hours, "media": args.media_max_age_hours},
        timeout_seconds=args.timeout_seconds,
    )
    write_status(aggregate)

    if args.json:
        print(json.dumps(aggregate, default=str))
    else:
        print(aggregate["message"])
        for name, res in aggregate["targets"].items():
            print(f"  {name}: {res['state']} ({res.get('duration_seconds')}s)")

    if aggregate.get("failed_targets"):
        return 1
    if aggregate.get("already_running_targets"):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
