from __future__ import annotations

import argparse
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .ipc import atomic_write_text, daemon_is_running, enqueue_request


TASK_TYPE_KEYWORDS: dict[str, set[str]] = {
    "propose": {"spec", "design", "draft", "propose", "blueprint"},
    "review": {"review", "audit", "check", "verify"},
    "apply": {"implement", "code", "build", "apply", "fix"},
    "provider-smoke": {"smoke", "provider-smoke"},
}

# Built-in defaults only. A deployment's own high-risk vocabulary — the names of
# its private files, stores, and subsystems — does not belong in this package;
# it is supplied through `risk-rules.yaml` (contract draft at the repository
# root, loader implementation pending). Keep this tuple generic: anything added
# here ships to every user of the engine.
HIGH_RISK_KEYWORDS = (
    "orchestrator/",
    "router/",
    "dispatch/",
    "daemon/",
    "lock/",
    "scheduler/",
    "memory/",
    "persistent-state",
)
MEDIUM_RISK_KEYWORDS = ("publish", "deploy", "webhook", "send", "inbox.md", "recap", "metadata")
STOP_GATE_KEYWORDS = ("publish", "deploy", "send", "delete", "drop")
TRACKED_EXECUTION_PATTERNS = {
    "propose_spec": {
        "type": "propose",
        "profile": Path(__file__).resolve().parent / "profiles" / "propose.yaml",
    },
    "spec_review": {
        "type": "spec-review",
        "profile": Path(__file__).resolve().parent / "profiles" / "spec_review.yaml",
    },
    "codex_implement_claude_review": {
        "type": "apply",
        "profile": Path(__file__).resolve().parent / "profiles" / "codex_implement_claude_review.yaml",
    },
    "claude_apply_codex_review": {
        "type": "apply",
        "profile": Path(__file__).resolve().parent / "profiles" / "claude_apply_codex_review.yaml",
    },
    "provider_smoke": {
        "type": "provider-smoke",
        "profile": Path(__file__).resolve().parent / "profiles" / "provider_smoke.yaml",
    },
    "provider_smoke_gated": {
        "type": "provider-smoke",
        "profile": Path(__file__).resolve().parent / "profiles" / "provider_smoke_gated.yaml",
    },
}
STOP_GATE_REVIEWER_PROFILES = {
    "codex": {
        "owner": "claude",
        "type": "stop-gate",
        "profile": Path(__file__).resolve().parent / "profiles" / "stop_gate_claude.yaml",
    },
    "claude": {
        "owner": "codex",
        "type": "stop-gate",
        "profile": Path(__file__).resolve().parent / "profiles" / "stop_gate_codex.yaml",
    },
}
OPENSPEC_PRESENT_KEYWORDS = (
    "openspec/",
    "specs/",
    "changes/",
    "proposal.md",
    "spec.md",
    "design.md",
    "tasks.md",
    "task_id",
)
FORMAL_CHANGE_KEYWORDS = ("formal change", "architecture change", "breaking change")
TARGET_HINT_KEYWORDS = {
    "orch",
    "orchestrator",
    "router",
    "route",
    "dispatch",
    "daemon",
    "scheduler",
    "memory",
    "openspec",
    "provider",
    "fallback",
    "intake",
    "preflight",
}
GENERIC_SCOPE_WORDS = {
    "a",
    "an",
    "the",
    "to",
    "for",
    "with",
    "and",
    "or",
    "do",
    "thing",
    "bug",
    "issue",
    "task",
    "work",
    "change",
    "changes",
    "please",
}


@dataclass(frozen=True)
class StartFlags:
    task_type: str | None
    scope: str | None
    worktree: Path | None
    approved_spec: Path | None
    effort: str | None
    dry_run: bool


def start_from_args(home: Path, args: argparse.Namespace) -> dict[str, Any]:
    scope = args.scope
    if args.scope_file:
        scope = args.scope_file.read_text(encoding="utf-8").strip()
    flags = StartFlags(
        task_type=args.task_type,
        scope=scope,
        worktree=args.worktree,
        approved_spec=args.approved_spec,
        effort=args.effort,
        dry_run=args.dry_run,
    )
    return run_start(home, args.description, flags)


def start_go_from_args(home: Path, args: argparse.Namespace) -> dict[str, Any]:
    return run_start_go(home, args.task_id)


def start_sync_from_args(home: Path, args: argparse.Namespace) -> dict[str, Any]:
    return run_start_sync(home, args.task_id)


def gate_status_from_args(home: Path, args: argparse.Namespace) -> dict[str, Any]:
    return gate_status(home, args.task_id)


def gate_run_from_args(home: Path, args: argparse.Namespace) -> dict[str, Any]:
    return run_gate_run(home, args.task_id)


def gate_sync_from_args(home: Path, args: argparse.Namespace) -> dict[str, Any]:
    return run_gate_sync(home, args.task_id)


def gate_allow_from_args(home: Path, args: argparse.Namespace) -> dict[str, Any]:
    return run_gate_decision(home, args.task_id, "ALLOW", args.reason)


def gate_block_from_args(home: Path, args: argparse.Namespace) -> dict[str, Any]:
    return run_gate_decision(home, args.task_id, "BLOCK", args.reason)


def run_start(home: Path, description: str, flags: StartFlags) -> dict[str, Any]:
    task_id = uuid.uuid4().hex[:8]
    now = _now()
    tasks_dir = home / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)

    task_record = _build_task_record(task_id, now, description, flags)
    task_path = tasks_dir / f"{task_id}.yaml"
    routing_path = tasks_dir / f"{task_id}-routing.yaml"
    _write_yaml(task_path, task_record)
    _notify(home, "queued", "info", f"task-{task_id} queued, pattern TBD", routing_path)

    preflight = _preflight(task_record, flags)
    routing: dict[str, Any]
    if preflight["status"] != "pass":
        task_record["stage"] = preflight["status"]
        routing = _routing_decision(
            task_id=task_id,
            created_at=_now(),
            description=description,
            task_record=task_record,
            preflight=preflight,
            pattern=None,
            executor=None,
            reviewer=None,
            route_source="rule",
            risk=_risk_from_signals(task_record),
            stop_gate=False,
            complexity="low",
            rationale=f"preflight {preflight['status']}: {preflight['reason']}",
        )
        _write_yaml(task_path, task_record)
        _write_yaml(routing_path, routing)
        sev = "error" if preflight["status"] == "blocked" else "warn"
        _notify(home, preflight["status"], sev, f"task-{task_id} {preflight['status']}: {preflight['reason']}", routing_path)
        return _result(task_id, task_path, routing_path, task_record, routing)

    task_record["stage"] = "route"
    routing = _route(task_id, description, task_record, preflight)
    if flags.dry_run:
        if not routing["auto_start"]:
            task_record["stage"] = "waiting_user"
            _notify(
                home,
                "waiting_user",
                "warn",
                f"task-{task_id} waiting go/no-go (ref: {routing_path})",
                routing_path,
            )
        else:
            task_record["stage"] = "route"
        _write_yaml(task_path, task_record)
        _write_yaml(routing_path, routing)
        return _result(task_id, task_path, routing_path, task_record, routing)

    if not routing["auto_start"]:
        task_record["stage"] = "waiting_user"
        routing["go"] = {
            "required": True,
            "command": f"python3 -m orchestrator start-go {task_id}",
            "reason": _go_required_reason(routing),
        }
        _notify(
            home,
            "waiting_user",
            "warn",
            f"task-{task_id} waiting go/no-go (ref: {routing_path})",
            routing_path,
        )
    else:
        execution = _enqueue_for_routing(home, task_id, task_record, routing, "auto_start=true")
        if execution["status"] == "enqueued":
            task_record["stage"] = "execute"
            _notify(
                home,
                "execute",
                "info",
                f"task-{task_id} enqueued daemon request {execution['request_id']}",
                Path(execution["request_path"]),
            )
        else:
            task_record["stage"] = "blocked"
            routing["auto_start"] = False
            _notify(home, "blocked", "error", f"task-{task_id} blocked: {execution['reason']}", routing_path)
        task_record["execution"] = execution
        routing["execution"] = execution
    _write_yaml(task_path, task_record)
    _write_yaml(routing_path, routing)
    return _result(task_id, task_path, routing_path, task_record, routing)


