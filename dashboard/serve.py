#!/usr/bin/env python3
"""
Local server for the dashboard, with an on-demand refresh endpoint.

Why this exists: the dashboard is a single self-contained HTML file with a
data snapshot baked into it, and the payload (blended price, benchmark joins,
coverage/quality) is computed by Python. A page opened straight off disk can
therefore never refresh itself - it has no way to write data/normalized/*.json
or republish the HTML. This server provides that missing piece.

Usage:
    python dashboard/serve.py [--host 127.0.0.1] [--port 8765]
                              [--token SECRET] [--cooldown-seconds 600]
                              [--max-age-hours 72]

Endpoints:
    GET  /                the dashboard HTML
    GET  /healthz         liveness probe
    GET  /api/status      server config, data freshness, job state, weekly picks
    POST /api/refresh     start a refresh (202 Accepted; poll /api/status)

Safety defaults:
  - binds to 127.0.0.1 only, so nothing is exposed on the network unless asked;
  - refuses /api/refresh when a refresh is already running (409);
  - enforces a cooldown between refreshes (429 + Retry-After);
  - rejects cross-origin POSTs (Origin check) and requires a JSON content type,
    so a random web page cannot silently drive localhost;
  - if --token is set, /api/refresh additionally requires it (401 otherwise).

Only the dashboard file is ever served, by exact filename match - there is no
directory listing and no path traversal.

Note: this is the "works locally" iteration. Before anyone outside the machine
can reach it, add TLS, real authentication and per-user rate limiting - see the
README's refresh section.
"""
from __future__ import annotations

import argparse
import json
import secrets
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

DASHBOARD_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(DASHBOARD_DIR))

import build_dashboard  # noqa: E402
import build_image_dashboard  # noqa: E402
import weekly_picks  # noqa: E402

PROJECT_ROOT = build_dashboard.PROJECT_ROOT
DASHBOARD_PATH = build_dashboard.OUTPUT_PATH
IMAGE_DASHBOARD_PATH = build_image_dashboard.OUTPUT_PATH
REFRESH_SCRIPT = DASHBOARD_DIR / "refresh_all.py"

# The unified refresh runs BOTH pipelines as separate processes, so its ceiling
# must exceed the sum of refresh_all.py's per-target timeouts (2 x 600s). If it
# did not, a slow media fetch would be killed mid-run and the chat result would
# be thrown away with it.
DEFAULT_REFRESH_TIMEOUT_SECONDS = 1500.0

# Exactly what may be served, by filename.
SERVABLE = {
    "/": DASHBOARD_PATH,
    "/index.html": DASHBOARD_PATH,
    "/dashboard.html": DASHBOARD_PATH,
    "/image_model_analysis.html": IMAGE_DASHBOARD_PATH,
}

# Refresh requests carry an empty JSON object; anything larger is not ours.
MAX_BODY_BYTES = 64 * 1024


class RefreshJob:
    """Single-flight background refresh, so a POST returns immediately and the
    browser can poll instead of holding a request open for a minute."""

    def __init__(self, cooldown_seconds: float):
        self.cooldown_seconds = float(cooldown_seconds)
        self._lock = threading.Lock()
        self.state = "idle"           # idle | running | ok | failed
        self.started_at: str | None = None
        self.finished_at: str | None = None
        self.last_result: dict | None = None
        self.last_completed_epoch: float | None = None

    # -- read ---------------------------------------------------------------
    def snapshot(self) -> dict:
        with self._lock:
            retry_after = self._retry_after_locked()
            return {
                "state": self.state,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "result": self.last_result,
                "cooldown_seconds": self.cooldown_seconds,
                "retry_after_seconds": retry_after,
            }

    def _retry_after_locked(self) -> int:
        if self.last_completed_epoch is None:
            return 0
        remaining = self.cooldown_seconds - (time.time() - self.last_completed_epoch)
        return max(0, int(remaining + 0.999))

    # -- write --------------------------------------------------------------
    def start(self, args: argparse.Namespace) -> tuple[str, int]:
        """Returns (outcome, http_status)."""
        with self._lock:
            if self.state == "running":
                return "running", 409
            retry_after = self._retry_after_locked()
            if retry_after > 0:
                return "cooldown", 429
            self.state = "running"
            self.started_at = datetime.now(timezone.utc).isoformat()
            self.finished_at = None
            self.last_result = None

        thread = threading.Thread(target=self._run, args=(args,), daemon=True)
        thread.start()
        return "started", 202

    def _run(self, args: argparse.Namespace) -> None:
        # dashboard/refresh_all.py runs refresh.py (chat) and refresh_media.py
        # (image) as two independent subprocesses and reports each one's outcome
        # separately, so one pipeline failing still leaves the other updated.
        command = [sys.executable, str(REFRESH_SCRIPT), "--json",
                   "--chat-max-age-hours", str(args.max_age_hours),
                   "--media-max-age-hours", str(args.media_max_age_hours)]
        if getattr(args, "skip_fetch", False):
            command.append("--skip-fetch")
        if getattr(args, "no_snapshot", False):
            command.append("--no-snapshot")

        try:
            proc = subprocess.run(
                command, cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=args.timeout_seconds,
            )
            result = self._parse_result(proc.stdout or "", proc.stderr or "")
        except subprocess.TimeoutExpired:
            result = {
                "ok": False,
                "error": "timeout",
                "message": f"The refresh did not finish within {args.timeout_seconds}s.",
            }
        except Exception as exc:  # a crash here must never strand the job in "running"
            result = {
                "ok": False,
                "error": "runner_error",
                "message": f"The refresh could not be started: {exc}",
            }

        with self._lock:
            self.last_result = result
            self.state = "ok" if result.get("ok") else "failed"
            self.finished_at = datetime.now(timezone.utc).isoformat()
            self.last_completed_epoch = time.time()

    @staticmethod
    def _parse_result(stdout: str, stderr: str) -> dict:
        """refresh.py --json prints a single JSON object as its last line."""
        for line in reversed(stdout.strip().splitlines()):
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
            "detail": (stderr + stdout)[-1500:],
        }


