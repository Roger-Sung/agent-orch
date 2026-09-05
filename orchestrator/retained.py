"""Read-only inspection, not recovery or clearance, of interrupted stage output."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping

from .profile import profile_from_snapshot
from .runner import allowed_outcomes, classify_result, extract_envelope


DRIFT_REASONS = frozenset({"protected_root_drift", "workspace_escape"})


def inspect_retained(task: Mapping[str, Any], run: Mapping[str, Any]) -> dict[str, Any]:
    root = Path(task["artifact_dir"]).resolve(strict=True)

    def read(raw: str, expected_hash: str | None = None) -> bytes:
        path = Path(raw)
        if not path.resolve(strict=True).is_relative_to(root) or not path.is_file():
            raise ValueError("retained_evidence_outside_artifacts")
        with path.open("rb") as handle:
            data = handle.read(64 * 1024 * 1024 + 1)
        if len(data) > 64 * 1024 * 1024:
            raise ValueError("retained_evidence_too_large")
        if expected_hash is not None and hashlib.sha256(data).hexdigest() != expected_hash:
            raise ValueError("retained_evidence_hash_mismatch")
        return data

    if not run["sealed"] or not run["manifest_hash"]:
        raise ValueError("retained_run_not_sealed")
    manifest = json.loads(read(run["manifest_path"], run["manifest_hash"]))
    if not isinstance(manifest, dict) or type(manifest.get("schema_version")) is not int:
        raise ValueError("retained_manifest_invalid")
    version = manifest["schema_version"]
    if version not in {1, 2}:
        raise ValueError("retained_manifest_version_unsupported")
    for name, expected in {
        "task_id": task["id"], "run_token": run["run_token"], "stage": run["stage"],
        "lease_token": run["lease_token"], "owner": run["owner"],
        "exit_code": run["exit_code"], "outcome": run["outcome"],
        "log_path": run["log_path"],
    }.items():
        if manifest.get(name) != expected:
            raise ValueError(f"retained_manifest_binding_mismatch:{name}")
    if manifest.get("classification") != "blocked" or manifest.get("reason") not in DRIFT_REASONS:
        raise ValueError("retained_run_not_drift_blocked")
    if type(manifest.get("timed_out")) is not bool:
        raise ValueError("retained_manifest_invalid:timed_out")
    for name in ("log_hash", "output_hash"):
        if not isinstance(manifest.get(name), str) or not re.fullmatch(r"[0-9a-f]{64}", manifest[name]):
            raise ValueError(f"retained_manifest_invalid:{name}")
    log = read(run["log_path"], manifest["log_hash"])
    input_text = read(task["input_snapshot_path"], task["input_hash"]).decode("utf-8", errors="replace")
    read(task["profile_snapshot_path"], task["profile_hash"])
    profile = profile_from_snapshot(Path(task["profile_snapshot_path"]), task["profile_hash"])
    stage = profile.stage(run["stage"])
    drift = None
    if version == 2:
        for name in ("profile_hash", "input_hash"):
            if manifest.get(name) != task[name]:
                raise ValueError(f"retained_manifest_binding_mismatch:{name}")
        output = read(manifest["output_path"], manifest["output_hash"])
        evidence_hash = manifest.get("containment_evidence_hash")
        if not isinstance(evidence_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", evidence_hash):
            raise ValueError("retained_drift_evidence_missing")
        drift = json.loads(read(manifest["containment_evidence_path"], evidence_hash))
        if not isinstance(drift, dict) or drift.get("task_id") != task["id"] or drift.get("log_path") != run["log_path"]:
            raise ValueError("retained_drift_binding_mismatch")
        if drift.get("attribution") != "unknown" or not isinstance(drift.get("violations"), list) or not drift["violations"]:
            raise ValueError("retained_drift_evidence_invalid")
        for item in drift["violations"]:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str) or item.get("kind") not in {"added", "modified", "removed", "unverified"}:
                raise ValueError("retained_drift_evidence_invalid")
    else:
        # Legacy logs are accepted only if reconstruction matches the sealed
        # output hash. The shared old drift file is NOT run-bound evidence.
        candidates = [log]
        _, separator, body = log.partition(b"\n\n--- output ---\n")
        if separator:
            candidates.append(body)
            match = re.search(rb"containment_violation_count=\d+\ncontainment_evidence=[^\n]+\n\Z", body)
            if match:
                candidates.append(body[:match.start()])
        output = next((value for value in candidates if hashlib.sha256(value).hexdigest() == manifest["output_hash"]), None)
        if output is None:
            raise ValueError("retained_legacy_output_unverifiable")
    # Same derivation as the prompt footer and the live classification, from
    # the input snapshot this function already verifies: a drift-blocked
    # envelope run must not report a spurious unknown_outcome here.
    outcomes = set(allowed_outcomes(stage.outcomes, extract_envelope(input_text) is not None))
    candidate = classify_result(manifest["exit_code"], output.decode("utf-8"), outcomes, manifest["timed_out"])
    if version == 2:
        for name in ("outcome", "classification", "reason"):
            if manifest.get(f"candidate_{name}") != getattr(candidate, name):
                raise ValueError(f"retained_candidate_mismatch:{name}")
    return {
        "task_id": task["id"], "task_revision": task["revision"], "run_token": run["run_token"],
        "stage": run["stage"], "manifest_path": run["manifest_path"], "manifest_hash": run["manifest_hash"],
        "schema_version": version, "original_stop_reason": manifest["reason"],
        "integrity": "verified", "candidate_outcome": candidate.outcome,
        "candidate_classification": candidate.classification, "candidate_reason": candidate.reason,
        "containment_attribution": "unknown", "drift_evidence": drift,
        "source_snapshot_verified": False, "authorised_to_advance": False,
        "disposition": "independent_containment_review_required",
        "legacy_limitations": "historical containment and run-bound drift evidence absent" if version == 1 else None,
    }
