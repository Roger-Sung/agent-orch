"""STATE-TABLE §5 stop evidence: E-1's revocation, and E-3's boot-id reader.

E-1 - revoke the operation's write capability, then verify.

Nothing here tries to establish that a process died.  That question turned out
to be unanswerable: a provider can `setsid` its helpers into process groups of
their own, so an empty original group proves only that the group is empty, not
that no writer survives (D-2026-09-16-01).  Which providers detach is equally
unverifiable and drifts with their versions, so it cannot be the criterion
either.

What the engine *can* observe about its own directories is used instead: the
roots are renamed aside, and only then is the host asked whether anything still
holds them.  Both steps are facts with a return value, not inferences.

The order is the whole mechanism.  Probing first would leave a gap between "the
probe came back empty" and "the rename happened" in which a process could open
the tree; renaming first means every later open by the old path finds nothing,
so a clean probe afterwards covers the remaining case of a descriptor that was
already open.  A survivor may recreate the old path and write there, but that
is a new inode - the operation's tree has already moved - and cleanup removes
it; the evidence is unaffected.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, Callable, Sequence

LSOF = "/usr/sbin/lsof"
SYSCTL = "/usr/sbin/sysctl"
KIND = "write_capability_revoked"

# Why a revocation did not produce evidence.  Every one of them leaves the
# operation unknown, which is the same conservative default E-3 uses.
NO_ROOTS = "no_roots"
CROSS_DEVICE = "cross_device"
RENAME_FAILED = "rename_failed"
OPEN_DESCRIPTORS = "open_descriptors"
ENUMERATION_INCOMPLETE = "enumeration_incomplete"


def lsof_holders(paths: Sequence[Path]) -> list[str] | None:
    """Processes holding any of `paths`, or None if the host could not say.

    `lsof` exits 1 both for "nothing holds this" and for "I could not look", so
    the two are told apart by stderr: a warning there means the enumeration was
    incomplete, and an incomplete enumeration is not evidence of an empty one.
    """
    holders: list[str] = []
    for path in paths:
        argv = [LSOF, "-F", "pn"]
        argv += ["+D", str(path)] if path.is_dir() else ["--", str(path)]
        try:
            done = subprocess.run(argv, capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            return None
        if done.returncode not in (0, 1) or done.stderr.strip():
            return None
        holders += [line for line in done.stdout.splitlines() if line.startswith("p")]
    return holders


def revoke(roots: Sequence[Path], *, quarantine: Path,
           holders: Callable[[Sequence[Path]], list[str] | None] = lsof_holders,
           ) -> dict[str, Any]:
    """Rename every root aside, then confirm nothing holds what was moved.

    Returns the evidence record itself - what was moved where, and what the
    probe saw - because §5 requires the operation to record how the evidence was
    obtained, not merely that it was.
    """
    roots = [Path(r) for r in roots]
    if not roots:
        # An empty list would otherwise "revoke" nothing and report success,
        # which is exactly the silent pass this evidence exists to prevent.
        return {"kind": KIND, "ok": False, "reason": NO_ROOTS, "moved": [], "holders": None}

    quarantine = Path(quarantine)
    quarantine.mkdir(parents=True, exist_ok=True)
    device = os.stat(quarantine).st_dev

    present = [r for r in roots if r.exists()]
    # Checked for every root before moving any: a cross-device rename would
    # fall back to copy-and-delete, which is not atomic, and stopping half way
    # through leaves some roots revoked and others live.
    for root in present:
        if os.stat(root).st_dev != device:
            return {"kind": KIND, "ok": False, "reason": CROSS_DEVICE,
                    "moved": [], "holders": None, "root": str(root)}

    moved: list[dict[str, str]] = []
    for index, root in enumerate(present):
        target = quarantine / f"{index}-{root.name}"
        try:
            os.rename(root, target)
        except OSError as exc:
            return {"kind": KIND, "ok": False, "reason": RENAME_FAILED,
                    "moved": moved, "holders": None, "detail": str(exc)}
        moved.append({"root": str(root), "moved_to": str(target)})

    seen = holders([Path(entry["moved_to"]) for entry in moved])
    if seen is None:
        return {"kind": KIND, "ok": False, "reason": ENUMERATION_INCOMPLETE,
                "moved": moved, "holders": None}
    if seen:
        return {"kind": KIND, "ok": False, "reason": OPEN_DESCRIPTORS,
                "moved": moved, "holders": sorted(set(seen))}
    return {"kind": KIND, "ok": True, "reason": None, "moved": moved, "holders": []}


def boot_session_uuid() -> str | None:
    """E-3's input: the host's boot session id, or None if it cannot be read.

    Generated by the kernel at boot and read-only, so a changed value means the
    host restarted and no process from before it survives.  A failed read
    returns None rather than a placeholder: comparing against a value that is
    not a boot id would turn "could not tell" into evidence.
    """
    try:
        done = subprocess.run([SYSCTL, "-n", "kern.bootsessionuuid"],
                              capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    value = done.stdout.strip()
    return value if done.returncode == 0 and value else None
