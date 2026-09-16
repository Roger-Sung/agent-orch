"""Role permission smoke (IMPLEMENTATION-PLAN §3.1).

Run once per target-package or CLI version change; the sealed report's hash
goes into the contract, so a permission boundary that quietly changed shows up
as a contract change rather than as a surprise mid-run.

Every assertion here is deliberately a *listed* path rather than a universal
claim.  `extra` only widens the existing L1 allowlist, so "no write outside the
worktree succeeds" is not something this can establish; what it can establish is
that each named path is refused, and that is what the report says (joint-r1).
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Sequence

from ..profile import canonical_json

RELEASED = "pass"
REFUSED = "fail"


def _attempt_write(path: Path) -> bool:
    """True when the write succeeded - which for most probes is the failure."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("smoke\n", encoding="utf-8")
    except OSError:
        return False
    try:
        path.unlink()
    except OSError:
        pass
    return True


def write_probes(role: str, forbidden: Sequence[Path]) -> list[dict[str, Any]]:
    """(a)(b)(c): each forbidden path, named individually."""
    results = []
    for path in forbidden:
        succeeded = _attempt_write(Path(path))
        results.append({
            "probe": "write",
            "role": role,
            "path": str(path),
            "expected": "refused",
            "observed": "written" if succeeded else "refused",
            "status": REFUSED if succeeded else RELEASED,
        })
    return results


def network_probe(role: str, *, connect: Callable[[str, int], bool] | None = None) -> dict[str, Any]:
    """(d): probe against a listener we start ourselves.

    Connecting to a closed port proves nothing - the refusal and the absence of
    a listener look identical. So a real listener is started and the question
    becomes whether the sandbox blocks a connection that would otherwise work.
    """
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    host, port = server.getsockname()
    try:
        if connect is not None:
            reachable = connect(host, port)
        else:
            with socket.create_connection((host, port), timeout=2):
                reachable = True
    except OSError:
        reachable = False
    finally:
        server.close()
    return {
        "probe": "network",
        "role": role,
        "target": f"{host}:{port}",
        "listener_started": True,
        "expected": "blocked",
        "observed": "reachable" if reachable else "blocked",
        # Recorded, not asserted: the sandbox policy is what it is, and the
        # report is the evidence of what it actually did.
        "status": RELEASED if not reachable else REFUSED,
    }


def feature_probe(binary: str, *, run: Callable[[list[str]], subprocess.CompletedProcess] | None = None
                  ) -> dict[str, Any]:
    """(e): snapshot the provider's feature list so `--disable` can be checked."""
    runner = run or (lambda argv: subprocess.run(argv, capture_output=True, text=True))
    try:
        completed = runner([binary, "features", "list"])
        output = completed.stdout.strip()
        ok = completed.returncode == 0
    except OSError as exc:
        output, ok = str(exc), False
    return {
        "probe": "features",
        "binary": binary,
        "observed": output[:2000],
        "status": RELEASED if ok else REFUSED,
    }


def build_report(*, target_id: str, target_version: str, cli_versions: dict[str, str],
                 probes: Sequence[dict[str, Any]]) -> dict[str, Any]:
    failures = [p for p in probes if p["status"] != RELEASED]
    return {
        "smoke_version": 1,
        "target_id": target_id,
        "target_package_version": target_version,
        "cli_versions": dict(cli_versions),
        "probes": list(probes),
        "passed": not failures,
        "failed_probes": [p.get("probe") for p in failures],
    }


def seal(report: dict[str, Any], path: Path) -> str:
    """Write the report and return its hash - the `smoke_ref` for the contract."""
    import hashlib

    body = canonical_json(report)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return "sha256:" + hashlib.sha256(body).hexdigest()
