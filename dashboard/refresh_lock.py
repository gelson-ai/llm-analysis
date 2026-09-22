"""Cross-process refresh lock.

O_EXCL creation, so "check" and "claim" are one atomic step: two refreshes can
never both believe they hold the lock. A lock file older than ``stale_after``
can only be left over from a process that died, so it may be taken over once.

NOTE ON DUPLICATION
dashboard/refresh.py already contains an equivalent class. It is a frozen
surface (see .github/copilot-instructions.md) and is deliberately NOT
refactored to import this module, so the two copies coexist. They must stay
behaviourally identical; tests/test_refresh_media.py asserts that they agree on
mutual exclusion by having one class refuse a path the other has already
claimed. The important property - that the two pipelines use DIFFERENT lock
FILES - is asserted there too.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

# A refresh normally takes seconds (the media one takes minutes). If the lock is
# older than this it can only be from a process that died.
LOCK_STALE_SECONDS = 1800


class RefreshLock:
    """Claim a lock file exclusively, with one stale-lock takeover attempt."""

    def __init__(self, path: Path, stale_after: int = LOCK_STALE_SECONDS):
        self.path = Path(path)
        self.stale_after = stale_after
        self.acquired = False

    def _write_owner(self) -> None:
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(
                {"pid": os.getpid(), "started_at": datetime.now(timezone.utc).isoformat()},
                handle,
            )

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
