"""Filesystem containment for stages: prevention (L1) and detection (L2).

The worktree + git layer already in `runner.py` stops a result from *leaving*
through git. It does not stop a stage from writing outside its workspace —
same UNIX user, same filesystem. That gap is real: an executor once rewrote a
live store outside its workspace and only a human review caught it.

Two layers close it, and they are deliberately independent:

L1 — prevention. The stage's child process runs under `sandbox-exec` with a
     write allowlist: its workspace, its own artifact directory, the temporary
     directories, and the provider CLI's own state directories. Everything
     else is read-only to it.

L2 — detection. Before and after the stage, take a cheap sentinel snapshot of
     the declared protected roots and compare. If anything outside the
     workspace changed, the task is stopped and quarantined rather than
     advanced, and the offending paths are recorded as evidence.

L1 without L2 would trust a deprecated Apple tool completely. L2 without L1
would notice damage only after it happened. Neither is sufficient; both are
cheap.

A third layer — a separate UNIX identity and a network egress allowlist — is
out of scope and documented as a known gap rather than implied.
"""

from __future__ import annotations

import hashlib
import os
import platform
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


SANDBOX_EXEC = "/usr/bin/sandbox-exec"
PROTECTED_ROOTS_ENV = "ORCH_PROTECTED_ROOTS"

#: Directories a provider CLI legitimately writes to while running a stage.
#: Sourced from an observed write-path probe, not from guesswork; missing one
#: turns a healthy stage into a mysterious failure, so the policy is to allow
#: the whole state directory rather than individual files. SQLite in
#: particular writes `-wal` and `-shm` siblings, and denying those corrupts the
#: database rather than merely blocking a write.
PROVIDER_STATE_DIRS = (
    "~/.claude",
    "~/.cache/claude",
    "~/Library/Caches/claude-cli-nodejs",
    "~/.codex",
    "~/Library/Application Support/Codex",
    "~/Library/Application Support/com.openai.codex",
    "~/Library/Caches/com.openai.codex",
)

#: Paths every child process needs to write regardless of the task.
BASE_WRITE_ALLOW = ("/dev",)

#: Never sentinel these, even when they sit inside a protected root: they are
#: written by the provider CLI itself, by the OS, or by the orchestrator.
DEFAULT_SENTINEL_EXCLUDES = (
    ".git/objects",
    ".git/logs",
    "__pycache__",
    ".DS_Store",
    "node_modules",
    ".venv",
)


class ContainmentError(RuntimeError):
    """Containment could not be established. Always fail closed on this."""


class ContainmentConfigError(ContainmentError):
    """The configuration contradicts itself — e.g. a write root over a protected root.

    Distinct from an environment failure because the operator's next action is
    different: this one needs a configuration change, not a disk or a permission.
    """


class SandboxSetupError(ContainmentError):
    """The sandbox could not be set up for an environmental reason.

    A full disk or an unwritable artifact directory is not a misconfiguration of
    the containment policy, and reporting it as one sends the operator looking in
    the wrong place.
    """


# ---------------------------------------------------------------------------
# L1 — sandbox-exec write allowlist
# ---------------------------------------------------------------------------


def sandbox_available() -> bool:
    """True when this host can enforce L1 at all.

    `sandbox-exec` is deprecated by Apple and may disappear. That is precisely
    why callers must treat a False here as a stop condition rather than as
    permission to run unconfined.
    """
    if platform.system() != "Darwin":
        return False
    return os.path.isfile(SANDBOX_EXEC) and os.access(SANDBOX_EXEC, os.X_OK)


def _sbpl_quote(path: str) -> str:
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _resolved_existing(paths: Iterable[str | os.PathLike[str]]) -> list[str]:
    """Resolve to realpaths, keeping only what exists.

    The sandbox matches on resolved paths, so `/tmp` (a symlink on macOS) must
    become `/private/tmp` or the rule silently never matches. Non-existent
    directories are dropped: a `subpath` rule for a missing directory is not an
    error, but keeping the list honest makes the generated profile readable.
    """
    resolved: list[str] = []
    seen: set[str] = set()
    for raw in paths:
        expanded = os.path.expanduser(str(raw))
        if not os.path.exists(expanded):
            continue
        real = os.path.realpath(expanded)
        if real not in seen:
            seen.add(real)
            resolved.append(real)
    return resolved


