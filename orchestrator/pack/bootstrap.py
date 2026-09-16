"""Deterministic Python entry point for target and engine CLIs (IDENTITIES §2.6).

Run as:

    python3 -I -S -B -X pycache_prefix=<fresh> bootstrap.py --root <pkg> -- <entry> [args]

Why each flag is there, since together they are the whole point:

* ``-I`` isolates the interpreter - but it also drops the script's own directory
  from ``sys.path``, which is why an explicit ``--root`` exists at all.
* ``-S`` skips ``site``, so nothing from site-packages or a stray ``.pth`` file
  can join the import closure.
* ``-B`` plus a fresh ``pycache_prefix`` keeps compiled bytecode out of the
  package, whose digest is an identity input; a stray ``.pyc`` would make the
  same source hash differently.

The meta-path finder then refuses any non-stdlib module that resolves outside
``--root``.  Its limits are stated rather than papered over: code inside the
package can still manipulate ``sys.path`` or use an absolute loader, so this
bounds *accidental* leakage, not a hostile package (G-11).
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import sys
import sysconfig
from types import ModuleType

SELF_CHECK_FLAGS = ("isolated", "no_site", "dont_write_bytecode")


class RootOnlyFinder:
    """Rejects imports whose origin escapes the package root."""

    def __init__(self, root: str, stdlib_paths: tuple[str, ...]) -> None:
        self.root = os.path.realpath(root)
        self.stdlib_paths = tuple(os.path.realpath(p) for p in stdlib_paths if p)

    def _allowed(self, origin: str | None) -> bool:
        if origin in (None, "built-in", "frozen"):
            return True
        real = os.path.realpath(origin)
        if real.startswith(self.root + os.sep) or real == self.root:
            return True
        return any(real.startswith(p + os.sep) for p in self.stdlib_paths)

    def find_spec(self, fullname: str, path=None, target=None):
        # Returning None means "I do not claim this module"; the real finders
        # run next.  The check happens on their result, in `verify_spec`.
        return None

    def verify_spec(self, spec) -> None:
        if spec is not None and not self._allowed(getattr(spec, "origin", None)):
            raise ImportError(
                f"module {spec.name!r} resolves to {spec.origin!r}, outside the package root"
            )


class GuardedPathFinder(importlib.machinery.PathFinder):
    """PathFinder that validates every spec it produces."""

    guard: RootOnlyFinder | None = None

    @classmethod
    def find_spec(cls, fullname, path=None, target=None):
        spec = super().find_spec(fullname, path, target)
        if cls.guard is not None:
            cls.guard.verify_spec(spec)
        return spec


def self_check(pycache_prefix: str | None) -> list[str]:
    """Report which interpreter guarantees are missing.

    Reported rather than assumed: the child cannot fix its own flags, so the
    receipt records `loader_policy_violation` and the observation is marked
    unusable instead of the run quietly proceeding under weaker isolation.
    """
    problems = []
    for flag in SELF_CHECK_FLAGS:
        if not getattr(sys.flags, flag, 0):
            problems.append(f"sys.flags.{flag} is not set")
    if pycache_prefix is not None and sys.pycache_prefix != pycache_prefix:
        problems.append(
            f"sys.pycache_prefix is {sys.pycache_prefix!r}, expected {pycache_prefix!r}"
        )
    return problems


def stdlib_paths() -> tuple[str, ...]:
    """The stdlib directories, including the one holding C extension modules.

    `lib-dynload` is easy to forget and its absence is not subtle: `subprocess`
    and friends import `_posixsubprocess` from there, so leaving it out breaks
    the stdlib rather than tightening isolation.
    """
    paths = sysconfig.get_paths()
    found = [paths.get("stdlib"), paths.get("platstdlib")]
    for base in list(found):
        if base:
            dynload = os.path.join(base, "lib-dynload")
            if os.path.isdir(dynload):
                found.append(dynload)
    return tuple(dict.fromkeys(p for p in found if p))


def install(root: str) -> RootOnlyFinder:
    """Pin sys.path to the package root plus the stdlib, and guard imports."""
    stdlib = stdlib_paths()
    guard = RootOnlyFinder(root, stdlib)
    sys.path = [os.path.realpath(root)] + list(stdlib)
    GuardedPathFinder.guard = guard
    sys.meta_path = [
        finder for finder in sys.meta_path if finder is not importlib.machinery.PathFinder
    ] + [GuardedPathFinder]
    return guard


def run_entry(root: str, entry: str, argv: list[str]) -> int:
    install(root)
    module = ModuleType("__main__")
    module.__file__ = os.path.join(root, entry)
    sys.argv = [module.__file__, *argv]
    with open(module.__file__, "rb") as handle:
        code = compile(handle.read(), module.__file__, "exec")
    exec(code, module.__dict__)
    return 0


def main(argv: list[str]) -> int:
    if "--root" not in argv or "--" not in argv:
        print("usage: bootstrap.py --root <dir> -- <entry> [args]", file=sys.stderr)
        return 2
    root = argv[argv.index("--root") + 1]
    rest = argv[argv.index("--") + 1:]
    if not rest:
        print("bootstrap: no entry given", file=sys.stderr)
        return 2

    problems = self_check(os.environ.get("ORCH_EXPECTED_PYCACHE_PREFIX"))
    if problems:
        print("loader_policy_violation: " + "; ".join(problems), file=sys.stderr)
        return 3

    return run_entry(root, rest[0], rest[1:])


if __name__ == "__main__":  # pragma: no cover - process entry
    raise SystemExit(main(sys.argv[1:]))


def launch_argv(python: str, bootstrap_path: str, root: str, entry: str,
                pycache_prefix: str, args: tuple[str, ...] = ()) -> list[str]:
    """The exact argv the engine spawns (IDENTITIES §2.6).

    ``-X pycache_prefix`` is used rather than ``PYTHONPYCACHEPREFIX`` because
    ``-I`` ignores the environment variable but honours the ``-X`` option.
    """
    return [
        python, "-I", "-S", "-B", "-X", f"pycache_prefix={pycache_prefix}",
        bootstrap_path, "--root", root, "--", entry, *args,
    ]
