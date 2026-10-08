from __future__ import annotations

import json
import hashlib
import sqlite3
from types import SimpleNamespace
import os
import signal
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .containment import protected_roots_from_env, validate_home_outside_protected
from .controller import Controller, ControllerError
from .ipc import IPCError, atomic_write_json, atomic_write_text, hold_daemon_lock
from .kanban.commands import handle_request as handle_kanban_request, KanbanError
from .profile import ProfileError
from .runner import require_unattended_consent


# Atomic request writes normally live for milliseconds. Keep a generous grace
# so startup reconciliation cannot steal a temp file from a concurrent caller.
STARTUP_TEMP_GRACE_SECONDS = 60.0


def run_daemon(home: Path, poll_interval: float = 3.0, *, mode: str | None = None) -> None:
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
    mode = daemon_mode(mode)
    if mode == "progress-management":
        return _run_progress(home, poll_interval)
    # Checked here rather than only in the launcher: a deployment with its own
    # launcher would otherwise skip the acknowledgement without noticing.
    require_unattended_consent()
    home = home.resolve()
    # A protected root that covers this home would turn every stage's own
    # bookkeeping into protected_root_drift; refuse before creating anything.
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
        if action == "kanban":
            kanban = handle_kanban_request(controller, req)
            outcome = {"request": req, "request_id": request_id, "kanban": kanban}
            stamp = uuid.uuid4().hex[:8]
            atomic_write_json(processed / f"{req_path.stem}.{stamp}.result.json", outcome)
            os.replace(req_path, processed / f"{req_path.stem}.{stamp}.request.json")
            return
        elif action == "resume":
            task_id = req["task_id"]
            print(f"[orchestrator-daemon] picked up {req_path.name}: resume {task_id}", flush=True)
            result = controller.resume(task_id, operation_id=request_id, rerun_stage=req.get("rerun_stage", False))
        elif action == "run":
            task_type = req["type"]
            profile = Path(req["profile"])
            input_path = Path(req["input"])
            print(f"[orchestrator-daemon] picked up {req_path.name}: type={task_type}", flush=True)
            workspace = req.get("workspace")
            # A pack-v1 request names its own id, because the controller looks
            # a pack up by its task id; anything else leaves the machine
            # querying a pack that does not exist.  Everything else keeps the
            # request id, as before.
            task_id = controller.submit(
                task_type,
                profile,
                input_path,
                task_id=req.get("task_id") or request_id,
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
    except (ControllerError, ProfileError, IPCError, KanbanError, sqlite3.Error, OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
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
                try:
                    age_seconds = time.time() - path.stat().st_mtime
                except FileNotFoundError:
                    # A concurrent atomic writer already published or removed
                    # the temp file after iterdir() observed it.
                    continue
                if age_seconds < STARTUP_TEMP_GRACE_SECONDS:
                    # This may be an active atomic write. A later startup can
                    # quarantine it if its writer died and it becomes stale.
                    continue
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


PROGRESS_COMMANDS = frozenset({"create", "edit", "report-progress", "archive"})


def daemon_mode(mode: str | None = None) -> str:
    value = os.environ.get("ORCH_DAEMON_MODE", "full") if mode is None else mode
    if value not in {"full", "progress-management"}:
        raise ValueError("invalid ORCH_DAEMON_MODE / daemon mode")
    return value


def _progress_request(path: Path) -> tuple[dict | None, str]:
    """Classify before claim or completed-result lookup; never log payloads."""
    if path.name.startswith(".") or path.suffix != ".json" or not path.is_file() or path.is_symlink():
        return None, "non_request"
    try:
        request = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return None, "unreadable_or_bad_json"
    if not isinstance(request, dict) or request.get("action") != "kanban" or not isinstance(request.get("command"), str) or request["command"] not in PROGRESS_COMMANDS:
        return None, "not_allowed"
    return request, "allowed"


def _progress_handle(context, path: Path, processed: Path, request: dict) -> bool:
    result_path = processed / (path.stem + ".progress.result.json")
    request_path = processed / (path.stem + ".progress.request.json")
    if result_path.exists():
        try:
            existing = json.loads(result_path.read_text(encoding="utf-8"))
            if existing.get("request") != request:
                return False
        except (OSError, ValueError, AttributeError):
            return False
    else:
        try:
            request_id = request["request_id"]
            if not isinstance(request_id, str) or str(uuid.UUID(request_id)) != request_id:
                raise ValueError("invalid request id")
            kanban = handle_kanban_request(context, request)
            outcome = {"request": request, "request_id": request_id, "kanban": kanban}
        except (KanbanError, KeyError, TypeError, ValueError, sqlite3.Error) as exc:
            # Exception strings may contain actor-supplied text. Keep diagnostics
            # and IPC error categories finite; original bytes stay in request.
            outcome = {"request": request, "request_id": request.get("request_id"), "error": type(exc).__name__}
        atomic_write_json(result_path, outcome)
    if request_path.exists() and request_path.read_bytes() != path.read_bytes():
        return False
    os.replace(path, request_path)
    return True


def _progress_scan(context, inbox: Path, processing: Path, processed: Path) -> dict[str, int]:
    counts = {"handled": 0, "not_allowed": 0, "non_request": 0, "unreadable_or_bad_json": 0, "collision": 0}
    for directory in (processing, inbox):
        for path in sorted(directory.iterdir()):
            request, category = _progress_request(path)
            if request is None:
                counts[category] += 1
                continue
            if directory == inbox:
                claimed = processing / path.name
                if claimed.exists():
                    counts["collision"] += 1
                    continue
                try:
                    os.replace(path, claimed)
                except FileNotFoundError:
                    continue
                path = claimed
            if _progress_handle(context, path, processed, request):
                counts["handled"] += 1
            else:
                counts["collision"] += 1
    return counts


def _progress_code_hash() -> str:
    root = Path(__file__).parent
    digest = hashlib.sha256()
    for name in ("cli.py", "daemon.py", "db.py", "kanban/__init__.py", "kanban/store.py", "kanban/commands.py", "kanban/quota.py", "kanban/read.py", "kanban/view.py"):
        digest.update(name.encode() + b"\0" + (root / name).read_bytes())
    return digest.hexdigest()


def _run_progress(home: Path, poll_interval: float) -> None:
    from .db import connect_progress
    home = home.resolve()
    validate_home_outside_protected(home, protected_roots_from_env())
    pid_path = home / "daemon.pid"
    with hold_daemon_lock(home):
        conn = connect_progress(home / "orchestrator.db")
        context = SimpleNamespace(conn=conn, home=home)
        stop = threading.Event()
        previous = {}
        try:
            for signum in (signal.SIGTERM, signal.SIGINT):
                previous[signum] = signal.signal(signum, lambda *_: stop.set())
            for name in ("inbox", "processing", "processed"):
                (home / name).mkdir(exist_ok=True)
            atomic_write_text(pid_path, f"{os.getpid()}\n")
            print(f"[orchestrator-daemon] mode=progress-management code_sha256={_progress_code_hash()} pid={os.getpid()}", flush=True)
            last_counts = None
            while not stop.is_set():
                counts = _progress_scan(context, home / "inbox", home / "processing", home / "processed")
                if counts != last_counts:
                    print(f"[orchestrator-daemon] progress queue categories={counts}", flush=True)
                    last_counts = counts
                stop.wait(poll_interval)
        finally:
            conn.close()
            for signum, handler in previous.items():
                signal.signal(signum, handler)
            try:
                if pid_path.read_text(encoding="utf-8").strip() == str(os.getpid()):
                    pid_path.unlink()
            except OSError:
                pass