def write_allowlist(workspace: Path, artifact_dir: Path, extra: Iterable[str | os.PathLike[str]] = ()) -> list[str]:
    """The directories a contained stage may write to."""
    candidates: list[str | os.PathLike[str]] = [workspace, artifact_dir]
    candidates.extend(BASE_WRITE_ALLOW)
    candidates.extend(PROVIDER_STATE_DIRS)
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        candidates.append(codex_home)
    for var in ("TMPDIR", "TEMP", "TMP"):
        value = os.environ.get(var)
        if value:
            candidates.append(value)
    candidates.extend(["/tmp", "/private/tmp", "/private/var/tmp", "/var/folders"])
    candidates.extend(extra)
    return _resolved_existing(candidates)


def build_sandbox_profile(workspace: Path, artifact_dir: Path, extra: Iterable[str | os.PathLike[str]] = ()) -> str:
    """Generate the SBPL profile text.

    Shape: allow everything, then deny all writes, then re-allow writes to the
    allowlist. Reads and network stay open on purpose — this layer is about
    *unintended mutation*, and pretending it also provides network isolation
    would overstate it.
    """
    allowed = write_allowlist(workspace, artifact_dir, extra)
    lines = [
        "(version 1)",
        ";; agent-orch L1: write allowlist. Reads and network are unrestricted;",
        ";; this layer exists to stop writes outside the task workspace.",
        "(allow default)",
        "(deny file-write*)",
    ]
    for path in allowed:
        lines.append(f"(allow file-write* (subpath {_sbpl_quote(path)}))")
    lines.append("")
    return "\n".join(lines)


@dataclass(frozen=True)
class SandboxDecision:
    """Whether L1 is in force for this stage, and why."""

    mode: str  # "sandboxed" | "unsandboxed" | "unavailable"
    reason: str
    profile_path: Path | None = None

    @property
    def blocks_run(self) -> bool:
        return self.mode == "unavailable"

    def wrap(self, command: list[str]) -> list[str]:
        if self.mode != "sandboxed" or self.profile_path is None:
            return command
        return [SANDBOX_EXEC, "-f", str(self.profile_path), *command]


def prepare_sandbox(
    workspace: Path,
    artifact_dir: Path,
    *,
    allow_unsandboxed: bool = False,
    extra_allow: Iterable[str | os.PathLike[str]] = (),
    protected_roots: Iterable[str | os.PathLike[str]] | None = None,
) -> SandboxDecision:
    """Set up L1, or refuse to run.

    Fail-closed: when the host cannot sandbox and the caller has not explicitly
    accepted that with `allow_unsandboxed`, the decision blocks the run. A
    silent downgrade to the previous unconfined behaviour is exactly the bug
    this layer exists to prevent.
    """
    declared_extra = tuple(extra_allow) + extra_write_roots_from_env()
    watched = protected_roots if protected_roots is not None else protected_roots_from_env()
    # Validate before the availability check: a contradictory configuration is
    # worth reporting even on a host that could not enforce L1 anyway.
    validate_extra_write_roots(declared_extra, watched)

    if not sandbox_available():
        if allow_unsandboxed:
            return SandboxDecision("unsandboxed", "sandbox_explicitly_disabled")
        return SandboxDecision("unavailable", "sandbox_unavailable")
    if allow_unsandboxed:
        return SandboxDecision("unsandboxed", "sandbox_explicitly_disabled")

    try:
        artifact_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SandboxSetupError(f"cannot create the stage artifact directory: {exc}") from exc
    profile_path = artifact_dir / "sandbox.sb"
    try:
        profile_path.write_text(build_sandbox_profile(workspace, artifact_dir, declared_extra), encoding="utf-8")
    except OSError as exc:
        raise SandboxSetupError(f"cannot write sandbox profile: {exc}") from exc
    return SandboxDecision("sandboxed", "sandbox_active", profile_path)


# ---------------------------------------------------------------------------
# L2 — sentinel snapshot over declared protected roots
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Violation:
    path: str
    kind: str  # "added" | "modified" | "removed"

    def as_dict(self) -> dict[str, str]:
        return {"path": self.path, "kind": self.kind}


