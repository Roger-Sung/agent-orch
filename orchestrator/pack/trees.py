"""Snapshot and restore a candidate tree, for `A_restore_tree` (STATE-TABLE §5).

`freeze_output` records a candidate's fingerprint, which is enough to notice
that a tree changed and not nearly enough to put it back.  Restoring needs the
content, so the bytes go to the blob store and the shape goes in a snapshot.

Restoring is exact, not additive.  A restore that only rewrites the files it
knows about leaves whatever the interrupted writer created still sitting in the
tree, and the result would carry the candidate's fingerprint while not being
the candidate - the one outcome this exists to prevent.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .blobs import BlobStore
from .errors import PackError

SNAPSHOT_VERSION = 1


def snapshot(store: BlobStore, root: Path) -> dict[str, Any]:
    """Store every file's bytes and return the tree's shape."""
    root = Path(root)
    if not root.is_dir():
        raise PackError(f"cannot snapshot {root}: not a directory")
    files: list[dict[str, Any]] = []
    for path in sorted(p for p in root.rglob("*") if p.is_file() and not p.is_symlink()):
        data = path.read_bytes()
        digest = store.put(data)
        files.append({
            "path": str(path.relative_to(root)),
            "sha256": digest,
            # Only the execute bit survives: it is the one mode difference that
            # changes whether a restored tree behaves like the original.
            "executable": bool(path.stat().st_mode & 0o111),
        })
    return {"version": SNAPSHOT_VERSION, "files": files}


def restore(store: BlobStore, root: Path, tree: dict[str, Any]) -> dict[str, int]:
    """Make `root` exactly the snapshot again, and report what that took."""
    if tree.get("version") != SNAPSHOT_VERSION:
        raise PackError(f"unknown snapshot version {tree.get('version')!r}")
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)

    wanted = {entry["path"]: entry for entry in tree["files"]}
    present = {str(p.relative_to(root)) for p in root.rglob("*")
               if p.is_file() or p.is_symlink()}

    counts = {"written": 0, "removed": 0, "unchanged": 0}
    for relpath, entry in wanted.items():
        # The store is content-addressed and verifies on read, so a tampered
        # or truncated blob raises here rather than being written into the tree.
        data = store.get(entry["sha256"])
        target = root / relpath
        if target.is_file() and not target.is_symlink() and target.read_bytes() == data:
            counts["unchanged"] += 1
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            counts["written"] += 1
        mode = os.stat(target).st_mode
        os.chmod(target, (mode | 0o111) if entry["executable"] else (mode & ~0o111))

    for relpath in sorted(present - set(wanted)):
        # Anything the interrupted writer left behind goes, or the tree would
        # carry the candidate's fingerprint without being the candidate.
        (root / relpath).unlink()
        counts["removed"] += 1

    for directory in sorted((p for p in root.rglob("*") if p.is_dir()), reverse=True):
        if not any(directory.iterdir()):
            directory.rmdir()
    return counts
