# -*- coding: utf-8 -*-
"""Atomic JSON/text writes with pre-write snapshots.

Two guarantees from PLAN §5.7 are implemented here:

* a reader never sees a half-written document — content goes to a unique
  temporary file in the same directory, is ``flush``-ed and ``fsync``-ed, then
  ``os.replace``-d over the destination;
* the previous version is copied to ``<snapshot_root>/<file>/…`` before it is
  replaced, so any state change can be rolled back.

Older snapshots are *not* deleted: they are moved into a sibling ``.expired``
directory.  This keeps the project's "no true deletion" rule even for internal
retention housekeeping.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from typing import Any

_SNAPSHOT_LOCK_NAMES = frozenset({".expired"})


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def snapshot_file(path: str, snapshot_root: str, keep: int = 10) -> str | None:
    """Copy the current ``path`` into ``snapshot_root``; return the copy path.

    Does nothing when the file does not exist or already lives under the
    snapshot root (which would recurse).
    """
    path = os.path.abspath(path)
    snapshot_root = os.path.abspath(snapshot_root)
    if not os.path.isfile(path):
        return None
    if os.path.commonpath([path, snapshot_root]) == snapshot_root:
        return None
    dest_dir = os.path.join(snapshot_root, os.path.basename(path))
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, "%s-%d.bak" % (_utc_stamp(), os.getpid()))
    counter = 0
    while os.path.exists(dest):  # two snapshots in the same microsecond
        counter += 1
        dest = os.path.join(dest_dir, "%s-%d-%d.bak" % (_utc_stamp(), os.getpid(), counter))
    shutil.copy2(path, dest)
    _expire_old_snapshots(dest_dir, keep)
    return dest


def _expire_old_snapshots(directory: str, keep: int) -> None:
    """Move snapshots beyond ``keep`` into ``.expired/`` (never true-delete)."""
    if keep < 0:
        return
    try:
        names = sorted(
            name for name in os.listdir(directory)
            if name.endswith(".bak") and name not in _SNAPSHOT_LOCK_NAMES
        )
    except OSError:
        return
    stale = names[:-keep] if keep else names
    if not stale:
        return
    expired = os.path.join(directory, ".expired")
    os.makedirs(expired, exist_ok=True)
    for name in stale:
        source = os.path.join(directory, name)
        target = os.path.join(expired, name)
        if os.path.exists(target):
            target = os.path.join(expired, "%s-%d" % (name, os.getpid()))
        try:
            os.replace(source, target)
        except OSError:
            pass


def snapshot_candidates(path: str, snapshot_root: str) -> list[str]:
    """Snapshot paths for ``path``, newest first (empty when none exist)."""
    directory = os.path.join(os.path.abspath(snapshot_root), os.path.basename(path))
    try:
        names = sorted(
            name for name in os.listdir(directory)
            if name.endswith(".bak") and not name.startswith(".expired")
        )
    except OSError:
        return []
    return [os.path.join(directory, name) for name in reversed(names)]


def latest_snapshot(path: str, snapshot_root: str) -> str | None:
    """Newest good snapshot for ``path``, or None."""
    candidates = snapshot_candidates(path, snapshot_root)
    return candidates[0] if candidates else None


def atomic_write_text(
    path: str,
    text: str,
    *,
    snapshot_root: str | None = None,
    keep: int = 10,
    encoding: str = "utf-8",
) -> None:
    """Atomically replace ``path`` with ``text``, snapshotting the old version."""
    path = os.path.abspath(path)
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    if snapshot_root:
        snapshot_file(path, snapshot_root, keep=keep)
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".tmp", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding=encoding) as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            if os.path.exists(tmp):
                # Keep the failed temporary file in place rather than deleting it.
                os.replace(tmp, tmp + ".failed")
        except OSError:
            pass
        raise


def atomic_write_json(
    path: str,
    payload: Any,
    *,
    snapshot_root: str | None = None,
    keep: int = 10,
    indent: int = 1,
) -> None:
    """Atomically serialise ``payload`` as UTF-8 JSON."""
    atomic_write_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=indent, default=str),
        snapshot_root=snapshot_root,
        keep=keep,
    )