@dataclass
class Sentinel:
    """Detects writes to protected roots that happen outside the workspace.

    Cost control, in order:
      1. excluded subtrees are never walked;
      2. anything inside the workspace is ignored — that is where work belongs;
      3. the snapshot records mtime and size for every file, and a content hash
         only for files below `hash_below_bytes`;
      4. the hash is consulted only for files that already look modified, so a
         touch that did not change content is reported as benign rather than as
         a violation.

    Files above the hash threshold cannot be proven unchanged, so any mtime or
    size difference on them counts as a violation. That asymmetry is deliberate:
    the expensive-to-verify case fails closed.
    """

    roots: tuple[Path, ...]
    workspace: Path | None = None
    excludes: tuple[str, ...] = DEFAULT_SENTINEL_EXCLUDES
    hash_below_bytes: int = 262_144
    _cache: dict[str, str] = field(default_factory=dict, repr=False)

    def _is_excluded(self, path: Path) -> bool:
        text = str(path)
        for token in self.excludes:
            if token in text.split(os.sep) or f"{os.sep}{token}{os.sep}" in text or text.endswith(f"{os.sep}{token}"):
                return True
        return False

    def _in_workspace(self, path: Path) -> bool:
        """True when `path` is inside the workspace.

        Uses path containment on resolved paths rather than string prefixes: a
        sibling directory whose name merely starts with the workspace name
        (`/w/task` vs `/w/task-protected`) must NOT be treated as inside it,
        or an over-broad exclusion quietly reopens the hole this class closes.
        """
        if self.workspace is None:
            return False
        try:
            path.resolve().relative_to(self.workspace.resolve())
        except (ValueError, OSError):
            return False
        return True

    def snapshot(self) -> dict[str, tuple[int, int, str | None]]:
        state: dict[str, tuple[int, int, str | None]] = {}
        for root in self.roots:
            if not root.exists():
                continue
            for dirpath, dirnames, filenames in os.walk(root):
                current = Path(dirpath)
                if self._is_excluded(current) or self._in_workspace(current):
                    dirnames[:] = []
                    continue
                dirnames[:] = [d for d in dirnames if not self._is_excluded(current / d)]
                for name in filenames:
                    path = current / name
                    if self._is_excluded(path) or self._in_workspace(path):
                        continue
                    try:
                        stat = path.stat()
                    except OSError:
                        continue
                    digest: str | None = None
                    if stat.st_size <= self.hash_below_bytes:
                        digest = self._hash(path)
                    state[str(path)] = (stat.st_mtime_ns, stat.st_size, digest)
        return state

    @staticmethod
    def _hash(path: Path) -> str | None:
        try:
            with path.open("rb") as handle:
                return hashlib.sha256(handle.read()).hexdigest()
        except OSError:
            return None

    def compare(self, before: dict[str, tuple[int, int, str | None]]) -> list[Violation]:
        after = self.snapshot()
        violations: list[Violation] = []
        for path, entry in after.items():
            previous = before.get(path)
            if previous is None:
                violations.append(Violation(path, "added"))
                continue
            if previous == entry:
                continue
            prev_mtime, prev_size, prev_hash = previous
            _, size, digest = entry
            if prev_hash is not None and digest is not None and prev_hash == digest:
                # Touched but not changed: mtime moved, content identical.
                continue
            if prev_size == size and prev_hash is None and digest is None:
                # Too large to hash: cannot prove it is unchanged, so treat the
                # mtime change as a violation rather than assume innocence.
                violations.append(Violation(path, "modified"))
                continue
            violations.append(Violation(path, "modified"))
        for path in before:
            if path not in after:
                violations.append(Violation(path, "removed"))
        violations.sort(key=lambda item: (item.kind, item.path))
        return violations


EXTRA_WRITE_ROOTS_ENV = "ORCH_EXTRA_WRITE_ROOTS"


def extra_write_roots_from_env(value: str | None = None) -> tuple[Path, ...]:
    """Additional write roots a deployment declares (`os.pathsep` separated).

    Real work needs to write outside the workspace: a JVM build wants its
    dependency cache and daemon directory, a package manager wants its store.
    Without a way to say so, the only escape was `--allow-unsandboxed`, which
    turns the whole layer off for every path at once. This is the narrow
    version of that escape — the deployment names the directories, and
    everything else stays denied.

    Empty by default; unset means the previous behaviour exactly.
    """
    raw = os.environ.get(EXTRA_WRITE_ROOTS_ENV, "") if value is None else value
    roots = []
    for part in raw.split(os.pathsep):
        part = part.strip()
        if part:
            roots.append(Path(os.path.expanduser(part)))
    return tuple(roots)


def _fold(path: str) -> str:
    """Casefolded form, used *in addition* to the exact comparison.

    macOS filesystems are case-insensitive by default and `realpath` does not
    normalise case, so `/users/<name>` and `/Users/<name>` resolve to different
    strings while naming the same directory. Comparing both ways can only ever
    refuse more configurations, never fewer, which is the safe direction for a
    guard: a case-sensitive filesystem might see two genuinely distinct paths
    rejected, and that is a far cheaper mistake than admitting an overlap.
    """
    return path.casefold()