class Handler(BaseHTTPRequestHandler):
    server_version = "LLMAnalysisDashboard/1.0"
    protocol_version = "HTTP/1.1"

    # -- plumbing -----------------------------------------------------------
    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("[%s] %s\n" % (datetime.now(timezone.utc).strftime("%H:%M:%S"), fmt % args))

    def _send_json(self, payload: dict, status: int = 200, headers: dict | None = None) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (headers or {}).items():
            self.send_header(key, str(value))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, content_type: str) -> None:
        try:
            body = path.read_bytes()
        except OSError:
            self._send_json({"ok": False, "error": "dashboard_missing",
                             "message": "The dashboard has not been built yet. Run dashboard/refresh.py once."}, 404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> bytes:
        """Always consume the request body.

        With HTTP/1.1 keep-alive, an unread body stays in the socket and is
        then parsed as the next request line, which corrupts the connection
        and makes the browser's follow-up poll fail.
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            return b""
        if length <= 0:
            return b""
        if length > MAX_BODY_BYTES:
            return b""
        return self.rfile.read(length)

    def _same_origin(self) -> bool:
        """An absent Origin means a same-origin navigation or a non-browser
        client; a present one must match this server."""
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = self.headers.get("Host") or ""
        return origin in {f"http://{host}", f"https://{host}"}

    def _token_ok(self) -> bool:
        expected = getattr(self.server, "token", None)
        if not expected:
            return True
        supplied = ""
        auth = self.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            supplied = auth[7:].strip()
        if not supplied:
            supplied = (self.headers.get("X-Refresh-Token") or "").strip()
        return secrets.compare_digest(supplied, expected)

    def _status_payload(self) -> dict:
        retrieved_at = build_dashboard.read_retrieved_at()
        age = build_dashboard.data_age_hours(retrieved_at)
        history = weekly_picks.load_history()
        return {
            "ok": True,
            "server": {
                "time_utc": datetime.now(timezone.utc).isoformat(),
                "token_required": bool(getattr(self.server, "token", None)),
            },
            "data": {
                "retrieved_at": retrieved_at,
                "age_hours": round(age, 2) if age is not None else None,
                # Was a hardcoded 72, which silently disagreed with the configured
                # window and with the Worker's STALE_AFTER_HOURS of 192.
                "stale": (age is None) or age > self.server.args.max_age_hours,
                "dashboard_built_at": build_dashboard.dashboard_built_at(),
                "model_count": build_dashboard.model_count(),
            },
            "media": self._media_status(),
            "job": self.server.job.snapshot(),
            "weekly": weekly_picks.embed_view(history),
        }

    def _media_status(self) -> dict:
        """Freshness of the IMAGE dashboard's own data, from its own paths.

        Mirrors the shape of `data` so both dashboards can read one contract.
        Deliberately reads the media pipeline's files only - never the chat
        pipeline's status.json.
        """
        retrieved_at = build_image_dashboard.read_retrieved_at()
        age = build_image_dashboard.data_age_hours(retrieved_at)
        built_at = None
        model_count = None
        status_path = DASHBOARD_DIR / "media_status.json"
        if status_path.exists():
            try:
                with open(status_path, encoding="utf-8") as handle:
                    media_status = json.load(handle)
                built_at = media_status.get("generated_at")
                model_count = media_status.get("model_count")
            except (OSError, ValueError):
                pass
        return {
            "retrieved_at": retrieved_at,
            "age_hours": round(age, 2) if age is not None else None,
            "stale": (age is None) or age > self.server.args.media_max_age_hours,
            "dashboard_built_at": built_at,
            "model_count": model_count,
        }

    # -- routes -------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 (http.server naming)
        path = urlparse(self.path).path
        if path in SERVABLE:
            self._send_file(SERVABLE[path], "text/html; charset=utf-8")
            return
        if path == "/healthz":
            self._send_json({"ok": True})
            return
        if path == "/api/status":
            self._send_json(self._status_payload())
            return
        self._send_json({"ok": False, "error": "not_found"}, 404)

    def do_OPTIONS(self) -> None:  # noqa: N802
        # No CORS headers are ever emitted, so a cross-origin preflight fails.
        self._send_json({"ok": False, "error": "method_not_allowed"}, 405)

    def do_POST(self) -> None:  # noqa: N802
        self._read_body()
        path = urlparse(self.path).path
        if path != "/api/refresh":
            self._send_json({"ok": False, "error": "not_found"}, 404)
            return

        if not self._same_origin():
            self._send_json({"ok": False, "error": "cross_origin_blocked"}, 403)
            return
        if not self._token_ok():
            self._send_json({"ok": False, "error": "unauthorized",
                             "message": "A refresh token is required."}, 401)
            return
        content_type = (self.headers.get("Content-Type") or "").lower()
        if "application/json" not in content_type:
            self._send_json({"ok": False, "error": "unsupported_media_type",
                             "message": "POST /api/refresh requires Content-Type: application/json."}, 415)
            return

        outcome, status = self.server.job.start(self.server.args)
        if outcome == "started":
            self._send_json({"ok": True, "state": "running",
                             "message": "Refresh started."}, status)
            return
        if outcome == "cooldown":
            self._send_json({"ok": False, "error": "cooldown",
                             "message": "A refresh completed recently.",
                             "retry_after_seconds": self.server.job.snapshot()["retry_after_seconds"]}, status,
                            headers={"Retry-After": self.server.job.snapshot()["retry_after_seconds"]})
            return
        self._send_json({"ok": False, "error": "refresh_already_running",
                         "message": "A refresh is already running."}, status)


def build_server(args: argparse.Namespace) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True
    httpd.args = args
    httpd.token = args.token
    httpd.job = RefreshJob(args.cooldown_seconds)
    return httpd


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1",
                        help="Interface to bind (default 127.0.0.1 - local machine only)")
    parser.add_argument("--port", type=int, default=8765, help="Port (default 8765)")
    parser.add_argument("--token", default=None,
                        help="Require this token on POST /api/refresh. Set it before exposing the server.")
    parser.add_argument("--cooldown-seconds", type=float, default=600.0,
                        help="Minimum seconds between refreshes (default 600)")
    parser.add_argument("--max-age-hours", type=float, default=72.0,
                        help="Freshness window passed to the CHAT build (default 72)")
    parser.add_argument("--media-max-age-hours", type=float, default=192.0,
                        help="Freshness window passed to the IMAGE build (default 192). "
                             "Deliberately wider than the chat window: media data shares the "
                             "weekly cadence, and its build has always defaulted to 192.")
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_REFRESH_TIMEOUT_SECONDS,
                        help="Give up on a whole unified refresh after this long "
                             f"(default {DEFAULT_REFRESH_TIMEOUT_SECONDS:g}; must exceed "
                             "2x refresh_all.py's per-target timeout)")
    parser.add_argument("--skip-fetch", action="store_true",
                        help="Do not re-fetch from OpenRouter; recompute and rebuild from the data already on disk")
    parser.add_argument("--no-snapshot", action="store_true",
                        help="Never write a timestamped snapshot on refresh")
    parser.add_argument("--open", action="store_true", help="Open the dashboard in the default browser")
    args = parser.parse_args()

    httpd = build_server(args)
    url = f"http://{args.host}:{args.port}/"
    print(f"Dashboard served at {url}")
    print(f"  refresh endpoint : POST {url}api/refresh" + ("  (token required)" if args.token else "  (no token)"))
    print(f"  cooldown         : {args.cooldown_seconds:.0f}s between refreshes")
    print("  Ctrl+C to stop.")
    if args.open:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:  # pragma: no cover - best effort
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
