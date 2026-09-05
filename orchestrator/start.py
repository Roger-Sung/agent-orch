from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .containment import extra_write_roots_from_env, protected_roots_from_env, sandbox_available
from .ipc import atomic_write_text, daemon_is_running, enqueue_request
from .profile import ProfileError, load_profile
from .risk_rules import load_risk_rules
from .runner import (
    ENVELOPE_AXES,
    ENVELOPE_BEGIN,
    ENVELOPE_END,
    ENVELOPE_ENUM_AXIS,
    ENVELOPE_SCHEMA_VERSION,
    ENVELOPE_SOURCE_DEFAULT,
    ENVELOPE_SOURCE_ENGINE,
    ENVELOPE_SOURCE_REQUIREMENT,
    ENVELOPE_STATES,
    SCOPE_EXPANSION_USER_DECISION,
    EnvelopeError,
    SubprocessRunner,
    allow_unsandboxed_requested,
    envelope_block_text,
    extract_envelope,
    provider_command,
    render_envelope_block,
)


PROFILES_DIR_ENV = "ORCH_PROFILES_DIR"


def profiles_dir() -> Path:
    """Where the tracked execution patterns look for their stage machines.

    Defaults to the profiles shipped with the package. A deployment that keeps
    its own prompts — which is the expected case, since a prompt encodes what
    *that* system wants a stage to do — points ORCH_PROFILES_DIR at its own
    directory. Without this, using the engine as an upstream dependency would
    silently run the generic example profiles.

    A configured directory that does not exist is an error rather than a
    silent fallback: falling back would run prompts the operator did not
    choose, which is exactly the failure worth being noisy about.
    """
    configured = os.environ.get(PROFILES_DIR_ENV)
    if not configured:
        return Path(__file__).resolve().parent / "profiles"
    path = Path(os.path.expanduser(configured))
    if not path.is_dir():
        raise ValueError(f"{PROFILES_DIR_ENV} is not a directory: {path}")
    return path


TASK_TYPE_KEYWORDS: dict[str, set[str]] = {
    "propose": {"spec", "design", "draft", "propose", "blueprint"},
    "review": {"review", "audit", "check", "verify"},
    "apply": {"implement", "code", "build", "apply", "fix"},
    "provider-smoke": {"smoke", "provider-smoke"},
}

# Built-in defaults only. A deployment's own high-risk vocabulary — the names of
# its private files, stores, and subsystems — does not belong in this package;
# it is supplied through `risk-rules.yaml` (contract draft at the repository
# root; the loader lives in risk_rules.py). Keep this tuple generic: anything added
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
def _tracked_execution_patterns() -> dict[str, dict[str, object]]:
    return {
        "propose_spec": {
            "type": "propose",
            "profile": profiles_dir() / "propose.yaml",
        },
        "spec_review": {
            "type": "spec-review",
            "profile": profiles_dir() / "spec_review.yaml",
        },
        "codex_implement_claude_review": {
            "type": "apply",
            "profile": profiles_dir() / "codex_implement_claude_review.yaml",
        },
        "claude_apply_codex_review": {
            "type": "apply",
            "profile": profiles_dir() / "claude_apply_codex_review.yaml",
        },
        "provider_smoke": {
            "type": "provider-smoke",
            "profile": profiles_dir() / "provider_smoke.yaml",
        },
        "provider_smoke_gated": {
            "type": "provider-smoke",
            "profile": profiles_dir() / "provider_smoke_gated.yaml",
        },
    }
