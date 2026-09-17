"""Guards the invariant that a media snapshot can never be mistaken for the
chat pipeline's daily snapshot by dashboard/refresh.py.

Regression this locks down: media snapshots used to be written to
`data/snapshots/<date>/media/`. `refresh.py`'s `snapshot_exists_for()` decides
whether today's chat snapshot already exists by nothing more than looking for a
directory directly under `data/snapshots/` named `<date>` or `<date>-N`. So that
folder made refresh.py believe the day's chat snapshot had already run, and it
passed `--no-snapshot` to the chat pipeline - silently losing the day's real
chat snapshot whenever the media pipeline happened to run first.

These tests import and call the REAL `snapshot_exists_for`, not a copy of its
logic, so they cannot drift away from what refresh.py actually does.
"""
import sys
from pathlib import Path

import pytest

from config import settings
from src import media_pipeline

DASHBOARD_DIR = Path(__file__).resolve().parent.parent / "dashboard"
if str(DASHBOARD_DIR) not in sys.path:
    sys.path.insert(0, str(DASHBOARD_DIR))

import refresh  # noqa: E402  (frozen chat-side logic, imported read-only)

DATE = "2026-09-17"


def _write_snapshot(run_date: str) -> None:
    """Call the real media snapshot writer with dummy payloads."""
    media_pipeline._write_media_snapshot(
        run_date,
        {"data": []},          # raw image models payload
        {"data": []},          # raw video models payload
        {},                    # raw image endpoints map
        [],                    # normalized image models
        [],                    # normalized video models
        [],                    # media benchmarks
        {},                    # coverage report
        {},                    # data quality report
        [],                    # prompt benchmarks
        [],                    # prompt benchmark raw rows
    )


# ---------------------------------------------------------------------------
# The static invariant
# ---------------------------------------------------------------------------
def test_media_snapshots_are_not_under_the_chat_snapshots_dir():
    assert settings.MEDIA_SNAPSHOTS_DIR != settings.SNAPSHOTS_DIR
    assert settings.SNAPSHOTS_DIR not in settings.MEDIA_SNAPSHOTS_DIR.parents
    assert settings.MEDIA_SNAPSHOTS_DIR.name == "media_snapshots"


# ---------------------------------------------------------------------------
# The behavioural guarantee, checked against the real function
# ---------------------------------------------------------------------------
def test_media_snapshot_does_not_make_refresh_skip_the_chat_snapshot(tmp_path, monkeypatch):
    media_root = tmp_path / "media_snapshots"
    chat_root = tmp_path / "snapshots"
    monkeypatch.setattr(settings, "MEDIA_SNAPSHOTS_DIR", media_root)
    monkeypatch.setattr(refresh, "SNAPSHOTS_DIR", chat_root)

    _write_snapshot(DATE)

    assert (media_root / DATE).is_dir(), "the media snapshot should have been written"
    # The chat pipeline must still be free to write its own snapshot today.
    assert refresh.snapshot_exists_for(DATE) is False
    # ...and the media run must not have touched the chat snapshots root at all.
    assert not chat_root.exists()


def test_media_snapshot_suffix_loop_stays_inside_its_own_directory(tmp_path, monkeypatch):
    media_root = tmp_path / "media_snapshots"
    monkeypatch.setattr(settings, "MEDIA_SNAPSHOTS_DIR", media_root)

    _write_snapshot(DATE)
    _write_snapshot(DATE)

    assert sorted(p.name for p in media_root.iterdir()) == [DATE, f"{DATE}-2"]


# ---------------------------------------------------------------------------
# Prove the tests above can actually fail
# ---------------------------------------------------------------------------
def test_frozen_check_still_detects_a_real_chat_snapshot(tmp_path, monkeypatch):
    """Control: the check is not simply always False."""
    chat_root = tmp_path / "snapshots"
    monkeypatch.setattr(refresh, "SNAPSHOTS_DIR", chat_root)
    (chat_root / DATE).mkdir(parents=True)

    assert refresh.snapshot_exists_for(DATE) is True


def test_the_old_media_layout_would_have_collided(tmp_path, monkeypatch):
    """Documents precisely what was fixed.

    Under the old layout a media run created `data/snapshots/<date>/media/`, and
    the frozen check - which only inspects direct children of SNAPSHOTS_DIR -
    saw a directory named `<date>` and returned True.
    """
    chat_root = tmp_path / "snapshots"
    monkeypatch.setattr(refresh, "SNAPSHOTS_DIR", chat_root)
    (chat_root / DATE / "media").mkdir(parents=True)

    assert refresh.snapshot_exists_for(DATE) is True


def test_empty_snapshots_root_reports_no_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(refresh, "SNAPSHOTS_DIR", tmp_path / "does-not-exist")
    assert refresh.snapshot_exists_for(DATE) is False
