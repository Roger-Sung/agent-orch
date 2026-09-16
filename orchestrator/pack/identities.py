"""The six pack-v1 identities (IDENTITIES, D-2026-09-14-07).

Only ``candidate_fingerprint`` and the two digests over directory trees touch
the filesystem; everything else is a hash over data the caller already holds.

Two invariants run through the whole module and are worth stating once:

* **Same input, same hash, on any machine** (I-5).  Every git call goes through
  ``git_argv`` so the pinned ``-c`` overrides cannot be forgotten, and the
  managed set is computed against a throwaway index so the real index's staging
  state - ``git add -f``, ``git rm --cached`` - cannot move the result.
* **Refusing to mint an identity is a first-class outcome** (I-7).  A FIFO in
  the tree does not produce a fingerprint "without that entry"; it raises
  ``IdentityRefused`` with the spec's reject code, and the caller turns that
  into a hold.
"""
from __future__ import annotations

import hashlib
import os
import stat
import struct
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Iterable, Sequence

from ..profile import canonical_json
from .errors import ContractRejected, IdentityRefused

CANDIDATE_MAGIC = b"agent-orch-candidate-v4\0"
PACKAGE_MAGIC = b"agent-orch-package-v1\0"

# I-8: every git invocation is pinned so that a user's ~/.gitconfig cannot
# change what the engine sees.
GIT_PINS = (
    "-c", "core.excludesFile=/dev/null",
    "-c", "core.ignoreCase=false",
    "-c", "core.precomposeUnicode=false",
    "-c", "core.quotePath=false",
    "-c", "core.fileMode=true",
)

KIND_ABSENT = b"A"
KIND_SYMLINK = b"L"
KIND_FILE = b"F"
KIND_EXEC = b"X"
KIND_GITLINK = b"G"

EXISTENCE_KINDS = frozenset({KIND_FILE, KIND_EXEC, KIND_SYMLINK})
ZERO32 = b"\0" * 32

# IDENTITIES §6.0 stage A': when several reject codes hold at once, the freeze
# reports the first in this fixed order.  Collecting and then ranking - rather
# than raising at the first one encountered - is what makes the reported code
# independent of set iteration order, which is the whole point of pinning it.
REJECT_ORDER: tuple[str, ...] = (
    "nested_repo_untracked",
    "ignore_file_not_bound",
    "unsupported_entry",
    "gitlink_replaced",
    "path_aliasing",
    "path_name_mismatch",
    "scope_enumeration_failed",
    "symlink_dir_in_scope",
    "nested_repo_in_scope",
    "excluded_source_in_scope",
)
_REJECT_RANK = {code: rank for rank, code in enumerate(REJECT_ORDER)}


class Rejections:
    """Collects candidate reject codes and yields the first by REJECT_ORDER.

    Within one code the spec keeps the bytewise-smallest path, so two runs over
    the same tree report the same path as well as the same code.
    """

    def __init__(self) -> None:
        self._best: dict[str, bytes | None] = {}
        self._detail: dict[str, str | None] = {}

    def add(self, code: str, path: bytes | None = None, detail: str | None = None) -> None:
        if code not in _REJECT_RANK:
            raise AssertionError(f"reject code {code!r} is not ranked in REJECT_ORDER")
        current = self._best.get(code, ...)
        if current is ... or (path is not None and (current is None or path < current)):
            self._best[code] = path
            self._detail[code] = detail

    def __bool__(self) -> bool:
        return bool(self._best)

    def raise_first(self) -> None:
        if not self._best:
            return
        code = min(self._best, key=lambda c: _REJECT_RANK[c])
        raise IdentityRefused(code, self._best[code], self._detail[code])


def git_argv(*args: str) -> list[str]:
    return ["git", *GIT_PINS, *args]


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> bytes:
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    proc = subprocess.run(
        git_argv("-C", str(repo), *args),
        capture_output=True,
        env=full_env,
        check=False,
    )
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()
        raise IdentityRefused("git_failed", detail=f"{' '.join(args)}: {detail}")
    return proc.stdout