def _stop_gate_reviewer_profiles() -> dict[str, dict[str, object]]:
    return {
        "codex": {
            "owner": "claude",
            "type": "stop-gate",
            "profile": profiles_dir() / "stop_gate_claude.yaml",
        },
        "claude": {
            "owner": "codex",
            "type": "stop-gate",
            "profile": profiles_dir() / "stop_gate_codex.yaml",
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
    #: Explicit executor choice for apply tasks. The keyword forms in the brief
    #: ("executor=codex", "codex implement", "let codex") remain a fallback, but
    #: a convention hidden inside free text is exactly what a second operator
    #: misses; the flag states it, and it wins over the keywords.
    executor: str | None = None


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
        executor=getattr(args, "executor", None),
    )
    return run_start(home, args.description, flags)


def start_go_from_args(home: Path, args: argparse.Namespace) -> dict[str, Any]:
    return run_start_go(home, args.task_id)


def start_sync_from_args(home: Path, args: argparse.Namespace) -> dict[str, Any]:
    return run_start_sync(home, args.task_id)


def read_start_status(home: Path, task_id: str) -> dict[str, Any]:
    """Read an ``orch start`` lifecycle task without mutating or syncing it."""
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
    return _result(task_id, task_path, routing_path, task_record, routing)


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
        outcome = _result(task_id, task_path, routing_path, task_record, routing)
        # Everything an operator would otherwise reverse-engineer from source
        # before daring to start: stages and owners, the commands and models
        # that would actually run, the workspace, and the containment inputs.
        outcome["plan"] = _execution_plan(task_record, routing)
        return outcome

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
        elif execution["status"] == "waiting_user":
            # An unresolved interpretation envelope is not a fault: it is the
            # operator's question to answer. It takes the existing intake stop,
            # so `start-go` refuses for the existing preflight reason until the
            # requirement is restated and `orch start` is re-run.
            task_record["stage"] = "waiting_user"
            routing["auto_start"] = False
            routing["preflight"] = {"status": "waiting_user", "reason": execution["reason"]}
            _notify(
                home,
                "waiting_user",
                "warn",
                f"task-{task_id} waiting_user: {execution['reason']}",
                routing_path,
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
    if execution["status"] == "waiting_user":
        # Same intake stop as the auto-start path: the task stays at
        # waiting_user with the reason on the preflight record, and a repeated
        # start-go refuses with the existing preflight refusal.
        task_record["stage"] = "waiting_user"
        task_record["execution"] = execution
        routing["preflight"] = {"status": "waiting_user", "reason": execution["reason"]}
        routing["auto_start"] = False
        routing["execution"] = execution
        _write_yaml(task_path, task_record)
        _write_yaml(routing_path, routing)
        _notify(
            home,
            "waiting_user",
            "warn",
            f"task-{task_id} waiting_user: {execution['reason']}",
            routing_path,
        )
        raise ValueError(execution["reason"])
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
    reviewer = _stop_gate_reviewer_profiles().get(str(executor))
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

    result_path, processed_result = _find_latest_processed_result_for_task(
        home, result_path, processed_result
    )
    processed_request_id = processed_result.get("request_id")
    if isinstance(processed_request_id, str) and processed_request_id:
        request_id = processed_request_id

    if _is_pending_user_decision(processed_result):
        # A held gate reviewer is not a malformed result. Nothing is recorded as
        # a recommendation, no ALLOW and no BLOCK, and `gate` keeps its pending
        # status, so gate-allow and gate-block remain available afterwards.
        pending = {
            "type": "stop_gate_review_pending",
            "task_id": task_id,
            "observed_at": _now(),
            "request_id": request_id,
            "processed_result_path": str(result_path),
            "controller_status": processed_result.get("status"),
            "stop_reason": processed_result.get("stop_reason"),
            "reason": "stop-gate reviewer printed needs_user_decision; the gate is still undecided",
        }
        updated_gate = dict(gate)
        updated_gate["review_execution"] = review_execution
        updated_gate["review_pending"] = pending
        task_record["stage"] = "waiting_user"
        task_record["gate"] = updated_gate
        routing["gate"] = updated_gate
        _write_yaml(task_path, task_record)
        _write_yaml(routing_path, routing)
        _notify(
            home,
            "gate-review-pending",
            "warn",
            f"task-{task_id} stop-gate reviewer requires a user decision; gate still undecided",
            result_path,
        )
        return _result(task_id, task_path, routing_path, task_record, routing)

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


def _is_pending_user_decision(result: dict[str, Any]) -> bool:
    """The reviewer's controller task stopped on the engine hold, not on a fault."""
    return (
        "error" not in result
        and result.get("status") == "waiting_user"
        and result.get("stop_reason") == "user_decision_required"
    )


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
    block = _verified_envelope_block(home, task_id, execution_result)
    if block is not None:
        # The stop-gate reviews under the same frozen envelope the executor ran
        # under. These are already-verified bytes copied unchanged, never a
        # re-render and never a re-resolution, so no second authority is
        # created (E-1, E-16). It is the final section here too, because the
        # gate review input is hashed the same way when it is submitted.
        lines.extend([block, ""])
    input_path.parent.mkdir(parents=True, exist_ok=True)
    input_path.write_text("\n".join(lines), encoding="utf-8")
    return input_path


def _verified_envelope_block(
    home: Path, task_id: str, execution_result: dict[str, Any]
) -> str | None:
    """The controller task's envelope block, verified before it is read.

    `gate-run` runs outside the controller process, so it cannot rely on
    `_read_verified_input`. The routing decision's
    `execution_result.evidence_path` already names the controller task's
    `evidence.json`, which already carries `input_snapshot_path` and
    `input_hash`; this verifies that snapshot against that hash and then reads
    the block out of the verified bytes.

    A task whose own execution input carries a block but whose evidence cannot
    be verified raises rather than reviewing without one — otherwise deleting
    one file would silently downgrade a stop gate to an envelope-free review.
    """
    local_input = home / "tasks" / f"{task_id}-execution-input.md"
    expected: str | None = None
    if local_input.is_file():
        expected = envelope_block_text(local_input.read_text(encoding="utf-8"))

    def missing(reason: str) -> None:
        if expected is not None:
            raise ValueError(
                f"stop-gate for task {task_id} cannot reuse the frozen interpretation envelope: {reason}"
            )

    evidence_path = execution_result.get("evidence_path")
    if not evidence_path or not Path(str(evidence_path)).is_file():
        missing(f"execution result names no readable evidence.json ({evidence_path})")
        return None
    try:
        evidence = json.loads(Path(str(evidence_path)).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        missing(f"cannot read {evidence_path}: {exc}")
        return None
    snapshot_path = evidence.get("input_snapshot_path") if isinstance(evidence, dict) else None
    input_hash = evidence.get("input_hash") if isinstance(evidence, dict) else None
    if not snapshot_path or not input_hash or not Path(str(snapshot_path)).is_file():
        missing(f"{evidence_path} names no readable input snapshot")
        return None
    data = Path(str(snapshot_path)).read_bytes()
    actual = hashlib.sha256(data).hexdigest()
    if actual != input_hash:
        raise ValueError(
            f"stop-gate for task {task_id}: controller input snapshot {snapshot_path} hash mismatch "
            f"(expected {input_hash}, got {actual})"
        )
    block = envelope_block_text(data.decode("utf-8"))
    if block is None:
        missing(f"verified controller input snapshot {snapshot_path} carries no envelope block")
    return block


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
            "executor": flags.executor,
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
    # Built-in generic defaults first, then whatever vocabulary the deployment
    # declared. An external file can only ever raise the assessment: it must
    # not be able to talk the engine out of a risk its own defaults found.
    external = load_risk_rules().evaluate(text)
    path_high = any(keyword in lowered for keyword in HIGH_RISK_KEYWORDS) or external["high"]
    path_medium = any(keyword in lowered for keyword in MEDIUM_RISK_KEYWORDS) or external["medium"]
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


# ---------------------------------------------------------------------------
# Interpretation Envelope — intake resolution and emission (spec section 3,
# steps 1-3).
#
# Resolution happens once per execution attempt, inside `_enqueue_for_routing`,
# after routing and request-id creation and before the execution input is
# written or anything is enqueued. That position is what makes the three
# "before any provider is invoked" guarantees hold as stated: a structural
# preflight stop and a `--dry-run` never reach this code and make zero resolver
# calls, and an attempt that does reach enqueue makes at most one.
#
# The resolver reads the immutable requirement sources in the natural language
# the operator already writes them in. No heading, bullet, keyword, marker,
# section or ordering is required of the user, and none is parsed: the engine
# owns validation of the reply, not the shape of the request.
# ---------------------------------------------------------------------------

#: Engine-internal framing for the resolver's single bounded proposal. This is
#: not a user-facing format: it never appears in a task source, in an operator
#: message, or in the canonical envelope, and the accepted result is re-rendered
#: from scratch rather than copied out of these bytes.
RESOLVER_BEGIN = "<!--ORCH-ENVELOPE-PROPOSAL-BEGIN-->"
RESOLVER_END = "<!--ORCH-ENVELOPE-PROPOSAL-END-->"
#: Bounds on the untrusted reply. Everything beyond them fails closed.
RESOLVER_TIMEOUT_SECONDS = 240
RESOLVER_REPLY_MAX_CHARS = 64 * 1024
ENVELOPE_MAX_MEMBERS = 32
ENVELOPE_MAX_MEMBER_CHARS = 400
ENVELOPE_MAX_EVIDENCE_CHARS = 1200
#: How many candidate files or modules a task_owned_write_targets stop names.
#: The list is a prompt to think with, not an enumeration to trust.
WRITE_TARGET_CANDIDATE_LIMIT = 12
#: The one axis whose safe default is not `[]` (section 1.3).
ENVELOPE_SEMANTIC_AXIS = "semantic_change_surface"
#: The one axis with no default at all (section 1.3).
ENVELOPE_WRITE_AXIS = "task_owned_write_targets"
#: The axes whose safe default is the empty set.
ENVELOPE_EMPTY_DEFAULT_AXES = ("assurance_ceiling", "threat_model", "evidence_ceiling")
#: Exactly the keys one proposed axis object may carry.
RESOLVER_AXIS_KEYS = frozenset({"state", "value", "evidence", "detail"})
#: The intake resolver's owner, fixed rather than routed.
#:
#: The resolver is not a stage: it answers one bounded question about the
#: task's own text before any stage exists, so it has no reason to follow the
#: executor. Fixing it to Claude keeps the boundary a single auditable command
#: whose CLI can state the required "no tools, no filesystem, no user settings,
#: no session" shape in its own vocabulary, instead of one boundary per
#: provider whose weakest member decides what intake is actually isolated from.
#: This affects intake resolution only; executor and reviewer routing are
#: untouched, and no fallback, alternate route or provider-selection policy is
#: added — an unusable Claude fails intake closed like any other resolver
#: failure.
RESOLVER_OWNER = "claude"

#: The isolation flags, spelled out so the boundary is auditable rather than
#: assumed. They have to say, in the CLI's own vocabulary: no tools, no MCP
#: servers, no user configuration, no session state carried in or out. A
#: variadic option is never the last element, because the prompt is appended
#: after these and a trailing `<tools...>` option would swallow it.
RESOLVER_ISOLATION_FLAGS: tuple[str, ...] = (
    # No tool at all. This is the CLI's availability control, not a permission
    # list: `--tools ""` is documented as "disable all tools", so nothing from
    # the built-in set exists in the child. An allowlist option such as
    # `--allowedTools` only decides what is pre-approved among tools that are
    # still present, which is a weaker claim than the boundary needs. With no
    # tool available the child cannot read a file, run a command or browse.
    "--tools", "",
    # No MCP server from any other configuration may add one back.
    "--strict-mcp-config",
    # Load no user/project/local settings: no hooks, no plugins, no agents,
    # no permission defaults from this machine's configuration.
    "--setting-sources", "",
    # Nothing about this call is written into session state.
    "--no-session-persistence",
)

#: Options whose value is variadic in the CLI above. The prompt is a positional
#: argument appended after the flags, so one of these may never be last:
#: `--tools "" <prompt>` reads the prompt as a second tool name and leaves the
#: resolver with no question to answer.
RESOLVER_VARIADIC_FLAGS = frozenset(
    {"--tools", "--allowedTools", "--allowed-tools", "--disallowedTools", "--disallowed-tools"}
)

#: The only environment a resolver child inherits. An allowlist rather than a
#: blocklist: a variable this engine has never heard of cannot leak workspace,
#: deployment or task context into a process that is supposed to see nothing
#: but the immutable sources. Provider authentication has to survive, so the
#: resolver owner's own namespace and the machine's TLS/proxy settings are
#: named — and only that owner's, since the resolver is no longer whichever
#: provider routing happened to pick.
RESOLVER_ENV_ALLOW_NAMES = frozenset(
    {
        "HOME", "PATH", "USER", "LOGNAME", "SHELL", "TERM", "TZ",
        "LANG", "LC_ALL", "LC_CTYPE",
        "TMPDIR", "TEMP", "TMP",
        "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS", "REQUESTS_CA_BUNDLE",
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
        "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    }
)
RESOLVER_ENV_ALLOW_PREFIXES = ("ANTHROPIC_", "CLAUDE_")


class EnvelopeResolverError(ValueError):
    """The resolver produced nothing the engine may act on. Always fails closed."""


@dataclass(frozen=True)
class EnvelopeAxis:
    state: str
    #: Members for a set-valued axis; the enum string for scope_expansion_policy.
    value: Any
    default_applied: bool
    source: Any
    #: Why the axis is unresolved, verbatim enough for the operator to restate.
    detail: str | None = None


@dataclass(frozen=True)
class EnvelopeResolution:
    """Either a fully accepted envelope, or the fail-closed stop and its reason."""

    axes: dict[str, EnvelopeAxis] | None
    stop_reason: str | None
    #: Display-only diagnostics. Never rendered into the canonical envelope.
    candidates: tuple[str, ...] = ()


def _requirement_sources(task_record: dict[str, Any]) -> list[tuple[str, str]]:
    """The complete immutable requirement sources, in reading order.

    Task text, user scope and any supplied approved spec — nothing else. A
    provider or implementation choice is not a requirement source, which is
    what stops `task_owned_write_targets` being derived from a layout the user
    never stated. `--worktree` is absent for the same reason: it is the
    execution boundary, and it grants no write authority.
    """
    sources: list[tuple[str, str]] = [("task text", str(task_record.get("task_description") or ""))]
    scope = task_record.get("scope")
    if scope:
        sources.append(("scope", str(scope)))
    approved_spec = (task_record.get("flags") or {}).get("approved_spec")
    if approved_spec:
        path = Path(str(approved_spec)).expanduser()
        try:
            sources.append((f"approved spec {path}", path.read_text(encoding="utf-8")))
        except OSError:
            # A missing or unreadable spec is already a preflight blocker; do
            # not let it silently become "the sources said nothing".
            sources.append((f"approved spec {path}", ""))
    return sources


def _scope_text(sources: list[tuple[str, str]]) -> str:
    """The task's own scope — its task text when no scope was given."""
    by_label = dict(sources)
    scope = by_label.get("scope") or ""
    return scope if scope.strip() else (by_label.get("task text") or "")


def _normalise_for_grounding(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def _grounded(quote: str, blob: str) -> bool:
    """Is `quote` verbatim in the immutable sources, up to whitespace?

    Only whitespace runs and letter case are normalised, so a reply cannot
    paraphrase, summarise or invent its way past this: the words have to be the
    operator's own.
    """
    normalised = _normalise_for_grounding(quote)
    return bool(normalised) and normalised in blob


def _carried_by(member: str, quote: str) -> bool:
    """Does the quoted source span actually contain this member?

    Exact source-span grounding, and only that. Grounding a quote is not
    grounding a member: a genuine sentence from the sources says nothing about
    a requirement invented next to it, so every declared member has to occur in
    the very text offered as its evidence.

    What this deliberately does *not* do is read the span. Whether the span
    authorises the member or forbids it — polarity, negation, a read-only
    reference, an ambiguous authorisation — is ordinary natural language, and
    the resolver is this engine's single semantic interpreter for it
    (`_resolver_prompt`). A second, deterministic natural-language reader here
    would be a keyword list pretending to be a parser: strong enough to look
    like a guarantee, weak enough to miss the next phrasing, and impossible to
    keep honest across the languages operators actually write in.
    """
    return _normalise_for_grounding(member) in _normalise_for_grounding(quote)


#: A token in the sources that could be a repository path. Deliberately
#: inclusive: it decides only whether the sources are *silent* about paths, and
#: an over-inclusive match costs a stop, never an unearned authorisation.
_SOURCE_PATH_TOKEN = re.compile(r"(?:[\w.~-]+/)+[\w.-]+")


def _sources_name_any_path(sources: list[tuple[str, str]]) -> bool:
    """Do the immutable sources mention anything path-shaped at all?

    Used only to judge an empty declared write set. Prose slashes (`and/or`,
    `read/write`) are excluded by requiring a file extension or a second
    separator, so ordinary sentences stay silent while any real path — even one
    the reply omitted — makes silence an unsupportable claim.
    """
    for _, text in sources:
        for match in _SOURCE_PATH_TOKEN.finditer(text):
            token = match.group(0)
            if token.count("/") >= 2 or re.search(r"\.[A-Za-z0-9]{1,8}$", token):
                return True
    return False


def _normalise_write_target(raw: str, worktree: str | None) -> str | None:
    """A grounded path token, normalised. Callers ground it *before* calling.

    A relative path is resolved against the execution worktree only here, after
    its authority is already established in the sources: the worktree fixes
    where a source-named relative path lands, it never adds one.
    """
    candidate = raw.strip().strip("`").strip().rstrip(",;")
    if not candidate or " " in candidate.strip("/"):
        return None
    expanded = os.path.expanduser(candidate)
    if not os.path.isabs(expanded) and worktree:
        expanded = os.path.join(str(Path(str(worktree)).expanduser()), expanded)
    normalised = os.path.normpath(expanded)
    if normalised in {".", ""}:
        return None
    return normalised


# ---------------------------------------------------------------------------
# The resolver boundary.
# ---------------------------------------------------------------------------


def _resolver_prompt(sources: list[tuple[str, str]]) -> str:
    """The one question the resolver is asked.

    It carries the immutable sources and nothing else: no worktree, no routing
    decision, no profile prompt, no prior report, no engine state. What it asks
    for is a proposal; what the engine does with the answer is decided by
    `_parse_resolver_reply`, which trusts none of it.

    This prompt is where the semantic half of the contract lives. Reading
    ordinary prose — polarity, negation, a read-only reference, an ambiguous
    grant — belongs here and nowhere else, so the obligation is stated as a
    rule the resolver must follow, not as a hint: anything it cannot pin down
    is `unresolved`, and a stop is the correct answer.
    """
    blocks = [
        f"### SOURCE: {label}\n{text.strip()}" if text.strip() else f"### SOURCE: {label}\n(empty)"
        for label, text in sources
    ]
    return "\n\n".join(
        [
            "You are an intake resolver for one task. Read the immutable requirement sources "
            "below and report what they do and do not determine. You have no tools, no "
            "filesystem and no network. Do not perform the task. Do not ask questions. Do not "
            "add requirements the sources do not carry.",
            "\n\n".join(blocks),
            "Report six axes:",
            "- semantic_change_surface: the named behaviours the task is permitted to change.\n"
            "- task_owned_write_targets: the paths the sources explicitly authorise writing. A "
            "path is authorised only where a source says so; an execution directory, a "
            "repository root or a path merely mentioned as context, background or read-only is "
            "not authorisation.\n"
            "- assurance_ceiling: the proof/hardening items the sources permit building.\n"
            "- threat_model: the named adversaries the design must withstand.\n"
            "- evidence_ceiling: the observation/instrumentation items the sources permit adding.\n"
            "- scope_expansion_policy: what happens when work exceeds the surface; the only "
            "value that can be honoured is \"user_decision\".",
            "Give each axis exactly one state:\n"
            "- \"declared\" — the sources state a requirement for this axis.\n"
            "- \"semantically_silent\" — the sources say nothing about this axis.\n"
            "- \"unresolved\" — the sources carry a requirement for this axis that you cannot "
            "reduce to one determinate value, or two that conflict.\n"
            "Prefer \"unresolved\" over a guess. Being unsure is a correct answer; inventing a "
            "value is not.",
            "You are the only reader of this text. Nothing downstream re-reads the sources to "
            "check what they meant, so meaning is settled here:\n"
            "- Read polarity before you read words. A sentence that forbids, excludes, defers or "
            "restricts something is not a requirement to do it. \"Do not rewrite the daemon lease "
            "protocol\" does not put rewriting the daemon lease protocol in "
            "semantic_change_surface; \"Keep orchestrator/controller.py untouched\" and "
            "\"orchestrator/controller.py is read-only background\" do not put that path in "
            "task_owned_write_targets. A negated or read-only reference is never returned as "
            "write authority or as positive semantic-change authority — not as a member, and not "
            "with the negating sentence as its evidence.\n"
            "- Where authorisation, polarity or scope is ambiguous — the sources hint at a "
            "requirement without stating it, permit something only conditionally, or could be "
            "read either way — the axis is \"unresolved\". Do not resolve an ambiguity in the "
            "direction of more authority.\n"
            "- A quote is evidence only for what it actually asserts about the member. A true "
            "sentence that happens to sit near the member, or that concerns a different axis, is "
            "not evidence; if you cannot quote a span that states the requirement, the member "
            "does not belong in \"declared\".\n"
            "- \"unresolved\" costs the operator one restatement. A wrong \"declared\" freezes "
            "authority the sources never granted. They are not comparable; choose the stop.",
            "Special rules:\n"
            "- task_owned_write_targets has no default: if the sources do not determine the set, "
            "its state is \"unresolved\". An empty declared set means the sources determine that "
            "this task writes no repository path at all; if the sources mention any path, or the "
            "task is one that changes the repository, the set is not empty but \"unresolved\".\n"
            "- If semantic_change_surface is \"semantically_silent\", its value is the behaviours "
            "the task's own scope names, quoted verbatim from the scope and not expanded. If you "
            "cannot read that behaviour set off the scope, the axis is \"unresolved\".\n"
            "- If assurance_ceiling, threat_model or evidence_ceiling is \"semantically_silent\", "
            "its value is [].\n"
            "- If scope_expansion_policy is \"semantically_silent\", its value is \"user_decision\".",
            "Answer with exactly one JSON object between the two markers below and nothing else "
            "outside them. Each axis is an object with exactly the keys \"state\", \"value\", "
            "\"evidence\" and \"detail\".\n"
            "- \"value\": a list of short strings, or \"user_decision\" for scope_expansion_policy. "
            "Use [] when the state is \"unresolved\".\n"
            "- \"evidence\": one verbatim quote from the sources per declared member, in the same "
            "order as \"value\"; [] otherwise. Each quote must contain the member it is offered "
            "for: a genuine sentence that says nothing about the member is not evidence. A "
            "write-target quote must contain the path itself and must not be a quote that "
            "forbids writing it or calls it read-only.\n"
            "- \"detail\": for \"unresolved\", one sentence naming the conflicting or unrecognised "
            "text; otherwise \"\".\n"
            "When task_owned_write_targets is \"unresolved\" you may add a top-level "
            "\"candidates\" list of files or modules the change may reach. It is a prompt for the "
            "operator to think with and confers no authority.",
            f"{RESOLVER_BEGIN}\n"
            '{"schema_version": 1, "semantic_change_surface": {"state": "...", "value": [], '
            '"evidence": [], "detail": ""}, "task_owned_write_targets": {...}, '
            '"assurance_ceiling": {...}, "threat_model": {...}, "evidence_ceiling": {...}, '
            '"scope_expansion_policy": {...}}\n'
            f"{RESOLVER_END}",
        ]
    )


def _resolver_command() -> list[str]:
    """The existing Claude provider command, isolated.

    A misconfigured provider command is a resolver failure, not a crash: every
    error raised here is an `EnvelopeResolverError`, so it takes the same
    fail-closed `waiting_user` path as a timeout or a malformed reply. An
    unset, empty or unquotable `ORCH_CLAUDE_COMMAND` must stop intake, not the
    process — there is no retry and no fallback to another provider.
    """
    flags = RESOLVER_ISOLATION_FLAGS
    if flags[-1] in RESOLVER_VARIADIC_FLAGS:
        raise EnvelopeResolverError(
            f"the intake resolver isolation flags end with the variadic option {flags[-1]!r}, "
            "which would consume the prompt"
        )
    try:
        configured = list(provider_command(RESOLVER_OWNER))
    except ValueError as exc:
        raise EnvelopeResolverError(
            f"the configured provider command for the {RESOLVER_OWNER!r} intake resolver is "
            f"unusable: {exc}"
        ) from exc
    return configured + list(flags)


def _resolver_environment(env: dict[str, str] | None = None) -> dict[str, str]:
    """The child's environment: provider authentication and nothing else.

    An allowlist, not a blocklist. Orchestrator variables name homes,
    workspaces and commands the resolver has no business seeing — but so do
    editor, CI, shell and deployment variables this engine has never heard of,
    and a blocklist silently forwards every one of them. What survives is the
    resolver owner's own namespace plus the TLS/proxy settings its
    authentication needs; anything else that a deployment turns out to require
    will fail the call closed rather than widen the child's view.
    """
    source = os.environ if env is None else env
    return {
        key: value
        for key, value in source.items()
        if key in RESOLVER_ENV_ALLOW_NAMES or key.startswith(RESOLVER_ENV_ALLOW_PREFIXES)
    }


def _invoke_resolver(prompt: str) -> str:
    """The provider process boundary. One bounded call, no tools, no workspace.

    This is the only function in intake that spawns anything, and it is the
    seam an offline test replaces: everything above it is engine logic and
    everything below it is the Claude CLI.
    """
    command = _resolver_command() + [prompt]
    with tempfile.TemporaryDirectory(prefix="orch-envelope-resolver-") as sandbox:
        try:
            completed = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=RESOLVER_TIMEOUT_SECONDS,
                check=False,
                cwd=sandbox,
                env=_resolver_environment(),
            )
        except FileNotFoundError as exc:
            raise EnvelopeResolverError(f"intake resolver CLI is unavailable: {exc}") from exc
        except OSError as exc:
            raise EnvelopeResolverError(f"intake resolver could not be started: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise EnvelopeResolverError(
                f"intake resolver timed out after {RESOLVER_TIMEOUT_SECONDS}s"
            ) from exc
    if completed.returncode != 0:
        tail = (completed.stdout or "").strip()[-400:]
        raise EnvelopeResolverError(
            f"intake resolver exited {completed.returncode}: {tail or '(no output)'}"
        )
    return completed.stdout or ""


def _resolver_payload(reply: str) -> dict[str, Any]:
    """The single bounded proposal carried by an untrusted reply."""
    if len(reply) > RESOLVER_REPLY_MAX_CHARS:
        raise EnvelopeResolverError(
            f"intake resolver reply is oversized ({len(reply)} > {RESOLVER_REPLY_MAX_CHARS} chars)"
        )
    begins = reply.count(RESOLVER_BEGIN)
    ends = reply.count(RESOLVER_END)
    if begins != 1 or ends != 1:
        raise EnvelopeResolverError(
            f"intake resolver reply does not carry exactly one proposal (begin={begins}, end={ends})"
        )
    start = reply.index(RESOLVER_BEGIN) + len(RESOLVER_BEGIN)
    stop = reply.index(RESOLVER_END)
    if stop < start:
        raise EnvelopeResolverError("intake resolver reply closes its proposal before it opens it")
    try:
        payload = json.loads(reply[start:stop])
    except json.JSONDecodeError as exc:
        raise EnvelopeResolverError(f"intake resolver proposal is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise EnvelopeResolverError("intake resolver proposal must be a JSON object")
    if payload.get("schema_version") != ENVELOPE_SCHEMA_VERSION:
        raise EnvelopeResolverError(
            f"intake resolver proposal has unsupported schema_version {payload.get('schema_version')!r}"
        )
    allowed = {"schema_version", "candidates", *ENVELOPE_AXES}
    if not set(ENVELOPE_AXES) <= set(payload) or not set(payload) <= allowed:
        missing = sorted(set(ENVELOPE_AXES) - set(payload))
        extra = sorted(set(payload) - allowed)
        raise EnvelopeResolverError(
            f"intake resolver proposal axes missing={missing} unexpected={extra}"
        )
    return payload


def _proposed_members(axis: str, entry: dict[str, Any]) -> list[str]:
    value = entry["value"]
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise EnvelopeResolverError(f"proposed {axis} value must be a list of non-empty strings")
    members = [item.strip() for item in value]
    if len(members) > ENVELOPE_MAX_MEMBERS:
        raise EnvelopeResolverError(
            f"proposed {axis} names {len(members)} members, over the {ENVELOPE_MAX_MEMBERS} bound"
        )
    if any(len(item) > ENVELOPE_MAX_MEMBER_CHARS for item in members):
        raise EnvelopeResolverError(
            f"proposed {axis} carries a member longer than {ENVELOPE_MAX_MEMBER_CHARS} characters"
        )
    if len(set(members)) != len(members):
        raise EnvelopeResolverError(f"proposed {axis} repeats a member")
    return members


def _proposed_evidence(axis: str, entry: dict[str, Any]) -> list[str]:
    evidence = entry["evidence"]
    if not isinstance(evidence, list) or any(not isinstance(item, str) for item in evidence):
        raise EnvelopeResolverError(f"proposed {axis} evidence must be a list of strings")
    if any(len(item) > ENVELOPE_MAX_EVIDENCE_CHARS for item in evidence):
        raise EnvelopeResolverError(
            f"proposed {axis} carries a quote longer than {ENVELOPE_MAX_EVIDENCE_CHARS} characters"
        )
    return evidence


def _proposed_detail(axis: str, entry: dict[str, Any]) -> str:
    detail = entry["detail"]
    if not isinstance(detail, str):
        raise EnvelopeResolverError(f"proposed {axis} detail must be a string")
    return detail.strip()[:ENVELOPE_MAX_EVIDENCE_CHARS]


def _axis_from_proposal(
    axis: str,
    entry: Any,
    *,
    blob: str,
    scope_blob: str,
    worktree: str | None,
    apply_shaped: bool,
    sources_name_paths: bool,
) -> EnvelopeAxis:
    """One proposed axis, validated into an engine value or into `unresolved`.

    Nothing here is repaired, retried or partially accepted: a proposal that
    does not satisfy the axis's own rule either raises — which fails the whole
    resolution closed — or is recorded `unresolved`, which takes the section
    1.4 stop.
    """
    if not isinstance(entry, dict) or set(entry) != RESOLVER_AXIS_KEYS:
        raise EnvelopeResolverError(
            f"proposed {axis} must be an object with exactly {sorted(RESOLVER_AXIS_KEYS)}"
        )
    state = entry["state"]
    if state not in ENVELOPE_STATES:
        raise EnvelopeResolverError(f"proposed {axis} has unknown state {state!r}")
    detail = _proposed_detail(axis, entry)

    if state == "unresolved":
        return EnvelopeAxis(
            "unresolved", None, False, None,
            detail or "the sources do not reduce this axis to one determinate value",
        )

    if axis == ENVELOPE_ENUM_AXIS:
        if entry["value"] != SCOPE_EXPANSION_USER_DECISION:
            # Section 1.3: a declared value intake cannot express in the axis's
            # canonical shape is unresolved, not a silent downgrade.
            return EnvelopeAxis(
                "unresolved", None, False, None,
                f"the sources ask for a scope expansion policy the engine does not honour "
                f"({entry['value']!r}); {SCOPE_EXPANSION_USER_DECISION} is the only value",
            )
        if state == "declared":
            quotes = _proposed_evidence(axis, entry)
            if not quotes or not all(_grounded(quote, blob) for quote in quotes):
                raise EnvelopeResolverError(
                    f"proposed {axis} is declared without a verbatim quote from the sources"
                )
            return EnvelopeAxis("declared", SCOPE_EXPANSION_USER_DECISION, False, ENVELOPE_SOURCE_REQUIREMENT)
        return EnvelopeAxis(
            "semantically_silent", SCOPE_EXPANSION_USER_DECISION, True, ENVELOPE_SOURCE_DEFAULT
        )

    members = _proposed_members(axis, entry)
    quotes = _proposed_evidence(axis, entry)

    if state == "semantically_silent":
        if axis == ENVELOPE_WRITE_AXIS:
            # Section 1.3 gives this axis no default, so the state is
            # unreachable: silence about paths is exactly the unresolved case.
            return EnvelopeAxis(
                "unresolved", None, False, None,
                "the sources name no write targets, and this axis has no default; it is never "
                "inferred from the semantic change surface, from an implementation layout, or "
                "from the execution worktree",
            )
        if axis in ENVELOPE_EMPTY_DEFAULT_AXES:
            if members:
                raise EnvelopeResolverError(
                    f"proposed {axis} is semantically_silent but carries members; "
                    "its safe default is []"
                )
            return EnvelopeAxis("semantically_silent", [], True, {})
        # semantic_change_surface: the section 1.3 default is the behaviours
        # the task's own scope names, verbatim and unexpanded. A non-empty
        # scope may not silently produce [].
        if not members:
            if _normalise_for_grounding(scope_blob):
                raise EnvelopeResolverError(
                    "proposed semantic_change_surface is semantically_silent with an empty value "
                    "while the task's scope is not empty; the default is the behaviours the scope "
                    "names, and an undeterminable behaviour set is unresolved"
                )
            return EnvelopeAxis("semantically_silent", [], True, {})
        ungrounded = [member for member in members if not _grounded(member, scope_blob)]
        if ungrounded:
            raise EnvelopeResolverError(
                f"proposed semantic_change_surface default expands beyond the task's own scope: "
                f"{ungrounded!r} is not verbatim in it"
            )
        return EnvelopeAxis(
            "semantically_silent",
            members,
            True,
            {member: ENVELOPE_SOURCE_DEFAULT for member in members},
        )

    # state == "declared"
    if not members:
        if axis == ENVELOPE_WRITE_AXIS:
            # "This task authorises no repository write" is a claim about the
            # sources, so the sources have to support it. Two conditions the
            # engine can check for itself, because a resolver assertion is not
            # evidence: the request is not apply-shaped (an apply task changes
            # the repository, so an empty write set contradicts it), and the
            # sources name nothing path-shaped that the reply left unaccounted
            # for. Otherwise the set is undetermined, which is `unresolved`.
            if apply_shaped:
                return EnvelopeAxis(
                    "unresolved", None, False, None,
                    "the sources authorise no write target, but this is an apply task, which "
                    "changes the repository; the paths it may write are undetermined and are "
                    "never inferred from the semantic change surface, from an implementation "
                    "layout, or from the execution worktree",
                )
            if sources_name_paths:
                return EnvelopeAxis(
                    "unresolved", None, False, None,
                    "the sources authorise no write target while naming paths the reply does "
                    "not account for, so the sources do not determine the write set",
                )
            # A determinate "this task authorises no repository write" — the
            # shape of a propose or review task, whose only output is its own
            # report. It is not silence: E-17 still puts the engine-owned
            # outputs inside the axis, and E-11 turns any other write into a
            # user decision. Section 1.3 reserves `unresolved` for sources that
            # do not determine the set, not for sources that determine it empty.
            return EnvelopeAxis("declared", [], False, {})
        raise EnvelopeResolverError(f"proposed {axis} is declared with an empty value")
    if len(quotes) != len(members):
        raise EnvelopeResolverError(
            f"proposed {axis} carries {len(quotes)} quotes for {len(members)} declared members"
        )
    for member, quote in zip(members, quotes):
        if not _grounded(quote, blob):
            raise EnvelopeResolverError(
                f"proposed {axis} member {member!r} quotes {quote!r}, which is not verbatim in the sources"
            )
    if axis != ENVELOPE_WRITE_AXIS:
        for member, quote in zip(members, quotes):
            # A genuine quote is not evidence for a requirement it does not
            # carry. Without this, an invented member paired with any real
            # sentence from the sources would freeze into the envelope as a
            # declared requirement the operator never wrote.
            if not _carried_by(member, quote):
                raise EnvelopeResolverError(
                    f"proposed {axis} member {member!r} is not carried by the source text quoted "
                    f"for it ({quote!r}), so the sources do not declare it"
                )
        return EnvelopeAxis(
            "declared", members, False, {member: ENVELOPE_SOURCE_REQUIREMENT for member in members}
        )

    targets: list[str] = []
    for member, quote in zip(members, quotes):
        if not _carried_by(member, quote):
            # The path's authority has to be in the quote, not merely near it.
            return EnvelopeAxis(
                "unresolved", None, False, None,
                f"the write target {member!r} is not carried by the source text quoted for it, so "
                "the sources do not authorise writing it",
            )
        # Whether that span authorises the path or forbids it is the resolver's
        # judgement, made once, in the one place this engine reads prose. The
        # prompt requires `unresolved` for a negated, read-only or otherwise
        # ambiguous reference, and a reply that ignores that is a resolver
        # failure — not something a denial-word list here could reliably catch.
        normalised = _normalise_write_target(member, worktree)
        if normalised is None:
            return EnvelopeAxis(
                "unresolved", None, False, None,
                f"the sources name {member!r} as a write target, which is not a path",
            )
        if normalised not in targets:
            targets.append(normalised)
    return EnvelopeAxis(
        "declared", targets, False, {target: ENVELOPE_SOURCE_REQUIREMENT for target in targets}
    )


def _proposed_candidates(payload: dict[str, Any], axes: dict[str, EnvelopeAxis]) -> list[str]:
    """The display-only diagnostic list, accepted only where it is meaningful.

    It is stripped here and never reaches `_envelope_payload`, so no candidate
    can become authority by being mentioned.
    """
    raw = payload.get("candidates")
    if raw is None:
        return []
    if axes[ENVELOPE_WRITE_AXIS].state != "unresolved":
        raise EnvelopeResolverError(
            "intake resolver offered write-target candidates for an axis that is not unresolved"
        )
    if not isinstance(raw, list) or any(not isinstance(item, str) or not item.strip() for item in raw):
        raise EnvelopeResolverError("intake resolver candidates must be a list of non-empty strings")
    if len(raw) > WRITE_TARGET_CANDIDATE_LIMIT:
        raise EnvelopeResolverError(
            f"intake resolver offered {len(raw)} candidates, over the {WRITE_TARGET_CANDIDATE_LIMIT} bound"
        )
    if any(len(item) > ENVELOPE_MAX_MEMBER_CHARS for item in raw):
        raise EnvelopeResolverError("intake resolver offered an oversized candidate")
    return [item.strip() for item in raw]


def _parse_resolver_reply(
    reply: str, sources: list[tuple[str, str]], task_record: dict[str, Any]
) -> tuple[dict[str, EnvelopeAxis], list[str]]:
    payload = _resolver_payload(reply)
    blob = _normalise_for_grounding("\n".join(text for _, text in sources))
    scope_blob = _normalise_for_grounding(_scope_text(sources))
    worktree = (task_record.get("flags") or {}).get("worktree")
    apply_shaped = str(task_record.get("task_type_hint") or "") == "apply"
    sources_name_paths = _sources_name_any_path(sources)
    axes = {
        axis: _axis_from_proposal(
            axis,
            payload[axis],
            blob=blob,
            scope_blob=scope_blob,
            worktree=worktree,
            apply_shaped=apply_shaped,
            sources_name_paths=sources_name_paths,
        )
        for axis in ENVELOPE_AXES
    }
    return axes, _proposed_candidates(payload, axes)


def _resolve_envelope(task_record: dict[str, Any]) -> EnvelopeResolution:
    """One resolver call, then the accepted envelope or the fail-closed stop.

    The routing decision is not an input: the resolver owner is fixed
    (`RESOLVER_OWNER`), and the sources it reads are the task's own immutable
    requirement text. Routing still decides who executes and who reviews.
    """
    sources = _requirement_sources(task_record)
    try:
        reply = _invoke_resolver(_resolver_prompt(sources))
        axes, candidates = _parse_resolver_reply(reply, sources, task_record)
    except EnvelopeResolverError as exc:
        return EnvelopeResolution(
            None,
            "interpretation envelope unresolved before any provider runs; the intake resolver "
            f"produced nothing the engine may act on: {exc}",
        )
    if _unresolved_axes(axes):
        return EnvelopeResolution(
            None, _envelope_stop_reason(axes, task_record, candidates), tuple(candidates)
        )
    return EnvelopeResolution(axes, None)


def _unresolved_axes(axes: dict[str, EnvelopeAxis]) -> list[str]:
    return [axis for axis in ENVELOPE_AXES if axes[axis].state == "unresolved"]


# ---------------------------------------------------------------------------
# Emission and the section 1.4 stop.
# ---------------------------------------------------------------------------


def _engine_owned_write_targets(home: Path, task_id: str, request_id: str, routing: dict[str, Any]) -> list[str]:
    """This task's own outputs, added to the axis unconditionally (E-17).

    Deterministic functions of the task id and the routing decision, not an
    inference from the sources: the controller writes stage reports into the
    task artifact directory, and a stop-gate task's reviewer is told to write
    exactly one output path. A declared value that omits them is not a
    conflict; intake adds them regardless.
    """
    targets = [os.path.normpath(str(home / "tasks" / request_id / "reports"))]
    if routing.get("stop_gate") is True:
        targets.append(os.path.normpath(str(home / "tasks" / f"{task_id}-gate-review-output.md")))
    return targets


def _envelope_payload(
    axes: dict[str, EnvelopeAxis],
    *,
    home: Path,
    task_id: str,
    request_id: str,
    routing: dict[str, Any],
) -> dict[str, Any]:
    """The canonical JSON for a fully resolved envelope.

    Re-rendered from the accepted values; no provider byte is copied through.
    """
    payload: dict[str, Any] = {"schema_version": ENVELOPE_SCHEMA_VERSION}
    for axis in ENVELOPE_AXES:
        resolved = axes[axis]
        value = resolved.value
        source = resolved.source
        if axis == ENVELOPE_WRITE_AXIS:
            value = list(value)
            source = dict(source)
            for engine_owned in _engine_owned_write_targets(home, task_id, request_id, routing):
                if engine_owned not in value:
                    value.append(engine_owned)
                source[engine_owned] = ENVELOPE_SOURCE_ENGINE
        payload[axis] = {
            "state": resolved.state,
            "value": value,
            "default_applied": resolved.default_applied,
            "source": source,
        }
    return payload


def _envelope_stop_reason(
    axes: dict[str, EnvelopeAxis], task_record: dict[str, Any], candidates: list[str]
) -> str:
    """The section 1.4 stop message: names the axis and the offending text."""
    unresolved = _unresolved_axes(axes)
    parts = [
        "interpretation envelope unresolved before any provider runs; restate the requirement and "
        "re-run orch start"
    ]
    for axis in unresolved:
        parts.append(f"axis {axis}: {axes[axis].detail}")
    if ENVELOPE_WRITE_AXIS in unresolved:
        parts.append(_write_target_stop_guidance(task_record, candidates))
    return "; ".join(parts)


def _write_target_stop_guidance(task_record: dict[str, Any], candidates: list[str]) -> str:
    """The four things a write-target stop must additionally carry (section 1.4).

    The operator is being asked to enumerate a scope they may not have
    considered in full, so the stop warns about impact reach, offers candidates
    to think with, states what leaving one out costs, and says plainly that the
    candidates authorise nothing.
    """
    named = candidates or _write_target_candidates(task_record)
    return (
        "the impact scope of this change may reach files or modules beyond the ones the task text "
        "named; candidates to think with (these confer NO write authority and none of them is "
        f"written into any envelope): {', '.join(named)}; leaving a needed target out means a "
        "later in-scope write to an unlisted path falls outside the frozen envelope, becomes "
        "needs_user_decision and stops the task again after provider cost has been spent; only your "
        "restated requirement, re-read at the next orch start, can put a path in this axis"
    )


def _write_target_candidates(task_record: dict[str, Any]) -> list[str]:
    """Candidate files or modules the change may reach.

    The engine's own fallback, used when the resolver offered none. Section 1.4
    forbids issuing the stop without at least one candidate, so this always
    returns something. Deliberately not confined to the immutable sources — the
    axis is unresolved precisely because those sources do not determine it —
    and deliberately never written into an envelope.
    """
    candidates: list[str] = []

    def add(value: str) -> None:
        if value and value not in candidates and len(candidates) < WRITE_TARGET_CANDIDATE_LIMIT:
            candidates.append(value)

    flags = task_record.get("flags") or {}
    text = " ".join(part for _, part in _requirement_sources(task_record) if part)
    for match in re.finditer(r"(?:[\w.~-]*/)+[\w.-]+", text):
        add(match.group(0))
    for match in re.finditer(r"\b[\w-]+\.(?:py|sh|md|ya?ml|json|db|ts|tsx|js|mjs)\b", text):
        add(match.group(0))
    worktree = flags.get("worktree")
    if worktree:
        root = Path(str(worktree)).expanduser()
        try:
            entries = sorted(entry.name for entry in root.iterdir() if not entry.name.startswith("."))
        except OSError:
            entries = []
        for entry in entries:
            add(str(root / entry))
        add(str(root))
    approved_spec = flags.get("approved_spec")
    if approved_spec:
        add(str(approved_spec))
    if not candidates:
        add(f"{task_record['task_id']} task artifact reports/ directory")
    return candidates


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
        return {
            "status": "waiting_user",
            "reason": (
                f"apply requires approved OpenSpec: {flags.approved_spec} carries no explicit approval "
                "marker line (e.g. 'Status: approved'); a marker word in prose does not count"
            ),
        }
    # Interpretation envelope, last, and structural only: a task source that
    # carries an envelope marker of its own would make the writer-owned block
    # unverifiable, so it stops here. This is a text check on the immutable
    # sources — it invokes nothing. Axis resolution itself is not a preflight
    # condition; it happens once per execution attempt in
    # `_enqueue_for_routing`, so a stop here and a `--dry-run` both cost zero
    # resolver calls.
    for label, text in _requirement_sources(task_record):
        if ENVELOPE_BEGIN in text or ENVELOPE_END in text:
            return {
                "status": "waiting_user",
                "reason": (
                    f"{label} carries an interpretation-envelope marker; the block is writer-owned and "
                    "task-supplied markers are never read as an envelope. Remove the marker and re-run "
                    "orch start"
                ),
            }
    return {"status": "pass", "reason": None}


def _route(task_id: str, description: str, task_record: dict[str, Any], preflight: dict[str, str | None]) -> dict[str, Any]:
    task_type = task_record["task_type_hint"]
    text = _combined_text(description, task_record.get("scope"))
    route_source = _route_source(text, task_type)
    pattern, executor, reviewer = _pattern(task_type, text, task_record["flags"].get("executor"))
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


def _pattern(
    task_type: str, text: str, executor_flag: str | None = None
) -> tuple[str | None, str | None, str | None]:
    if executor_flag is not None and task_type != "apply":
        # Only apply has profiles for both pairings; every other type has a
        # fixed executor. Silently ignoring the flag would let an operator
        # believe they chose something.
        raise ValueError(f"--executor applies to apply tasks only; task type here is {task_type!r}")
    if task_type == "propose":
        return "propose_spec", "claude", "codex"
    if task_type == "review":
        return "spec_review", "codex", "claude"
    if task_type == "apply":
        # Default pairing: implement with Claude, review with Codex. Review is a
        # short-output, high-leverage position, and Codex has been the stricter
        # reviewer of the two in practice — so the pairing puts it where finding
        # a real problem pays most. The explicit --executor flag wins; the
        # keyword forms in the brief remain a fallback for older callers.
        lowered = text.lower()
        if executor_flag == "codex":
            return "codex_implement_claude_review", "codex", "claude"
        if executor_flag == "claude":
            return "claude_apply_codex_review", "claude", "codex"
        if "codex implement" in lowered or "executor=codex" in lowered or "let codex" in lowered:
            return "codex_implement_claude_review", "codex", "claude"
        return "claude_apply_codex_review", "claude", "codex"
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
    if load_risk_rules().evaluate(text)["require_stop_gate"]:
        return True
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
    tracked = _tracked_execution_patterns().get(pattern)
    if tracked is None:
        return {
            "status": "blocked",
            "reason": f"no tracked profile for pattern {pattern!r}",
            "pattern": pattern,
        }

    # The request id is drawn before the input is written because it is also
    # the controller task id, and E-17 puts that task's own reports directory
    # inside `task_owned_write_targets`.
    request_id = str(uuid.uuid4())
    # The one resolver call of this execution attempt, after routing and the
    # request id and before anything is written or enqueued. Routing order,
    # input-write timing and request-id generation are all unchanged, and an
    # unresolved result stops here — before the daemon, and therefore before
    # any task executor, task reviewer or stop-gate provider runs.
    resolution = _resolve_envelope(task_record)
    if resolution.axes is None:
        return {
            "status": "waiting_user",
            "reason": resolution.stop_reason,
            "pattern": pattern,
        }
    try:
        input_path = _write_execution_input(
            home, task_id, task_record, routing, reason, request_id, resolution.axes
        )
    except EnvelopeError as exc:
        return {
            "status": "blocked",
            "reason": f"interpretation envelope framing rejected before enqueue: {exc}",
            "pattern": pattern,
        }
    request = {
        "request_id": request_id,
        "action": "run",
        "type": tracked["type"],
        "profile": str(Path(tracked["profile"]).resolve()),
        "input": str(input_path.resolve()),
    }
    # When a worktree is given it travels to the controller: the executor runs
    # confined to it and without push credentials.
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
    request_id: str | None = None,
    axes: dict[str, "EnvelopeAxis"] | None = None,
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
    emit_envelope = request_id is not None and axes is not None
    if emit_envelope:
        # The envelope is the final section, so its writer-owned position is
        # part of what makes the block verifiable, not just its markers.
        unresolved = _unresolved_axes(axes)
        if unresolved:
            raise EnvelopeError(f"unresolved interpretation envelope axes: {', '.join(unresolved)}")
        payload = _envelope_payload(
            axes, home=home, task_id=task_id, request_id=request_id, routing=routing
        )
        lines.extend([render_envelope_block(payload), ""])
    rendered = "\n".join(lines)
    if emit_envelope:
        # Read the rendered file back through the same fail-closed extractor
        # every consumer uses, before anything is enqueued (E-20).
        readback = extract_envelope(rendered)
        if readback != payload:
            raise EnvelopeError("rendered envelope block does not read back as the resolved envelope")
    input_path.parent.mkdir(parents=True, exist_ok=True)
    input_path.write_text(rendered, encoding="utf-8")
    return input_path


def _execution_plan(task_record: dict[str, Any], routing: dict[str, Any]) -> dict[str, Any]:
    """What would run, computed before anything does.

    All of this is knowable at intake time, and all of it has been looked up
    by reading source when it should have been printed: the stage machine and
    its owners, the actual provider commands and models, the workspace, the
    containment inputs, and why the approved spec counted as approved.
    """
    flags = task_record.get("flags", {})
    pattern = routing.get("pattern")
    profile_entry = _tracked_execution_patterns().get(pattern) if pattern else None
    profile_path = profile_entry["profile"] if profile_entry else None

    stages: list[dict[str, Any]] | str
    if profile_path is None:
        stages = "no profile selected"
    else:
        try:
            profile = load_profile(Path(profile_path))
            stages = [
                {
                    "stage": name,
                    "owner": stage.owner,
                    "attempt_cap": stage.attempt_cap,
                    "timeout": stage.timeout,
                    "outcomes": dict(stage.outcomes or {}),
                }
                if not stage.terminal
                else {"stage": name, "terminal": stage.terminal}
                for name, stage in profile.stages.items()
            ]
        except (ProfileError, OSError) as exc:
            stages = f"profile failed to load: {exc}"

    commands: dict[str, dict[str, str | None]] = {}
    for owner in ("claude", "codex"):
        try:
            command = SubprocessRunner._command(owner)
        except ValueError as exc:
            commands[owner] = {"command": None, "model": None, "error": str(exc)}
            continue
        commands[owner] = {
            "command": " ".join(command),
            "model": SubprocessRunner._model_from_command(command) or "unspecified",
        }

    approved_spec = flags.get("approved_spec")
    return {
        "pattern": pattern,
        "profile": str(profile_path) if profile_path else None,
        "stages": stages,
        "provider_commands": commands,
        "workspace": flags.get("worktree"),
        "containment": {
            "sandbox_available": sandbox_available(),
            "allow_unsandboxed": allow_unsandboxed_requested(),
            "protected_roots": [str(root) for root in protected_roots_from_env()],
            "extra_write_roots": [str(root) for root in extra_write_roots_from_env()],
            "l2_detection": "on" if protected_roots_from_env() else "OFF — no protected roots declared",
        },
        "approved_spec": {
            "path": approved_spec,
            "approved": _spec_is_approved(Path(approved_spec)) if approved_spec else None,
            "rule": "an explicit marker line such as 'Status: approved' (status/approval/decision × approved/ready/accepted/final)",
        },
        "risk": routing.get("risk"),
        "stop_gate": routing.get("stop_gate"),
        "auto_start": routing.get("auto_start"),
    }


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


#: An explicit marker line, not a word found anywhere. The previous check
#: matched `approved|ready|accepted|final` in prose, which failed both ways: a
#: spec discussing "when this is ready" counted as approved, and a spec whose
#: real approval was written in another language did not. The field form is a
#: contract the approver states on purpose:  `Status: ready`, `approval:
#: approved`, `Decision = accepted`.
#:
#: Anchored at BOTH ends of the value: a qualified marker is not an approval.
#: `Status: approved pending review` and `Status: final draft` state exactly
#: the opposite of what an end-anchorless pattern would have read into them.
_SPEC_APPROVAL_RE = re.compile(
    r"^\s*(?:status|approval|decision)\s*[:=]\s*(?:approved|ready|accepted|final)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def _spec_is_approved(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    return bool(_SPEC_APPROVAL_RE.search(text))


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
