# -*- coding: utf-8 -*-
"""Path fence: materials are read-only, writes default to the workspace.

A path is classified as one of:

``workspace``
    Inside the study workspace (including the default
    ``<materials>/exam-cram``).  Reads and writes are allowed — except for the
    internal control paths listed in :data:`PROTECTED_NAMES` /
    :data:`PROTECTED_DIRS`, which are read-only even for the workspace.
``materials``
    Inside the materials folder but *not* inside the workspace — the course
    files.  Reads are allowed; writes are always rejected, even with an L2
    grant.
``outside``
    Anything else.  Reads/writes need an L2 grant recorded in the user's
    permission store.

All comparisons are made on ``realpath`` values, so ``../`` traversal and
symlinks cannot escape the fence.  Comparison keys additionally apply
``os.path.normcase`` and Unicode NFC normalisation so case-insensitive
filesystems and NFD/NFC spellings cannot turn an inside path into an outside
path (or vice versa).
"""
from __future__ import annotations

import os
import unicodedata

WORKSPACE = "workspace"
MATERIALS = "materials"
OUTSIDE = "outside"

#: Single files inside the workspace that belong to the engine/user, never to
#: the model.  They are readable (read-only) but can never be written or
#: deleted through the file tools.
PROTECTED_NAMES = frozenset({
    "permissions.json",       # legacy grant table (kept only for evidence)
    "study_state.json",       # progress + history + learner/review state
    "session.json",           # kernel checkpoint
    "knowledge_graph.json",   # prerequisite graph
    "chapters.json",          # generated chapter index
    "quiz_bank.json",         # generated question bank
    "figures.json",           # generated figure index
    "index.json",             # generated BM25 search index
})

#: Whole directories inside the workspace that the model must not modify.
PROTECTED_DIRS = frozenset({
    ".security",              # write circuit-breaker state
    "audit",                  # append-only permission audit log
    ".snapshots",             # recovery snapshots
    ".trash",                 # deletion records and trashed files
    "memory",                 # long-term memory layers
    "logs",                   # kernel event log (memory import source)
})


def canonical(path: str) -> str:
    """Absolute ``realpath`` for comparisons (``~`` expanded)."""
    return os.path.realpath(os.path.abspath(os.path.expanduser(str(path))))


def path_key(path: str) -> str:
    """Comparison key: realpath + NFC + platform case folding.

    ``normcase`` is the identity on POSIX and lowercases on Windows; the NFC
    step makes canonically-equivalent Unicode spellings compare equal.  The key
    is only used for comparisons — file access still uses :func:`canonical`.
    """
    return os.path.normcase(unicodedata.normalize("NFC", canonical(path)))


def is_within(path: str, root: str) -> bool:
    """True when ``path`` is ``root`` or a descendant of it (realpath aware)."""
    p, r = path_key(path), path_key(root)
    try:
        return os.path.commonpath([p, r]) == r
    except ValueError:  # different drives on Windows
        return False


def same_path(left: str, right: str) -> bool:
    """True when both names refer to the same existing file, else key equality."""
    try:
        if os.path.exists(left) and os.path.exists(right):
            return os.path.samefile(left, right)
    except OSError:  # pragma: no cover - race/permission dependent
        pass
    return path_key(left) == path_key(right)


def is_protected_path(workspace: str, path: str) -> bool:
    """True when ``path`` is an engine-owned control path inside ``workspace``.

    The check is purely lexical *after* ``realpath`` resolution: a symlink in
    the workspace that points at ``audit/`` is protected, and ``audit/../x`` is
    classified by its final location.
    """
    target = canonical(path)
    ws = canonical(workspace)
    if not is_within(target, ws) or same_path(target, ws):
        return False
    relative = os.path.relpath(target, ws).replace(os.sep, "/")
    parts = [part for part in relative.split("/") if part not in ("", ".")]
    if not parts:
        return False
    if parts[0].lower() in PROTECTED_DIRS:
        return True
    return len(parts) == 1 and parts[0].lower() in PROTECTED_NAMES


