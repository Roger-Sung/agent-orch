from __future__ import annotations

import json
import os
import signal
import threading
import uuid
from pathlib import Path
from typing import Any

from .containment import protected_roots_from_env, validate_home_outside_protected
from .controller import Controller, ControllerError
from .ipc import IPCError, atomic_write_json, atomic_write_text, hold_daemon_lock
from .profile import ProfileError
from .runner import require_unattended_consent


def run_daemon(home: Path, poll_interval: float = 3.0) -> None:
    """The long-running service: watch home/inbox/*.json, run each request, write
    results to processed/.

    A caller - phone, terminal, cron - only drops a request file into the inbox,
    which is a plain file write and therefore possible from a sandbox. The work
    itself always runs in this service's clean environment, independent of where
    the caller was or whether it had network access.

    This service is the only Controller and the single writer. Do not run a CLI
    submit against the same home at the same time.
    """
    if poll_interval <= 0:
        raise ValueError("poll interval must be positive")
    # Checked here rather than only in the launcher: a deployment with its own
    # launcher would otherwise skip the acknowledgement without noticing.
    require_unattended_consent()
    home = home.resolve()
    # A protected root that covers this home would turn every stage's own
    # bookkeeping into a workspace_escape; refuse before creating anything.
    validate_home_outside_protected(home, protected_roots_from_env())
    inbox = home / "inbox"
    processing = home / "processing"
    processed = home / "processed"
    inbox.mkdir(parents=True, exist_ok=True)
    processing.mkdir(parents=True, exist_ok=True)
    processed.mkdir(parents=True, exist_ok=True)
    pid_path = home / "daemon.pid"
    with hold_daemon_lock(home):
        atomic_write_text(pid_path, f"{os.getpid()}\n")
        # Controller init orphan-blocks any task left marked running by a crash.
        controller = Controller(home, event_callback=_print_controller_event)
        reconciliation = _reconcile_startup_requests(controller, inbox, processing, processed)
        stop_requested = threading.Event()

        def request_stop(_signum: int, _frame: object) -> None:
            stop_requested.set()

        previous_handlers = {
            signum: signal.signal(signum, request_stop) for signum in (signal.SIGTERM, signal.SIGINT)
        }
        print(
            f"[orchestrator-daemon] startup reconciliation complete {reconciliation}; "
            f"watching {inbox} (interval={poll_interval}s)",
            flush=True,
        )
        try:
            while not stop_requested.is_set():
                # A crash can leave a claimed request here. Request IDs are also
                # task/operation idempotency keys, so replay is safe.
                for req_path in sorted(processing.glob("*.json")):
                    _handle(controller, req_path, processed)
                for req_path in sorted(inbox.glob("*.json")):
                    claimed = processing / req_path.name
                    try:
                        os.replace(req_path, claimed)
                    except FileNotFoundError:
                        continue
                    _handle(controller, claimed, processed)
                stop_requested.wait(poll_interval)
        finally:
            controller.close()
            for signum, previous in previous_handlers.items():
                signal.signal(signum, previous)
            try:
                if pid_path.read_text(encoding="utf-8").strip() == str(os.getpid()):
                    pid_path.unlink(missing_ok=True)
            except OSError:
                pass