def run_start_go(home: Path, task_id: str) -> dict[str, Any]:
    task_path = home / "tasks" / f"{task_id}.yaml"
    routing_path = home / "tasks" / f"{task_id}-routing.yaml"
    if not task_path.is_file():
        raise ValueError(f"orch start task not found: {task_id}")
    if not routing_path.is_file():
        raise ValueError(f"routing decision not found for orch start task: {task_id}")

    task_record = _read_yaml(task_path)
    routing = _read_yaml(routing_path)
    if not isinstance(task_record, dict) or not isinstance(routing, dict):
        raise ValueError(f"invalid orch start state for task: {task_id}")

    preflight = routing.get("preflight", {})
    if task_record.get("stage") == "blocked" or preflight.get("status") == "blocked":
        reason = preflight.get("reason") or task_record.get("stage")
        raise ValueError(f"task {task_id} is blocked before execution: {reason}")
    if preflight.get("status") != "pass" or not routing.get("pattern"):
        reason = preflight.get("reason") or "route did not produce an executable pattern"
        raise ValueError(f"task {task_id} cannot start-go from preflight waiting_user: {reason}")
    if task_record.get("stage") != "waiting_user":
        raise ValueError(f"task {task_id} is not waiting for post-route go/no-go (stage={task_record.get('stage')})")

    execution = _enqueue_for_routing(home, task_id, task_record, routing, "explicit start-go")
    if execution["status"] != "enqueued":
        task_record["stage"] = "blocked"
        task_record["execution"] = execution
        routing["execution"] = execution
        _write_yaml(task_path, task_record)
        _write_yaml(routing_path, routing)
        raise ValueError(execution["reason"])

    task_record["stage"] = "execute"
    task_record["execution"] = execution
    routing["execution"] = execution
    _write_yaml(task_path, task_record)
    _write_yaml(routing_path, routing)
    _notify(home, "execute", "info", f"task-{task_id} enqueued daemon request {execution['request_id']}", Path(execution["request_path"]))
    return _result(task_id, task_path, routing_path, task_record, routing)


def run_start_sync(home: Path, task_id: str) -> dict[str, Any]:
    task_path = home / "tasks" / f"{task_id}.yaml"
    routing_path = home / "tasks" / f"{task_id}-routing.yaml"
    if not task_path.is_file():
        raise ValueError(f"orch start task not found: {task_id}")
    if not routing_path.is_file():
        raise ValueError(f"routing decision not found for orch start task: {task_id}")

    task_record = _read_yaml(task_path)
    routing = _read_yaml(routing_path)
    if not isinstance(task_record, dict) or not isinstance(routing, dict):
        raise ValueError(f"invalid orch start state for task: {task_id}")

    request_id = _execution_request_id(task_record, routing)
    if not request_id:
        raise ValueError(f"task {task_id} has no execution request_id to sync")

    result_path = _find_processed_result(home, request_id)
    if result_path is None:
        processing_path = _find_processing_request(home, request_id)
        if processing_path is not None:
            controller_status = _controller_status_for_sync(home, request_id)
            detail = ""
            if controller_status is not None:
                task = controller_status.get("task", {})
                runs = controller_status.get("stage_runs", [])
                running = next((run for run in runs if run.get("status") == "running"), None)
                if isinstance(task, dict):
                    detail += (
                        f"; controller_status={task.get('status')} "
                        f"stage={task.get('current_stage')} owner={task.get('owner')}"
                    )
                if isinstance(running, dict):
                    detail += (
                        f"; elapsed={running.get('running_elapsed_seconds')}s"
                        f"/timeout={running.get('timeout_seconds')}s"
                        f"; log={running.get('log_path')}"
                    )
            raise ValueError(
                f"daemon request still processing for request_id {request_id}: {processing_path}{detail}"
            )
        raise ValueError(f"processed daemon result not found for request_id {request_id}")

    try:
        processed_result = json.loads(result_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"cannot parse processed daemon result {result_path}: {exc}") from exc

    result_path, processed_result = _find_latest_processed_result_for_task(
        home, result_path, processed_result
    )
    processed_request_id = processed_result.get("request_id")
    if isinstance(processed_request_id, str) and processed_request_id:
        request_id = processed_request_id

    existing_execution_result = routing.get("execution_result")
    existing_gate = routing.get("gate")
    execution_result = _execution_result_summary(request_id, result_path, processed_result, routing)
    existing_gate_decision = _gate_decision(task_record, routing)
    decided_gate = None
    if existing_gate_decision is not None and _is_stop_gate_pending(execution_result):
        final_stage = str(existing_gate_decision.get("final_stage") or task_record.get("stage") or "done")
        execution_result["lifecycle_stage"] = final_stage
        execution_result["gate_required"] = False
        base_gate = task_record.get("gate") if isinstance(task_record.get("gate"), dict) else routing.get("gate")
        if isinstance(base_gate, dict):
            decided_gate = dict(base_gate)
            decided_gate.update(
                {
                    "status": "decided",
                    "stage": final_stage,
                    "decision": existing_gate_decision.get("decision"),
                    "decided_at": existing_gate_decision.get("decided_at"),
                    "decision_reason": existing_gate_decision.get("reason"),
                    "decision_artifact_path": existing_gate_decision.get("decision_artifact_path"),
                }
            )
    if (
        _is_stop_gate_pending(execution_result)
        and isinstance(existing_execution_result, dict)
        and _same_execution_result_except_synced_at(existing_execution_result, execution_result)
    ):
        execution_result["synced_at"] = existing_execution_result.get("synced_at")
    task_record["stage"] = execution_result["lifecycle_stage"]
    task_record["execution_result"] = execution_result
    routing["execution_result"] = execution_result
    notify = True
    if decided_gate is not None:
        task_record["gate"] = decided_gate
        routing["gate"] = decided_gate
        notify = False
    elif _is_stop_gate_pending(execution_result):
        gate_summary = _stop_gate_summary(task_id, routing, execution_result)
        review_execution = _gate_review_execution(task_record, routing)
        if isinstance(review_execution, dict):
            gate_summary["review_execution"] = review_execution
        task_record["gate"] = gate_summary
        routing["gate"] = gate_summary
        notify = not (isinstance(existing_gate, dict) and existing_gate == gate_summary)
    _write_yaml(task_path, task_record)
    _write_yaml(routing_path, routing)

    if notify:
        notification_summary = (
            f"task-{task_id} synced daemon result {request_id}: "
            f"controller_task_id={execution_result.get('controller_task_id')} "
            f"controller_status={execution_result.get('controller_status')} "
            f"stop_reason={execution_result.get('stop_reason')}"
        )
        if _is_stop_gate_pending(execution_result):
            notification_summary += " stop-gate approval required"
        _notify(
            home,
            execution_result["lifecycle_stage"],
            _sync_severity(execution_result["lifecycle_stage"]),
            notification_summary,
            result_path,
        )
    return _result(task_id, task_path, routing_path, task_record, routing)