def head_sha(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").decode().strip()


def compose_excludes(engine: Sequence[str], target: Sequence[str]) -> bytes:
    """Build the ``--exclude-from`` file body (IDENTITIES §2.3).

    Engine patterns come first and negation is forbidden, which together make
    the engine list unremovable: ``--exclude-from`` is applied after the
    per-directory ``.gitignore`` files, and without ``!`` nothing downstream can
    re-include what the engine excluded.
    """
    lines: list[str] = []
    for source, patterns in (("engine", engine), ("target", target)):
        for pattern in patterns:
            if not pattern or pattern.strip() == "":
                raise ContractRejected("invalid_exclude_pattern", f"{source}: empty pattern")
            if pattern.startswith("!"):
                raise ContractRejected("invalid_exclude_pattern", f"{source}: negation {pattern!r}")
            if pattern.startswith("#"):
                raise ContractRejected("invalid_exclude_pattern", f"{source}: comment {pattern!r}")
            if "\n" in pattern:
                raise ContractRejected("invalid_exclude_pattern", f"{source}: newline in {pattern!r}")
            lines.append(pattern)
    return ("\n".join(lines) + "\n").encode("utf-8") if lines else b""


def managed_set(
    repo: Path, excludes_body: bytes, rejections: "Rejections | None" = None
) -> tuple[set[bytes], set[bytes], dict[bytes, str]]:
    """Return ``(M_head, U, head_gitlink_shas)`` - the managed set M (step 1).

    ``M_head`` is every path in the HEAD tree, read through a temporary index so
    the working index never participates.  ``U`` is everything on disk that is
    neither in HEAD nor excluded.  ``head_gitlink_shas`` maps each mode-160000
    path to the sha the superproject records, which is the only source for an
    uninitialised submodule.
    """
    with tempfile.TemporaryDirectory() as tmp:
        index_path = Path(tmp) / "index"
        excludes_path = Path(tmp) / "excludes"
        excludes_path.write_bytes(excludes_body)
        env = {"GIT_INDEX_FILE": str(index_path)}

        _git(repo, "read-tree", "HEAD", env=env)
        head_out = _git(repo, "ls-files", "-z", "--stage", env=env)
        others_out = _git(
            repo,
            "ls-files", "-z", "--others",
            "--exclude-per-directory=.gitignore",
            f"--exclude-from={excludes_path}",
            env=env,
        )

    m_head: set[bytes] = set()
    head_gitlink_shas: dict[bytes, str] = {}
    for record in head_out.split(b"\0"):
        if not record:
            continue
        meta, _, path = record.partition(b"\t")
        fields = meta.split(b" ")
        m_head.add(path)
        if fields[0] == b"160000":
            head_gitlink_shas[path] = fields[1].decode()

    others: set[bytes] = set()
    for path in others_out.split(b"\0"):
        if not path:
            continue
        if path.endswith(b"/"):
            # ls-files --others collapses a nested repo to its directory; we
            # cannot hash a tree we do not manage.
            if rejections is None:
                raise IdentityRefused("nested_repo_untracked", path)
            rejections.add("nested_repo_untracked", path)
            continue
        others.add(path)

    return m_head, others, head_gitlink_shas


def _gitlink_entry(
    repo: Path, path: bytes, head_gitlink_sha: str, rejections: "Rejections"
) -> bytes | None:
    """Resolve one gitlink to its 32-byte payload (IDENTITIES §2.3 state machine)."""
    full = repo / os.fsdecode(path)
    try:
        st = os.lstat(full)
    except FileNotFoundError:
        return KIND_ABSENT + ZERO32
    except OSError as exc:
        rejections.add("scope_enumeration_failed", path, str(exc))
        return None

    if not stat.S_ISDIR(st.st_mode):
        rejections.add("gitlink_replaced", path)
        return None
    if (full / ".git").exists():
        sha = _git(full, "rev-parse", "HEAD").decode().strip()
        return KIND_GITLINK + bytes.fromhex(sha).rjust(32, b"\0")
    if any(full.iterdir()):
        rejections.add("gitlink_replaced", path)
        return None
    # Uninitialised submodule: the superproject's HEAD tree is the only source.
    return KIND_GITLINK + bytes.fromhex(head_gitlink_sha).rjust(32, b"\0")


def _entry_for(repo: Path, path: bytes, in_head: bool, rejections: "Rejections") -> bytes | None:
    full = repo / os.fsdecode(path)
    try:
        st = os.lstat(full)
    except FileNotFoundError:
        if in_head:
            return KIND_ABSENT + ZERO32
        rejections.add("scope_enumeration_failed", path, "vanished during freeze")
        return None
    except OSError as exc:
        rejections.add("scope_enumeration_failed", path, str(exc))
        return None

    mode = st.st_mode
    if stat.S_ISLNK(mode):
        link = os.readlink(full).encode("utf-8", "surrogateescape")
        return KIND_SYMLINK + hashlib.sha256(link).digest()
    if stat.S_ISREG(mode):
        try:
            digest = hashlib.sha256(full.read_bytes()).digest()
        except OSError as exc:
            rejections.add("scope_enumeration_failed", path, str(exc))
            return None
        return (KIND_EXEC if mode & 0o111 else KIND_FILE) + digest
    rejections.add("unsupported_entry", path)
    return None


def _check_aliases(repo: Path, paths: Iterable[bytes], rejections: "Rejections") -> None:
    """(a) two managed paths sharing one inode, then (b) byte-exact path names."""
    seen: dict[tuple[int, int], bytes] = {}
    existing: list[bytes] = []
    for path in paths:
        full = repo / os.fsdecode(path)
        try:
            st = os.lstat(full)
        except OSError:
            continue
        existing.append(path)
        if stat.S_ISREG(st.st_mode):
            key = (st.st_dev, st.st_ino)
            if key in seen:
                rejections.add("path_aliasing", max(path, seen[key]))
            else:
                seen[key] = path

    for path in existing:
        parent = repo
        for segment in path.split(b"/")[:-1]:
            try:
                entries = os.listdir(os.fsencode(parent))
            except OSError as exc:
                rejections.add("path_name_mismatch", path, str(exc))
                break
            if segment not in entries:
                rejections.add("path_name_mismatch", path)
                break
            parent = parent / os.fsdecode(segment)
        else:
            leaf = path.rsplit(b"/", 1)[-1]
            try:
                entries = os.listdir(os.fsencode(parent))
            except OSError as exc:
                rejections.add("path_name_mismatch", path, str(exc))
                continue
            if leaf not in entries:
                rejections.add("path_name_mismatch", path)


def _encode_body(entries: dict[bytes, bytes]) -> bytes:
    out = bytearray()
    for path in sorted(entries):
        payload = entries[path]
        out += struct.pack(">I", len(path)) + path + payload
    return bytes(out)


def tree_manifest(repo: Path, excludes_body: bytes = b"") -> dict[str, Any]:
    """Freeze the working tree into the v4 record stream (IDENTITIES §2.3).

    Returns the body, the entry map, and both digests.  Raises
    ``IdentityRefused`` with the spec's code for anything that cannot be
    represented - the caller never gets a fingerprint computed over a partial
    tree.
    """
    repo = Path(repo)
    rejections = Rejections()
    m_head, others, head_gitlink_shas = managed_set(repo, excludes_body, rejections)
    managed = m_head | others
    _check_aliases(repo, managed, rejections)

    entries: dict[bytes, bytes] = {}
    for path in sorted(managed):
        if path in head_gitlink_shas:
            entry = _gitlink_entry(repo, path, head_gitlink_shas[path], rejections)
        else:
            entry = _entry_for(repo, path, path in m_head, rejections)
        if entry is not None:
            entries[path] = entry

    check_ignore_binding(repo, managed, excludes_body, rejections)
    rejections.raise_first()

    body = _encode_body(entries)
    head = head_sha(repo)
    header = CANDIDATE_MAGIC + struct.pack(">B", len(bytes.fromhex(head))) + bytes.fromhex(head)
    return {
        "body": body,
        "entries": entries,
        "head": head,
        "tree_manifest_sha256": hashlib.sha256(body).hexdigest(),
        "candidate_fingerprint": "sha256:" + hashlib.sha256(header + body).hexdigest(),
    }


def candidate_fingerprint(repo: Path, excludes_body: bytes = b"") -> str:
    return tree_manifest(repo, excludes_body)["candidate_fingerprint"]


def existence_set(manifest: dict[str, Any]) -> set[bytes]:
    """Paths whose kind is F / X / L (EV-R-04D)."""
    return {path for path, payload in manifest["entries"].items() if payload[:1] in EXISTENCE_KINDS}


# --------------------------------------------------------------------------
# ignore binding (IDENTITIES §2.3)
# --------------------------------------------------------------------------

def excluded_set(repo: Path, excludes_body: bytes) -> set[bytes]:
    """Set E - the directories and files git reports as excluded."""
    with tempfile.TemporaryDirectory() as tmp:
        index_path = Path(tmp) / "index"
        excludes_path = Path(tmp) / "excludes"
        excludes_path.write_bytes(excludes_body)
        env = {"GIT_INDEX_FILE": str(index_path)}
        _git(repo, "read-tree", "HEAD", env=env)
        out = _git(
            repo,
            "ls-files", "-z", "--others", "--ignored", "--directory",
            "--exclude-per-directory=.gitignore",
            f"--exclude-from={excludes_path}",
            env=env,
        )
    return {path for path in out.split(b"\0") if path}


def check_ignore_binding(
    repo: Path, managed: set[bytes], excludes_body: bytes, rejections: "Rejections"
) -> None:
    """Every .gitignore that still matters must itself be in M.

    An unbound .gitignore would let someone change which paths are hashed
    without changing the fingerprint - the rule exists to close exactly that.
    A .gitignore *inside* an excluded directory does not take part in matching,
    so it is not required to be bound.
    """
    excluded = excluded_set(repo, excludes_body)
    excluded_dirs = [p.rstrip(b"/") for p in excluded if p.endswith(b"/")]

    for dirpath, dirnames, filenames in os.walk(os.fsencode(repo)):
        dirnames[:] = [d for d in dirnames if d != b".git"]
        rel_dir = os.path.relpath(dirpath, os.fsencode(repo))
        rel_dir = b"" if rel_dir == b"." else rel_dir
        # An initialised submodule is a separate repository; its ignore files
        # bind there, not here.
        if rel_dir and (Path(os.fsdecode(dirpath)) / ".git").exists():
            dirnames[:] = []
            continue
        if b".gitignore" not in filenames:
            continue
        rel = b".gitignore" if not rel_dir else rel_dir + b"/.gitignore"
        under_excluded = any(
            rel_dir == d or rel_dir.startswith(d + b"/") for d in excluded_dirs
        )
        if under_excluded:
            continue
        if rel not in managed:
            rejections.add("ignore_file_not_bound", rel)


# --------------------------------------------------------------------------
# scope expansion and protection (IDENTITIES §2.3)
# --------------------------------------------------------------------------

def classify_scope_item(item: str, *, always_dir: bool = False) -> str:
    """``dir`` | ``glob`` | ``file`` - decided by the literal, never by disk.

    Deciding by what is on disk would make the contract's meaning depend on the
    working tree it is checked against, so the spec pins it to the spelling.
    """
    if always_dir:
        return "dir"
    if any(marker in item for marker in ("*", "?")):
        return "glob"
    if item.endswith("/"):
        return "dir"
    return "file"


def _segments(value: str) -> list[str]:
    return [seg for seg in value.strip("/").split("/") if seg]


def _match_segment(pattern: str, segment: str) -> bool:
    """`*` and `?` inside one segment; engine-implemented, no fnmatch."""
    p = list(pattern)
    s = list(segment)
    pi = si = 0
    star_p = star_s = -1
    while si < len(s):
        if pi < len(p) and (p[pi] == "?" or p[pi] == s[si]):
            pi += 1
            si += 1
        elif pi < len(p) and p[pi] == "*":
            star_p = pi
            star_s = si
            pi += 1
        elif star_p >= 0:
            star_s += 1
            si = star_s
            pi = star_p + 1
        else:
            return False
    while pi < len(p) and p[pi] == "*":
        pi += 1
    return pi == len(p)


def glob_match(pattern: str, path: str) -> bool:
    """Segment-wise match where ``**`` is its own segment matching zero or more."""
    pats = _segments(pattern)
    segs = _segments(path)

    def walk(pi: int, si: int) -> bool:
        while pi < len(pats):
            if pats[pi] == "**":
                for skip in range(si, len(segs) + 1):
                    if walk(pi + 1, skip):
                        return True
                return False
            if si >= len(segs) or not _match_segment(pats[pi], segs[si]):
                return False
            pi += 1
            si += 1
        return si == len(segs)

    return walk(0, 0)


def _glob_literal_prefix(pattern: str) -> str:
    out: list[str] = []
    for seg in _segments(pattern):
        if seg == "**" or "*" in seg or "?" in seg:
            break
        out.append(seg)
    return "/".join(out)


def _glob_dir_may_match(pattern: str, rel_dir: str) -> bool:
    """Could any file under ``rel_dir`` still match ``pattern``?"""
    pats = _segments(pattern)
    segs = _segments(rel_dir)

    def walk(pi: int, si: int) -> bool:
        if si == len(segs):
            return pi < len(pats)
        if pi >= len(pats):
            return False
        if pats[pi] == "**":
            return any(walk(pi + 1, skip) for skip in range(si, len(segs) + 1)) or True
        if not _match_segment(pats[pi], segs[si]):
            return False
        return walk(pi + 1, si + 1)

    return walk(0, 0)


def _ancestors(rel: str) -> list[str]:
    segs = _segments(rel)
    return ["/".join(segs[:i]) for i in range(len(segs))]


def scan_scope(
    repo: Path,
    managed: set[bytes],
    *,
    readable_roots: Sequence[str] = (),
    files_writable: Sequence[str] = (),
    protected_source_globs: Sequence[str] = (),
    rejections: "Rejections | None" = None,
) -> set[str]:
    """Expand the contract's scope to the file set S and enforce its protections.

    The reachable-directory set R is what keeps this from over-rejecting: a
    symlinked directory or a nested repo that no scope item could ever reach is
    none of our business, so only directories inside R are inspected.
    """
    own = rejections is None
    rejections = rejections or Rejections()
    repo_b = os.fsencode(repo)

    items: list[tuple[str, str]] = [
        (item, classify_scope_item(item, always_dir=True)) for item in readable_roots
    ] + [(item, classify_scope_item(item)) for item in files_writable]

    # Ancestors of every scope item must not themselves be symlinks, otherwise
    # the whole subtree the contract names is not the subtree we scanned.
    for item, _kind in items:
        for ancestor in _ancestors(item) + [item.rstrip("/")]:
            if not ancestor:
                continue
            try:
                st = os.lstat(repo / ancestor)
            except FileNotFoundError:
                break
            except OSError as exc:
                rejections.add("scope_enumeration_failed", ancestor.encode(), str(exc))
                break
            if stat.S_ISLNK(st.st_mode):
                rejections.add("symlink_dir_in_scope", ancestor.encode())
                break

    def in_reachable(rel_dir: str) -> bool:
        for item, kind in items:
            base = item.rstrip("/")
            if kind == "dir":
                if rel_dir == base or rel_dir.startswith(base + "/") or base.startswith(rel_dir + "/") or rel_dir == "":
                    return True
            elif kind == "file":
                if base.startswith(rel_dir + "/") or rel_dir == "":
                    return True
            else:
                prefix = _glob_literal_prefix(item)
                if prefix and not (rel_dir == prefix or rel_dir.startswith(prefix + "/") or prefix.startswith(rel_dir + "/") or rel_dir == ""):
                    continue
                if _glob_dir_may_match(item, rel_dir) or rel_dir == "":
                    return True
        return False

    def matches_scope(rel_file: str) -> bool:
        for item, kind in items:
            base = item.rstrip("/")
            if kind == "file" and rel_file == base:
                return True
            if kind == "dir" and (rel_file.startswith(base + "/") or base == ""):
                return True
            if kind == "glob" and glob_match(item, rel_file):
                return True
        return False

    selected: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(repo_b, onerror=lambda e: rejections.add(
        "scope_enumeration_failed", getattr(e, "filename", b"") or b"", str(e)
    )):
        rel_dir_b = os.path.relpath(dirpath, repo_b)
        rel_dir = "" if rel_dir_b == b"." else os.fsdecode(rel_dir_b)
        if b".git" in dirnames and rel_dir:
            if in_reachable(rel_dir):
                rejections.add("nested_repo_in_scope", os.fsencode(rel_dir + "/.git"))
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if d != b".git"]

        if not in_reachable(rel_dir):
            dirnames[:] = []
            continue

        for name in list(dirnames):
            child = os.path.join(dirpath, name)
            if os.path.islink(child):
                child_rel = os.fsdecode(os.path.relpath(child, repo_b))
                if in_reachable(child_rel) or any(
                    _segments(item.rstrip("/"))[: len(_segments(child_rel))] == _segments(child_rel)
                    for item, _ in items
                ):
                    rejections.add("symlink_dir_in_scope", os.fsencode(child_rel))
                dirnames.remove(name)

        for name in filenames:
            rel_file = os.fsdecode(os.path.join(rel_dir.encode() if rel_dir else b"", name)) if rel_dir else os.fsdecode(name)
            if matches_scope(rel_file):
                selected.add(rel_file)

    for rel_file in sorted(selected):
        if os.fsencode(rel_file) in managed:
            continue
        if any(glob_match(pattern, rel_file) for pattern in protected_source_globs):
            rejections.add("excluded_source_in_scope", os.fsencode(rel_file))

    if own:
        rejections.raise_first()
    return selected


