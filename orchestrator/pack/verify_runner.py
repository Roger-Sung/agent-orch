"""Running one verify invocation and deciding whether it is usable (ENVELOPES §3).

The engine builds the plan, runs the target CLI, and then judges the result in
a fixed order: transport, schema, cleanup, and only then the reported status.
The order is what stops a plausible-looking envelope from carrying a PASS that
nothing actually established - a crashed CLI, a leaked schema and a green
`status` field can all coexist, and each of the first two disqualifies the run
before its own claim is ever read.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any, Callable, Sequence

from ..profile import canonical_json
from .envelopes import validate_verify, verify_usable
from .errors import EnvelopeInvalid
from .resources import cleanup_gate


def invocation_id(check_id: str, params: dict[str, Any]) -> str:
    """`check_id@sha256(canonical_json(params))[:16]` (IDENTITIES §2.4)."""
    digest = hashlib.sha256(canonical_json(params)).hexdigest()[:16]
    return f"{check_id}@{digest}"


def expand_argv(template: Sequence[str], params: dict[str, Any]) -> list[str]:
    """Substitute `{name}` placeholders; an unknown placeholder is an error.

    Leaving an unresolved placeholder in the argv would run a command nobody
    wrote, so this refuses rather than passing the literal through.
    """
    import shlex

    out: list[str] = []
    for item in template:
        stripped = item.strip()
        if (stripped.startswith("{") and stripped.endswith("}")
                and stripped[1:-1] in params):
            # A whole item that is just a placeholder may carry a command
            # *fragment* - an obligation selector such as
            # ":module:test --tests SomeTest" is several arguments, and passing
            # it as one would hand the tool a single nonsense argument.
            out.extend(shlex.split(str(params[stripped[1:-1]])))
            continue
        rendered = item
        for key, value in params.items():
            rendered = rendered.replace("{" + key + "}", str(value))
        if "{" in rendered and "}" in rendered:
            raise EnvelopeInvalid("EV-V-004", f"unresolved placeholder in argv item {item!r}")
        out.append(rendered)
    return out


def build_plan(*, pack_id: str, change: str, target_id: str, contract_version: int,
               operation_id: str, attempt_id: str, candidate_fingerprint: str,
               check: dict[str, Any], params: dict[str, Any], subject: dict[str, Any],
               workspace: Path, artifacts_root: Path,
               target_package_digest: str, target_package_version: str,
               tool_versions: dict[str, str],
               assigned_resources: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Assemble the verify plan the CLI is handed."""
    inv = invocation_id(check["check_id"], params)
    return {
        "verify_plan_id": f"VP-{operation_id}",
        "target_id": target_id,
        "change": change,
        "pack": pack_id,
        "contract_version": contract_version,
        "work_attempt_id": attempt_id,
        "operation_id": operation_id,
        "invocation_id": inv,
        "candidate_fingerprint": candidate_fingerprint,
        "subject": subject,
        "result_kind": check["result_kind"],
        "check_id": check["check_id"],
        "selector": check.get("selector"),
        "params": params,
        "target_package_digest": target_package_digest,
        "target_package_version": target_package_version,
        "tool_versions": tool_versions,
        "expected_argv": expand_argv(check["argv_template"], params),
        "artifacts_root": str(artifacts_root),
        "workspace": str(workspace),
        "assigned_resources": [
            {k: v for k, v in resource.items() if k != "name"} | {"name": resource["name"]}
            for resource in assigned_resources
        ],
    }


def prepare_artifacts_root(root: Path) -> bool:
    """Create the artifacts root and report whether it started empty.

    A non-empty root means a previous run's files could be presented as this
    run's evidence, so the emptiness is recorded as transport state rather than
    silently cleaned - the engine must know it happened (V8).
    """
    root.mkdir(parents=True, exist_ok=True)
    return not any(root.iterdir())


def enumerate_artifacts(root: Path) -> list[dict[str, Any]]:
    """Every file under the root, with its hash, for completeness checking."""
    found: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        found.append({
            "path": str(path.relative_to(root)),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        })
    return found


def check_artifact_completeness(root: Path, reported: Sequence[dict[str, Any]]) -> list[str]:
    """EV-V-015: every JUnit XML under the root must be reported.

    Missing XML is invalidating rather than a warning: an unreported result file
    is exactly how a failing test disappears from an otherwise green run.
    """
    on_disk = {
        str(path.relative_to(root))
        for path in root.rglob("*.xml")
        if path.is_file() and "test-results" in path.parts
    }
    listed = {entry["path"] for entry in reported}
    return sorted(on_disk - listed)