def gate_status(home: Path, task_id: str) -> dict[str, Any]:
    task_path = home / "tasks" / f"{task_id}.yaml"
    routing_path = home / "tasks" / f"{task_id}-routing.yaml"
    if not task_path.is_file():
        raise ValueError(f"orch start task not found: {task_id}")
    if not routing_path.is_file():
        raise ValueError(f"routing decision not found for orch start task: {task_id}")

    task_record = _read_yaml(task_path)
    routing = _read_yaml(routing_path)
    if not isinstance(task_record, dict) or not isinstance(routing, dict):
        raise ValueError(f"invalid orch start state for task: {task_id}")

    return {
        "task_id": task_id,
        "status": task_record.get("stage"),
        "routing_stop_gate": routing.get("stop_gate"),
        "gate": routing.get("gate") or task_record.get("gate"),
        "gate_review_execution": routing.get("gate_review_execution") or task_record.get("gate_review_execution"),
        "gate_review_result": _gate_review_result(task_record, routing),
        "decision": routing.get("gate_decision") or task_record.get("gate_decision"),
        "execution_result": routing.get("execution_result") or task_record.get("execution_result"),
        "task_record": str(task_path),
        "routing_decision": str(routing_path),
    }


def run_gate_run(home: Path, task_id: str) -> dict[str, Any]:
    task_path = home / "tasks" / f"{task_id}.yaml"
    routing_path = home / "tasks" / f"{task_id}-routing.yaml"
    if not task_path.is_file():
        raise ValueError(f"orch start task not found: {task_id}")
    if not routing_path.is_file():
        raise ValueError(f"routing decision not found for orch start task: {task_id}")

    task_record = _read_yaml(task_path)
    routing = _read_yaml(routing_path)
    if not isinstance(task_record, dict) or not isinstance(routing, dict):
        raise ValueError(f"invalid orch start state for task: {task_id}")
    if _gate_decision(task_record, routing) is not None:
        raise ValueError(f"stop-gate for task {task_id} is already decided")
    if _gate_review_execution(task_record, routing) is not None:
        raise ValueError(f"stop-gate reviewer for task {task_id} is already enqueued")

    gate, execution_result = _require_pending_stop_gate(task_id, task_record, routing)
    executor = routing.get("executor") or gate.get("executor")
    reviewer = STOP_GATE_REVIEWER_PROFILES.get(str(executor))
    if reviewer is None:
        raise ValueError(f"unsupported stop-gate executor for cross-provider review: {executor!r}")
    if not daemon_is_running(home):
        raise ValueError(
            "orchestrator daemon is not running; gate-run only enqueues a daemon inbox request "
            "and will not run stop-gate review in-process"
        )

    created_at = _now()
    expected_output_path = home / "tasks" / f"{task_id}-gate-review-output.md"
    input_path = _write_gate_review_input(
        home=home,
        task_id=task_id,
        task_path=task_path,
        routing_path=routing_path,
        task_record=task_record,
        routing=routing,
        gate=gate,
        execution_result=execution_result,
        expected_output_path=expected_output_path,
    )
    request_id = str(uuid.uuid4())
    profile_path = Path(reviewer["profile"]).resolve()
    request = {
        "request_id": request_id,
        "action": "run",
        "type": reviewer["type"],
        "profile": str(profile_path),
        "input": str(input_path.resolve()),
    }
    request_path = enqueue_request(home, request)
    summary = {
        "type": "stop_gate_review_execution",
        "status": "enqueued",
        "created_at": created_at,
        "request_id": request_id,
        "request_path": str(request_path),
        "controller_task_id": request_id,
        "profile": str(profile_path),
        "owner": reviewer["owner"],
        "input_path": str(input_path),
        "expected_output_path": str(expected_output_path),
        "original_request_id": execution_result.get("request_id"),
        "processed_result_path": execution_result.get("processed_result_path"),
        "pattern": routing.get("pattern"),
        "executor": executor,
        "reviewer": routing.get("reviewer"),
        "route_source": routing.get("route_source"),
    }

    task_record["gate_review_execution"] = summary
    routing["gate_review_execution"] = summary
    updated_gate = dict(gate)
    updated_gate["review_execution"] = summary
    task_record["gate"] = updated_gate
    routing["gate"] = updated_gate
    _write_yaml(task_path, task_record)
    _write_yaml(routing_path, routing)
    _notify(
        home,
        "gate-review-enqueued",
        "warn",
        f"task-{task_id} stop-gate reviewer enqueued request {request_id} owner={reviewer['owner']}",
        request_path,
    )
    return _result(task_id, task_path, routing_path, task_record, routing)


def run_gate_sync(home: Path, task_id: str) -> dict[str, Any]:
    task_path = home / "tasks" / f"{task_id}.yaml"
    routing_path = home / "tasks" / f"{task_id}-routing.yaml"
    if not task_path.is_file():
        raise ValueError(f"orch start task not found: {task_id}")
    if not routing_path.is_file():
        raise ValueError(f"routing decision not found for orch start task: {task_id}")

    task_record = _read_yaml(task_path)
    routing = _read_yaml(routing_path)
    if not isinstance(task_record, dict) or not isinstance(routing, dict):
        raise ValueError(f"invalid orch start state for task: {task_id}")
    if _gate_decision(task_record, routing) is not None:
        raise ValueError(f"stop-gate for task {task_id} is already decided")
    if _gate_review_result(task_record, routing) is not None:
        raise ValueError(f"stop-gate review result for task {task_id} is already synced")

    gate, execution_result = _require_pending_stop_gate(task_id, task_record, routing)
    review_execution = _gate_review_execution(task_record, routing)
    if not isinstance(review_execution, dict):
        raise ValueError(f"stop-gate reviewer for task {task_id} has not been enqueued")
    request_id = review_execution.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        raise ValueError(f"stop-gate reviewer for task {task_id} has no request_id")

    result_path = _find_processed_result(home, request_id)
    if result_path is None:
        raise ValueError(f"processed stop-gate reviewer result not found for request_id {request_id}")
    try:
        processed_result = json.loads(result_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"cannot parse processed stop-gate reviewer result {result_path}: {exc}") from exc
    if not isinstance(processed_result, dict):
        raise ValueError(f"malformed stop-gate reviewer result {result_path}: root must be an object")

    outcome = _extract_gate_review_outcome(processed_result, request_id, result_path)
    recommendation = {"allow": "ALLOW", "block": "BLOCK"}[outcome]
    synced_at = _now()
    review_result = _gate_review_result_summary(
        task_id=task_id,
        request_id=request_id,
        result_path=result_path,
        processed_result=processed_result,
        outcome=outcome,
        recommendation=recommendation,
        synced_at=synced_at,
        routing=routing,
        gate=gate,
        execution_result=execution_result,
        review_execution=review_execution,
    )

    updated_gate = dict(gate)
    updated_gate["review_execution"] = review_execution
    updated_gate["review_result"] = review_result
    task_record["stage"] = "waiting_user"
    task_record["gate"] = updated_gate
    task_record["gate_review_result"] = review_result
    routing["gate"] = updated_gate
    routing["gate_review_result"] = review_result
    _write_yaml(task_path, task_record)
    _write_yaml(routing_path, routing)
    _notify(
        home,
        "gate-review-synced",
        "warn" if recommendation == "ALLOW" else "error",
        f"task-{task_id} stop-gate reviewer recommendation {recommendation} result={result_path}",
        result_path,
    )
    return _result(task_id, task_path, routing_path, task_record, routing)