# --------------------------------------------------------------------------
# plan / manifest identities (IDENTITIES §2.2)
# --------------------------------------------------------------------------

PLAN_SUBSET_PACK_KEYS = ("id", "tasks", "files_writable", "obligations", "depends", "tier")


def plan_subset(manifest: dict[str, Any]) -> dict[str, Any]:
    """The canonical subset the plan identity is taken over (pack_readiness.py:632-641).

    Array order follows the manifest: reordering packs is a plan change, so it
    must not be normalised away.
    """
    packs = []
    for pack in manifest.get("packs", []):
        entry = {key: pack.get(key) for key in PLAN_SUBSET_PACK_KEYS}
        entry["obligations"] = [
            ob["id"] if isinstance(ob, dict) else ob for ob in (pack.get("obligations") or [])
        ]
        packs.append(entry)
    return {
        "packs": packs,
        # The subset carries deferral *refs* only (pack_readiness.py:632-641):
        # who owns a deferral and when it releases are not part of the plan
        # identity, so editing an owner must not look like a plan change.
        "deferred": [
            item["ref"] if isinstance(item, dict) else item
            for item in manifest.get("deferred", [])
        ],
        "evidence_declared": list(manifest.get("evidence_declared", [])),
    }


def plan_digest(manifest: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(plan_subset(manifest))).hexdigest()