def validate_workspace(workspace: str, materials: str | None = None) -> None:
    """Refuse obviously unsafe workspace roots.

    Raises:
        ValueError: for a filesystem root, the user's home directory, or the
            materials folder itself (the latter would make the read-only
            materials rule impossible to enforce).
    """
    ws = canonical(workspace)
    if ws == os.path.dirname(ws) or same_path(ws, os.path.abspath(os.sep)):
        raise ValueError("workspace must not be the filesystem root")
    home = canonical(os.path.expanduser("~"))
    if same_path(ws, home):
        raise ValueError("workspace must not be the user's home directory")
    if materials is not None and same_path(ws, canonical(materials)):
        raise ValueError("workspace must not be the materials folder itself")


def validate_grant_target(target: str) -> None:
    """Refuse grants that would hand over a disk root, home or an ancestor.

    A directory *below* home is fine (that is the normal media/downloads case);
    home itself and any directory containing home are not.
    """
    path = canonical(target)
    drive, tail = os.path.splitdrive(path)
    if path in (os.path.abspath(os.sep), os.path.sep) or (
        tail in ("", "/", "\\") and (drive or path.startswith("//") or path.startswith("\\\\"))
    ):
        raise ValueError("refusing to grant a filesystem/drive/UNC root")
    if path.startswith("//") or path.startswith("\\\\"):
        # ``//server/share`` is a UNC share root; descendants are still local
        # paths but must be granted explicitly and are checked below.
        parts = [part for part in path.replace("\\", "/").split("/") if part]
        if len(parts) <= 2:
            raise ValueError("refusing to grant a UNC share root")
    home = canonical(os.path.expanduser("~"))
    if same_path(path, home):
        raise ValueError("refusing to grant the user's home directory")
    if is_within(home, path):
        raise ValueError("refusing to grant an ancestor of the user's home directory")


class PathPolicy:
    """Classify and check paths against the material/workspace fence."""

    def __init__(self, workspace: str, materials: str | None = None) -> None:
        self.workspace = canonical(workspace)
        self.materials = canonical(materials) if materials else None

    def resolve(self, path: str | None, base: str | None = None) -> str:
        """Resolve a user-supplied path relative to the workspace by default."""
        if path is None or str(path).strip() == "":
            return self.workspace
        text = os.path.expanduser(str(path))
        if not os.path.isabs(text):
            text = os.path.join(base or self.workspace, text)
        return canonical(text)

    def locate(self, path: str | None, base: str | None = None) -> str:
        """Return ``workspace``, ``materials`` or ``outside`` for ``path``."""
        target = self.resolve(path, base)
        if self.materials and is_within(target, self.materials):
            if is_within(self.workspace, self.materials):
                return WORKSPACE if is_within(target, self.workspace) else MATERIALS
            return MATERIALS
        if is_within(target, self.workspace):
            return WORKSPACE
        return OUTSIDE

    def describe(self, path: str | None, base: str | None = None) -> dict[str, str]:
        """Resolved path plus its fence category."""
        target = self.resolve(path, base)
        return {"path": target, "location": self.locate(target)}

    def read_allowed(self, path: str | None, base: str | None = None) -> bool:
        """Reads are allowed inside materials or the workspace."""
        return self.locate(path, base) in (WORKSPACE, MATERIALS)

    def write_allowed(self, path: str | None, base: str | None = None) -> bool:
        """Writes are allowed inside the workspace only (control paths excluded)."""
        return self.locate(path, base) == WORKSPACE and not self.is_protected(path, base)

    def is_protected(self, path: str | None, base: str | None = None) -> bool:
        """True when the path is an engine-owned control path in the workspace."""
        return is_protected_path(self.workspace, self.resolve(path, base))

    def is_material(self, path: str | None, base: str | None = None) -> bool:
        """True when the path is course material (read-only)."""
        return self.locate(path, base) == MATERIALS

    def is_workspace(self, path: str | None, base: str | None = None) -> bool:
        """True when the path is inside the writable workspace."""
        return self.locate(path, base) == WORKSPACE
