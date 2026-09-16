"""verify for the fixture target: run the plan's argv, emit a test envelope.

Two contract obligations are visible here and are the reason this file exists
rather than a stub: the CLI must run the plan's own `expected_argv` (not
re-derive one, and not reuse a previous run's artifacts), and it must report
every JUnit XML it produced - an unreported one is how a failing test
disappears from an otherwise green run (EV-V-015).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import emit, fail  # noqa: E402


def aggregate(artifacts_root: Path) -> dict | None:
    """Sum every JUnit XML, or None when none were produced."""
    files = sorted(artifacts_root.glob("test-results/**/*.xml"))
    if not files:
        return None
    totals = {"total": 0, "passed": 0, "failed": 0, "skipped": 0, "errors": 0}
    for path in files:
        suite = ET.parse(path).getroot()
        tests = int(suite.get("tests", 0))
        failures = int(suite.get("failures", 0))
        errors = int(suite.get("errors", 0))
        skipped = int(suite.get("skipped", 0))
        totals["total"] += tests
        totals["failed"] += failures
        totals["errors"] += errors
        totals["skipped"] += skipped
        totals["passed"] += tests - failures - errors - skipped
    return totals


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)

    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    artifacts_root = Path(plan["artifacts_root"])
    artifacts_root.mkdir(parents=True, exist_ok=True)
    workspace = Path(plan["workspace"])

    env = dict(os.environ)
    env["ORCH_ARTIFACTS_ROOT"] = str(artifacts_root)
    completed = subprocess.run(
        plan["expected_argv"], cwd=workspace, env=env,
        capture_output=True, text=True,
    )

    (artifacts_root / "stdout.log").write_text(completed.stdout, encoding="utf-8")
    (artifacts_root / "stderr.log").write_text(completed.stderr, encoding="utf-8")

    tests = aggregate(artifacts_root)
    outcome = "completed"
    if tests is None:
        status = "SKIP" if completed.returncode == 0 else "ERROR"
    elif tests["total"] == 0:
        status = "SKIP"
    elif tests["failed"] + tests["errors"] > 0:
        status = "FAIL"
    else:
        status = "PASS"

    artifacts = []
    for path in sorted(artifacts_root.rglob("*")):
        if not path.is_file():
            continue
        kind = "junit_xml" if path.suffix == ".xml" else "log"
        artifacts.append({
            "path": str(path.relative_to(artifacts_root)),
            "kind": kind,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        })

    envelope = {
        "verify_plan_id": plan["verify_plan_id"],
        "operation_id": plan["operation_id"],
        "invocation_id": plan["invocation_id"],
        "candidate_fingerprint": plan["candidate_fingerprint"],
        "result_kind": plan["result_kind"],
        "subject": plan["subject"],
        "target_package_digest": plan["target_package_digest"],
        "target_package_version": plan["target_package_version"],
        "tool_versions": plan["tool_versions"],
        "execution": {
            "cwd": plan["workspace"],
            "argv": plan["expected_argv"],
            "timed_out": False,
            "signal": None,
            "exit_code": completed.returncode,
            "outcome": outcome,
        },
        "result": {"status": status, "selector": plan.get("selector"), "tests": tests},
        "artifacts": artifacts,
        "cleanup": [
            {"resource_id": resource["resource_id"], "status": "not_needed",
             "evidence": "fixture target assigns no resources"}
            for resource in plan.get("assigned_resources", [])
        ],
    }
    Path(args.out).write_text(json.dumps(envelope, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
