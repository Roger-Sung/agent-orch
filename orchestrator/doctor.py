"""`orch doctor`: check a deployment's wiring before a task discovers it.

Every check here answers a question an operator otherwise answers by reading
source or by watching a stage fail: is state going where I think it is, are
both provider CLIs actually reachable from *this* environment, is L1 possible
on this host, is L2 actually on, and do the declared roots contradict each
other. Nothing is mutated; the command is safe to run while the daemon works.

Statuses: ``ok`` (as intended), ``warn`` (legal but worth knowing — L2 off,
`ORCH_HOME` defaulted), ``fail`` (a stage would refuse or misbehave). The exit
code is 1 when anything failed, so the command can gate a provisioning script.
"""

from __future__ import annotations

import fcntl
import os
import sys
from pathlib import Path
from typing import Any

from .config import ConfigFileError, config_path
from .containment import (
    ContainmentConfigError,
    extra_write_roots_from_env,
    protected_roots_from_env,
    sandbox_available,
    validate_extra_write_roots,
    validate_home_outside_protected,
)
from .risk_rules import load_risk_rules
from .runner import (
    ALLOW_UNATTENDED_ENV,
    ALLOW_UNSANDBOXED_ENV,
    SubprocessRunner,
    unattended_flags_in_use,
)


def _check(name: str, status: str, detail: str) -> dict[str, str]:
    return {"check": name, "status": status, "detail": detail}


def _daemon_check(home: Path) -> dict[str, str]:
    """Whether a daemon holds the service lock — without creating anything.

    `ipc.daemon_is_running` creates the home directory and the lock file as a
    side effect, which is fine for enqueueing clients and wrong for a command
    that advertises itself as read-only. This probe only opens what already
    exists.
    """
    lock_path = home / "service.lock"
    if not lock_path.is_file():
        return _check("daemon", "warn", "no daemon has run against this home (no service lock present)")
    try:
        with lock_path.open("r", encoding="utf-8") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return _check("daemon", "ok", "daemon holds the service lock")
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            return _check("daemon", "warn", "no daemon is running against this home")
    except OSError as exc:
        return _check("daemon", "warn", f"cannot probe the service lock: {exc}")


