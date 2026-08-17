from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

from .config import ConfigFileError, load_config_into_env
from .controller import Controller, ControllerError
from .daemon import run_daemon
from .doctor import run_doctor
from .runner import ALLOW_UNSANDBOXED_ENV, UnattendedConsentError
from .ipc import IPCError, daemon_is_running, enqueue_request, wait_for_result
from .profile import ProfileError
from .start import (
    gate_allow_from_args,
    gate_block_from_args,
    gate_run_from_args,
    gate_status_from_args,
    gate_sync_from_args,
    start_from_args,
    start_go_from_args,
    start_sync_from_args,
)


def default_home() -> Path:
    configured = os.environ.get("ORCH_HOME")
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).resolve().parents[1] / "output" / "orchestrator"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python3 -m orchestrator", description="agent-orch: a stateful dispatcher for multi-provider agent tasks")
    parser.add_argument(
        "--allow-unsandboxed",
        action="store_true",
        help=(
            "run mutating stages without the L1 write sandbox. Only meaningful on a host where "
            "sandbox-exec is missing; without this flag such a host refuses to run them at all."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start", help="intake, preflight, and route a stateful lifecycle task")
    start.add_argument("description")
    start.add_argument("--task-type", choices=["propose", "apply", "review", "provider-smoke"])
    scope_group = start.add_mutually_exclusive_group()
    scope_group.add_argument("--scope")
    scope_group.add_argument("--scope-file", type=Path)
    start.add_argument("--worktree", type=Path)
    start.add_argument("--approved-spec", type=Path)
    start.add_argument(
        "--executor",
        choices=["claude", "codex"],
        help=(
            "which provider implements an apply task. Without it, intake falls back to "
            "sniffing the brief for 'executor=codex' / 'codex implement' / 'let codex', "
            "which is easy to miss; the flag is explicit and wins over the keywords."
        ),
    )
    start.add_argument(
        "--effort",
        choices=["low", "medium", "high"],
        help="recorded in the task record for the operator's own use; routing does not consult it yet",
    )
    start.add_argument("--dry-run", action="store_true", help="route and print the execution plan without enqueuing anything")

    start_go = subparsers.add_parser("start-go", help="approve a post-route orch start task and enqueue it")
    start_go.add_argument("task_id")

    start_sync = subparsers.add_parser("start-sync", help="sync an orch start task from a daemon processed result")
    start_sync.add_argument("task_id")

    gate_status = subparsers.add_parser("gate-status", help="inspect a pending orch start stop-gate")
    gate_status.add_argument("task_id")

    gate_run = subparsers.add_parser("gate-run", help="enqueue a cross-provider stop-gate reviewer")
    gate_run.add_argument("task_id")

    gate_sync = subparsers.add_parser("gate-sync", help="sync a stop-gate reviewer recommendation")
    gate_sync.add_argument("task_id")

    gate_allow = subparsers.add_parser("gate-allow", help="record a manual ALLOW stop-gate decision")
    gate_allow.add_argument("task_id")
    gate_allow.add_argument("--reason")

    gate_block = subparsers.add_parser("gate-block", help="record a manual BLOCK stop-gate decision")
    gate_block.add_argument("task_id")
    gate_block.add_argument("--reason")

    # Enqueue: write a request into the inbox and nothing else - no Controller is
    # constructed, so this is safe from a sandbox. The daemon picks it up.
    enqueue = subparsers.add_parser("enqueue", help="drop a task request into the inbox for the daemon")
    enqueue.add_argument("--type", dest="task_type")
    enqueue.add_argument("--profile", type=Path)
    enqueue.add_argument("--input", type=Path)
    enqueue.add_argument("--resume", dest="resume_id", help="enqueue a resume request for an existing task id")

    # The long-running service: watch the inbox and execute (the only Controller,
    # and the single writer).
    subparsers.add_parser("daemon", help="run the always-on service that watches the inbox")

    subparsers.add_parser(
        "doctor",
        help="check the deployment wiring: config, ORCH_HOME, provider CLIs, L1/L2, overlaps (read-only)",
    )

    submit = subparsers.add_parser("submit", help="submit through the daemon and wait for its result")
    submit.add_argument("--type", required=True, dest="task_type")
    submit.add_argument("--profile", required=True, type=Path)
    submit.add_argument("--input", required=True, type=Path)
    submit.add_argument(
        "--in-process",
        action="store_true",
        help="run locally only when the daemon is stopped (trusted terminal/debugging)",
    )
    submit.add_argument("--wait-timeout", type=float, default=_default_wait_timeout())

    status = subparsers.add_parser("status", help="show task state (read-only, safe while daemon runs)")
    status.add_argument("id")

    resume = subparsers.add_parser("resume", help="resume through the daemon and wait for its result")
    resume.add_argument("id")
    resume.add_argument(
        "--in-process",
        action="store_true",
        help="run locally only when the daemon is stopped (trusted terminal/debugging)",
    )
    resume.add_argument("--wait-timeout", type=float, default=_default_wait_timeout())
    return parser


def _enqueue(home: Path, args: argparse.Namespace) -> dict:
    request_id = str(uuid.uuid4())
    resume_id = getattr(args, "resume_id", None)
    if args.command == "resume":
        resume_id = args.id
    if resume_id:
        req = {"request_id": request_id, "action": "resume", "task_id": resume_id}
    else:
        task_type = getattr(args, "task_type", None)
        profile = getattr(args, "profile", None)
        input_path = getattr(args, "input", None)
        if not (task_type and profile and input_path):
            raise ControllerError("enqueue requires --type --profile --input (or --resume <id>)")
        req = {
            "request_id": request_id,
            "action": "run",
            "type": task_type,
            "profile": str(profile.resolve()),
            "input": str(input_path.resolve()),
        }
    path = enqueue_request(home, req)
    return {"enqueued": str(path), "request": req}


def _broker_and_wait(home: Path, args: argparse.Namespace) -> tuple[dict, bool]:
    if not daemon_is_running(home):
        raise IPCError(
            "orchestrator daemon is not running; start/install the LaunchAgent, "
            "or use --in-process only from a trusted terminal"
        )
    enqueued = _enqueue(home, args)
    result = wait_for_result(home, Path(enqueued["enqueued"]), args.wait_timeout)
    return result, "error" in result


def main(argv: list[str] | None = None) -> int:
    try:
        # Fill unset ORCH_* variables from the optional config file — BEFORE
        # the parser is built, because defaults like the submit/resume wait
        # timeout are read from the environment at parser-construction time.
        # The environment always wins; the acknowledgement gates are refused
        # in the file.
        load_config_into_env()
    except ConfigFileError as exc:
        print(f"orchestrator: {exc}", file=sys.stderr)
        return 2
    args = build_parser().parse_args(argv)
    if getattr(args, "allow_unsandboxed", False):
        # Passed to stage subprocesses through the environment, so a daemon
        # started with the flag keeps the opt-out and a stage never has to
        # guess. It is deliberately noisy to set.
        os.environ[ALLOW_UNSANDBOXED_ENV] = "1"
    home = default_home()
    if not os.environ.get("ORCH_HOME"):
        # Defaulting is legal but has burned an operator before: a CLI without
        # ORCH_HOME creates tasks in a state directory the daemon never reads.
        print(
            f"orchestrator: warning: ORCH_HOME is not set; using {home}. "
            "A daemon configured with a different ORCH_HOME will never see this state.",
            file=sys.stderr,
        )

    if args.command == "doctor":
        report = run_doctor(home)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1 if report["summary"]["fail"] else 0

    if args.command == "start":
        try:
            print(json.dumps(start_from_args(home, args), ensure_ascii=False, indent=2))
            return 0
        except (OSError, ValueError) as exc:
            print(f"orchestrator: {exc}", file=sys.stderr)
            return 2

    if args.command == "start-go":
        try:
            print(json.dumps(start_go_from_args(home, args), ensure_ascii=False, indent=2))
            return 0
        except (OSError, ValueError, IPCError) as exc:
            print(f"orchestrator: {exc}", file=sys.stderr)
            return 2

    if args.command == "start-sync":
        try:
            print(json.dumps(start_sync_from_args(home, args), ensure_ascii=False, indent=2))
            return 0
        except (OSError, ValueError) as exc:
            print(f"orchestrator: {exc}", file=sys.stderr)
            return 2

    if args.command == "gate-status":
        try:
            print(json.dumps(gate_status_from_args(home, args), ensure_ascii=False, indent=2))
            return 0
        except (OSError, ValueError) as exc:
            print(f"orchestrator: {exc}", file=sys.stderr)
            return 2

    if args.command == "gate-run":
        try:
            print(json.dumps(gate_run_from_args(home, args), ensure_ascii=False, indent=2))
            return 0
        except (OSError, ValueError, IPCError) as exc:
            print(f"orchestrator: {exc}", file=sys.stderr)
            return 2

    if args.command == "gate-sync":
        try:
            print(json.dumps(gate_sync_from_args(home, args), ensure_ascii=False, indent=2))
            return 0
        except (OSError, ValueError) as exc:
            print(f"orchestrator: {exc}", file=sys.stderr)
            return 2

    if args.command == "gate-allow":
        try:
            print(json.dumps(gate_allow_from_args(home, args), ensure_ascii=False, indent=2))
            return 0
        except (OSError, ValueError) as exc:
            print(f"orchestrator: {exc}", file=sys.stderr)
            return 2

    if args.command == "gate-block":
        try:
            print(json.dumps(gate_block_from_args(home, args), ensure_ascii=False, indent=2))
            return 0
        except (OSError, ValueError) as exc:
            print(f"orchestrator: {exc}", file=sys.stderr)
            return 2

    if args.command == "daemon":
        try:
            poll_interval = float(os.environ.get("ORCH_POLL_INTERVAL", "3"))
            run_daemon(home, poll_interval=poll_interval)
            return 0
        except UnattendedConsentError as exc:
            # Same wording and same exit code as the launcher check, so the two
            # gates are indistinguishable to whoever is reading the failure.
            print(f"orchestrator daemon: {exc}", file=sys.stderr)
            return 78  # EX_CONFIG
        except (ControllerError, IPCError, OSError, ValueError) as exc:
            print(f"orchestrator: {exc}", file=sys.stderr)
            return 2

    if args.command == "enqueue":
        try:
            print(json.dumps(_enqueue(home, args), ensure_ascii=False, indent=2))
            return 0
        except (ControllerError, IPCError, OSError) as exc:
            print(f"orchestrator: {exc}", file=sys.stderr)
            return 2

    if args.command in {"submit", "resume"} and not args.in_process:
        try:
            result, failed = _broker_and_wait(home, args)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 2 if failed else 0
        except (ControllerError, IPCError, OSError) as exc:
            print(f"orchestrator: {exc}", file=sys.stderr)
            return 2

    if args.command in {"submit", "resume"} and daemon_is_running(home):
        print("orchestrator: --in-process refused while daemon owns the service lock", file=sys.stderr)
        return 2

    # status is read-only: no orphan block, so it is safe while the daemon runs.
    controller = None
    try:
        controller = Controller(home, read_only=(args.command == "status"))
        if args.command == "submit":
            task_id = controller.submit(args.task_type, args.profile, args.input)
            result = controller.run_until_stop(task_id)
        elif args.command == "status":
            result = controller.status(args.id)
        else:
            result = controller.resume(args.id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ControllerError, ProfileError, OSError) as exc:
        print(f"orchestrator: {exc}", file=sys.stderr)
        return 2
    finally:
        if controller is not None:
            controller.close()


def _default_wait_timeout() -> float:
    raw = os.environ.get("ORCH_WAIT_TIMEOUT", "1800")
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"invalid ORCH_WAIT_TIMEOUT: {raw!r}") from exc
