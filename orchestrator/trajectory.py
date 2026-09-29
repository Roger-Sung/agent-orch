"""Strict append-only trajectory-v1 primitives.

This module deliberately does not emit lifecycle events.  It defines the
schema, canonical bytes and transaction-bound store used by the controller in
the next slice.  Keeping emission out of this layer prevents trajectory state
from becoming a second lifecycle authority.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import sqlite3
import unicodedata
import uuid
from pathlib import PurePosixPath
from typing import Any, Mapping


SCHEMA_VERSION = 1
EVENT_VERSION = 1
NORMALIZER_VERSION = "trajectory-v1"
BASELINE_NAMESPACE = uuid.UUID("b6a71f4d-cdb7-5f04-a808-49df5d731c01")

SUPPORTED_EVENT_TYPES = frozenset(
    {
        "task.created",
        "task.transition.committed",
        "stage.claimed",
        "stage.settled",
        "provider.dispatch_intent",
        "provider.dispatched",
        "provider.settled",
        "session.bound",
        "session.rebound",
        "evidence.sealed",
        "migration.baseline",
    }
)
RESERVED_EVENT_TYPES = frozenset(
    {
        "prompt.assembled",
        "context.mounted",
        "tool.requested",
        "tool.dispatch_intent",
        "tool.dispatched",
        "tool.settled",
        "approval.requested",
        "approval.decided",
        "subagent.spawned",
        "subagent.message",
        "subagent.settled",
        "join.decided",
        "artifact.committed",
        "gate.decided",
    }
)

TOP_LEVEL_KEYS = frozenset(
    {
        "schema_version",
        "trajectory_id",
        "seq",
        "event_id",
        "event_type",
        "event_version",
        "recorded_at_ms",
        "task",
        "run",
        "invocation_id",
        "workflow_attempt_ref",
        "session_ref",
        "parent_event_id",
        "causal_event_ids",
        "actor",
        "body",
        "evidence_refs",
        "sensitivity",
        "retention_class",
        "normalizer_version",
        "prev_event_hash",
        "event_hash",
    }
)

TASK_STATUSES = frozenset({"queued", "running", "waiting_user", "blocked", "done", "failed", "paused"})
ACTOR_KINDS = frozenset({"controller", "provider", "tool", "user", "gate", "subagent", "migrator"})
SENSITIVITIES = frozenset({"public", "internal", "sensitive"})
RETENTION_CLASSES = frozenset({"structural", "task-lifecycle", "sealed-evidence", "ephemeral-ref"})
AVAILABILITY = frozenset({"present", "unavailable", "corrupt"})
USAGE_BASES = frozenset({"per-turn", "cumulative", "delta-from-cumulative", "unavailable"})
MISSING_DOMAINS = frozenset({"prompt", "context", "provider", "session", "tool", "approval", "subagent", "join", "gate", "usage", "evidence"})

# These are schema vocabulary, not a reflection of whatever text happens to be
# in a provider result or a profile today. A producer maps an unknown native
# value to ``other`` plus the digest of that value; accepting any syntactically
# valid identifier here would silently turn the fixed v1 vocabulary into a
# provider-controlled extension point.
TRANSITION_REASON_CODES = frozenset(
    {
        "task_created",
        "stage_completed",
        "edge_cap",
        "transition_cap",
        "needs_user_decision",
        "resumed",
        "orphaned_running",
        "provider_preflight_failed",
        "other",
    }
)
OUTCOME_CODES = frozenset(
    {
        "allow",
        "applied",
        "block",
        "blocked",
        "contract_findings",
        "contract_pass",
        "drafted",
        "implemented",
        "needs_correction",
        "needs_repair",
        "needs_revision",
        "needs_simplification",
        "needs_user_decision",
        "prerun_done",
        "produced",
        "ready",
        "repaired",
        "review",
        "reviewed",
        "revised",
        "simplified",
        "validated",
        "other",
    }
)
STAGE_CLASSIFICATIONS = frozenset({"success", "blocked", "paused", "waiting_user", "failed"})
PROVIDER_RESULT_CLASSES = frozenset({"success", "blocked", "paused", "failed", "unknown"})
USAGE_UNAVAILABLE_REASON_CODES = frozenset(
    {
        "provider_cli_usage_not_reported",
        "runner_usage_unavailable",
        "usage_basis_unverified",
        "not_applicable_provider_preflight_failed",
        "other",
    }
)
SESSION_REBOUND_REASON_CODES = frozenset(
    {
        "provider_session_replaced",
        "session_unavailable",
        "session_corrupt",
        "session_expired",
        "resume",
        "other",
    }
)
OWNER_ROLES = frozenset({"executor", "reviewer"})
EVIDENCE_KINDS = frozenset(
    {"run-manifest", "transport-receipt", "session-binding", "checkpoint", "final-response"}
)
SEAL_KINDS = frozenset(
    {"db-committed-run-manifest", "controller-receipt", "session-binding", "checkpoint"}
)
SUPPORTED_EVIDENCE_SEAL_VERSIONS = frozenset({1, 2, 3})
EVIDENCE_SEAL_KINDS = {
    "run-manifest": "db-committed-run-manifest",
    "transport-receipt": "controller-receipt",
    "session-binding": "session-binding",
    "checkpoint": "checkpoint",
    "final-response": "db-committed-run-manifest",
}

IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,127}$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
HEX_DIGEST = re.compile(r"^[0-9a-f]{64}$")
PATH_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")
MEDIA_TYPE = re.compile(r"^[a-z0-9][a-z0-9.+-]*/[a-z0-9][a-z0-9.+-]*$")
DETECTABLE_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:ghp_|github_pat_|xox[baprs]-|sk-(?:proj-)?)[A-Za-z0-9_-]{8,}"),
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
    re.compile(
        r"(?:api[_-]?key|access[_-]?token|auth[_-]?token|token|password|passwd|credential|"
        r"client[_-]?secret|private[_-]?key)[:=][A-Za-z0-9._/-]{8,}",
        re.IGNORECASE,
    ),
)


class TrajectoryError(ValueError):
    """A trajectory event is structurally invalid or cannot be appended."""


def _keys(value: Any, expected: frozenset[str] | set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != set(expected):
        raise TrajectoryError(f"{label} keys mismatch")
    return value


def _integer(value: Any, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise TrajectoryError(f"{label} must be an integer >= {minimum}")
    return value


def _nullable_integer(value: Any, label: str) -> int | None:
    if value is None:
        return None
    return _integer(value, label)


def _boolean(value: Any, label: str) -> bool:
    if type(value) is not bool:
        raise TrajectoryError(f"{label} must be a boolean")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise TrajectoryError(f"{label} must be a non-empty string")
    if unicodedata.normalize("NFC", value) != value:
        raise TrajectoryError(f"{label} must be NFC-normalized")
    return value


def _identifier(value: Any, label: str) -> str:
    value = _text(value, label)
    if IDENTIFIER.fullmatch(value) is None:
        raise TrajectoryError(f"{label} is not a stable identifier")
    return value


def _uuid(value: Any, label: str) -> str:
    value = _text(value, label)
    try:
        parsed = str(uuid.UUID(value))
    except (ValueError, AttributeError) as exc:
        raise TrajectoryError(f"{label} must be a canonical UUID") from exc
    if parsed != value:
        raise TrajectoryError(f"{label} must be a canonical UUID")
    return value


def _nullable_uuid(value: Any, label: str) -> str | None:
    if value is None:
        return None
    return _uuid(value, label)


def _digest(value: Any, label: str) -> str:
    value = _text(value, label)
    if DIGEST.fullmatch(value) is None:
        raise TrajectoryError(f"{label} must be a sha256 digest")
    return value


def _nullable_digest(value: Any, label: str) -> str | None:
    if value is None:
        return None
    return _digest(value, label)


def _ref_id(value: Any, label: str) -> str:
    value = _text(value, label)
    if not value.startswith("evref:"):
        raise TrajectoryError(f"{label} must start with evref:")
    _uuid(value.removeprefix("evref:"), label)
    return value


def _nullable_ref_id(value: Any, label: str) -> str | None:
    if value is None:
        return None
    return _ref_id(value, label)


def _enum(value: Any, allowed: frozenset[str], label: str) -> str:
    value = _text(value, label)
    if value not in allowed:
        raise TrajectoryError(f"unsupported {label}")
    return value


def _enum_with_other_digest(
    value: Any,
    digest: Any,
    allowed: frozenset[str],
    label: str,
    digest_label: str,
) -> str:
    code = _enum(value, allowed, label)
    normalized_digest = _nullable_digest(digest, digest_label)
    if (code == "other") != (normalized_digest is not None):
        raise TrajectoryError(f"{digest_label} is required only for {label}=other")
    return code


def _reject_detectable_secrets(value: Any) -> None:
    """Reject recognizable credentials without reflecting their value.

    The typed schema is the primary control. This recursive pass is a
    defense-in-depth backstop for fields that intentionally accept opaque
    identifiers, especially ``workflow_attempt_ref``.
    """
    if isinstance(value, str):
        if any(pattern.search(value) for pattern in DETECTABLE_SECRET_PATTERNS):
            raise TrajectoryError("trajectory event contains detectable secret material")
        return
    if isinstance(value, list):
        for item in value:
            _reject_detectable_secrets(item)
        return
    if isinstance(value, dict):
        for item in value.values():
            _reject_detectable_secrets(item)


def _nullable_identifier(value: Any, label: str) -> str | None:
    if value is None:
        return None
    return _identifier(value, label)


def _normalize(value: Any) -> Any:
    if value is None or type(value) in {bool, int}:
        return value
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    if isinstance(value, dict):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TrajectoryError("JSON object keys must be strings")
            normalized_key = unicodedata.normalize("NFC", key)
            if normalized_key in normalized:
                raise TrajectoryError("normalization produced a duplicate key")
            normalized[normalized_key] = _normalize(item)
        return normalized
    raise TrajectoryError(f"unsupported JSON value type: {type(value).__name__}")


def _canonical_json(value: Any) -> bytes:
    normalized = _normalize(value)
    return json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _validate_usage(value: Any, label: str) -> None:
    value = _keys(
        value,
        {
            "basis",
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "unavailable_reason_code",
            "unavailable_reason_digest",
        },
        label,
    )
    basis = _enum(value["basis"], USAGE_BASES, f"{label}.basis")
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        _nullable_integer(value[key], f"{label}.{key}")
    reason = value["unavailable_reason_code"]
    reason_digest = value["unavailable_reason_digest"]
    if basis == "unavailable":
        _enum_with_other_digest(
            reason,
            reason_digest,
            USAGE_UNAVAILABLE_REASON_CODES,
            f"{label}.unavailable_reason_code",
            f"{label}.unavailable_reason_digest",
        )
        if any(value[key] is not None for key in ("input_tokens", "output_tokens", "total_tokens")):
            raise TrajectoryError(f"{label} unavailable usage cannot carry token counts")
    elif reason is not None or reason_digest is not None:
        raise TrajectoryError(f"{label} unavailable reason fields must be null when usage is available")


def _validate_evidence_ref(value: Any, label: str) -> str:
    value = _keys(
        value,
        {
            "ref_id",
            "kind",
            "relative_path",
            "sha256",
            "size_bytes",
            "media_type",
            "seal",
            "sensitivity",
            "retention_class",
            "availability_at_append",
        },
        label,
    )
    ref_id = _ref_id(value["ref_id"], f"{label}.ref_id")
    _enum(value["kind"], EVIDENCE_KINDS, f"{label}.kind")
    path = _text(value["relative_path"], f"{label}.relative_path")
    pure = PurePosixPath(path)
    components = path.split("/")
    if (
        pure.is_absolute()
        or not pure.parts
        or any(part in {"", ".", ".."} for part in components)
        or pure.as_posix() != path
    ):
        raise TrajectoryError(f"{label}.relative_path must stay below the task artifact root")
    if any(PATH_SEGMENT.fullmatch(part) is None for part in pure.parts):
        raise TrajectoryError(f"{label}.relative_path contains an unsafe segment")
    digest = _text(value["sha256"], f"{label}.sha256")
    if HEX_DIGEST.fullmatch(digest) is None:
        raise TrajectoryError(f"{label}.sha256 must be lowercase hex")
    _integer(value["size_bytes"], f"{label}.size_bytes")
    media_type = _text(value["media_type"], f"{label}.media_type")
    if MEDIA_TYPE.fullmatch(media_type) is None:
        raise TrajectoryError(f"{label}.media_type is invalid")
    seal = _keys(value["seal"], {"kind", "schema_version", "run_token", "manifest_hash"}, f"{label}.seal")
    seal_kind = _enum(seal["kind"], SEAL_KINDS, f"{label}.seal.kind")
    if seal_kind != EVIDENCE_SEAL_KINDS[value["kind"]]:
        raise TrajectoryError(f"{label}.seal.kind does not match evidence kind")
    seal_version = _integer(seal["schema_version"], f"{label}.seal.schema_version", minimum=1)
    if seal_version not in SUPPORTED_EVIDENCE_SEAL_VERSIONS:
        raise TrajectoryError(f"unsupported {label}.seal.schema_version")
    _identifier(seal["run_token"], f"{label}.seal.run_token")
    manifest_hash = _text(seal["manifest_hash"], f"{label}.seal.manifest_hash")
    if HEX_DIGEST.fullmatch(manifest_hash) is None:
        raise TrajectoryError(f"{label}.seal.manifest_hash must be lowercase hex")
    _enum(value["sensitivity"], SENSITIVITIES, f"{label}.sensitivity")
    _enum(value["retention_class"], RETENTION_CLASSES, f"{label}.retention_class")
    _enum(value["availability_at_append"], AVAILABILITY, f"{label}.availability_at_append")
    return ref_id


def _validate_body(event_type: str, body: Any, refs: Mapping[str, Mapping[str, Any]]) -> None:
    label = f"body[{event_type}]"
    if event_type == "task.created":
        body = _keys(body, {"profile_digest", "input_digest"}, label)
        _digest(body["profile_digest"], f"{label}.profile_digest")
        _digest(body["input_digest"], f"{label}.input_digest")
    elif event_type == "task.transition.committed":
        body = _keys(
            body,
            {"transition_seq", "operation_id", "from_status", "to_status", "reason_code", "reason_digest", "outcome_code", "outcome_digest"},
            label,
        )
        _integer(body["transition_seq"], f"{label}.transition_seq", minimum=1)
        _identifier(body["operation_id"], f"{label}.operation_id")
        if body["from_status"] is not None:
            _enum(body["from_status"], TASK_STATUSES, f"{label}.from_status")
        _enum(body["to_status"], TASK_STATUSES, f"{label}.to_status")
        _enum_with_other_digest(
            body["reason_code"],
            body["reason_digest"],
            TRANSITION_REASON_CODES,
            f"{label}.reason_code",
            f"{label}.reason_digest",
        )
        outcome = body["outcome_code"]
        if outcome is not None:
            _enum_with_other_digest(
                outcome,
                body["outcome_digest"],
                OUTCOME_CODES,
                f"{label}.outcome_code",
                f"{label}.outcome_digest",
            )
        elif body["outcome_digest"] is not None:
            raise TrajectoryError(f"{label}.outcome_digest requires outcome_code")
    elif event_type == "stage.claimed":
        body = _keys(body, {"lease_digest", "owner_role"}, label)
        _digest(body["lease_digest"], f"{label}.lease_digest")
        _enum(body["owner_role"], OWNER_ROLES, f"{label}.owner_role")
    elif event_type == "stage.settled":
        body = _keys(body, {"classification", "outcome_code", "outcome_digest", "elapsed_ms", "usage", "sealed"}, label)
        _enum(body["classification"], STAGE_CLASSIFICATIONS, f"{label}.classification")
        outcome = body["outcome_code"]
        if outcome is not None:
            _enum_with_other_digest(
                outcome,
                body["outcome_digest"],
                OUTCOME_CODES,
                f"{label}.outcome_code",
                f"{label}.outcome_digest",
            )
        elif body["outcome_digest"] is not None:
            raise TrajectoryError(f"{label}.outcome_digest requires outcome_code")
        _integer(body["elapsed_ms"], f"{label}.elapsed_ms")
        _boolean(body["sealed"], f"{label}.sealed")
        _validate_usage(body["usage"], f"{label}.usage")
        # R4: a settled stage may only claim "sealed" while pointing at the
        # DB-committed run manifest that proves it. An unsealed or blocked run
        # says so explicitly instead of producing usable success evidence.
        if body["sealed"] and not any(
            ref["kind"] == "run-manifest" and ref["seal"]["kind"] == "db-committed-run-manifest"
            for ref in refs.values()
        ):
            raise TrajectoryError(
                f"{label}.sealed requires a db-committed run-manifest evidence ref"
            )
    elif event_type == "provider.dispatch_intent":
        body = _keys(body, {"policy_digest", "capability_digest"}, label)
        _digest(body["policy_digest"], f"{label}.policy_digest")
        _digest(body["capability_digest"], f"{label}.capability_digest")
    elif event_type == "provider.dispatched":
        body = _keys(body, {"transport_receipt_ref"}, label)
        if _ref_id(body["transport_receipt_ref"], f"{label}.transport_receipt_ref") not in refs:
            raise TrajectoryError(f"{label}.transport_receipt_ref is not present in evidence_refs")
    elif event_type == "provider.settled":
        body = _keys(body, {"result_class", "elapsed_ms", "usage", "final_response_ref"}, label)
        _enum(body["result_class"], PROVIDER_RESULT_CLASSES, f"{label}.result_class")
        _integer(body["elapsed_ms"], f"{label}.elapsed_ms")
        _validate_usage(body["usage"], f"{label}.usage")
        response_ref = _nullable_ref_id(body["final_response_ref"], f"{label}.final_response_ref")
        if response_ref is not None and response_ref not in refs:
            raise TrajectoryError(f"{label}.final_response_ref is not present in evidence_refs")
    elif event_type == "session.bound":
        body = _keys(body, {"role", "binding_ref"}, label)
        _enum(body["role"], OWNER_ROLES, f"{label}.role")
        if _ref_id(body["binding_ref"], f"{label}.binding_ref") not in refs:
            raise TrajectoryError(f"{label}.binding_ref is not present in evidence_refs")
    elif event_type == "session.rebound":
        body = _keys(body, {"predecessor_event_id", "reason_code", "reason_digest", "checkpoint_ref"}, label)
        _uuid(body["predecessor_event_id"], f"{label}.predecessor_event_id")
        _enum_with_other_digest(
            body["reason_code"],
            body["reason_digest"],
            SESSION_REBOUND_REASON_CODES,
            f"{label}.reason_code",
            f"{label}.reason_digest",
        )
        if _ref_id(body["checkpoint_ref"], f"{label}.checkpoint_ref") not in refs:
            raise TrajectoryError(f"{label}.checkpoint_ref is not present in evidence_refs")
    elif event_type == "evidence.sealed":
        body = _keys(body, {"seal_version"}, label)
        _integer(body["seal_version"], f"{label}.seal_version", minimum=1)
        if not refs:
            raise TrajectoryError(f"{label} requires evidence_refs")
    elif event_type == "migration.baseline":
        body = _keys(
            body,
            {"baseline_id", "source_snapshot_digest", "completeness", "missing_domains", "coverage_through_transition_seq"},
            label,
        )
        _uuid(body["baseline_id"], f"{label}.baseline_id")
        _digest(body["source_snapshot_digest"], f"{label}.source_snapshot_digest")
        if body["completeness"] != "partial":
            raise TrajectoryError(f"{label}.completeness must be partial")
        missing = body["missing_domains"]
        if not isinstance(missing, list) or missing != sorted(set(missing)) or not missing:
            raise TrajectoryError(f"{label}.missing_domains must be a sorted unique non-empty list")
        for item in missing:
            _enum(item, MISSING_DOMAINS, f"{label}.missing_domains")
        _integer(body["coverage_through_transition_seq"], f"{label}.coverage_through_transition_seq")
    else:  # guarded by validate_event, retained for fail-closed maintenance
        raise TrajectoryError(f"unsupported event_type: {event_type}")


def _validate_event(event: Any, *, require_hash: bool) -> dict[str, Any]:
    _reject_detectable_secrets(event)
    expected = TOP_LEVEL_KEYS if require_hash else TOP_LEVEL_KEYS - {"event_hash"}
    event = _keys(event, expected, "trajectory event")
    if event["schema_version"] != SCHEMA_VERSION or type(event["schema_version"]) is not int:
        raise TrajectoryError("unsupported schema_version")
    _integer(event["seq"], "seq", minimum=1)
    _uuid(event["event_id"], "event_id")
    event_type = _text(event["event_type"], "event_type")
    if event_type in RESERVED_EVENT_TYPES or event_type not in SUPPORTED_EVENT_TYPES:
        raise TrajectoryError(f"unsupported event_type: {event_type}")
    if event["event_version"] != EVENT_VERSION or type(event["event_version"]) is not int:
        raise TrajectoryError("unsupported event_version")
    _integer(event["recorded_at_ms"], "recorded_at_ms")

    task = _keys(event["task"], {"task_id", "revision"}, "task")
    task_id = _identifier(task["task_id"], "task.task_id")
    _integer(task["revision"], "task.revision")
    if event["trajectory_id"] != f"task:{task_id}":
        raise TrajectoryError("trajectory_id does not match task_id")

    run = event["run"]
    if run is not None:
        run = _keys(run, {"run_token", "stage", "cycle", "attempt"}, "run")
        _identifier(run["run_token"], "run.run_token")
        _identifier(run["stage"], "run.stage")
        _integer(run["cycle"], "run.cycle", minimum=1)
        _integer(run["attempt"], "run.attempt", minimum=1)
    run_required = event_type.startswith("stage.") or event_type.startswith("provider.") or event_type == "evidence.sealed"
    if run_required and run is None:
        raise TrajectoryError(f"{event_type} requires run identity")
    if event_type in {"task.created", "migration.baseline"} and run is not None:
        raise TrajectoryError(f"{event_type} cannot carry run identity")

    invocation_id = _nullable_identifier(event["invocation_id"], "invocation_id")
    if event_type.startswith("provider.") and invocation_id is None:
        raise TrajectoryError(f"{event_type} requires invocation_id")
    if not event_type.startswith("provider.") and invocation_id is not None:
        raise TrajectoryError(f"{event_type} cannot carry invocation_id in v1")
    _nullable_identifier(event["workflow_attempt_ref"], "workflow_attempt_ref")
    session_ref = _nullable_identifier(event["session_ref"], "session_ref")
    if event_type.startswith("session.") and session_ref is None:
        raise TrajectoryError(f"{event_type} requires session_ref")
    _nullable_uuid(event["parent_event_id"], "parent_event_id")
    causal = event["causal_event_ids"]
    if not isinstance(causal, list) or causal != sorted(set(causal)):
        raise TrajectoryError("causal_event_ids must be a sorted unique list")
    for item in causal:
        _uuid(item, "causal_event_ids item")

    actor = _keys(event["actor"], {"kind", "id", "provider", "model"}, "actor")
    _enum(actor["kind"], ACTOR_KINDS, "actor.kind")
    _identifier(actor["id"], "actor.id")
    _nullable_identifier(actor["provider"], "actor.provider")
    _nullable_identifier(actor["model"], "actor.model")

    evidence = event["evidence_refs"]
    if not isinstance(evidence, list):
        raise TrajectoryError("evidence_refs must be a list")
    ref_ids: dict[str, Mapping[str, Any]] = {}
    for index, item in enumerate(evidence):
        ref_id = _validate_evidence_ref(item, f"evidence_refs[{index}]")
        if ref_id in ref_ids:
            raise TrajectoryError("duplicate evidence ref_id")
        ref_ids[ref_id] = item
        if run is not None and item["seal"]["run_token"] != run["run_token"]:
            raise TrajectoryError("evidence ref run binding does not match event run")
    _validate_body(event_type, event["body"], ref_ids)

    _enum(event["sensitivity"], SENSITIVITIES, "sensitivity")
    _enum(event["retention_class"], RETENTION_CLASSES, "retention_class")
    if event["normalizer_version"] != NORMALIZER_VERSION:
        raise TrajectoryError("unsupported normalizer_version")
    _nullable_digest(event["prev_event_hash"], "prev_event_hash")

    if require_hash:
        supplied = _digest(event["event_hash"], "event_hash")
        unhashed = {key: value for key, value in event.items() if key != "event_hash"}
        actual = "sha256:" + hashlib.sha256(_canonical_json(unhashed)).hexdigest()
        if supplied != actual:
            raise TrajectoryError("event_hash mismatch")
    return event


def seal_event(event: dict[str, Any]) -> dict[str, Any]:
    """Normalize, validate and hash one event that does not yet have event_hash."""
    normalized = _normalize(copy.deepcopy(event))
    _validate_event(normalized, require_hash=False)
    normalized["event_hash"] = "sha256:" + hashlib.sha256(_canonical_json(normalized)).hexdigest()
    _validate_event(normalized, require_hash=True)
    return normalized


def canonical_event_bytes(event: dict[str, Any]) -> bytes:
    """Return the one persisted encoding after verifying its self-hash."""
    _validate_event(event, require_hash=True)
    return _canonical_json(event)


class TrajectoryStore:
    """Append events inside the controller's already-open write transaction."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def append(self, event: dict[str, Any]) -> None:
        if not self.conn.in_transaction:
            raise TrajectoryError("trajectory append requires an existing transaction")
        _validate_event(event, require_hash=True)
        task_id = event["task"]["task_id"]
        task = self.conn.execute("SELECT id,revision FROM tasks WHERE id=?", (task_id,)).fetchone()
        if task is None:
            raise TrajectoryError("trajectory task does not exist")
        if int(task["revision"]) != event["task"]["revision"]:
            raise TrajectoryError("trajectory task revision mismatch")

        run = event["run"]
        run_token = run["run_token"] if run is not None else None
        if run is not None:
            row = self.conn.execute(
                "SELECT task_id,stage,cycle,attempt FROM stage_runs WHERE run_token=?", (run_token,)
            ).fetchone()
            if row is None or (
                row["task_id"], row["stage"], row["cycle"], row["attempt"]
            ) != (task_id, run["stage"], run["cycle"], run["attempt"]):
                raise TrajectoryError("trajectory run identity mismatch")

        last = self.conn.execute(
            "SELECT seq,event_hash FROM trajectory_events WHERE trajectory_id=? ORDER BY seq DESC LIMIT 1",
            (event["trajectory_id"],),
        ).fetchone()
        expected_seq = 1 if last is None else int(last["seq"]) + 1
        expected_prev = None if last is None else last["event_hash"]
        if event["seq"] != expected_seq:
            raise TrajectoryError("trajectory seq is not the next contiguous value")
        if event["prev_event_hash"] != expected_prev:
            raise TrajectoryError("trajectory prev_event_hash mismatch")

        if event["event_type"].startswith("provider."):
            prior = []
            for row in self.conn.execute(
                "SELECT canonical_json FROM trajectory_events WHERE trajectory_id=? ORDER BY seq",
                (event["trajectory_id"],),
            ):
                candidate = json.loads(bytes(row["canonical_json"]))
                if candidate["invocation_id"] == event["invocation_id"]:
                    prior.append(candidate)
            prior_types = [candidate["event_type"] for candidate in prior]
            event_type = event["event_type"]
            if event_type == "provider.dispatch_intent":
                if prior:
                    raise TrajectoryError("trajectory invocation_id already has a dispatch intent")
            else:
                intents = [candidate for candidate in prior if candidate["event_type"] == "provider.dispatch_intent"]
                if len(intents) != 1:
                    raise TrajectoryError("provider event requires exactly one earlier dispatch intent")
                intent = intents[0]
                if (
                    intent["run"] != event["run"]
                    or intent["actor"]["provider"] != event["actor"]["provider"]
                    or intent["actor"]["model"] != event["actor"]["model"]
                ):
                    raise TrajectoryError("provider invocation identity changed after dispatch intent")
                known_sessions = {
                    candidate["session_ref"] for candidate in prior
                    if candidate["session_ref"] is not None
                }
                if intent["session_ref"] is not None:
                    known_sessions.add(intent["session_ref"])
                if known_sessions and event["session_ref"] not in known_sessions:
                    raise TrajectoryError("provider invocation session changed after binding")
                if event_type in prior_types:
                    raise TrajectoryError(f"duplicate {event_type} for invocation_id")
                if event_type == "provider.dispatched" and "provider.settled" in prior_types:
                    raise TrajectoryError("provider.dispatched cannot follow provider.settled")

        referenced = [value for value in [event["parent_event_id"], *event["causal_event_ids"]] if value]
        if referenced:
            placeholders = ",".join("?" for _ in referenced)
            rows = self.conn.execute(
                f"SELECT event_id,seq FROM trajectory_events WHERE trajectory_id=? AND event_id IN ({placeholders})",
                (event["trajectory_id"], *referenced),
            ).fetchall()
            found = {row["event_id"]: int(row["seq"]) for row in rows}
            if set(found) != set(referenced) or any(seq >= event["seq"] for seq in found.values()):
                raise TrajectoryError("parent/causal event must be earlier in the same trajectory")

        if event["event_type"] == "session.rebound":
            predecessor_id = event["body"]["predecessor_event_id"]
            predecessor_row = self.conn.execute(
                "SELECT seq,canonical_json FROM trajectory_events "
                "WHERE trajectory_id=? AND event_id=?",
                (event["trajectory_id"], predecessor_id),
            ).fetchone()
            if predecessor_row is None or int(predecessor_row["seq"]) >= event["seq"]:
                raise TrajectoryError(
                    "session rebound predecessor must be earlier in the same trajectory"
                )
            predecessor = json.loads(bytes(predecessor_row["canonical_json"]))
            if predecessor["event_type"] not in {"session.bound", "session.rebound"}:
                raise TrajectoryError("session rebound predecessor must be a session binding event")
            if predecessor["session_ref"] == event["session_ref"]:
                raise TrajectoryError("session rebound must replace the predecessor session")

        encoded = canonical_event_bytes(event)
        try:
            self.conn.execute(
                """INSERT INTO trajectory_events(
                       trajectory_id,seq,event_id,schema_version,event_type,event_version,
                       task_id,run_token,recorded_at_ms,canonical_json,prev_event_hash,event_hash
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    event["trajectory_id"],
                    event["seq"],
                    event["event_id"],
                    event["schema_version"],
                    event["event_type"],
                    event["event_version"],
                    task_id,
                    run_token,
                    event["recorded_at_ms"],
                    encoded,
                    event["prev_event_hash"],
                    event["event_hash"],
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise TrajectoryError("trajectory event conflicts with durable state") from exc



def digest_text(value: str) -> str:
    """Return the v1 digest form without ever retaining the source text."""
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


TRAJECTORY_MODES = frozenset({"off", "write", "read"})
TRAJECTORY_ENV = "ORCH_TRAJECTORY_V1"
#: The one line an operator sees for an unusable gate value. It deliberately
#: does not echo the rejected value: the gate is read from an environment that
#: also carries credentials, and a misdirected variable must not be reflected.
TRAJECTORY_CONFIG_ERROR = (
    f"orchestrator: {TRAJECTORY_ENV} is not a supported value; trajectory is disabled "
    "(expected off, write, or read)"
)


def trajectory_mode(env: Mapping[str, str] | None = None) -> str:
    """Read the rollout gate. The absent value is deliberately ``off``.

    An unknown value fails closed to ``off`` and reports a configuration error
    on stderr rather than raising: the gate guards an additive audit surface,
    so a typo must not take the lifecycle down with it.
    """
    import os
    import sys

    source = os.environ if env is None else env
    value = source.get(TRAJECTORY_ENV, "off").strip().lower()
    if value not in TRAJECTORY_MODES:
        print(TRAJECTORY_CONFIG_ERROR, file=sys.stderr)
        return "off"
    return value


def _row_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _canonical_state_snapshot(conn: sqlite3.Connection, task_id: str) -> dict[str, Any]:
    """Bounded canonical input for a legacy baseline; contains no artifact payloads."""
    task = conn.execute(
        "SELECT id,type,status,stop_reason,current_stage,owner,revision,profile_hash,input_hash,"
        "transitions_count,max_transitions,resume_allowance,created_at,updated_at "
        "FROM tasks WHERE id=?",
        (task_id,),
    ).fetchone()
    if task is None:
        raise TrajectoryError("trajectory task does not exist")
    runs = [
        _row_dict(row)
        for row in conn.execute(
            "SELECT run_token,stage,cycle,attempt,owner,status,exit_code,outcome,sealed,model,"
            "duration_ms,usage_input_tokens,usage_output_tokens,usage_total_tokens,"
            "usage_unavailable_reason,started_at,ended_at FROM stage_runs WHERE task_id=? "
            "ORDER BY started_at,rowid",
            (task_id,),
        )
    ]
    transitions = [
        _row_dict(row)
        for row in conn.execute(
            "SELECT seq,operation_id,run_token,stage,owner,edge,outcome,from_status,to_status,reason,at "
            "FROM transitions WHERE task_id=? ORDER BY seq",
            (task_id,),
        )
    ]
    return {"task": _row_dict(task), "stage_runs": runs, "transitions": transitions}


class TrajectoryWriter:
    """Build and append v1 events inside an existing canonical transaction.

    The writer deliberately receives only typed, already-normalized fields. It
    never accepts provider output or an arbitrary mapping as an event body.
    """

    def __init__(self, conn: sqlite3.Connection, mode: str):
        if mode not in TRAJECTORY_MODES:
            raise TrajectoryError("invalid trajectory writer mode")
        self.conn = conn
        self.mode = mode

    @property
    def enabled(self) -> bool:
        return self.mode in {"write", "read"}

    def has_events(self, task_id: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM trajectory_events WHERE trajectory_id=? LIMIT 1", (f"task:{task_id}",)
        ).fetchone() is not None

    def ensure_legacy_baseline(self, task_id: str, *, recorded_at_ms: int) -> str | None:
        if not self.enabled:
            return None
        rows = self.conn.execute(
            "SELECT event_type,canonical_json FROM trajectory_events WHERE trajectory_id=? ORDER BY seq",
            (f"task:{task_id}",),
        ).fetchall()
        event_types = {row["event_type"] for row in rows}
        if "migration.baseline" in event_types:
            return None
        live_transition_seqs = {
            int(json.loads(bytes(row["canonical_json"]))["body"]["transition_seq"])
            for row in rows
            if row["event_type"] == "task.transition.committed"
        }
        canonical_transition_seqs = {
            int(row["seq"])
            for row in self.conn.execute(
                "SELECT seq FROM transitions WHERE task_id=?", (task_id,)
            )
        }
        # A task born with the writer enabled needs no baseline only while its
        # complete canonical transition history is represented by live events.
        # If the gate was temporarily off, the gap is covered once by the same
        # deterministic partial baseline used for a fully legacy task.
        if (
            "task.created" in event_types
            and canonical_transition_seqs.issubset(live_transition_seqs)
        ):
            return None
        snapshot = _canonical_state_snapshot(self.conn, task_id)
        coverage = snapshot["transitions"][-1]["seq"] if snapshot["transitions"] else 0
        baseline_id = str(uuid.uuid5(BASELINE_NAMESPACE, f"task:{task_id}"))
        return self.append(
            task_id,
            "migration.baseline",
            recorded_at_ms=recorded_at_ms,
            event_id=baseline_id,
            actor_kind="migrator",
            actor_id="native-orchestrator",
            body={
                "baseline_id": baseline_id,
                "source_snapshot_digest": "sha256:" + hashlib.sha256(_canonical_json(snapshot)).hexdigest(),
                "completeness": "partial",
                "missing_domains": sorted(MISSING_DOMAINS),
                "coverage_through_transition_seq": int(coverage),
            },
            retention_class="structural",
        )

    def append(
        self,
        task_id: str,
        event_type: str,
        *,
        recorded_at_ms: int,
        body: dict[str, Any],
        run: Mapping[str, Any] | None = None,
        invocation_id: str | None = None,
        workflow_attempt_ref: str | None = None,
        session_ref: str | None = None,
        actor_kind: str = "controller",
        actor_id: str = "native-orchestrator",
        actor_provider: str | None = None,
        actor_model: str | None = None,
        evidence_refs: list[dict[str, Any]] | None = None,
        sensitivity: str = "internal",
        retention_class: str = "task-lifecycle",
        parent_event_id: str | None = None,
        causal_event_ids: list[str] | None = None,
        event_id: str | None = None,
    ) -> str | None:
        if not self.enabled:
            return None
        if not self.conn.in_transaction:
            raise TrajectoryError("trajectory writer requires an existing transaction")
        task = self.conn.execute("SELECT revision FROM tasks WHERE id=?", (task_id,)).fetchone()
        if task is None:
            raise TrajectoryError("trajectory task does not exist")
        last = self.conn.execute(
            "SELECT seq,event_id,event_hash FROM trajectory_events WHERE trajectory_id=? "
            "ORDER BY seq DESC LIMIT 1",
            (f"task:{task_id}",),
        ).fetchone()
        seq = 1 if last is None else int(last["seq"]) + 1
        previous_hash = None if last is None else last["event_hash"]
        if parent_event_id is None and last is not None:
            parent_event_id = last["event_id"]
        run_identity = None
        if run is not None:
            run_identity = {
                "run_token": run["run_token"],
                "stage": run["stage"],
                "cycle": int(run["cycle"]),
                "attempt": int(run["attempt"]),
            }
        event = {
            "schema_version": SCHEMA_VERSION,
            "trajectory_id": f"task:{task_id}",
            "seq": seq,
            "event_id": event_id or str(uuid.uuid4()),
            "event_type": event_type,
            "event_version": EVENT_VERSION,
            "recorded_at_ms": int(recorded_at_ms),
            "task": {"task_id": task_id, "revision": int(task["revision"])},
            "run": run_identity,
            "invocation_id": invocation_id,
            "workflow_attempt_ref": workflow_attempt_ref,
            "session_ref": session_ref,
            "parent_event_id": parent_event_id,
            "causal_event_ids": sorted(set(causal_event_ids or [])),
            "actor": {
                "kind": actor_kind,
                "id": actor_id,
                "provider": actor_provider,
                "model": actor_model,
            },
            "body": body,
            "evidence_refs": evidence_refs or [],
            "sensitivity": sensitivity,
            "retention_class": retention_class,
            "normalizer_version": NORMALIZER_VERSION,
            "prev_event_hash": previous_hash,
        }
        sealed = seal_event(event)
        TrajectoryStore(self.conn).append(sealed)
        return sealed["event_id"]


def parity_diagnostics(
    conn: sqlite3.Connection, task_id: str, event_ids: list[str]
) -> list[dict[str, Any]]:
    """Compare newly appended events with canonical rows using closed diagnostics."""
    diagnostics: list[dict[str, Any]] = []
    for event_id in event_ids:
        row = conn.execute(
            "SELECT canonical_json FROM trajectory_events WHERE task_id=? AND event_id=?",
            (task_id, event_id),
        ).fetchone()
        if row is None:
            diagnostics.append({"code": "event_missing", "event_id": event_id, "event_seq": None})
            continue
        try:
            event = json.loads(bytes(row["canonical_json"]))
            canonical_event_bytes(event)
        except (ValueError, TypeError, json.JSONDecodeError):
            diagnostics.append({"code": "event_invalid", "event_id": event_id, "event_seq": None})
            continue
        seq = event["seq"]
        code = None
        if event["event_type"] == "task.created":
            canonical = conn.execute(
                "SELECT profile_hash,input_hash FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            if canonical is None or event["body"] != {
                "profile_digest": f"sha256:{canonical['profile_hash']}",
                "input_digest": f"sha256:{canonical['input_hash']}",
            }:
                code = "task_created_mismatch"
        elif event["event_type"] == "task.transition.committed":
            canonical = conn.execute(
                "SELECT operation_id,from_status,to_status,reason,outcome FROM transitions "
                "WHERE task_id=? AND seq=?",
                (task_id, event["body"]["transition_seq"]),
            ).fetchone()
            if canonical is None or canonical["operation_id"] != event["body"]["operation_id"] \
                    or canonical["from_status"] != event["body"]["from_status"] \
                    or canonical["to_status"] != event["body"]["to_status"]:
                code = "transition_mismatch"
        elif event["run"] is not None:
            canonical = conn.execute(
                "SELECT stage,cycle,attempt FROM stage_runs WHERE task_id=? AND run_token=?",
                (task_id, event["run"]["run_token"]),
            ).fetchone()
            if canonical is None or (
                canonical["stage"], int(canonical["cycle"]), int(canonical["attempt"])
            ) != (
                event["run"]["stage"], event["run"]["cycle"], event["run"]["attempt"]
            ):
                code = "run_mismatch"
        task = conn.execute("SELECT revision FROM tasks WHERE id=?", (task_id,)).fetchone()
        if code is None and (task is None or int(task["revision"]) != event["task"]["revision"]):
            if task is None or int(event["task"]["revision"]) > int(task["revision"]):
                code = "task_revision_mismatch"
        if code is not None:
            diagnostics.append({"code": code, "event_id": event_id, "event_seq": seq})
    return diagnostics