def run_doctor(home: Path) -> dict[str, Any]:
    checks: list[dict[str, str]] = []

    # Config file: present and loadable, or explicitly absent.
    try:
        path = config_path()
        if path is None:
            checks.append(_check("config_file", "ok", "no config file; environment variables only"))
        else:
            checks.append(_check("config_file", "ok", f"loaded {path}"))
    except ConfigFileError as exc:
        checks.append(_check("config_file", "fail", str(exc)))

    # ORCH_HOME: the silent-fallback trap. Defaulting works, but state lands
    # under the repository instead of where the daemon is probably looking.
    if os.environ.get("ORCH_HOME"):
        checks.append(_check("orch_home", "ok", f"ORCH_HOME set; state at {home}"))
    else:
        checks.append(
            _check(
                "orch_home",
                "warn",
                f"ORCH_HOME is not set; defaulting to {home}. A CLI and a daemon "
                "that resolve this differently operate on different states.",
            )
        )

    checks.append(_daemon_check(home))

    # Provider CLIs: resolved and answering --version in *this* environment,
    # which is the environment that matters and often not the shell's. A
    # malformed command (broken shell quoting) must land as a failed check,
    # not abort the whole report.
    runner = SubprocessRunner()
    for owner in ("claude", "codex"):
        try:
            result = runner.preflight(owner)
        except ValueError as exc:
            checks.append(_check(f"provider_{owner}", "fail", f"provider command is malformed: {exc}"))
            continue
        status = "ok" if result.status == "pass" else "fail"
        command = " ".join(result.command)
        checks.append(_check(f"provider_{owner}", status, f"{result.reason}: {command}"))

    # Unattended acknowledgement: only meaningful when a known flag is in use.
    hits: list | None
    try:
        hits = unattended_flags_in_use()
    except ValueError as exc:
        hits = None
        checks.append(_check("unattended_consent", "fail", f"provider command is malformed: {exc}"))
    if hits:
        acknowledged = os.environ.get(ALLOW_UNATTENDED_ENV, "").strip() == "1"
        detail = ", ".join(f"{variable} carries {flag}" for variable, flag in hits)
        checks.append(
            _check(
                "unattended_consent",
                "ok" if acknowledged else "fail",
                f"{detail}; {ALLOW_UNATTENDED_ENV}={'1' if acknowledged else 'unset — the daemon will refuse to start'}",
            )
        )
    elif hits is not None:
        checks.append(
            _check(
                "unattended_consent",
                "ok",
                "no known approval-disabling flag in the configured commands "
                "(detection is by known flag names only)",
            )
        )

    # L1: available, or explicitly waived.
    if sandbox_available():
        checks.append(_check("l1_sandbox", "ok", "sandbox-exec is available"))
    elif os.environ.get(ALLOW_UNSANDBOXED_ENV, "").strip().lower() in {"1", "true", "yes", "on"}:
        checks.append(_check("l1_sandbox", "warn", "sandbox unavailable and explicitly waived; mutating stages run unconfined"))
    else:
        checks.append(_check("l1_sandbox", "fail", "sandbox-exec unavailable; mutating stages will refuse to run"))

    # L2: off is legal, but it should never be off by accident — and a declared
    # root that does not exist is worse than none, because the sentinel skips
    # missing roots silently while the operator believes they are watched.
    protected = protected_roots_from_env()
    if protected:
        unusable: list[str] = []
        for root in protected:
            if not root.exists():
                unusable.append(f"{root} does not exist")
            elif not root.is_dir():
                unusable.append(f"{root} is not a directory")
            elif not os.access(root, os.R_OK | os.X_OK):
                unusable.append(f"{root} is not readable")
        if unusable:
            checks.append(
                _check(
                    "l2_protected_roots",
                    "fail",
                    "declared but unwatchable — the sentinel silently skips these: " + "; ".join(unusable),
                )
            )
        else:
            checks.append(_check("l2_protected_roots", "ok", f"{len(protected)} root(s) declared and readable"))
        try:
            validate_home_outside_protected(home, protected)
            checks.append(_check("home_outside_protected", "ok", "ORCH_HOME does not overlap a protected root"))
        except ContainmentConfigError as exc:
            checks.append(_check("home_outside_protected", "fail", str(exc)))
    else:
        checks.append(
            _check(
                "l2_protected_roots",
                "warn",
                "ORCH_PROTECTED_ROOTS is empty: L2 detection is OFF and this deployment has L1 only",
            )
        )

    # Declared write roots must not contradict the watched roots.
    extra = extra_write_roots_from_env()
    try:
        validate_extra_write_roots(extra, protected)
        checks.append(
            _check(
                "write_root_overlap",
                "ok",
                f"{len(extra)} extra write root(s), none overlapping a protected root",
            )
        )
    except ContainmentConfigError as exc:
        checks.append(_check("write_root_overlap", "fail", str(exc)))

    # Intake configuration loads, or says why not.
    try:
        rules = load_risk_rules()
        checks.append(_check("risk_rules", "ok", rules.describe() if hasattr(rules, "describe") else "risk rules loaded"))
    except Exception as exc:  # a malformed rules file must surface, whatever its type
        checks.append(_check("risk_rules", "fail", f"{type(exc).__name__}: {exc}"))

    from .start import profiles_dir  # late import: start pulls in intake vocabulary

    try:
        directory = profiles_dir()
        checks.append(_check("profiles_dir", "ok", str(directory)))
    except ValueError as exc:
        checks.append(_check("profiles_dir", "fail", str(exc)))

    failed = [item for item in checks if item["status"] == "fail"]
    return {
        "home": str(home),
        "python": sys.version.split()[0],
        "checks": checks,
        "summary": {
            "ok": sum(1 for item in checks if item["status"] == "ok"),
            "warn": sum(1 for item in checks if item["status"] == "warn"),
            "fail": len(failed),
        },
    }
