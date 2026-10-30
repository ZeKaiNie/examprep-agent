# -*- coding: utf-8 -*-
"""Per-workspace advisory file lock.

The service layer is a *stateless facade*: every request loads the durable
session from disk, mutates it and saves it again.  Two concurrent requests for
the same workspace would otherwise race (read-modify-write) and silently lose
one update — FastAPI runs synchronous endpoints in a thread pool, so this is
reachable through ordinary HTTP concurrency.

The lock is an advisory ``flock`` on ``<workspace>/.security/workspace.lock``
(``msvcrt`` on Windows).  It is held only for the duration of one service call
and is not used for security decisions; it exists to make state transitions
serial per workspace.  Readers may take a shared lock; a wait longer than
``timeout`` raises :class:`WorkspaceBusyError` instead of hanging forever.
"""
from __future__ import annotations

import os
import time
from contextlib import contextmanager
from typing import Iterator

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

try:  # Windows
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None  # type: ignore[assignment]


class WorkspaceBusyError(RuntimeError):
    """The workspace lock could not be acquired within the timeout."""


def lock_path(workspace: str) -> str:
    """Lock file inside the engine-owned ``.security/`` directory."""
    return os.path.join(os.path.abspath(workspace), ".security", "workspace.lock")


def _acquire(fh, shared: bool, timeout: float) -> None:  # noqa: ANN001
    deadline = time.monotonic() + max(0.0, float(timeout))
    if fcntl is not None:
        mode = (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB
        while True:
            try:
                fcntl.flock(fh.fileno(), mode)
                return
            except (BlockingIOError, OSError) as exc:
                if isinstance(exc, OSError) and not isinstance(exc, BlockingIOError):
                    # Some platforms report EACCES/EAGAIN as plain OSError.
                    if getattr(exc, "errno", None) not in (11, 13, 35):
                        raise
                if time.monotonic() >= deadline:
                    raise WorkspaceBusyError(
                        "workspace is busy (could not acquire lock within %.1fs)" % timeout)
                time.sleep(0.01)
    if msvcrt is not None:  # pragma: no cover - Windows
        while True:
            try:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise WorkspaceBusyError(
                        "workspace is busy (could not acquire lock within %.1fs)" % timeout)
                time.sleep(0.01)
    # No platform lock available: fail open on a best-effort basis rather than
    # making the workspace unusable.
    return


def _release(fh) -> None:  # noqa: ANN001
    try:
        if fcntl is not None:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        if msvcrt is not None:  # pragma: no cover - Windows
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass


@contextmanager
def workspace_lock(workspace: str, *, shared: bool = False,
                   timeout: float = 30.0) -> Iterator[None]:
    """Hold the workspace lock for one state transition.

    ``shared=True`` is for read-only calls whose view must not observe a
    half-finished write; writers take the exclusive lock.
    """
    path = lock_path(workspace)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fh = open(path, "a+b")
    if msvcrt is not None:  # msvcrt needs at least one byte to lock
        fh.seek(0, os.SEEK_END)
        if fh.tell() == 0:
            fh.write(b"\0")
            fh.flush()
    try:
        _acquire(fh, shared, timeout)
        yield
    finally:
        _release(fh)
        fh.close()


__all__ = ["WorkspaceBusyError", "lock_path", "workspace_lock"]
