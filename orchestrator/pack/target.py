"""Loading a target package and calling its four CLIs (IMPLEMENTATION-PLAN §4).

This is the entire surface between the engine and a target.  The engine knows
four command shapes, a `profile.yaml` and a `VERSION`; it knows nothing about
what the target builds, tests or deploys, which is what lets a target be
removed as a directory (D-2026-09-15-01).

Every call goes through `_run_json`, so the same three rules hold everywhere:
stdout is JSON only, a non-zero exit is a transport failure rather than a
result, and unparseable stdout is refused instead of guessed at.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

from .bootstrap import launch_argv
from .errors import ContractRejected, PackError

REQUIRED_CLIS = ("validate_plan.py", "requirement_fingerprint.py", "env_probe.py", "verify.py")
REQUIRED_PROFILE_KEYS = {"version", "target_id", "checks"}


class TargetError(PackError):
    """The target package cannot be loaded or its CLI failed in transport."""


def parse_profile_yaml(text: str) -> dict[str, Any]:
    """Parse the block-YAML subset a target profile needs.

    The engine's own profile parser deliberately rejects sequences, and target
    profiles are made of them (argv templates, glob lists, tool lists).  Rather
    than widen that parser - and with it every legacy profile it validates -
    this reads the small subset: nested mappings, block sequences of scalars,
    and quoted or bare scalars.  Anything outside the subset is an error, not a
    best guess.
    """
    root: dict[str, Any] = {}
    # Each stack entry is (indent, container); a container is a dict or a list.
    stack: list[tuple[int, Any]] = [(-1, root)]

    def scalar(raw: str) -> Any:
        value = raw.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            return value[1:-1]
        if value in {"true", "false"}:
            return value == "true"
        if value.lstrip("-").isdigit():
            return int(value)
        return value

    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        content = line.strip()

        while stack and indent <= stack[-1][0]:
            stack.pop()
        if not stack:
            raise TargetError(f"line {number}: inconsistent indentation")
        container = stack[-1][1]

        if content.startswith("- "):
            # A `_Pending` container becomes a sequence on its first item.
            if not hasattr(container, "append"):
                raise TargetError(f"line {number}: sequence item outside a sequence")
            item = content[2:]
            key_part, sep, value_part = item.partition(":")
            if sep and not key_part.strip().startswith(("'", '"')):
                # `- key: value` opens a mapping *inside* the sequence; its
                # sibling keys arrive on the following, more-indented lines, so
                # the mapping is pushed as the current container.
                mapping: dict[str, Any] = {}
                if value_part.strip():
                    mapping[key_part.strip()] = scalar(value_part)
                container.append(mapping)
                stack.append((indent + 1, mapping))
                continue
            container.append(scalar(item))
            continue

        if ":" not in content:
            raise TargetError(f"line {number}: expected `key: value`")
        key, _, rest = content.partition(":")
        key, rest = key.strip(), rest.strip()
        if isinstance(container, list):
            raise TargetError(f"line {number}: mapping key inside a sequence")

        if rest == "[]":
            container[key] = []
        elif rest:
            container[key] = scalar(rest)
        else:
            # An empty value opens either a mapping or a sequence; which one is
            # decided by the first child line.  The pending object itself goes
            # into the parent, so that when its kind is settled the parent sees
            # the resolved container rather than the empty mapping it started as.
            pending = _Pending(container, key, {})
            container[key] = pending
            stack.append((indent, pending))
    _resolve_pending(root)
    return root


class _Pending:
    """A container whose kind is not known until its first child line."""

    def __init__(self, parent: Any, key: Any, mapping: dict[str, Any]) -> None:
        self.parent = parent
        self.key = key
        self.mapping = mapping
        self.sequence: list[Any] = []
        self.kind: str | None = None

    def append(self, value: Any) -> None:
        self.kind = "sequence"
        self.sequence.append(value)

    def __setitem__(self, key: str, value: Any) -> None:
        self.kind = "mapping"
        self.mapping[key] = value

    def __contains__(self, key: str) -> bool:
        return key in self.mapping

    def resolve(self) -> Any:
        return self.sequence if self.kind == "sequence" else self.mapping


def _resolve_pending(container: Any) -> None:
    items = container.items() if isinstance(container, dict) else enumerate(container)
    for key, value in list(items):
        if isinstance(value, _Pending):
            resolved = value.resolve()
            container[key] = resolved
            _resolve_pending(resolved)
        elif isinstance(value, (dict, list)):
            _resolve_pending(value)


class TargetPackage:
    """A loaded target package: version, profile, and the four CLIs."""

    def __init__(self, root: Path, *, python: str | None = None,
                 pycache_prefix: Path | None = None) -> None:
        self.root = Path(root)
        self.python = python or sys.executable
        # Bytecode must land outside the package: a stray `.pyc` would change
        # the package digest, which is an identity input (IDENTITIES §2.6).
        self._pycache = Path(pycache_prefix) if pycache_prefix else None
        self.version = self._load_version()
        self.profile = self._load_profile()

    # -- loading --------------------------------------------------------

    def _load_version(self) -> str:
        path = self.root / "VERSION"
        if not path.is_file():
            # Refused rather than defaulted: the version is half of
            # `generated_by`, which EM-012 checks against the frozen package.
            raise TargetError(f"target package {self.root} has no VERSION file")
        version = path.read_text(encoding="utf-8").strip()
        if not version:
            raise TargetError(f"target package {self.root} has an empty VERSION")
        return version

    def _load_profile(self) -> dict[str, Any]:
        path = self.root / "profile.yaml"
        if not path.is_file():
            raise TargetError(f"target package {self.root} has no profile.yaml")
        try:
            profile = parse_profile_yaml(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise TargetError(f"profile.yaml is unreadable: {exc}") from exc
        missing = REQUIRED_PROFILE_KEYS - set(profile)
        if missing:
            raise TargetError(f"profile.yaml is missing {sorted(missing)}")
        for name in REQUIRED_CLIS:
            if not (self.root / "cli" / name).is_file():
                raise TargetError(f"target package is missing cli/{name}")
        return profile

    @property
    def target_id(self) -> str:
        return str(self.profile["target_id"])

    @property
    def validator_version(self) -> str:
        """The *validator's* version, which EM-012 compares `generated_by` against.

        Distinct from the package version on purpose: a vendored validator has
        its own release line, and a target can re-package it without the
        validator changing.  Defaults to the package version for a target whose
        validator ships with it.
        """
        declared = self.profile.get("validator_version")
        return str(declared) if declared else self.version

    # -- calling --------------------------------------------------------

    def _launch(self, cli: str, args: Sequence[str]) -> list[str]:
        """Every target CLI starts through the engine bootstrap (IDENTITIES §2.6)."""
        import tempfile

        prefix = self._pycache or Path(tempfile.gettempdir()) / "orch-pack-pycache"
        prefix.mkdir(parents=True, exist_ok=True)
        bootstrap = Path(__file__).resolve().parent / "bootstrap.py"
        return launch_argv(self.python, str(bootstrap), str(self.root.resolve()),
                           f"cli/{cli}", str(prefix), tuple(args))

    def _run_json(self, cli: str, args: Sequence[str], *, expect_stdout: bool = True) -> Any:
        import os

        argv = self._launch(cli, args)
        prefix = argv[argv.index("-X") + 1].split("=", 1)[1]
        env = dict(os.environ)
        # The child self-checks against this, so a silently weakened launch is
        # reported rather than run under weaker isolation.
        env["ORCH_EXPECTED_PYCACHE_PREFIX"] = prefix
        completed = subprocess.run(
            argv, capture_output=True, text=True, cwd=str(self.root.resolve()), env=env,
        )
        if completed.returncode != 0:
            raise TargetError(
                f"{cli} exited {completed.returncode}: {completed.stderr.strip()}"
            )
        if not expect_stdout:
            return None
        try:
            return json.loads(completed.stdout)
        except ValueError as exc:
            # stdout is the contract; a CLI that prints prose there has broken
            # it, and parsing "as much as we can" would invent a result.
            raise TargetError(f"{cli} did not print JSON on stdout: {exc}") from exc

    def validate_plan(self, change_dir: Path, *, previous_tasks: Path | None = None,
                      write_manifest: bool = False, contract_version: int = 1) -> dict[str, Any]:
        # Paths are resolved before crossing the boundary: the CLI runs with
        # the package root as its cwd, so a caller's relative path would mean
        # something different on each side.
        args = ["--change-dir", str(Path(change_dir).resolve()), "--json",
                "--contract-version", str(contract_version)]
        if previous_tasks is not None:
            args += ["--previous-tasks", str(Path(previous_tasks).resolve())]
        if write_manifest:
            args.append("--write-manifest")
        return self._run_json("validate_plan.py", args)

    def requirement_fingerprint(self, change_dir: Path) -> dict[str, Any]:
        return self._run_json("requirement_fingerprint.py",
                              ["--change-dir", str(Path(change_dir).resolve())])

    def env_probe(self, tools: Sequence[str], *, argv: Sequence[str] = (),
                  env: dict[str, str] | None = None,
                  workspace: Path | None = None) -> dict[str, str]:
        """Probe tool versions; some tools only have one inside the workspace.

        A wrapper-based build tool reports the version the *project* pins, not
        whatever is on PATH, so the workspace is part of the question.
        """
        args = [
            "--tools", json.dumps(list(tools)),
            "--argv", json.dumps(list(argv)),
            "--env", json.dumps(env or {}),
        ]
        if workspace is not None:
            args += ["--workspace", str(Path(workspace).resolve())]
        versions = self._run_json("env_probe.py", args)
        # EV-V-003 compares the key set exactly, so a probe that answers about
        # different tools than it was asked about is a failure here, not later.
        if set(versions) != set(tools):
            raise TargetError(
                f"env-probe returned {sorted(versions)}, expected {sorted(tools)}"
            )
        return versions

    def verify(self, plan_path: Path, out_path: Path) -> None:
        self._run_json("verify.py",
                       ["--plan", str(Path(plan_path).resolve()),
                        "--out", str(Path(out_path).resolve())],
                       expect_stdout=False)

    def obligation_check(self) -> dict[str, Any]:
        """The template every obligation-derived check runs through.

        Declared once by the target because the selector is the only thing that
        varies between obligations; the flags that make a run real - no build
        cache, force re-execution - belong to the target and must not be
        re-invented per obligation.
        """
        declared = self.profile.get("obligation_check")
        if not isinstance(declared, dict):
            raise ContractRejected("no_obligation_check",
                                   f"{self.target_id} declares no obligation_check template")
        return {"check_id": "obligation_selector", **declared}

    def check(self, check_id: str) -> dict[str, Any]:
        checks = self.profile.get("checks") or {}
        if check_id not in checks:
            raise ContractRejected("unknown_check", check_id)
        declared = dict(checks[check_id])
        declared["check_id"] = check_id
        return declared


def load_target(path: Path, *, python: str | None = None) -> TargetPackage:
    return TargetPackage(Path(path), python=python)