def plan_fingerprint(manifest: dict[str, Any]) -> str:
    """12 hex, kept only as a diff id / compatibility field (I-3)."""
    return plan_digest(manifest).removeprefix("sha256:")[:12]


def manifest_sha256(manifest_bytes: bytes) -> str:
    return "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()


# --------------------------------------------------------------------------
# package digest (IDENTITIES §2.6)
# --------------------------------------------------------------------------

def package_digest(root: Path, magic: bytes = PACKAGE_MAGIC) -> str:
    """Hash a target or engine package directory by its actual bytes.

    Deliberately not derived from git status: ``assume-unchanged`` and
    ``showUntrackedFiles`` can both make a dirty tree look clean, and this digest
    is what says which code actually ran.
    """
    root = Path(root)
    root_b = os.fsencode(root.resolve())
    rejections = Rejections()
    entries: dict[bytes, bytes] = {}

    for dirpath, dirnames, filenames in os.walk(os.fsencode(root), followlinks=False):
        dirnames[:] = [d for d in dirnames if d != b".git"]
        if b"__pycache__" in dirnames:
            rel = os.path.relpath(os.path.join(dirpath, b"__pycache__"), os.fsencode(root))
            rejections.add("unsupported_entry", rel, "__pycache__ in package")
            dirnames.remove(b"__pycache__")
        for name in filenames:
            full = Path(os.fsdecode(os.path.join(dirpath, name)))
            rel = os.path.relpath(os.fsencode(full), os.fsencode(root))
            if name.endswith(b".pyc"):
                rejections.add("unsupported_entry", rel, "compiled bytecode in package")
                continue
            st = os.lstat(full)
            if stat.S_ISLNK(st.st_mode):
                resolved = os.path.realpath(full)
                if not os.fsencode(resolved).startswith(root_b + b"/"):
                    rejections.add("unsupported_entry", rel, "symlink escapes package")
                    continue
                link = os.readlink(full).encode("utf-8", "surrogateescape")
                entries[rel] = KIND_SYMLINK + hashlib.sha256(link).digest()
                continue
            if not stat.S_ISREG(st.st_mode):
                rejections.add("unsupported_entry", rel)
                continue
            digest = hashlib.sha256(full.read_bytes()).digest()
            entries[rel] = (KIND_EXEC if st.st_mode & 0o111 else KIND_FILE) + digest

    rejections.raise_first()
    return "sha256:" + hashlib.sha256(magic + _encode_body(entries)).hexdigest()


