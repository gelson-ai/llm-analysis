"""Contract tests for the two refresh entry points and their orchestrator.

The load-bearing promises here are about SEPARATION, not about fetching:

  * the two pipelines never share a lock file, or a stuck media run would block
    a chat refresh (and vice versa);
  * the media refresh never reads or writes the CHAT snapshot root - refresh.py
    ::snapshot_exists_for() treats any directory directly under data/snapshots/
    named <date> or <date>-N as "today's chat snapshot exists", so a media run
    writing there would silently suppress the chat snapshot for the day;
  * a failure in one target does not stop the other, and the aggregate result
    says which dashboard succeeded and which did not.

No network and no subprocesses: every child process is replaced.
"""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_DIR = PROJECT_ROOT / "dashboard"
if str(DASHBOARD_DIR) not in sys.path:
    sys.path.insert(0, str(DASHBOARD_DIR))

import build_dashboard  # noqa: E402
import refresh  # noqa: E402  (the chat pipeline - imported read-only, never modified)
import refresh_all  # noqa: E402
import refresh_lock  # noqa: E402
import refresh_media  # noqa: E402
import serve  # noqa: E402

WORKER_PATH = PROJECT_ROOT / "cloudflare-worker" / "worker.js"


def completed(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


# ---------------------------------------------------------------------------
# separation: locks
# ---------------------------------------------------------------------------
def test_the_two_pipelines_use_different_lock_files():
    assert refresh.LOCK_PATH != refresh_media.LOCK_PATH
    assert refresh.LOCK_PATH.name == ".refresh.lock"
    assert refresh_media.LOCK_PATH.name == ".media_refresh.lock"


def test_the_media_lock_is_not_in_a_chat_owned_location():
    for path in (refresh.LOCK_PATH, refresh_media.LOCK_PATH):
        assert path.parent.name == "analysis"
        assert "snapshots" not in path.parts


def test_the_shared_lock_and_the_chat_pipelines_lock_agree_on_mutual_exclusion(tmp_path):
    """refresh.py's RefreshLock is a frozen surface and keeps its own copy of the
    class, so the two implementations must still agree on the one property that
    matters: two holders of the same path cannot both believe they own it."""
    path = tmp_path / "shared.lock"
    chat_lock = refresh.RefreshLock(path)
    media_lock = refresh_lock.RefreshLock(path)

    assert chat_lock.acquire() is True
    assert media_lock.acquire() is False, "a second holder must be refused"
    chat_lock.release()
    assert media_lock.acquire() is True, "releasing must let the other proceed"
    media_lock.release()


def test_a_stale_lock_file_can_be_taken_over(tmp_path):
    path = tmp_path / "stale.lock"
    path.write_text("{}", encoding="utf-8")
    import os
    import time

    old = time.time() - 7200
    os.utime(path, (old, old))

    lock = refresh_lock.RefreshLock(path, stale_after=1800)
    assert lock.acquire() is True
    lock.release()


# ---------------------------------------------------------------------------
# separation: snapshot roots
# ---------------------------------------------------------------------------
def test_the_media_snapshot_check_looks_only_at_the_media_root(tmp_path, monkeypatch):
    media_root = tmp_path / "media_snapshots"
    chat_root = tmp_path / "snapshots"
    media_root.mkdir()
    chat_root.mkdir()
    monkeypatch.setattr(refresh_media.settings, "MEDIA_SNAPSHOTS_DIR", media_root)
    monkeypatch.setattr(refresh_media.settings, "SNAPSHOTS_DIR", chat_root)

    # A directory the CHAT pipeline would treat as "today's snapshot" must not
    # make the media pipeline believe it has already snapshotted.
    (chat_root / "2026-01-04").mkdir()
    assert refresh_media.media_snapshot_exists_for("2026-01-04") is False

    (media_root / "2026-01-02").mkdir()
    assert refresh_media.media_snapshot_exists_for("2026-01-02") is True
    assert refresh_media.media_snapshot_exists_for("2026-01-03") is False

    # The <date>-N repeat convention is honoured, like the chat side's.
    (media_root / "2026-01-03-2").mkdir()
    assert refresh_media.media_snapshot_exists_for("2026-01-03") is True


def test_the_media_refresh_never_writes_into_the_chat_snapshot_root():
    """A structural guarantee, checked at the source level: the module must not
    reference the chat snapshot root at all."""
    source = (DASHBOARD_DIR / "refresh_media.py").read_text(encoding="utf-8")
    assert "MEDIA_SNAPSHOTS_DIR" in source
    assert 'settings.SNAPSHOTS_DIR' not in source
    assert '"snapshots"' not in source


# ---------------------------------------------------------------------------
# the media refresh writes its own status, and only its own
# ---------------------------------------------------------------------------
def _install_media(tmp_path, monkeypatch, run_impl):
    lock_path = tmp_path / ".media_refresh.lock"
    status_path = tmp_path / "media_status.json"
    monkeypatch.setattr(refresh_media, "LOCK_PATH", lock_path)
    monkeypatch.setattr(refresh_media, "STATUS_PATH", status_path)
    monkeypatch.setattr(refresh_media, "subprocess", SimpleNamespace(run=run_impl))
    monkeypatch.setattr(refresh_media, "media_snapshot_exists_for", lambda _date: False)
    monkeypatch.setattr(
        refresh_media.build_image_dashboard, "build_payload",
        lambda: {"data_retrieved_at": "2026-09-17T00:00:00+00:00",
                 "generated_at": "2026-09-22T00:00:00+00:00",
                 "rows": [{"model_id": "a"}, {"model_id": "b"}]},
    )
    return status_path


def test_media_refresh_publishes_a_status_file_for_its_own_pipeline(tmp_path, monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return completed(0, stdout="pipeline output\n")

    status_path = _install_media(tmp_path, monkeypatch, fake_run)
    chat_status = refresh.build_dashboard.STATUS_PATH
    before = chat_status.read_text(encoding="utf-8") if chat_status.exists() else None

    result = refresh_media.run_refresh()

    assert result["ok"] is True
    assert result["target"] == "media"
    assert len(calls) == 2, "the pipeline then the build"
    assert calls[0][1].endswith("run_media_pipeline.py")
    assert calls[1][1].endswith("build_image_dashboard.py")

    written = json.loads(status_path.read_text(encoding="utf-8"))
    assert written["pipeline"] == "media"
    assert written["model_count"] == 2
    assert written["retrieved_at"] == "2026-09-17T00:00:00+00:00"

    after = chat_status.read_text(encoding="utf-8") if chat_status.exists() else None
    assert after == before, "the media refresh must not touch the chat status file"


def test_media_refresh_aborts_without_rebuilding_when_the_fetch_fails(tmp_path, monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return completed(1, stderr="MEDIA PIPELINE FAILED: boom")

    status_path = _install_media(tmp_path, monkeypatch, fake_run)
    result = refresh_media.run_refresh()

    assert result["ok"] is False
    assert result["error"] == "media_pipeline_failed"
    assert len(calls) == 1, "a failed fetch must not be followed by a rebuild"
    assert not status_path.exists(), "a failed run must not republish a status"
    # The lock must be released even on the failure path, or the next refresh
    # would report "already running" forever.
    assert not (tmp_path / ".media_refresh.lock").exists()


def test_media_refresh_reports_a_concurrent_run_as_its_own_error(tmp_path, monkeypatch):
    status_path = _install_media(tmp_path, monkeypatch, lambda cmd, **kw: completed(0))

    held = refresh_lock.RefreshLock(tmp_path / ".media_refresh.lock")
    assert held.acquire() is True
    try:
        result = refresh_media.run_refresh()
    finally:
        held.release()

    assert result["ok"] is False
    assert result["error"] == "refresh_already_running"
    assert not status_path.exists()


# ---------------------------------------------------------------------------
# the orchestrator: one failure must not stop the other target
# ---------------------------------------------------------------------------
def test_one_target_failing_does_not_stop_the_other(tmp_path, monkeypatch):
    seen = []

    def fake_run(cmd, **kwargs):
        seen.append(Path(cmd[1]).name)
        if Path(cmd[1]).name == "refresh.py":
            return completed(0, stdout=json.dumps({
                "ok": False, "error": "pipeline_failed",
                "message": "Data fetch failed, so the dashboard was left untouched.",
            }) + "\n")
        return completed(0, stdout=json.dumps({"ok": True, "target": "media"}) + "\n")

    monkeypatch.setattr(refresh_all, "subprocess", SimpleNamespace(run=fake_run))
    monkeypatch.setattr(refresh_all, "STATUS_PATH", tmp_path / "refresh_status.json")

    aggregate = refresh_all.run_all()

    assert seen == ["refresh.py", "refresh_media.py"], "both targets must be attempted"
    assert aggregate["ok"] is False
    assert aggregate["failed_targets"] == ["chat"]
    assert aggregate["targets"]["chat"]["state"] == "failed"
    assert aggregate["targets"]["media"]["state"] == "ok"
    # The message has to name BOTH dashboards, or the button cannot report
    # per-dashboard success/failure.
    assert "Chat: failed" in aggregate["message"]
    assert "Image: updated." in aggregate["message"]


def test_the_cli_persists_the_per_target_status_and_returns_the_right_exit_code(tmp_path, monkeypatch, capsys):
    """refresh_status.json is what the deployed Worker reads - it cannot see this
    process's in-memory state - so main() must persist it."""
    monkeypatch.setattr(refresh_all, "STATUS_PATH", tmp_path / "refresh_status.json")
    monkeypatch.setattr(sys, "argv", ["refresh_all.py"])

    cases = [
        ({"failed_targets": [], "already_running_targets": []}, 0, "ok"),
        ({"failed_targets": ["chat"], "already_running_targets": []}, 1, "failed"),
        ({"failed_targets": [], "already_running_targets": ["media"]}, 2, "running"),
    ]
    for overrides, expected_code, expected_state in cases:
        aggregate = {
            "ok": not overrides["failed_targets"] and not overrides["already_running_targets"],
            "message": "summary",
            "targets": {"chat": {"state": "failed"}, "media": {"state": "ok"}},
            "failed_targets": overrides["failed_targets"],
            "already_running_targets": overrides["already_running_targets"],
            "duration_seconds": 1.0,
        }
        monkeypatch.setattr(refresh_all, "run_all", lambda _aggregate=aggregate, **kwargs: _aggregate)
        assert refresh_all.main() == expected_code
        written = json.loads((tmp_path / "refresh_status.json").read_text(encoding="utf-8"))
        assert written["state"] == expected_state
        assert set(written["targets"]) == {"chat", "media"}
        capsys.readouterr()


def test_an_already_running_target_is_distinguished_from_a_failure(tmp_path, monkeypatch):
    def fake_run(cmd, **kwargs):
        return completed(0, stdout=json.dumps({
            "ok": False, "error": "refresh_already_running",
            "message": "Another refresh is already in progress.",
        }) + "\n")

    monkeypatch.setattr(refresh_all, "subprocess", SimpleNamespace(run=fake_run))
    monkeypatch.setattr(refresh_all, "STATUS_PATH", tmp_path / "refresh_status.json")
    aggregate = refresh_all.run_all(targets=("media",))

    assert aggregate["ok"] is False
    assert aggregate["already_running_targets"] == ["media"]
    assert aggregate["failed_targets"] == []
    assert aggregate["targets"]["media"]["state"] == "already_running"


def test_each_target_keeps_its_own_freshness_window(tmp_path, monkeypatch):
    commands = []

    def fake_run(cmd, **kwargs):
        commands.append(cmd)
        return completed(0, stdout=json.dumps({"ok": True}) + "\n")

    monkeypatch.setattr(refresh_all, "subprocess", SimpleNamespace(run=fake_run))
    monkeypatch.setattr(refresh_all, "STATUS_PATH", tmp_path / "refresh_status.json")
    refresh_all.run_all()

    chat_cmd, media_cmd = commands
    assert chat_cmd[chat_cmd.index("--max-age-hours") + 1] == "72.0"
    assert media_cmd[media_cmd.index("--max-age-hours") + 1] == "192.0"


def test_a_timeout_is_a_failure_not_a_crash(tmp_path, monkeypatch):
    import subprocess as real_subprocess

    def fake_run(cmd, **kwargs):
        raise real_subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

    monkeypatch.setattr(refresh_all, "subprocess", SimpleNamespace(run=fake_run))
    monkeypatch.setattr(refresh_all, "STATUS_PATH", tmp_path / "refresh_status.json")
    aggregate = refresh_all.run_all(targets=("media",))

    assert aggregate["ok"] is False
    assert aggregate["targets"]["media"]["error"] == "timeout"


# ---------------------------------------------------------------------------
# serve.py wires the unified button to the orchestrator
# ---------------------------------------------------------------------------
def test_serve_runs_the_orchestrator_with_a_window_per_dashboard(monkeypatch):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return completed(0, stdout=json.dumps({"ok": True, "targets": {}}) + "\n")

    monkeypatch.setattr(serve, "subprocess", SimpleNamespace(run=fake_run))
    job = serve.RefreshJob(cooldown_seconds=600)
    args = SimpleNamespace(max_age_hours=72.0, media_max_age_hours=192.0,
                           skip_fetch=False, no_snapshot=False, timeout_seconds=1500.0)
    job._run(args)

    cmd = captured["cmd"]
    assert Path(cmd[1]).name == "refresh_all.py"
    assert cmd[cmd.index("--chat-max-age-hours") + 1] == "72.0"
    assert cmd[cmd.index("--media-max-age-hours") + 1] == "192.0"
    assert job.state == "ok"


def test_serve_timeout_exceeds_the_orchestrators_own_ceiling():
    """Otherwise serve.py would kill a slow unified refresh mid-media-fetch and
    throw away a chat result that had already succeeded."""
    assert serve.DEFAULT_REFRESH_TIMEOUT_SECONDS > 2 * refresh_all.DEFAULT_TIMEOUT_SECONDS


# ---------------------------------------------------------------------------
# the deployed Worker reads the same artifacts
# ---------------------------------------------------------------------------
def test_the_workers_status_files_are_the_ones_the_pipelines_write():
    """The Worker reads these paths out of the repository, from another language
    in another repository directory. A rename on either side would silently
    break status reporting on the live site, and nothing else would notice."""
    worker = WORKER_PATH.read_text(encoding="utf-8")

    block = re.search(r"const STATUS_FILES = \{(.*?)\};", worker, re.S)
    assert block, "the Worker's STATUS_FILES map was removed or renamed"
    declared = dict(re.findall(r'(\w+):\s*"([^"]+)"', block.group(1)))

    assert declared == {
        "chat": f"dashboard/{build_dashboard.STATUS_PATH.name}",
        "media": f"dashboard/{refresh_media.STATUS_PATH.name}",
    }
    assert (
        f'REFRESH_STATUS_FILE = "dashboard/{refresh_all.STATUS_PATH.name}"' in worker
    ), "the Worker no longer reads the orchestrator's per-target status"


def test_the_worker_keeps_its_recency_guard():
    """refresh_status.json is a committed file holding whatever the last run
    wrote - including a LOCAL run, which is exactly what it contains right now.
    Without comparing its timestamp to the run it is attributed to, the live
    button would report a stale outcome as the result of a fresh refresh."""
    worker = WORKER_PATH.read_text(encoding="utf-8")
    assert "async function readRefreshTargets(token, run)" in worker
    assert "generated < started" in worker
    assert 'job.state === "idle" ? null' in worker


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_worker_script_parses():
    """worker.js has no other coverage - this repo has no JS test runner - so a
    syntax error in it would first be discovered by the live site breaking."""
    result = subprocess.run(
        [shutil.which("node"), "--check", str(WORKER_PATH)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