class VerifyReceipt:
    """What the engine observed about the CLI process itself."""

    def __init__(self, *, exit_code: int | None, timed_out: bool, signal: int | None,
                 artifacts_root_was_empty: bool, out_file_sha256: str | None) -> None:
        self.exit_code = exit_code
        self.timed_out = timed_out
        self.signal = signal
        self.artifacts_root_was_empty = artifacts_root_was_empty
        self.out_file_sha256 = out_file_sha256

    def transport_ok(self, expected_sha256: str | None = None) -> tuple[bool, list[str]]:
        reasons: list[str] = []
        if self.exit_code != 0:
            reasons.append(f"cli_exit_{self.exit_code}")
        if self.timed_out:
            reasons.append("cli_timed_out")
        if self.signal is not None:
            reasons.append(f"cli_signalled_{self.signal}")
        if not self.artifacts_root_was_empty:
            reasons.append("artifacts_root_not_empty")
        if expected_sha256 is not None and self.out_file_sha256 != expected_sha256:
            reasons.append("out_file_hash_mismatch")
        return (not reasons), reasons

    def to_dict(self) -> dict[str, Any]:
        return {
            "process_exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "signal": self.signal,
            "artifacts_root_was_empty": self.artifacts_root_was_empty,
            "out_file_sha256": self.out_file_sha256,
        }


def decide_usable(*, envelope: dict[str, Any], plan: dict[str, Any], receipt: VerifyReceipt,
                  contract_tool_versions: dict[str, str], contract_package_digest: str,
                  contract_package_version: str, artifacts_root: Path,
                  verified_release: dict[str, str],
                  engine_cleanup: dict[str, str]) -> dict[str, Any]:
    """Run §3.4 in order and return the observation's usability plus its reasons."""
    reasons: list[str] = []

    ok, transport_reasons = receipt.transport_ok()
    if not ok:
        # A CLI that crashed cannot vouch for anything it printed.
        return {"usable": False, "stage": "transport", "reasons": transport_reasons}
    reasons.extend(transport_reasons)

    try:
        validate_verify(
            envelope, plan=plan,
            contract_tool_versions=contract_tool_versions,
            contract_package_digest=contract_package_digest,
            contract_package_version=contract_package_version,
        )
    except EnvelopeInvalid as exc:
        return {"usable": False, "stage": "schema", "reasons": [exc.code]}

    missing = check_artifact_completeness(artifacts_root, envelope.get("artifacts", []))
    if missing:
        return {"usable": False, "stage": "schema",
                "reasons": [f"EV-V-015:unreported:{name}" for name in missing]}

    cleanup_ok, cleanup_reasons = cleanup_gate(
        envelope.get("cleanup", []), verified_release, engine_cleanup
    )
    if not cleanup_ok:
        return {"usable": False, "stage": "cleanup", "reasons": cleanup_reasons}

    if not verify_usable(envelope, candidate_fingerprint=plan["candidate_fingerprint"]):
        return {"usable": False, "stage": "result", "reasons": ["candidate_or_outcome_mismatch"]}

    return {"usable": True, "stage": "result", "reasons": []}


def run_verify(*, plan: dict[str, Any], artifacts_root: Path, out_path: Path,
               spawn: Callable[[dict[str, Any]], dict[str, Any]]) -> tuple[dict[str, Any] | None, VerifyReceipt]:
    """Run one invocation through an injected spawner and read back its envelope.

    The spawner is injected so the fixtures can produce every transport failure
    - kill, timeout, a CLI that writes nothing - without a real provider.
    """
    was_empty = prepare_artifacts_root(artifacts_root)
    observed = spawn(plan)
    envelope = None
    out_sha = None
    if out_path.exists():
        data = out_path.read_bytes()
        out_sha = hashlib.sha256(data).hexdigest()
        try:
            import json

            envelope = json.loads(data)
        except ValueError:
            envelope = None
    receipt = VerifyReceipt(
        exit_code=observed.get("exit_code"),
        timed_out=bool(observed.get("timed_out")),
        signal=observed.get("signal"),
        artifacts_root_was_empty=was_empty,
        out_file_sha256=out_sha,
    )
    return envelope, receipt