# --------------------------------------------------------------------------
# contract / environment / bundle (IDENTITIES §2.4-§2.6)
# --------------------------------------------------------------------------

def project_secret_refs(env: dict[str, Any] | None) -> dict[str, Any] | None:
    """Keep only ``NAME`` and ``source`` kind from ``secret_refs`` (joint-r2 J5).

    ``service`` / ``account`` say *where* to fetch the value at runtime.  They
    are deliberately outside every identity so that rotating a credential - or
    moving it to a different Keychain entry holding the same value - does not
    invalidate an existing acceptance.
    """
    if env is None:
        return None
    projected = dict(env)
    refs = projected.get("secret_refs")
    if isinstance(refs, dict):
        projected["secret_refs"] = {
            name: {"source": spec.get("source") if isinstance(spec, dict) else spec}
            for name, spec in sorted(refs.items())
        }
    return projected


def contract_projection(contract: dict[str, Any]) -> dict[str, Any]:
    """The projection canonical_json is taken over for contract_hash."""
    projected = dict(contract)
    policy = projected.get("execution_policy")
    if isinstance(policy, dict):
        roles = {}
        for role, spec in policy.items():
            if isinstance(spec, dict) and "env" in spec:
                spec = dict(spec)
                spec["env"] = project_secret_refs(spec["env"])
            roles[role] = spec
        projected["execution_policy"] = roles
    return projected


def contract_hash(contract: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(contract_projection(contract))).hexdigest()


def environment_digest(environment: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(environment)).hexdigest()


def bundle_view(
    *,
    contract_hash_value: str,
    candidate_output: str,
    review_round: int | None,
    prior_review_sha256: str | None,
    observations: Sequence[dict[str, Any]],
    history_snapshot_sha256: str,
    checkpoint_sha256: str | None,
) -> dict[str, Any]:
    return {
        "contract_hash": contract_hash_value,
        "candidate_output": candidate_output,
        "review_round": review_round,
        "prior_review_sha256": prior_review_sha256,
        "observations": sorted(observations, key=lambda o: o["id"]),
        "history_snapshot_sha256": history_snapshot_sha256,
        "checkpoint_sha256": checkpoint_sha256,
    }


def bundle_hash(view: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(view)).hexdigest()