def run_gate_decision(home: Path, task_id: str, decision: str, reason: str | None = None) -> dict[str, Any]:
    if decision not in {"ALLOW", "BLOCK"}:
        raise ValueError(f"unsupported gate decision: {decision}")

    task_path = home / "tasks" / f"{task_id}.yaml"
    routing_path = home / "tasks" / f"{task_id}-routing.yaml"
    decision_path = home / "tasks" / f"{task_id}-gate-decision.yaml"
    if not task_path.is_file():
        raise ValueError(f"orch start task not found: {task_id}")
    if not routing_path.is_file():
        raise ValueError(f"routing decision not found for orch start task: {task_id}")

    task_record = _read_yaml(task_path)
    routing = _read_yaml(routing_path)
    if not isinstance(task_record, dict) or not isinstance(routing, dict):
        raise ValueError(f"invalid orch start state for task: {task_id}")
    if decision_path.exists() or _gate_decision(task_record, routing) is not None:
        raise ValueError(f"stop-gate for task {task_id} is already decided")

    gate, execution_result = _require_pending_stop_gate(task_id, task_record, routing)
    decided_at = _now()
    final_stage = "done" if decision == "ALLOW" else "blocked"
    decision_summary = _gate_decision_summary(
        task_id=task_id,
        decision=decision,
        reason=reason,
        decided_at=decided_at,
        final_stage=final_stage,
        decision_path=decision_path,
        routing=routing,
        gate=gate,
        execution_result=execution_result,
    )

    decided_gate = dict(gate)
    decided_gate.update(
        {
            "status": "decided",
            "decision": decision,
            "decided_at": decided_at,
            "decision_reason": reason,
            "stage": final_stage,
            "decision_artifact_path": str(decision_path),
        }
    )
    task_record["stage"] = final_stage
    task_record["gate"] = decided_gate
    task_record["gate_decision"] = decision_summary
    routing["gate"] = decided_gate
    routing["gate_decision"] = decision_summary

    _write_yaml(decision_path, decision_summary)
    _write_yaml(task_path, task_record)
    _write_yaml(routing_path, routing)
    _notify(
        home,
        f"gate-{decision.lower()}",
        "info" if decision == "ALLOW" else "error",
        f"task-{task_id} stop-gate decision {decision} final_stage={final_stage} reason={reason or '(none)'}",
        decision_path,
    )
    return _result(task_id, task_path, routing_path, task_record, routing)


def _gate_decision(task_record: dict[str, Any], routing: dict[str, Any]) -> dict[str, Any] | None:
    for source in (routing.get("gate_decision"), task_record.get("gate_decision")):
        if isinstance(source, dict):
            return source
    for source in (routing.get("gate"), task_record.get("gate")):
        if isinstance(source, dict) and source.get("status") == "decided":
            return source
    return None


def _gate_review_execution(task_record: dict[str, Any], routing: dict[str, Any]) -> dict[str, Any] | None:
    for source in (routing.get("gate_review_execution"), task_record.get("gate_review_execution")):
        if isinstance(source, dict):
            return source
    for source in (routing.get("gate"), task_record.get("gate")):
        if isinstance(source, dict) and isinstance(source.get("review_execution"), dict):
            return source["review_execution"]
    return None


def _gate_review_result(task_record: dict[str, Any], routing: dict[str, Any]) -> dict[str, Any] | None:
    for source in (routing.get("gate_review_result"), task_record.get("gate_review_result")):
        if isinstance(source, dict):
            return source
    for source in (routing.get("gate"), task_record.get("gate")):
        if isinstance(source, dict) and isinstance(source.get("review_result"), dict):
            return source["review_result"]
    return None