def _handle(controller: Controller, req_path: Path, processed: Path) -> None:
    outcome: dict[str, Any]
    try:
        req = json.loads(req_path.read_text(encoding="utf-8"))
        request_id = req["request_id"]
        if str(uuid.UUID(request_id)) != request_id:
            raise ValueError(f"invalid request_id: {request_id!r}")
        action = req.get("action", "run")
        if action == "resume":
            task_id = req["task_id"]
            print(f"[orchestrator-daemon] picked up {req_path.name}: resume {task_id}", flush=True)
            result = controller.resume(task_id, operation_id=request_id)
        elif action == "run":
            task_type = req["type"]
            profile = Path(req["profile"])
            input_path = Path(req["input"])
            print(f"[orchestrator-daemon] picked up {req_path.name}: type={task_type}", flush=True)
            workspace = req.get("workspace")
            task_id = controller.submit(
                task_type,
                profile,
                input_path,
                task_id=request_id,
                operation_id=request_id,
                workspace=Path(workspace) if workspace else None,
            )
            result = controller.run_until_stop(task_id)
        else:
            raise ValueError(f"unsupported request action: {action!r}")
        task = result["task"]
        evidence_path = Path(task["artifact_dir"]) / "evidence.json"
        outcome = {
            **result,
            "request": req,
            "request_id": request_id,
            "task_id": task_id,
            "status": task["status"],
            "stop_reason": task["stop_reason"],
            "evidence_path": str(evidence_path) if evidence_path.is_file() else None,
            "transitions": result["transitions"],
            "notifications": result["notifications"],
        }
        print(f"[orchestrator-daemon] done {task_id}: {task['status']} ({task['stop_reason']})", flush=True)
    except (ControllerError, ProfileError, IPCError, OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
        outcome = {"request_path": str(req_path), "error": f"{type(exc).__name__}: {exc}"}
        print(f"[orchestrator-daemon] request failed {req_path.name}: {exc}", flush=True)

    stamp = uuid.uuid4().hex[:8]
    atomic_write_json(processed / f"{req_path.stem}.{stamp}.result.json", outcome)
    os.replace(req_path, processed / f"{req_path.stem}.{stamp}.request.json")


def _reconcile_startup_requests(
    controller: Controller,
    inbox: Path,
    processing: Path,
    processed: Path,
) -> dict[str, int]:
    """Classify queue residue before the daemon reports ready."""
    summary = {
        "inbox_ready": 0,
        "processing_replayable": 0,
        "quarantined": 0,
        "already_processed": 0,
    }
    for directory, kind in ((inbox, "inbox"), (processing, "processing")):
        for path in sorted(directory.iterdir()):
            if path.name.startswith("."):
                controller._quarantine(None, path, None, f"{kind}_partial_temp_file")
                summary["quarantined"] += 1
                continue
            if path.suffix != ".json":
                controller._quarantine(None, path, None, f"{kind}_unexpected_file")
                summary["quarantined"] += 1
                continue
            try:
                req = json.loads(path.read_text(encoding="utf-8"))
                request_id = req["request_id"]
                if str(uuid.UUID(request_id)) != request_id:
                    raise ValueError(f"invalid request_id: {request_id!r}")
            except (OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
                controller._quarantine(None, path, None, f"{kind}_corrupt_request:{type(exc).__name__}")
                summary["quarantined"] += 1
                continue
            if list(processed.glob(f"{path.stem}.*.result.json")):
                destination = processed / f"{path.stem}.startup-reconciled.request.json"
                os.replace(path, destination)
                summary["already_processed"] += 1
                continue
            if kind == "inbox":
                summary["inbox_ready"] += 1
            else:
                summary["processing_replayable"] += 1
    return summary


def _print_controller_event(event: str, payload: dict[str, Any]) -> None:
    if event == "stage_started":
        print(
            "[orchestrator-daemon] stage started "
            f"task={payload.get('task_id')} stage={payload.get('stage')} "
            f"owner={payload.get('owner')} model={payload.get('model')} "
            f"timeout={payload.get('timeout')}s log={payload.get('log_path')}",
            flush=True,
        )
        return
    if event == "stage_finished":
        print(
            "[orchestrator-daemon] stage finished "
            f"task={payload.get('task_id')} stage={payload.get('stage')} "
            f"owner={payload.get('owner')} outcome={payload.get('outcome')} "
            f"classification={payload.get('classification')} reason={payload.get('reason')} "
            f"exit_code={payload.get('exit_code')} timed_out={payload.get('timed_out')}",
            flush=True,
        )
        return
    print(f"[orchestrator-daemon] event {event}: {payload}", flush=True)
