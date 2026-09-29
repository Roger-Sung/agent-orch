"""Frozen trajectory acquisition and the side-effect-free R0 projection reducer."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

from .trajectory import (
    SENSITIVITIES,
    SUPPORTED_EVENT_TYPES,
    TOP_LEVEL_KEYS,
    TrajectoryError,
    canonical_event_bytes,
)

SNAPSHOT_VERSION = 1
PROJECTION_VERSION = 1
AUDIENCES = frozenset({"public", "internal", "sensitive"})
UNSUPPORTED_DOMAINS = (
    "approval",
    "context",
    "gate",
    "join",
    "prompt",
    "subagent",
    "tool",
)
DIAGNOSTIC_CODES = frozenset(
    {
        "canonical_mismatch",
        "duplicate_event_id",
        "event_invalid",
        "evidence_corrupt",
        "evidence_inventory_mismatch",
        "hash_chain_broken",
        "parent_invalid",
        "provider_event_duplicate",
        "provider_event_unpaired",
        "sequence_gap",
        "session_predecessor_invalid",
        "snapshot_invalid",
        "snapshot_version_unsupported",
        "trajectory_mismatch",
    }
)
UNKNOWN_CODES = frozenset(
    {
        "evidence_expired",
        "evidence_unavailable",
        "provider_handoff_unknown",
        "provider_result_unknown",
    }
)
SENSITIVITY_RANK = {"public": 0, "internal": 1, "sensitive": 2}


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _sha(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _canonical_state(conn: sqlite3.Connection, task_id: str) -> dict[str, Any]:
    task = conn.execute(
        "SELECT id,status,stop_reason,current_stage,owner,revision,profile_hash,input_hash,"
        "transitions_count,created_at,updated_at FROM tasks WHERE id=?",
        (task_id,),
    ).fetchone()
    if task is None:
        raise TrajectoryError("trajectory task does not exist")
    runs = [
        _row(item)
        for item in conn.execute(
            "SELECT run_token,stage,cycle,attempt,owner,status,outcome,sealed,manifest_hash,"
            "model,duration_ms,usage_input_tokens,usage_output_tokens,usage_total_tokens,"
            "usage_unavailable_reason,started_at,ended_at FROM stage_runs "
            "WHERE task_id=? ORDER BY started_at,rowid",
            (task_id,),
        )
    ]
    transitions = [
        _row(item)
        for item in conn.execute(
            "SELECT seq,operation_id,run_token,stage,owner,edge,outcome,from_status,to_status,"
            "reason,at FROM transitions WHERE task_id=? ORDER BY seq",
            (task_id,),
        )
    ]
    return {"task": _row(task), "stage_runs": runs, "transitions": transitions}


def _availability(root: Path, ref: dict[str, Any]) -> str:
    """Recheck a sealed pointer without returning its path, bytes, or exception."""
    try:
        relative = PurePosixPath(ref["relative_path"])
        if relative.is_absolute() or not relative.parts or any(
            part in {"", ".", ".."} for part in relative.parts
        ):
            return "corrupt"
        candidate = root.joinpath(*relative.parts)
        cursor = root
        for part in relative.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                return "corrupt"
        if not candidate.is_file():
            return (
                "expired"
                if ref["retention_class"] == "ephemeral-ref"
                else "unavailable"
            )
        resolved = candidate.resolve(strict=True)
        if not resolved.is_relative_to(root):
            return "corrupt"
        stat = candidate.stat()
        if stat.st_size != ref["size_bytes"]:
            return "corrupt"
        digest = hashlib.sha256()
        with candidate.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        return "present" if digest.hexdigest() == ref["sha256"] else "corrupt"
    except (OSError, KeyError, TypeError, ValueError):
        return "corrupt"


def freeze_snapshot(
    conn: sqlite3.Connection, task_id: str, *, captured_at_ms: int
) -> dict[str, Any]:
    """Freeze one SQLite read snapshot plus current sealed-ref availability.

    Acquisition may read SQLite and artifact files.  The returned object is the
    complete input to R0; the reducer itself performs no I/O.
    """
    own_transaction = not conn.in_transaction
    if own_transaction:
        conn.execute("BEGIN")
    try:
        task = conn.execute(
            "SELECT id,artifact_dir FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        if task is None:
            raise TrajectoryError("trajectory task does not exist")
        rows = conn.execute(
            "SELECT canonical_json FROM trajectory_events WHERE trajectory_id=? ORDER BY seq",
            (f"task:{task_id}",),
        ).fetchall()
        canonical_events = [bytes(item["canonical_json"]) for item in rows]
        events: list[dict[str, Any]] = []
        for raw in canonical_events:
            event = json.loads(raw)
            if canonical_event_bytes(event) != raw:
                raise TrajectoryError("stored trajectory event is not canonical")
            events.append(event)

        # A retained trajectory remains inspectable after its artifact root is
        # removed.  Individual refs then degrade to unavailable/expired; the
        # snapshot itself must still carry the event chain and canonical state.
        root = Path(task["artifact_dir"]).resolve(strict=False)
        inventory_by_id: dict[str, dict[str, Any]] = {}
        sensitivity = "internal"
        for event in events:
            event_sensitivity = event["sensitivity"]
            if SENSITIVITY_RANK[event_sensitivity] > SENSITIVITY_RANK[sensitivity]:
                sensitivity = event_sensitivity
            for ref in event["evidence_refs"]:
                if SENSITIVITY_RANK[ref["sensitivity"]] > SENSITIVITY_RANK[sensitivity]:
                    sensitivity = ref["sensitivity"]
                item = {
                    "ref_id": ref["ref_id"],
                    "kind": ref["kind"],
                    "sha256": ref["sha256"],
                    "size_bytes": ref["size_bytes"],
                    "media_type": ref["media_type"],
                    "sensitivity": ref["sensitivity"],
                    "retention_class": ref["retention_class"],
                    "availability_at_append": ref["availability_at_append"],
                    "availability": _availability(root, ref),
                }
                prior = inventory_by_id.get(item["ref_id"])
                if prior is not None and prior != item:
                    raise TrajectoryError("evidence ref_id changed across events")
                inventory_by_id[item["ref_id"]] = item

        inventory = [inventory_by_id[key] for key in sorted(inventory_by_id)]
        canonical_state = _canonical_state(conn, task_id)
        source_db_snapshot_hash = _sha(canonical_state)
        ordered_events = [raw.decode("utf-8") for raw in canonical_events]
        manifest = {
            "created_from": "live-acquisition",
            "event_count": len(events),
            "evidence_inventory_digest": _sha(inventory),
            "first_seq": events[0]["seq"] if events else None,
            "last_seq": events[-1]["seq"] if events else None,
            "event_head_hash": events[-1]["event_hash"] if events else None,
            "ordered_events_digest": _sha(ordered_events),
            "sensitivity": sensitivity,
            "source_db_snapshot_hash": source_db_snapshot_hash,
        }
        snapshot = {
            "snapshot_version": SNAPSHOT_VERSION,
            "trajectory_id": f"task:{task_id}",
            "captured_at_ms": int(captured_at_ms),
            "created_from": "live-acquisition",
            "ordered_events": ordered_events,
            "event_head_hash": manifest["event_head_hash"],
            "evidence_inventory": inventory,
            "canonical_state": canonical_state,
            "source_db_snapshot_hash": source_db_snapshot_hash,
            "sensitivity": sensitivity,
            "manifest": manifest,
        }
        snapshot["snapshot_digest"] = _sha(snapshot)
        return snapshot
    finally:
        if own_transaction:
            conn.execute("ROLLBACK")


@dataclass(frozen=True)
class ProjectionResult:
    projection_version: int
    trajectory_id: str | None
    input_head_hash: str | None
    integrity_status: str
    completeness: str
    task_lifecycle: dict[str, Any] | None
    stage_attempts: tuple[dict[str, Any], ...]
    invocations: tuple[dict[str, Any], ...]
    sessions: tuple[dict[str, Any], ...]
    evidence_graph: tuple[dict[str, Any], ...]
    model_visible_boundary_index: tuple[dict[str, Any], ...]
    usage_by_basis: dict[str, tuple[dict[str, Any], ...]]
    unsupported_domains: tuple[str, ...]
    unknowns: tuple[dict[str, Any], ...]
    diagnostics: tuple[dict[str, Any], ...]
    snapshot_manifest: dict[str, Any] | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _diagnostic(
    code: str,
    event: dict[str, Any] | None = None,
    *,
    expected_digest: str | None = None,
    actual_digest: str | None = None,
) -> dict[str, Any]:
    if code not in DIAGNOSTIC_CODES:
        raise AssertionError(code)
    return {
        "code": code,
        "event_seq": event.get("seq") if isinstance(event, dict) else None,
        "event_id": event.get("event_id") if isinstance(event, dict) else None,
        "expected_digest": expected_digest,
        "actual_digest": actual_digest,
    }


def _unknown(code: str, event: dict[str, Any], ref_id: str | None = None) -> dict[str, Any]:
    if code not in UNKNOWN_CODES:
        raise AssertionError(code)
    return {
        "code": code,
        "event_seq": event["seq"],
        "event_id": event["event_id"],
        "ref_id": ref_id,
    }


def _empty_projection(
    code: str, *, trajectory_id: str | None = None
) -> ProjectionResult:
    return ProjectionResult(
        projection_version=PROJECTION_VERSION,
        trajectory_id=trajectory_id,
        input_head_hash=None,
        integrity_status="corrupt",
        completeness="incomplete",
        task_lifecycle=None,
        stage_attempts=(),
        invocations=(),
        sessions=(),
        evidence_graph=(),
        model_visible_boundary_index=(),
        usage_by_basis={},
        unsupported_domains=UNSUPPORTED_DOMAINS,
        unknowns=(),
        diagnostics=(_diagnostic(code),),
        snapshot_manifest=None,
    )


def _snapshot_events(snapshot: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    events: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    ordered = snapshot.get("ordered_events") if isinstance(snapshot, dict) else None
    if not isinstance(ordered, list):
        return events, [_diagnostic("snapshot_invalid")]
    for raw in ordered:
        if not isinstance(raw, str):
            diagnostics.append(_diagnostic("event_invalid"))
            continue
        try:
            event = json.loads(raw)
            if canonical_event_bytes(event) != raw.encode("utf-8"):
                raise TrajectoryError("event bytes are not canonical")
        except (json.JSONDecodeError, TrajectoryError, TypeError, ValueError):
            diagnostics.append(_diagnostic("event_invalid"))
            continue
        events.append(event)
    return events, diagnostics


def reduce_snapshot(
    snapshot: Any,
    *,
    projection_version: int = PROJECTION_VERSION,
    audience_policy: Iterable[str] = ("public", "internal"),
) -> ProjectionResult:
    """Pure R0 fold: only its arguments influence the returned value."""
    audience = tuple(sorted(set(audience_policy)))
    if projection_version != PROJECTION_VERSION:
        return _empty_projection("snapshot_version_unsupported")
    if not audience or any(item not in AUDIENCES for item in audience):
        return _empty_projection("snapshot_invalid")
    expected_keys = {
        "snapshot_version",
        "trajectory_id",
        "captured_at_ms",
        "created_from",
        "ordered_events",
        "event_head_hash",
        "evidence_inventory",
        "canonical_state",
        "source_db_snapshot_hash",
        "sensitivity",
        "manifest",
        "snapshot_digest",
    }
    if not isinstance(snapshot, dict) or set(snapshot) != expected_keys:
        return _empty_projection("snapshot_invalid")
    trajectory_id = snapshot.get("trajectory_id")
    if snapshot.get("snapshot_version") != SNAPSHOT_VERSION:
        return _empty_projection(
            "snapshot_version_unsupported",
            trajectory_id=trajectory_id if isinstance(trajectory_id, str) else None,
        )
    canonical_state = snapshot.get("canonical_state")
    if (
        not isinstance(canonical_state, dict)
        or set(canonical_state) != {"task", "stage_runs", "transitions"}
        or not isinstance(canonical_state.get("task"), dict)
        or not isinstance(canonical_state.get("stage_runs"), list)
        or not all(isinstance(item, dict) for item in canonical_state["stage_runs"])
        or not isinstance(canonical_state.get("transitions"), list)
        or not all(isinstance(item, dict) for item in canonical_state["transitions"])
    ):
        return _empty_projection(
            "snapshot_invalid",
            trajectory_id=trajectory_id if isinstance(trajectory_id, str) else None,
        )
    unhashed = {key: value for key, value in snapshot.items() if key != "snapshot_digest"}
    if snapshot.get("snapshot_digest") != _sha(unhashed):
        return _empty_projection(
            "snapshot_invalid",
            trajectory_id=trajectory_id if isinstance(trajectory_id, str) else None,
        )
    if (
        not isinstance(trajectory_id, str)
        or not trajectory_id.startswith("task:")
        or snapshot.get("created_from")
        not in {"live-acquisition", "test-fixture", "legacy-baseline"}
        or snapshot.get("sensitivity") not in SENSITIVITIES
        or snapshot.get("source_db_snapshot_hash") != _sha(canonical_state)
    ):
        return _empty_projection("snapshot_invalid", trajectory_id=trajectory_id)

    events, diagnostics = _snapshot_events(snapshot)
    seen_ids: set[str] = set()
    by_id: dict[str, dict[str, Any]] = {}
    previous_hash: str | None = None
    for expected_seq, event in enumerate(events, 1):
        if set(event) != set(TOP_LEVEL_KEYS):
            diagnostics.append(_diagnostic("event_invalid", event))
            continue
        if event["trajectory_id"] != trajectory_id:
            diagnostics.append(_diagnostic("trajectory_mismatch", event))
        if event["seq"] != expected_seq:
            diagnostics.append(_diagnostic("sequence_gap", event))
        if event["event_id"] in seen_ids:
            diagnostics.append(_diagnostic("duplicate_event_id", event))
        seen_ids.add(event["event_id"])
        if event["prev_event_hash"] != previous_hash:
            diagnostics.append(
                _diagnostic(
                    "hash_chain_broken",
                    event,
                    expected_digest=previous_hash,
                    actual_digest=event["prev_event_hash"],
                )
            )
        refs = [event["parent_event_id"], *event["causal_event_ids"]]
        if any(ref is not None and ref not in by_id for ref in refs):
            diagnostics.append(_diagnostic("parent_invalid", event))
        by_id[event["event_id"]] = event
        previous_hash = event["event_hash"]

    inventory = snapshot["evidence_inventory"]
    if not isinstance(inventory, list):
        return _empty_projection("snapshot_invalid", trajectory_id=trajectory_id)
    expected_manifest = {
        "created_from": snapshot["created_from"],
        "event_count": len(events),
        "evidence_inventory_digest": _sha(inventory),
        "first_seq": events[0]["seq"] if events else None,
        "last_seq": events[-1]["seq"] if events else None,
        "event_head_hash": events[-1]["event_hash"] if events else None,
        "ordered_events_digest": _sha(snapshot["ordered_events"]),
        "sensitivity": snapshot["sensitivity"],
        "source_db_snapshot_hash": snapshot["source_db_snapshot_hash"],
    }
    if (
        snapshot["manifest"] != expected_manifest
        or snapshot["event_head_hash"] != expected_manifest["event_head_hash"]
    ):
        diagnostics.append(_diagnostic("snapshot_invalid"))

    attempts: dict[str, dict[str, Any]] = {}
    invocations: dict[str, dict[str, Any]] = {}
    sessions: list[dict[str, Any]] = []
    session_events: dict[str, dict[str, Any]] = {}
    boundaries: list[dict[str, Any]] = []
    usage: dict[str, list[dict[str, Any]]] = {}
    task_lifecycle: dict[str, Any] | None = None
    baseline: dict[str, Any] | None = None

    for event in events:
        kind = event["event_type"]
        body = event["body"]
        run = event["run"]
        if kind == "task.created":
            task_lifecycle = {
                "task_id": event["task"]["task_id"],
                "revision": event["task"]["revision"],
                "status": "queued",
                "transition_seq": None,
            }
        elif kind == "migration.baseline":
            baseline = {
                "baseline_id": body["baseline_id"],
                "coverage_through_transition_seq": body[
                    "coverage_through_transition_seq"
                ],
                "missing_domains": tuple(body["missing_domains"]),
            }
            if task_lifecycle is None:
                task_lifecycle = {
                    "task_id": event["task"]["task_id"],
                    "revision": event["task"]["revision"],
                    "status": None,
                    "transition_seq": body["coverage_through_transition_seq"],
                }
            else:
                task_lifecycle.update(
                    revision=event["task"]["revision"],
                    transition_seq=body["coverage_through_transition_seq"],
                )
        elif kind == "task.transition.committed":
            if task_lifecycle is None:
                task_lifecycle = {
                    "task_id": event["task"]["task_id"],
                    "revision": event["task"]["revision"],
                    "status": body["to_status"],
                    "transition_seq": body["transition_seq"],
                }
            else:
                task_lifecycle.update(
                    revision=event["task"]["revision"],
                    status=body["to_status"],
                    transition_seq=body["transition_seq"],
                )
        elif kind == "stage.claimed":
            attempts[run["run_token"]] = {
                **run,
                "status": "running",
                "classification": None,
                "outcome_code": None,
                "sealed": False,
            }
        elif kind == "stage.settled":
            item = attempts.setdefault(run["run_token"], {**run})
            item.update(
                status="settled",
                classification=body["classification"],
                outcome_code=body["outcome_code"],
                elapsed_ms=body["elapsed_ms"],
                usage=body["usage"],
                sealed=body["sealed"],
            )
            bucket = body["usage"]["basis"]
            usage.setdefault(bucket, []).append(
                {
                    "event_id": event["event_id"],
                    "run_token": run["run_token"],
                    "input_tokens": body["usage"]["input_tokens"],
                    "output_tokens": body["usage"]["output_tokens"],
                    "total_tokens": body["usage"]["total_tokens"],
                }
            )
        elif kind.startswith("provider."):
            invocation_id = event["invocation_id"]
            item = invocations.get(invocation_id)
            if kind == "provider.dispatch_intent":
                if item is not None:
                    diagnostics.append(_diagnostic("provider_event_duplicate", event))
                invocations[invocation_id] = {
                    "invocation_id": invocation_id,
                    "run_token": run["run_token"],
                    "provider": event["actor"]["provider"],
                    "model": event["actor"]["model"],
                    "session_ref": event["session_ref"],
                    "dispatch_state": "unknown",
                    "result_state": "unknown",
                    "intent_event_id": event["event_id"],
                }
            elif item is None:
                diagnostics.append(_diagnostic("provider_event_unpaired", event))
            elif kind == "provider.dispatched":
                if "dispatched_event_id" in item:
                    diagnostics.append(_diagnostic("provider_event_duplicate", event))
                item["dispatch_state"] = "dispatched"
                item["session_ref"] = event["session_ref"] or item["session_ref"]
                item["dispatched_event_id"] = event["event_id"]
            else:
                if "settled_event_id" in item:
                    diagnostics.append(_diagnostic("provider_event_duplicate", event))
                item["result_state"] = body["result_class"]
                item["session_ref"] = event["session_ref"] or item["session_ref"]
                item["settled_event_id"] = event["event_id"]
                item["elapsed_ms"] = body["elapsed_ms"]
                item["usage"] = body["usage"]
                if body["final_response_ref"] is not None:
                    boundaries.append(
                        {
                            "event_seq": event["seq"],
                            "event_id": event["event_id"],
                            "invocation_id": invocation_id,
                            "final_response_ref": body["final_response_ref"],
                        }
                    )
        elif kind in {"session.bound", "session.rebound"}:
            if kind == "session.rebound":
                predecessor = session_events.get(body["predecessor_event_id"])
                if (
                    predecessor is None
                    or predecessor["session_ref"] == event["session_ref"]
                ):
                    diagnostics.append(
                        _diagnostic("session_predecessor_invalid", event)
                    )
            item = {
                "event_id": event["event_id"],
                "event_type": kind,
                "session_ref": event["session_ref"],
                "predecessor_event_id": body.get("predecessor_event_id"),
                "role": body.get("role"),
            }
            sessions.append(item)
            session_events[event["event_id"]] = item

    unknowns: list[dict[str, Any]] = []
    event_for_invocation = {
        event["invocation_id"]: event
        for event in events
        if event["event_type"] == "provider.dispatch_intent"
    }
    for invocation_id in sorted(invocations):
        item = invocations[invocation_id]
        event = event_for_invocation[invocation_id]
        if item["result_state"] == "unknown":
            unknowns.append(_unknown("provider_result_unknown", event))
        if item["dispatch_state"] == "unknown":
            unknowns.append(_unknown("provider_handoff_unknown", event))

    event_refs_by_id: dict[str, dict[str, Any]] = {}
    for event in events:
        for ref in event["evidence_refs"]:
            comparable = {
                key: ref[key]
                for key in (
                    "ref_id", "kind", "sha256", "size_bytes", "media_type",
                    "sensitivity", "retention_class", "availability_at_append",
                )
            }
            prior = event_refs_by_id.get(ref["ref_id"])
            if prior is not None and prior != comparable:
                diagnostics.append(_diagnostic("evidence_inventory_mismatch", event))
            event_refs_by_id[ref["ref_id"]] = comparable

    inventory_by_id: dict[str, dict[str, Any]] = {}
    for item in inventory:
        expected_inventory_keys = {
            "ref_id", "kind", "sha256", "size_bytes", "media_type", "sensitivity",
            "retention_class", "availability_at_append", "availability",
        }
        if (
            not isinstance(item, dict)
            or set(item) != expected_inventory_keys
            or not isinstance(item.get("ref_id"), str)
        ):
            diagnostics.append(_diagnostic("snapshot_invalid"))
            continue
        inventory_by_id[item["ref_id"]] = item
        availability = item.get("availability")
        if availability == "corrupt":
            diagnostics.append(
                _diagnostic(
                    "evidence_corrupt",
                    expected_digest="sha256:" + item.get("sha256", ""),
                    actual_digest=None,
                )
            )
        elif availability in {"unavailable", "expired"}:
            first = next(
                (
                    event
                    for event in events
                    if any(
                        ref["ref_id"] == item["ref_id"]
                        for ref in event["evidence_refs"]
                    )
                ),
                None,
            )
            if first is not None:
                unknowns.append(
                    _unknown(
                        "evidence_expired"
                        if availability == "expired"
                        else "evidence_unavailable",
                        first,
                        item["ref_id"],
                    )
                )

    if set(inventory_by_id) != set(event_refs_by_id):
        diagnostics.append(_diagnostic("evidence_inventory_mismatch"))
    else:
        for ref_id, expected in event_refs_by_id.items():
            actual = {
                key: inventory_by_id[ref_id][key]
                for key in expected
            }
            if actual != expected:
                diagnostics.append(_diagnostic("evidence_inventory_mismatch"))
                break

    canonical_task = (
        canonical_state.get("task") if isinstance(canonical_state, dict) else None
    )
    if task_lifecycle is not None and isinstance(canonical_task, dict):
        if (
            task_lifecycle["status"] is not None
            and (
                task_lifecycle["status"] != canonical_task.get("status")
                or task_lifecycle["revision"] != canonical_task.get("revision")
            )
        ):
            diagnostics.append(
                _diagnostic(
                    "canonical_mismatch",
                    expected_digest=_sha(
                        {
                            "status": canonical_task.get("status"),
                            "revision": canonical_task.get("revision"),
                        }
                    ),
                    actual_digest=_sha(
                        {
                            "status": task_lifecycle["status"],
                            "revision": task_lifecycle["revision"],
                        }
                    ),
                )
            )
    elif isinstance(canonical_task, dict):
        diagnostics.append(_diagnostic("canonical_mismatch"))

    canonical_transitions = (
        canonical_state.get("transitions", []) if isinstance(canonical_state, dict) else []
    )
    canonical_transition_seqs = {
        int(item["seq"])
        for item in canonical_transitions
        if isinstance(item, dict) and type(item.get("seq")) is int
    }
    live_transition_seqs = {
        int(event["body"]["transition_seq"])
        for event in events
        if event["event_type"] == "task.transition.committed"
    }
    covered_through = (
        int(baseline["coverage_through_transition_seq"])
        if baseline is not None
        else 0
    )
    required_live = {
        seq for seq in canonical_transition_seqs if seq > covered_through
    }
    if not required_live.issubset(live_transition_seqs):
        diagnostics.append(_diagnostic("canonical_mismatch"))

    structural_codes = {
        "duplicate_event_id",
        "event_invalid",
        "evidence_corrupt",
        "evidence_inventory_mismatch",
        "hash_chain_broken",
        "parent_invalid",
        "provider_event_duplicate",
        "provider_event_unpaired",
        "sequence_gap",
        "session_predecessor_invalid",
        "snapshot_invalid",
        "trajectory_mismatch",
    }
    codes = {item["code"] for item in diagnostics}
    if codes & structural_codes:
        integrity_status = "corrupt"
    elif "canonical_mismatch" in codes:
        integrity_status = "mismatch"
    elif unknowns:
        integrity_status = "incomplete"
    else:
        integrity_status = "ok"

    completeness = "complete"
    if baseline is not None:
        completeness = "partial"
    if unknowns or integrity_status in {"corrupt", "mismatch"}:
        completeness = "incomplete"

    return ProjectionResult(
        projection_version=PROJECTION_VERSION,
        trajectory_id=trajectory_id,
        input_head_hash=snapshot["event_head_hash"],
        integrity_status=integrity_status,
        completeness=completeness,
        task_lifecycle=task_lifecycle,
        stage_attempts=tuple(attempts[key] for key in sorted(attempts)),
        invocations=tuple(invocations[key] for key in sorted(invocations)),
        sessions=tuple(sessions),
        evidence_graph=tuple(inventory_by_id[key] for key in sorted(inventory_by_id)),
        model_visible_boundary_index=tuple(boundaries),
        usage_by_basis={
            key: tuple(usage[key]) for key in sorted(usage)
        },
        unsupported_domains=UNSUPPORTED_DOMAINS,
        unknowns=tuple(unknowns),
        diagnostics=tuple(diagnostics),
        snapshot_manifest=snapshot["manifest"],
    )


def render_projection(
    snapshot: dict[str, Any],
    result: ProjectionResult,
    *,
    audiences: Iterable[str] = ("public", "internal"),
) -> dict[str, Any]:
    """Render bounded metadata after verification; never expose evidence paths."""
    allowed = frozenset(audiences)
    if not allowed or not allowed.issubset(AUDIENCES):
        raise TrajectoryError("unsupported R0 audience policy")
    events, _ = _snapshot_events(snapshot)
    availability = {
        item["ref_id"]: item.get("availability", "unavailable")
        for item in result.evidence_graph
    }
    rendered_events: list[dict[str, Any]] = []
    for event in events:
        refs = []
        for ref in event["evidence_refs"]:
            compact = {
                "ref_id": ref["ref_id"],
                "sha256": ref["sha256"],
                "availability": availability.get(
                    ref["ref_id"], "unavailable"
                ),
            }
            if ref["sensitivity"] in allowed:
                compact.update(
                    kind=ref["kind"],
                    size_bytes=ref["size_bytes"],
                    media_type=ref["media_type"],
                    sensitivity=ref["sensitivity"],
                )
            refs.append(compact)
        # Bodies are typed bounded metadata.  Sensitive refs remain compact
        # even when their containing event is otherwise internal.
        rendered_events.append(
            {
                "seq": event["seq"],
                "event_id": event["event_id"],
                "event_type": event["event_type"],
                "recorded_at_ms": event["recorded_at_ms"],
                "task_revision": event["task"]["revision"],
                "run": event["run"],
                "invocation_id": event["invocation_id"],
                "session_ref": event["session_ref"],
                "actor": event["actor"],
                "body": event["body"],
                "evidence_refs": refs,
                "sensitivity": event["sensitivity"],
                "event_hash": event["event_hash"],
            }
        )
    projection = result.to_dict()
    projection["evidence_graph"] = [
        (
            item
            if item.get("sensitivity") in allowed
            else {
                "ref_id": item.get("ref_id"),
                "sha256": item.get("sha256"),
                "availability": item.get("availability", "unavailable"),
            }
        )
        for item in projection["evidence_graph"]
    ]
    return {
        "schema_version": 1,
        "projection": projection,
        "snapshot": {
            "captured_at_ms": snapshot.get("captured_at_ms") if isinstance(snapshot, dict) else None,
            "manifest": snapshot.get("manifest") if isinstance(snapshot, dict) else None,
            "snapshot_digest": snapshot.get("snapshot_digest") if isinstance(snapshot, dict) else None,
        },
        "events": rendered_events,
    }


def projection_bytes(value: dict[str, Any]) -> bytes:
    """Canonical stdout representation for a rendered R0 projection."""
    return canonical_json_bytes(value) + b"\n"