def _same_object(first: str, second: str) -> bool:
    """Identity via the filesystem, when both paths exist.

    Catches what string work cannot: case variants on a case-insensitive
    volume, hardlinked directories, and two mount paths for one tree.
    """
    try:
        left = os.stat(first)
        right = os.stat(second)
    except OSError:
        return False
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _contains(outer: str, inner: str) -> bool:
    """Whether `outer` contains `inner`, component-aware.

    Not a plain string prefix test: `/a/b` must not count as containing
    `/a/bc`. Trailing separators are trimmed first, because the filesystem root
    is the one path whose separator is also its whole name — without that,
    `outer="/"` builds the prefix `"//"`, matches nothing, and a declared write
    root of `/` sails through the overlap guard while allowlisting the entire
    filesystem.
    """
    trimmed_outer = outer.rstrip(os.sep) or os.sep
    trimmed_inner = inner.rstrip(os.sep) or os.sep
    if trimmed_outer == trimmed_inner or _fold(trimmed_outer) == _fold(trimmed_inner):
        return True
    prefix = trimmed_outer if trimmed_outer.endswith(os.sep) else trimmed_outer + os.sep
    return trimmed_inner.startswith(prefix) or _fold(trimmed_inner).startswith(_fold(prefix))


def _forms(path: str | os.PathLike[str]) -> tuple[str, str]:
    """The two forms a path has to be judged in.

    Lexical (expanded, absolutised, symlinks intact) and resolved. Both matter:
    resolving alone hides the case where a *protected root is itself a symlink
    living inside a declared write root* — the target lies outside, so nothing
    looks wrong, while the stage can rewrite the link and re-point the sentinel
    anchor at a decoy tree of identical content. Comparing the lexical form as
    well catches that the link sits in writable space.
    """
    lexical = os.path.abspath(os.path.expanduser(str(path)))
    return lexical, os.path.realpath(lexical)


def validate_extra_write_roots(
    extra: Iterable[str | os.PathLike[str]], protected: Iterable[str | os.PathLike[str]]
) -> None:
    """Refuse a declared write root that overlaps a protected root.

    Allowing both would be incoherent: L1 would permit writes to a tree L2 is
    watching, so a stage could damage a protected root and the run would look
    clean because the write was "allowed". Overlap in either direction is a
    configuration error, and it fails closed rather than silently letting the
    allowlist punch a hole in the detection layer.

    Every comparison runs over both the lexical and the resolved form of each
    path, plus a filesystem-identity check, so a symlink cannot hide an overlap
    and a case variant cannot slip past on a case-insensitive volume.
    """
    watched = [(str(item), *_forms(item)) for item in protected]
    if not watched:
        return
    conflicts: list[str] = []
    for item in extra:
        declared = str(item)
        extra_forms = _forms(item)
        for original, protected_lexical, protected_resolved in watched:
            protected_forms = (protected_lexical, protected_resolved)
            if any(_same_object(e, p) for e in extra_forms for p in protected_forms):
                conflicts.append(f"{declared} is the same directory as protected root {original}")
                continue
            if any(_contains(p, e) for e in extra_forms for p in protected_forms):
                conflicts.append(f"{declared} is inside protected root {original}")
            elif any(_contains(e, p) for e in extra_forms for p in protected_forms):
                conflicts.append(f"{declared} contains protected root {original}")
    if conflicts:
        raise ContainmentConfigError(
            f"{EXTRA_WRITE_ROOTS_ENV} overlaps {PROTECTED_ROOTS_ENV}: "
            + "; ".join(conflicts)
            + ". A write root that covers a protected root would let L1 permit exactly what L2 watches for."
        )


def protected_roots_from_env(value: str | None = None) -> tuple[Path, ...]:
    """Read declared protected roots from `ORCH_PROTECTED_ROOTS` (os.pathsep separated).

    Empty by default. A deployment declares what it wants watched; the engine
    ships with no opinion about which directories on someone else's machine
    deserve protection.
    """
    raw = os.environ.get(PROTECTED_ROOTS_ENV, "") if value is None else value
    roots = []
    for part in raw.split(os.pathsep):
        part = part.strip()
        if part:
            roots.append(Path(os.path.expanduser(part)))
    return tuple(roots)
