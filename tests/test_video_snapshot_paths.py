"""Guards the invariant that a video-dashboard snapshot can never be mistaken for
the chat pipeline's daily snapshot by dashboard/refresh.py.

Same failure mode as tests/test_media_snapshot_paths.py: refresh.py's
snapshot_exists_for() decides whether today's chat snapshot already exists by
nothing more than looking for a directory directly under data/snapshots/ named
`<date>` or `<date>-N`. Any pipeline that writes a date-named directory into that
root makes refresh.py pass --no-snapshot to the chat pipeline, silently losing the
day's real chat snapshot.

The video dashboard therefore gets its own top-level root. These tests import and
call the REAL snapshot_exists_for(), not a copy of its logic, so they cannot drift
away from what refresh.py actually does.

Phase 0 covers the static paths and the behavioural guarantee. The writer-based
case (that the real video snapshot writer stays inside its own root) is added in
Phase 1 together with src/video_pipeline.py.
"""
import sys
from pathlib import Path

from config import settings

DASHBOARD_DIR = Path(__file__).resolve().parent.parent / "dashboard"
if str(DASHBOARD_DIR) not in sys.path:
    sys.path.insert(0, str(DASHBOARD_DIR))

import refresh  # noqa: E402  (frozen chat-side logic, imported read-only)

DATE = "2026-09-25"


# ---------------------------------------------------------------------------
# The static invariants
# ---------------------------------------------------------------------------
def test_video_snapshots_are_not_under_another_pipelines_snapshot_root():
    assert settings.VIDEO_DASHBOARD_SNAPSHOTS_DIR != settings.SNAPSHOTS_DIR
    assert settings.VIDEO_DASHBOARD_SNAPSHOTS_DIR != settings.MEDIA_SNAPSHOTS_DIR
    assert settings.SNAPSHOTS_DIR not in settings.VIDEO_DASHBOARD_SNAPSHOTS_DIR.parents
    assert settings.MEDIA_SNAPSHOTS_DIR not in settings.VIDEO_DASHBOARD_SNAPSHOTS_DIR.parents
    assert settings.VIDEO_DASHBOARD_SNAPSHOTS_DIR.parent == settings.DATA_DIR
    assert settings.VIDEO_DASHBOARD_SNAPSHOTS_DIR.name == "video_snapshots"


def test_no_video_dashboard_path_is_shared_with_another_pipeline():
    """A shared path would let the video refresh overwrite another pipeline's
    inputs - and the media pipeline's are embedded verbatim in the image page."""
    video = {
        name: value
        for name, value in vars(settings).items()
        if name.startswith("VIDEO_DASHBOARD_")
    }
    assert video, "the video dashboard paths are missing from config/settings.py"
    others = {
        value
        for name, value in vars(settings).items()
        if (name.endswith("_PATH") or name.endswith("_DIR"))
        and not name.startswith("VIDEO_DASHBOARD_")
    }
    assert set(video.values()) & others == set()


# ---------------------------------------------------------------------------
# The behavioural guarantee, checked against the real function
# ---------------------------------------------------------------------------
def test_a_video_snapshot_does_not_make_refresh_skip_the_chat_snapshot(tmp_path, monkeypatch):
    video_root = tmp_path / "video_snapshots"
    chat_root = tmp_path / "snapshots"
    monkeypatch.setattr(settings, "VIDEO_DASHBOARD_SNAPSHOTS_DIR", video_root)
    monkeypatch.setattr(refresh, "SNAPSHOTS_DIR", chat_root)

    (video_root / DATE).mkdir(parents=True)

    assert (video_root / DATE).is_dir(), "the video snapshot should have been written"
    # The chat pipeline must still be free to write its own snapshot today.
    assert refresh.snapshot_exists_for(DATE) is False
    # ...and the video run must not have touched the chat snapshots root at all.
    assert not chat_root.exists()


def test_a_video_snapshot_suffix_rollover_does_not_collide(tmp_path, monkeypatch):
    video_root = tmp_path / "video_snapshots"
    chat_root = tmp_path / "snapshots"
    monkeypatch.setattr(settings, "VIDEO_DASHBOARD_SNAPSHOTS_DIR", video_root)
    monkeypatch.setattr(refresh, "SNAPSHOTS_DIR", chat_root)

    (video_root / DATE).mkdir(parents=True)
    (video_root / f"{DATE}-2").mkdir(parents=True)

    assert refresh.snapshot_exists_for(DATE) is False


def test_all_three_pipelines_can_snapshot_the_same_day(tmp_path, monkeypatch):
    video_root = tmp_path / "video_snapshots"
    media_root = tmp_path / "media_snapshots"
    chat_root = tmp_path / "snapshots"
    monkeypatch.setattr(settings, "VIDEO_DASHBOARD_SNAPSHOTS_DIR", video_root)
    monkeypatch.setattr(settings, "MEDIA_SNAPSHOTS_DIR", media_root)
    monkeypatch.setattr(refresh, "SNAPSHOTS_DIR", chat_root)

    (video_root / DATE).mkdir(parents=True)
    (media_root / DATE).mkdir(parents=True)

    # Neither sibling root may convince the chat pipeline its own snapshot exists.
    assert refresh.snapshot_exists_for(DATE) is False

    (chat_root / DATE).mkdir(parents=True)

    assert refresh.snapshot_exists_for(DATE) is True


# ---------------------------------------------------------------------------
# Prove the tests above can actually fail
# ---------------------------------------------------------------------------
def test_frozen_check_still_detects_a_real_chat_snapshot(tmp_path, monkeypatch):
    """Control: the check is not simply always False."""
    chat_root = tmp_path / "snapshots"
    monkeypatch.setattr(refresh, "SNAPSHOTS_DIR", chat_root)
    (chat_root / DATE).mkdir(parents=True)

    assert refresh.snapshot_exists_for(DATE) is True


def test_a_date_named_directory_under_the_chat_root_would_still_collide(tmp_path, monkeypatch):
    """Documents the trap the separate video root exists to avoid."""
    chat_root = tmp_path / "snapshots"
    monkeypatch.setattr(refresh, "SNAPSHOTS_DIR", chat_root)
    (chat_root / DATE / "video").mkdir(parents=True)

    assert refresh.snapshot_exists_for(DATE) is True