def _require_pending_stop_gate(
    task_id: str,
    task_record: dict[str, Any],
    routing: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    if task_record.get("stage") != "waiting_user":
        raise ValueError(f"task {task_id} is not waiting on a stop-gate (stage={task_record.get('stage')})")
    if routing.get("stop_gate") is not True:
        raise ValueError(f"task {task_id} is not a stop-gate task")

    gate = routing.get("gate") or task_record.get("gate")
    if not isinstance(gate, dict) or gate.get("type") != "stop_gate" or gate.get("status") != "pending":
        raise ValueError(f"task {task_id} has no pending stop-gate summary")

    execution_result = routing.get("execution_result") or task_record.get("execution_result")
    if not isinstance(execution_result, dict) or execution_result.get("gate_required") is not True:
        raise ValueError(f"task {task_id} has no gate-required execution result")
    if execution_result.get("lifecycle_stage") != "waiting_user":
        raise ValueError(f"task {task_id} execution result is not gate-pending")
    if execution_result.get("controller_lifecycle_stage") != "done" or execution_result.get("controller_status") != "done":
        raise ValueError(f"task {task_id} controller execution has not reached done")
    return gate, execution_result


def _extract_gate_review_outcome(result: dict[str, Any], expected_request_id: str, result_path: Path) -> str:
    if result.get("request_id") not in {None, expected_request_id}:
        raise ValueError(
            f"malformed stop-gate reviewer result {result_path}: request_id does not match {expected_request_id}"
        )
    if "error" in result or result.get("status") != "done":
        raise ValueError(f"malformed stop-gate reviewer result {result_path}: reviewer did not finish done")
    stage_runs = result.get("stage_runs")
    transitions = result.get("transitions")
    if not isinstance(stage_runs, list) or not isinstance(transitions, list):
        raise ValueError(f"malformed stop-gate reviewer result {result_path}: missing stage_runs/transitions")

    outcomes: list[str] = []
    for collection_name, collection in (("stage_runs", stage_runs), ("transitions", transitions)):
        for index, item in enumerate(collection):
            if not isinstance(item, dict):
                raise ValueError(
                    f"malformed stop-gate reviewer result {result_path}: {collection_name}[{index}] is not an object"
                )
            outcome = item.get("outcome")
            if isinstance(outcome, str) and outcome:
                outcomes.append(outcome)
    distinct = sorted(set(outcomes))
    if not distinct:
        raise ValueError(f"malformed stop-gate reviewer result {result_path}: missing reviewer outcome")
    if len(distinct) > 1:
        raise ValueError(
            f"malformed stop-gate reviewer result {result_path}: ambiguous reviewer outcomes {', '.join(distinct)}"
        )
    outcome = distinct[0]
    if outcome not in {"allow", "block"}:
        raise ValueError(f"unsupported stop-gate reviewer outcome: {outcome}")
    return outcome


def _gate_review_result_summary(
    *,
    task_id: str,
    request_id: str,
    result_path: Path,
    processed_result: dict[str, Any],
    outcome: str,
    recommendation: str,
    synced_at: str,
    routing: dict[str, Any],
    gate: dict[str, Any],
    execution_result: dict[str, Any],
    review_execution: dict[str, Any],
) -> dict[str, Any]:
    return {
        "type": "stop_gate_review_result",
        "task_id": task_id,
        "synced_at": synced_at,
        "request_id": request_id,
        "processed_result_path": str(result_path),
        "controller_task_id": processed_result.get("task_id") or processed_result.get("request_id") or request_id,
        "controller_status": processed_result.get("status"),
        "stop_reason": processed_result.get("stop_reason"),
        "controller_lifecycle_stage": _lifecycle_stage_from_result(processed_result),
        "outcome": outcome,
        "recommendation": recommendation,
        "stage": "waiting_user",
        "gate_status_before": gate.get("status"),
        "original_request_id": execution_result.get("request_id"),
        "original_processed_result_path": execution_result.get("processed_result_path"),
        "original_controller_task_id": execution_result.get("controller_task_id"),
        "review_request_path": review_execution.get("request_path"),
        "review_input_path": review_execution.get("input_path"),
        "review_expected_output_path": review_execution.get("expected_output_path"),
        "pattern": routing.get("pattern"),
        "executor": routing.get("executor"),
        "reviewer": routing.get("reviewer"),
        "route_source": routing.get("route_source"),
    }


def _gate_decision_summary(
    *,
    task_id: str,
    decision: str,
    reason: str | None,
    decided_at: str,
    final_stage: str,
    decision_path: Path,
    routing: dict[str, Any],
    gate: dict[str, Any],
    execution_result: dict[str, Any],
) -> dict[str, Any]:
    execution = routing.get("execution")
    if not isinstance(execution, dict):
        execution = {}
    return {
        "type": "stop_gate_decision",
        "task_id": task_id,
        "decision": decision,
        "reason": reason,
        "decided_at": decided_at,
        "final_stage": final_stage,
        "decision_artifact_path": str(decision_path),
        "gate_status_before": gate.get("status"),
        "request_id": execution_result.get("request_id"),
        "request_path": execution.get("request_path"),
        "processed_result_path": execution_result.get("processed_result_path"),
        "controller_task_id": execution_result.get("controller_task_id"),
        "controller_status": execution_result.get("controller_status"),
        "controller_lifecycle_stage": execution_result.get("controller_lifecycle_stage"),
        "stop_reason": execution_result.get("stop_reason"),
        "pattern": routing.get("pattern"),
        "profile": execution.get("profile"),
        "executor": routing.get("executor"),
        "reviewer": routing.get("reviewer"),
        "route_source": routing.get("route_source"),
    }


def _write_gate_review_input(
    *,
    home: Path,
    task_id: str,
    task_path: Path,
    routing_path: Path,
    task_record: dict[str, Any],
    routing: dict[str, Any],
    gate: dict[str, Any],
    execution_result: dict[str, Any],
    expected_output_path: Path,
) -> Path:
    input_path = home / "tasks" / f"{task_id}-gate-review-input.md"
    execution = routing.get("execution")
    if not isinstance(execution, dict):
        execution = {}
    lines = [
        f"# orch start stop-gate review input: {task_id}",
        "",
        "## Purpose",
        "",
        "Review whether the original pending stop-gate task is ready for a later manual ALLOW/BLOCK decision.",
        "Do not apply a stop-gate decision from this review. Write the review artifact only.",
        "",
        "## Provenance",
        "",
        f"- Task record: {task_path}",
        f"- Routing decision: {routing_path}",
        f"- Processed execution result: {execution_result.get('processed_result_path')}",
        f"- Original request id: {execution_result.get('request_id')}",
        f"- Original controller task id: {execution_result.get('controller_task_id')}",
        f"- Original profile: {execution.get('profile')}",
        f"- Original pattern: {routing.get('pattern')}",
        f"- Original executor: {routing.get('executor')}",
        f"- Original reviewer: {routing.get('reviewer')}",
        f"- Route source: {routing.get('route_source')}",
        f"- Expected review output path: {expected_output_path}",
        "",
        "## Expected Output",
        "",
        "Write a concise stop-gate review to the expected output path using this exact section schema:",
        "",
        "```md",
        "# Stop-Gate Review",
        "",
        "## Recommendation",
        "",
        "- Recommendation: ALLOW | BLOCK",
        "- Typed outcome: allow | block",
        "- Reason: one sentence",
        "",
        "## Findings",
        "",
        "- High: none | list blocking issues with file/artifact references",
        "- Medium: none | list blocking issues with file/artifact references",
        "- Low: none | list nonblocking notes",
        "",
        "## Evidence",
        "",
        "- Task record reviewed: <path>",
        "- Routing decision reviewed: <path>",
        "- Processed result reviewed: <path>",
        "- Tests or artifacts checked: <summary>",
        "",
        "## Residual Risk",
        "",
        "- <remaining risk or none>",
        "",
        "## Manual Next Step",
        "",
        "- Run gate-allow if Recommendation is ALLOW; run gate-block or request repair if Recommendation is BLOCK.",
        "```",
        "",
        "The typed outcome printed by the reviewer process is the machine-readable source of truth.",
        "The review artifact schema is for human review, traceability, and future UI summarization.",
        "",
        "## Gate Summary",
        "",
        "```json",
        json.dumps(gate, ensure_ascii=False, indent=2),
        "```",
        "",
        "## Execution Result Summary",
        "",
        "```json",
        json.dumps(execution_result, ensure_ascii=False, indent=2),
        "```",
        "",
        "## Routing Decision",
        "",
        "```json",
        json.dumps(routing, ensure_ascii=False, indent=2),
        "```",
        "",
        "## Task Record",
        "",
        "```json",
        json.dumps(task_record, ensure_ascii=False, indent=2),
        "```",
        "",
    ]
    input_path.parent.mkdir(parents=True, exist_ok=True)
    input_path.write_text("\n".join(lines), encoding="utf-8")
    return input_path


def _execution_request_id(task_record: dict[str, Any], routing: dict[str, Any]) -> str | None:
    for source in (routing.get("execution"), task_record.get("execution")):
        if isinstance(source, dict):
            request_id = source.get("request_id")
            if isinstance(request_id, str) and request_id:
                return request_id
    return None


def _find_processed_result(home: Path, request_id: str) -> Path | None:
    processed = home / "processed"
    matches = sorted(processed.glob(f"*{request_id}*.result.json"))
    if not matches:
        return None
    return matches[-1]


def _find_latest_processed_result_for_task(
    home: Path, seed_path: Path, seed_result: dict[str, Any]
) -> tuple[Path, dict[str, Any]]:
    controller_task_id = seed_result.get("task_id")
    if not isinstance(controller_task_id, str) or not controller_task_id:
        return seed_path, seed_result

    latest_path = seed_path
    latest_result = seed_result
    latest_key = (seed_path.stat().st_mtime_ns, seed_path.name)
    for candidate in (home / "processed").glob("*.result.json"):
        if candidate == seed_path:
            continue
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if payload.get("task_id") != controller_task_id:
            continue
        candidate_key = (candidate.stat().st_mtime_ns, candidate.name)
        if candidate_key > latest_key:
            latest_path = candidate
            latest_result = payload
            latest_key = candidate_key
    return latest_path, latest_result


def _find_processing_request(home: Path, request_id: str) -> Path | None:
    processing = home / "processing"
    matches = sorted(processing.glob(f"*{request_id}*.json"))
    if not matches:
        return None
    return matches[-1]


def _controller_status_for_sync(home: Path, request_id: str) -> dict[str, Any] | None:
    try:
        from .controller import Controller

        controller = Controller(home, read_only=True)
        try:
            return controller.status(request_id)
        finally:
            controller.close()
    except Exception:
        return None


def _execution_result_summary(
    request_id: str,
    result_path: Path,
    result: dict[str, Any],
    routing: dict[str, Any],
) -> dict[str, Any]:
    controller_status = result.get("status")
    stop_reason = result.get("stop_reason")
    controller_lifecycle_stage = _lifecycle_stage_from_result(result)
    lifecycle_stage = _start_lifecycle_stage(controller_lifecycle_stage, routing)
    return {
        "synced_at": _now(),
        "request_id": request_id,
        "processed_result_path": str(result_path),
        "controller_task_id": result.get("task_id") or result.get("request_id") or request_id,
        "controller_status": controller_status,
        "stop_reason": stop_reason,
        "evidence_path": result.get("evidence_path"),
        "controller_lifecycle_stage": controller_lifecycle_stage,
        "lifecycle_stage": lifecycle_stage,
        "gate_required": _gate_required(controller_lifecycle_stage, routing),
        "error": result.get("error"),
    }


def _start_lifecycle_stage(controller_stage: str, routing: dict[str, Any]) -> str:
    if _gate_required(controller_stage, routing):
        return "waiting_user"
    return controller_stage


def _gate_required(controller_stage: str, routing: dict[str, Any]) -> bool:
    return controller_stage == "done" and routing.get("stop_gate") is True


def _is_stop_gate_pending(execution_result: dict[str, Any]) -> bool:
    return execution_result.get("gate_required") is True and execution_result.get("lifecycle_stage") == "waiting_user"


def _same_execution_result_except_synced_at(left: dict[str, Any], right: dict[str, Any]) -> bool:
    keys = {
        "request_id",
        "processed_result_path",
        "controller_task_id",
        "controller_status",
        "stop_reason",
        "controller_lifecycle_stage",
        "lifecycle_stage",
        "gate_required",
        "error",
        "evidence_path",
    }
    return all(left.get(key) == right.get(key) for key in keys)


def _stop_gate_summary(task_id: str, routing: dict[str, Any], execution_result: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "stop_gate",
        "status": "pending",
        "stage": "waiting_user",
        "task_id": task_id,
        "reason": "routing.stop_gate=true and controller result is done",
        "required_action": "future gate approval handling",
        "request_id": execution_result.get("request_id"),
        "processed_result_path": execution_result.get("processed_result_path"),
        "controller_task_id": execution_result.get("controller_task_id"),
        "controller_status": execution_result.get("controller_status"),
        "controller_lifecycle_stage": execution_result.get("controller_lifecycle_stage"),
        "pattern": routing.get("pattern"),
        "executor": routing.get("executor"),
        "reviewer": routing.get("reviewer"),
        "route_source": routing.get("route_source"),
    }


def _lifecycle_stage_from_result(result: dict[str, Any]) -> str:
    if "error" in result:
        return "failed"
    status = result.get("status")
    stop_reason = result.get("stop_reason")
    if status == "done":
        return "done"
    if status == "blocked":
        return "blocked"
    if status == "paused":
        return "paused"
    if status in {"waiting_user", "round_cap"} or stop_reason == "round_cap":
        return "waiting_user"
    if status == "failed":
        return "failed"
    return "failed"


def _sync_severity(stage: str) -> str:
    if stage == "done":
        return "info"
    if stage in {"waiting_user", "paused"}:
        return "warn"
    return "error"


def _build_task_record(task_id: str, now: str, description: str, flags: StartFlags) -> dict[str, Any]:
    text = _combined_text(description, flags.scope)
    task_type = flags.task_type or _infer_task_type(description)
    scope = flags.scope or _infer_scope(description)
    scope_ambiguous = _scope_ambiguous(description, flags.scope)
    approved_spec = str(flags.approved_spec) if flags.approved_spec else None
    worktree = str(flags.worktree) if flags.worktree else None
    signals = _signals(text, task_type, bool(flags.approved_spec), scope_ambiguous)
    return {
        "task_id": task_id,
        "created_at": now,
        "task_description": description,
        "task_type_hint": task_type,
        "scope": scope,
        "scope_ambiguous": scope_ambiguous,
        "flags": {
            "effort": flags.effort,
            "dry_run": flags.dry_run,
            "approved_spec": approved_spec,
            "worktree": worktree,
        },
        "signals": signals,
        "stage": "intake",
    }


def _infer_task_type(description: str) -> str:
    words = set(_words(description))
    for task_type, keywords in TASK_TYPE_KEYWORDS.items():
        if words & keywords:
            return task_type
    return "unknown"


def _infer_scope(description: str) -> str | None:
    if _has_concrete_target(description):
        return description.strip()
    return None


def _scope_ambiguous(description: str, explicit_scope: str | None) -> bool:
    if explicit_scope:
        return False
    if _has_concrete_target(description):
        return False
    tokens = _words(description)
    if len(tokens) > 15:
        return False
    non_type_tokens = [
        token
        for token in tokens
        if token not in _all_task_type_keywords() and token not in GENERIC_SCOPE_WORDS
    ]
    return not non_type_tokens


def _has_concrete_target(text: str) -> bool:
    lowered = text.lower()
    if re.search(r"(?:^|\s)(?:[a-z0-9_.-]+/)+[a-z0-9_.-]*(?:\s|$)", lowered):
        return True
    if re.search(r"\b[\w.-]+\.(?:py|sh|md|yaml|yml|json|db|ts|tsx|js|mjs)\b", lowered):
        return True
    if re.search(r"\b(?:b\d+|[0-9a-f]{8}-[0-9a-f-]{27,})\b", lowered):
        return True
    words = set(_words(lowered))
    return bool(words & TARGET_HINT_KEYWORDS)


def _signals(text: str, task_type: str, approved_spec_given: bool, scope_ambiguous: bool) -> list[dict[str, Any]]:
    lowered = text.lower()
    path_high = any(keyword in lowered for keyword in HIGH_RISK_KEYWORDS)
    path_medium = any(keyword in lowered for keyword in MEDIUM_RISK_KEYWORDS)
    openspec_present = approved_spec_given or any(keyword in lowered for keyword in OPENSPEC_PRESENT_KEYWORDS)
    openspec_required = (
        task_type == "apply"
        or any(keyword in lowered for keyword in FORMAL_CHANGE_KEYWORDS)
        or path_high
    )
    return [
        {"name": "task_type_keyword", "value": task_type, "source": "deterministic"},
        {"name": "scope_clarity", "value": "ambiguous" if scope_ambiguous else "clear", "source": "deterministic"},
        {"name": "path_touches_high_risk", "value": path_high, "source": "deterministic"},
        {"name": "path_touches_medium_risk", "value": path_medium, "source": "deterministic"},
        {"name": "openspec_required", "value": openspec_required, "source": "deterministic"},
        {"name": "openspec_present", "value": openspec_present, "source": "deterministic"},
    ]


def _preflight(task_record: dict[str, Any], flags: StartFlags) -> dict[str, str | None]:
    task_type = task_record["task_type_hint"]
    signal_map = _signal_map(task_record)
    if task_record["scope_ambiguous"]:
        return {
            "status": "blocked",
            "reason": "scope unclear - provide --scope or describe the target more specifically",
        }
    if task_type == "apply" and not flags.approved_spec:
        return {"status": "waiting_user", "reason": "apply requires approved spec: --approved-spec <path>"}
    if task_type == "apply" and not flags.worktree:
        return {"status": "blocked", "reason": "apply requires isolated worktree: --worktree <path>"}
    if flags.approved_spec and not flags.approved_spec.exists():
        return {"status": "blocked", "reason": f"approved spec not found: {flags.approved_spec}"}
    if flags.worktree and not flags.worktree.exists():
        return {"status": "blocked", "reason": f"worktree path not found: {flags.worktree}"}
    if signal_map["openspec_required"] and not signal_map["openspec_present"]:
        return {
            "status": "waiting_user",
            "reason": "formal change detected but no OpenSpec context found - provide spec file or task_id",
        }
    if task_type == "apply" and signal_map["openspec_required"] and flags.approved_spec and not _spec_is_approved(flags.approved_spec):
        return {"status": "waiting_user", "reason": f"apply requires approved OpenSpec: {flags.approved_spec} not yet marked approved"}
    return {"status": "pass", "reason": None}


def _route(task_id: str, description: str, task_record: dict[str, Any], preflight: dict[str, str | None]) -> dict[str, Any]:
    task_type = task_record["task_type_hint"]
    text = _combined_text(description, task_record.get("scope"))
    route_source = _route_source(text, task_type)
    pattern, executor, reviewer = _pattern(task_type, text)
    risk = _risk_from_signals(task_record)
    stop_gate = _stop_gate(text, risk)
    complexity = _complexity(task_type, risk, route_source)
    routing = _routing_decision(
        task_id=task_id,
        created_at=_now(),
        description=description,
        task_record=task_record,
        preflight=preflight,
        pattern=pattern,
        executor=executor,
        reviewer=reviewer,
        route_source=route_source,
        risk=risk,
        stop_gate=stop_gate,
        complexity=complexity,
        rationale=_rationale(task_type, pattern, executor, reviewer, route_source, risk, complexity),
    )
    routing["auto_start"] = _auto_start(preflight["status"], risk, route_source)
    return routing


def _pattern(task_type: str, text: str) -> tuple[str | None, str | None, str | None]:
    if task_type == "propose":
        return "propose_spec", "claude", "codex"
    if task_type == "review":
        return "spec_review", "codex", "claude"
    if task_type == "apply":
        lowered = text.lower()
        if "claude implement" in lowered or "executor=claude" in lowered or "let claude" in lowered:
            return "claude_apply_codex_review", "claude", "codex"
        return "codex_implement_claude_review", "codex", "claude"
    if task_type == "provider-smoke":
        lowered = text.lower()
        if "gated" in lowered or "stop-gate" in lowered or "stop gate" in lowered:
            return "provider_smoke_gated", "claude", "codex"
        return "provider_smoke", "claude", "codex"
    return None, None, None


def _risk_from_signals(task_record: dict[str, Any]) -> dict[str, str]:
    signal_map = _signal_map(task_record)
    implementation = "low"
    detection_cost = "low"
    if signal_map["path_touches_high_risk"]:
        implementation = "high"
    elif signal_map["path_touches_medium_risk"]:
        implementation = "medium"
    text = _combined_text(task_record["task_description"], task_record.get("scope")).lower()
    if any(keyword in text for keyword in ("memory/", "user.md", "soul.md", "memory.md")):
        detection_cost = "high"
    if signal_map["openspec_required"] and not signal_map["openspec_present"]:
        semantic = "high"
    elif signal_map["openspec_required"] and signal_map["openspec_present"]:
        semantic = "low" if _approved_spec_text(task_record) else "medium"
    else:
        semantic = "low"
    return {"semantic": semantic, "implementation": implementation, "detection_cost": detection_cost}


def _approved_spec_text(task_record: dict[str, Any]) -> bool:
    approved = task_record.get("flags", {}).get("approved_spec")
    if not approved:
        return False
    return _spec_is_approved(Path(approved))


def _stop_gate(text: str, risk: dict[str, str]) -> bool:
    lowered = text.lower()
    return (
        ("smoke" in lowered and ("gated" in lowered or "stop-gate" in lowered or "stop gate" in lowered))
        or
        risk["implementation"] == "high"
        or any(keyword in lowered for keyword in ("memory/", "daemon/", "dispatch/", "router/", "scheduler/"))
        or any(keyword in lowered for keyword in STOP_GATE_KEYWORDS)
    )


def _route_source(text: str, task_type: str) -> str:
    lowered = text.lower()
    if "route_source=mixed" in lowered or "mixed route" in lowered:
        return "mixed"
    if "route_source=model-adjudicated" in lowered or "model adjudication" in lowered:
        return "model-adjudicated"
    if task_type == "unknown":
        return "model-adjudicated"
    return "rule"


def _complexity(task_type: str, risk: dict[str, str], route_source: str) -> str:
    if "high" in risk.values() or (task_type == "apply" and risk["implementation"] == "high"):
        return "high"
    if route_source in {"mixed", "model-adjudicated"}:
        return "medium"
    if task_type == "apply":
        return "medium"
    return "low"


def _budget(complexity: str) -> dict[str, Any]:
    limits = {
        "low": (1, 4),
        "medium": (2, 6),
        "high": (3, 8),
    }
    max_repair_rounds, max_agent_turns = limits[complexity]
    return {
        "max_repair_rounds": max_repair_rounds,
        "max_agent_turns": max_agent_turns,
        "require_delta_repair": True,
        "require_delta_review": True,
        "stop_on_repeated_high": True,
        "pause_on_provider_quota_low": True,
    }


def _auto_start(preflight_status: str | None, risk: dict[str, str], route_source: str) -> bool:
    if preflight_status in {"blocked", "waiting_user"}:
        return False
    if risk["implementation"] == "high" or risk["semantic"] == "high":
        return False
    if route_source in {"mixed", "model-adjudicated"}:
        return False
    return True


def _routing_decision(
    *,
    task_id: str,
    created_at: str,
    description: str,
    task_record: dict[str, Any],
    preflight: dict[str, str | None],
    pattern: str | None,
    executor: str | None,
    reviewer: str | None,
    route_source: str,
    risk: dict[str, str],
    stop_gate: bool,
    complexity: str,
    rationale: str,
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "created_at": created_at,
        "task_summary": description.strip(),
        "pattern": pattern,
        "executor": executor,
        "reviewer": reviewer,
        "stop_gate": stop_gate,
        "risk": risk,
        "complexity": complexity,
        "budget": _budget(complexity),
        "model_policy": {
            "claude_default": "claude-opus-5",
            "sonnet_allowed": False,
            "ollama_role": "helper_only",
            "tribunal": "disabled_v1",
        },
        "auto_start": _auto_start(preflight["status"], risk, route_source),
        "preflight": preflight,
        "route_source": route_source,
        "signals": task_record["signals"],
        "rationale": rationale,
        "start_condition": "preflight pass + auto_start=true",
        "stop_condition": _stop_condition(pattern),
    }


def _rationale(
    task_type: str,
    pattern: str | None,
    executor: str | None,
    reviewer: str | None,
    route_source: str,
    risk: dict[str, str],
    complexity: str,
) -> str:
    if pattern is None:
        return f"task_type={task_type} requires model adjudication; v1 stub records no executable pattern."
    return (
        f"task_type={task_type} selects {pattern}; cross-provider executor={executor}, reviewer={reviewer}; "
        f"route_source={route_source}; risk={risk}; complexity={complexity}."
    )


def _stop_condition(pattern: str | None) -> str:
    if pattern == "propose_spec":
        return "Codex spec-review passes with no High-level gap"
    if pattern == "spec_review":
        return "Claude review response recorded"
    if pattern in {"codex_implement_claude_review", "claude_apply_codex_review"}:
        return "Implementation and cross-provider review complete"
    if pattern in {"provider_smoke", "provider_smoke_gated"}:
        return "Bounded real-provider smoke reaches done; gated smoke then awaits stop-gate decision"
    return "routing requires user decision"


def _go_required_reason(routing: dict[str, Any]) -> str:
    risk = routing.get("risk", {})
    if risk.get("implementation") == "high":
        return "implementation risk is high"
    if risk.get("semantic") == "high":
        return "semantic risk is high"
    if routing.get("route_source") != "rule":
        return f"route_source={routing.get('route_source')} requires explicit approval"
    return "auto_start=false requires explicit approval"


def _enqueue_for_routing(
    home: Path,
    task_id: str,
    task_record: dict[str, Any],
    routing: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    pattern = routing.get("pattern")
    tracked = TRACKED_EXECUTION_PATTERNS.get(pattern)
    if tracked is None:
        return {
            "status": "blocked",
            "reason": f"no tracked profile for pattern {pattern!r}",
            "pattern": pattern,
        }

    input_path = _write_execution_input(home, task_id, task_record, routing, reason)
    request_id = str(uuid.uuid4())
    request = {
        "request_id": request_id,
        "action": "run",
        "type": tracked["type"],
        "profile": str(Path(tracked["profile"]).resolve()),
        "input": str(input_path.resolve()),
    }
    # worktree 有給就一路帶到 controller：executor 會被關在裡面跑，且拿不到推送憑證。
    worktree = (task_record.get("flags") or {}).get("worktree")
    if worktree:
        request["workspace"] = str(Path(worktree).expanduser().resolve())
    request_path = enqueue_request(home, request)
    return {
        "status": "enqueued",
        "reason": reason,
        "request_id": request_id,
        "request_path": str(request_path),
        "controller_task_id": request_id,
        "pattern": pattern,
        "type": request["type"],
        "profile": request["profile"],
        "input": request["input"],
    }


def _write_execution_input(
    home: Path,
    task_id: str,
    task_record: dict[str, Any],
    routing: dict[str, Any],
    reason: str,
) -> Path:
    input_path = home / "tasks" / f"{task_id}-execution-input.md"
    scope = task_record.get("scope") or "(none)"
    lines = [
        f"# orch start execution input: {task_id}",
        "",
        f"- Reason: {reason}",
        f"- Pattern: {routing.get('pattern')}",
        f"- Executor: {routing.get('executor')}",
        f"- Reviewer: {routing.get('reviewer')}",
        f"- Route source: {routing.get('route_source')}",
        f"- Stop gate: {routing.get('stop_gate')}",
        f"- Complexity: {routing.get('complexity')}",
        "",
        "## Task",
        "",
        str(task_record.get("task_description", "")).strip(),
        "",
        "## Scope",
        "",
        str(scope),
        "",
        "## Routing Decision",
        "",
        "```json",
        json.dumps(routing, ensure_ascii=False, indent=2),
        "```",
        "",
    ]
    input_path.parent.mkdir(parents=True, exist_ok=True)
    input_path.write_text("\n".join(lines), encoding="utf-8")
    return input_path


def _result(
    task_id: str,
    task_path: Path,
    routing_path: Path,
    task_record: dict[str, Any],
    routing: dict[str, Any],
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "status": task_record["stage"],
        "task_record": str(task_path),
        "routing_decision": str(routing_path),
        "routing": routing,
    }


def _spec_is_approved(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8").lower()
    except OSError:
        return False
    return bool(re.search(r"\b(approved|ready|accepted|final)\b", text))


def _signal_map(task_record: dict[str, Any]) -> dict[str, Any]:
    return {signal["name"]: signal["value"] for signal in task_record["signals"]}


def _combined_text(description: str, scope: str | None) -> str:
    return " ".join(part for part in (description, scope) if part)


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9_]+", text.lower())


def _all_task_type_keywords() -> set[str]:
    return set().union(*TASK_TYPE_KEYWORDS.values())


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _notify(home: Path, notification_type: str, sev: str, summary: str, ref: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    entry = f"- {_now()} type={notification_type} sev={sev} ref={ref}: {summary}\n"
    with (home / "inbox.md").open("a", encoding="utf-8") as handle:
        handle.write(entry)


def _write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, _to_yaml(data))


def _read_yaml(path: Path) -> Any:
    lines = [line.rstrip("\n") for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not lines:
        return {}
    value, index = _parse_yaml_block(lines, 0, _line_indent(lines[0]))
    if index != len(lines):
        raise ValueError(f"cannot parse YAML artifact: {path}")
    return value


def _parse_yaml_block(lines: list[str], index: int, indent: int) -> tuple[Any, int]:
    if index >= len(lines):
        return {}, index
    stripped = lines[index][indent:]
    if stripped.startswith("-"):
        return _parse_yaml_list(lines, index, indent)
    return _parse_yaml_dict(lines, index, indent)


def _parse_yaml_dict(lines: list[str], index: int, indent: int) -> tuple[dict[str, Any], int]:
    result: dict[str, Any] = {}
    while index < len(lines):
        line = lines[index]
        current_indent = _line_indent(line)
        if current_indent < indent:
            break
        if current_indent > indent:
            raise ValueError(f"unexpected YAML indentation: {line!r}")
        stripped = line[indent:]
        if stripped.startswith("-"):
            break
        if ":" not in stripped:
            raise ValueError(f"invalid YAML mapping line: {line!r}")
        key, raw_value = stripped.split(":", 1)
        if raw_value.strip():
            result[key] = _parse_yaml_scalar(raw_value.strip())
            index += 1
        else:
            index += 1
            if index >= len(lines) or _line_indent(lines[index]) <= indent:
                result[key] = {}
            else:
                result[key], index = _parse_yaml_block(lines, index, _line_indent(lines[index]))
    return result, index


def _parse_yaml_list(lines: list[str], index: int, indent: int) -> tuple[list[Any], int]:
    result: list[Any] = []
    while index < len(lines):
        line = lines[index]
        current_indent = _line_indent(line)
        if current_indent < indent:
            break
        if current_indent != indent:
            raise ValueError(f"unexpected YAML list indentation: {line!r}")
        stripped = line[indent:]
        if not stripped.startswith("-"):
            break
        raw_value = stripped[1:].strip()
        if raw_value:
            result.append(_parse_yaml_scalar(raw_value))
            index += 1
        else:
            index += 1
            if index >= len(lines) or _line_indent(lines[index]) <= indent:
                result.append(None)
            else:
                item, index = _parse_yaml_block(lines, index, _line_indent(lines[index]))
                result.append(item)
    return result, index


def _line_indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _parse_yaml_scalar(value: str) -> Any:
    if value == "null":
        return None
    if value == "true":
        return True
    if value == "false":
        return False
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        try:
            return int(value)
        except ValueError:
            try:
                return float(value)
            except ValueError:
                return value


def _to_yaml(value: Any, indent: int = 0) -> str:
    lines = _yaml_lines(value, indent)
    return "\n".join(lines) + "\n"


def _yaml_lines(value: Any, indent: int) -> list[str]:
    prefix = " " * indent
    if isinstance(value, dict):
        lines: list[str] = []
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                lines.append(f"{prefix}{key}:")
                lines.extend(_yaml_lines(item, indent + 2))
            else:
                lines.append(f"{prefix}{key}: {_yaml_scalar(item)}")
        return lines
    if isinstance(value, list):
        lines = []
        for item in value:
            if isinstance(item, dict):
                lines.append(f"{prefix}-")
                lines.extend(_yaml_lines(item, indent + 2))
            elif isinstance(item, list):
                lines.append(f"{prefix}-")
                lines.extend(_yaml_lines(item, indent + 2))
            else:
                lines.append(f"{prefix}- {_yaml_scalar(item)}")
        return lines
    return [f"{prefix}{_yaml_scalar(value)}"]


def _yaml_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)
