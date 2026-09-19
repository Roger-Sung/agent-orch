from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import uuid
from pathlib import Path

from .config import ConfigFileError, load_config_into_env
from .containment import ContainmentError
from .controller import Controller, ControllerError
from .daemon import run_daemon
from .doctor import run_doctor
from .runner import ALLOW_UNSANDBOXED_ENV, UnattendedConsentError
from .ipc import IPCError, daemon_is_running, enqueue_request, wait_for_result
from .profile import ProfileError
from .watch import (
    WATCH_DEFAULT_BYTES,
    WATCH_MIN_BYTES,
    WATCH_SCHEMA_VERSION,
    WatchError,
    format_cursor,
    parse_cursor,
    read_window,
    validate_window_bytes,
)
from .start import (
    gate_allow_from_args,
    gate_block_from_args,
    gate_run_from_args,
    gate_status_from_args,
    gate_sync_from_args,
    read_start_status,
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

    pack_status = subparsers.add_parser("pack-status", help="show one pack-v1 pack's state")
    pack_status.add_argument("pack_id")
    pack_status.add_argument("--json", action="store_true", help="machine-readable projection")

    pack_list = subparsers.add_parser("pack-list", help="list pack-v1 packs and their states")
    pack_list.add_argument("--json", action="store_true")

    # Operator exits from a hold (STATE-TABLE §6 `A_*`).  Every hold in the
    # table names one; without a way to perform them a pack that enters a hold
    # never leaves it, which is as stuck as a pack that never stops.
    pack_resolve = subparsers.add_parser(
        "pack-resolve", help="A_resolve_operation: act on an operation whose result is unknown")
    pack_resolve.add_argument("pack_id")
    pack_resolve.add_argument("op_id")
    pack_resolve.add_argument(
        "action", choices=["verify_stopped", "restore_input", "restore_output", "recovery_run"])

    pack_revoke_writes = subparsers.add_parser(
        "pack-revoke-writes",
        help="E-1: rename an operation's write roots aside and confirm nothing holds them")
    pack_revoke_writes.add_argument("pack_id")
    pack_revoke_writes.add_argument("op_id")
    pack_revoke_writes.add_argument(
        "--root", type=Path, action="append", required=True, dest="roots",
        help="a write root to revoke; repeat for each. Named explicitly because"
             " revoking the wrong tree is not recoverable.")

    pack_rebind = subparsers.add_parser(
        "pack-rebind", help="A_rebind_session: the exit from hold(review_session_lost)")
    pack_rebind.add_argument("pack_id")
    pack_rebind.add_argument("op_id", help="the review operation whose session was lost")

    pack_raise_cap = subparsers.add_parser(
        "pack-raise-cap", help="A_raise_cap: add to one §3.4 counter's limit")
    pack_raise_cap.add_argument("pack_id")
    pack_raise_cap.add_argument("kind")
    pack_raise_cap.add_argument("extra", type=int)

    pack_allow_apply = subparsers.add_parser(
        "pack-allow-apply", help="A_allow_apply: authorise the apply and re-check the blockers")
    pack_allow_apply.add_argument("pack_id")

    pack_revoke_acceptance = subparsers.add_parser(
        "pack-revoke-acceptance", help="A_revoke_acceptance: withdraw an acceptance")
    pack_revoke_acceptance.add_argument("pack_id")
    pack_revoke_acceptance.add_argument("--reason", default="approval_revoked")

    start = subparsers.add_parser("start", help="intake, preflight, and route a stateful lifecycle task")
    start.add_argument("description")
    start.add_argument("--task-type", choices=["propose", "apply", "review", "provider-smoke"])
    scope_group = start.add_mutually_exclusive_group()
    scope_group.add_argument("--scope")
    scope_group.add_argument("--scope-file", type=Path)
    start.add_argument("--worktree", type=Path)
    start.add_argument("--approved-spec", type=Path)
    start.add_argument("--draft-spec", type=Path, help="opt-in: review an external draft once, without a planner")
    start.add_argument("--execution-config", type=Path, help="opt-in: versioned request-scoped stage model/effort JSON")
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
    enqueue.add_argument("--rerun-stage", action="store_true", help="explicitly rerun a containment-blocked stage; does not clear its evidence")

    # The long-running service: watch the inbox and execute (the only Controller,
    # and the single writer).
    subparsers.add_parser("daemon", help="run the always-on service that watches the inbox")
    session_register = subparsers.add_parser("review-session-register", help="register a new or explicitly imported same-host Fable session; no model call")
    session_register.add_argument("series")
    session_register.add_argument("--cwd", required=True, type=Path)
    session_register.add_argument("--receipt", type=Path, help="successful Claude JSON receipt for an existing local session")
    session_status = subparsers.add_parser("review-session-status", help="read a registered review session without invoking it")
    session_status.add_argument("series")
    session_reconcile = subparsers.add_parser("review-session-reconcile", help="clear a pending review only against its DB-committed sealed receipt")
    session_reconcile.add_argument("task_id")
    session_rehydrate = subparsers.add_parser("review-session-rehydrate", help="explicitly replace a stopped/lost session with checkpoint context; old tasks remain invalid")
    session_rehydrate.add_argument("series")
    session_rehydrate.add_argument("--expected-session", required=True)
    session_rehydrate.add_argument("--cwd", required=True, type=Path)
    session_rehydrate.add_argument("--reason", required=True)
    session_rehydrate.add_argument("--checkpoint", required=True, type=Path)

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
    retained = subparsers.add_parser("containment-inspect", help="verify retained stage output without running a provider or clearing containment")
    retained.add_argument("id")

    watch = subparsers.add_parser(
        "watch",
        help="print one bounded window of a run's live JSONL stream (read-only)",
        description=(
            "Print one bounded window of a stage run's live JSONL stream as a single JSON "
            "envelope on stdout. Everything it returns is non-authoritative evidence: it "
            "cannot mutate the task, the lease, the provider process, the stream or sealed "
            "evidence. records[] holds standard base64 of each complete record's raw bytes, "
            "trailing newline included, in file order. Feed next_cursor back verbatim to "
            "continue. A walk is complete only when eof is true AND next_cursor equals "
            "snapshot_bytes; eof true with next_cursor below snapshot_bytes means bytes are "
            "withheld (a partial tail or a corrupt line), so call again at the SAME returned "
            "cursor - that call returns the withheld record if an append completed it, else a "
            "named error. Stop as soon as error is not null; resuming afterwards is an "
            "explicit re-invocation, never an engine retry. Exit 0 on success, 2 on any "
            "failure, with the machine-readable code in the envelope's error field."
        ),
    )
    watch.add_argument("task_id")
    watch.add_argument(
        "--cursor",
        help=(
            "<run_token>:<offset> from a previous next_cursor, fed back verbatim. Selects the "
            "run by token, so a walk finishes the run it started even after a later run "
            "begins; drop it to watch the latest run from offset 0."
        ),
    )
    watch.add_argument(
        "--max-bytes",
        type=int,
        default=WATCH_DEFAULT_BYTES,
        help=(
            f"raw stream bytes this call may consider (default {WATCH_DEFAULT_BYTES}, minimum "
            f"{WATCH_MIN_BYTES}, refused rather than clamped below it). It does NOT bound the "
            "response, which is roughly 4/3 of the raw bytes plus envelope overhead."
        ),
    )

    resume = subparsers.add_parser("resume", help="resume through the daemon and wait for its result")
    resume.add_argument("id")
    resume.add_argument("--rerun-stage", action="store_true", help="explicitly rerun a containment-blocked stage; not an approval of its old result")
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
        if getattr(args, "rerun_stage", False):
            req["rerun_stage"] = True
    else:
        if getattr(args, "rerun_stage", False):
            raise ControllerError("--rerun-stage requires --resume")
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

    OPERATOR_ACTIONS = {
        "pack-resolve", "pack-revoke-writes", "pack-rebind", "pack-raise-cap",
        "pack-allow-apply", "pack-revoke-acceptance",
    }
    if args.command in OPERATOR_ACTIONS:
        from .db import connect
        from .pack import budgets as pack_budgets, revocation
        from .pack.errors import PackError
        from .pack.policy import PackPolicy
        from .pack.state_machine import PackMachine, PackStateError
        from .pack.store import PackStore

        conn = connect(home / "orch.db")
        try:
            store = PackStore(conn, create=False)
            machine = PackMachine(store)
            try:
                outcome = _run_operator_action(args, store, machine, PackPolicy,
                                               revocation)
            except (PackError, PackStateError, pack_budgets.BudgetError, KeyError) as exc:
                print(f"{args.command} refused: {exc}", file=sys.stderr)
                return 2
            conn.commit()
        finally:
            conn.close()
        print(outcome)
        return 0

    if args.command in {"pack-status", "pack-list"}:
        from .db import connect
        from .pack.policy import pack_status as pack_status_projection, render_status
        from .pack.store import PackStore

        # Read-only: inspecting a pack must never be able to change one.
        conn = connect(home / "orch.db", read_only=True)
        try:
            store = PackStore(conn, create=False)
            if args.command == "pack-list":
                rows = [dict(row) for row in conn.execute(
                    "SELECT pack_id,state,hold_reason,review_round FROM pack_packs"
                    " ORDER BY pack_id")]
                if args.json:
                    print(json.dumps(rows, ensure_ascii=False, indent=2))
                else:
                    for row in rows:
                        suffix = f"  hold={row['hold_reason']}" if row["hold_reason"] else ""
                        print(f"{row['pack_id']:<24} {row['state']:<28}"
                              f" round={row['review_round']}{suffix}")
                return 0
            try:
                status = pack_status_projection(store, args.pack_id)
            except KeyError:
                print(f"orchestrator: unknown pack {args.pack_id!r}", file=sys.stderr)
                return 2
            print(json.dumps(status, ensure_ascii=False, indent=2) if args.json
                  else render_status(status))
            return 0
        finally:
            conn.close()

    if args.command == "doctor":
        report = run_doctor(home)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1 if report["summary"]["fail"] else 0

    if args.command in {"review-session-register", "review-session-status", "review-session-reconcile", "review-session-rehydrate"}:
        from . import review_session
        try:
            if args.command == "review-session-register":
                result = review_session.register(home, args.series, args.cwd, receipt=args.receipt)
            elif args.command == "review-session-reconcile":
                result = review_session.reconcile(home, args.task_id)
            elif args.command == "review-session-rehydrate":
                result = review_session.rehydrate(home, args.series, expected_session=args.expected_session,
                                                  cwd=args.cwd, reason=args.reason, checkpoint=args.checkpoint)
            else:
                result = review_session.inspect(home, args.series, allow_pending=True)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        except (OSError, ValueError, KeyError) as exc:
            print(f"orchestrator: {exc}", file=sys.stderr)
            return 2

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
        except (ControllerError, ContainmentError, IPCError, OSError, ValueError) as exc:
            print(f"orchestrator: {exc}", file=sys.stderr)
            return 2

    if args.command == "enqueue":
        try:
            print(json.dumps(_enqueue(home, args), ensure_ascii=False, indent=2))
            return 0
        except (ControllerError, IPCError, OSError) as exc:
            print(f"orchestrator: {exc}", file=sys.stderr)
            return 2

    if args.command == "watch":
        try:
            return _watch(home, args)
        except (ControllerError, ProfileError, OSError, sqlite3.Error) as exc:
            # Pre-envelope generic failures - a mode=ro database open failure,
            # an unreadable home - keep the CLI's existing stderr-only path.
            # Inventing a watch error code for them would be new vocabulary
            # for a risk H3 does not introduce.
            print(f"orchestrator: {exc}", file=sys.stderr)
            return 2

    if args.command == "status" and (home / "tasks" / f"{args.id}.yaml").is_file():
        try:
            print(json.dumps(read_start_status(home, args.id), ensure_ascii=False, indent=2))
            return 0
        except (OSError, ValueError) as exc:
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
        controller = Controller(home, read_only=(args.command in {"status", "containment-inspect"}))
        if args.command == "submit":
            task_id = controller.submit(args.task_type, args.profile, args.input)
            result = controller.run_until_stop(task_id)
        elif args.command == "status":
            result = controller.status(args.id)
        elif args.command == "containment-inspect":
            result = controller.containment_inspect(args.id)
        else:
            result = controller.resume(args.id, rerun_stage=args.rerun_stage)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ControllerError, ProfileError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"orchestrator: {exc}", file=sys.stderr)
        return 2
    finally:
        if controller is not None:
            controller.close()


def _watch(home: Path, args: argparse.Namespace) -> int:
    """One read-only window, one envelope, one exit. No follow loop.

    Every path builds the same envelope shape, so a caller parses one thing on
    success and on a named failure. Resolution is entirely through reads that
    already exist: the read-only controller (which returns before
    `reconcile_startup`, so it cannot orphan-block a running task), the
    stage_runs rows `status()` already orders, and H2's own
    `log_path.with_suffix(".live.jsonl")` expression.
    """
    envelope: dict = {
        "schema_version": WATCH_SCHEMA_VERSION,
        "task_id": args.task_id,
        "run_token": None,
        # Echoed verbatim, including a malformed value, so an operator can see
        # what was rejected. Never a repaired, clamped or normalised value.
        "cursor": args.cursor,
        "next_cursor": None,
        "eof": False,
        "snapshot_bytes": None,
        "records": [],
        "error": None,
    }
    controller = Controller(home, read_only=True)
    try:
        try:
            validate_window_bytes(args.max_bytes)
            cursor_token: str | None = None
            cursor_offset = 0
            if args.cursor is not None:
                cursor_token, cursor_offset = parse_cursor(args.cursor)
            try:
                state = controller.status(args.task_id)
            except ControllerError as exc:
                # `Controller.status` looks the task up by exact equality and
                # swallows every other failure inside itself, so the only
                # ControllerError it can raise is the missing-task one.
                raise WatchError("task_not_found", str(exc)) from exc
            runs = state["stage_runs"]
            if not runs:
                raise WatchError("no_stage_run", f"task has no stage runs: {args.task_id}")
            if cursor_token is None:
                # Already ordered started_at,rowid by `status()`.
                run = runs[-1]
            else:
                run = next((row for row in runs if row["run_token"] == cursor_token), None)
                if run is None:
                    raise WatchError(
                        "cursor_run_token_unknown",
                        f"no stage run of {args.task_id} has run token {cursor_token}",
                    )
            run_token = run["run_token"]
            envelope["run_token"] = run_token
            live_path = Path(run["log_path"]).with_suffix(".live.jsonl")
            artifact_dir = Path(state["task"]["artifact_dir"])
            try:
                # Both sides resolved before the comparison, so a symlinked
                # live path cannot pass a lexical-only check. Same containment
                # the sealed manifest already applies to log_path.
                resolved_live = live_path.resolve()
                resolved_dir = artifact_dir.resolve()
            except OSError as exc:
                # Containment could not be established (a symlink loop, for
                # instance). Fail closed on the containment code rather than
                # read a path whose location is unknown.
                raise WatchError(
                    "live_path_outside_artifact_dir",
                    f"cannot resolve live stream path {live_path}: {exc}",
                ) from exc
            if not resolved_live.is_relative_to(resolved_dir):
                raise WatchError(
                    "live_path_outside_artifact_dir",
                    f"live stream outside artifact dir: {live_path}",
                )
            window = read_window(
                live_path, cursor_offset=cursor_offset, window_bytes=args.max_bytes
            )
        except WatchError as exc:
            envelope["error"] = exc.code
            envelope["snapshot_bytes"] = exc.snapshot_bytes
            print(json.dumps(envelope, ensure_ascii=False, indent=2))
            print(f"orchestrator: {exc}", file=sys.stderr)
            return 2
        envelope["records"] = list(window.records)
        envelope["next_cursor"] = format_cursor(run_token, window.next_offset)
        envelope["eof"] = window.eof
        envelope["snapshot_bytes"] = window.snapshot_bytes
        print(json.dumps(envelope, ensure_ascii=False, indent=2))
        return 0
    finally:
        controller.close()


def _default_wait_timeout() -> float:
    raw = os.environ.get("ORCH_WAIT_TIMEOUT", "1800")
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"invalid ORCH_WAIT_TIMEOUT: {raw!r}") from exc


def _run_operator_action(args, store, machine, PackPolicy, revocation) -> str:
    """One `A_*` action, reported in the operator's own vocabulary.

    Each returns what actually happened rather than "ok": these are the exits
    from a hold, and an operator who cannot see whether the pack moved has no
    way to tell a performed action from a refused one.
    """
    import uuid as _uuid

    pack_id = args.pack_id
    if args.command == "pack-raise-cap":
        machine.raise_cap(pack_id, args.kind, args.extra,
                          record_id=f"CAP-{_uuid.uuid4()}")
        caps = machine.budget_caps(pack_id)
        return f"{pack_id}: cap raised; effective caps now {caps}"

    if args.command == "pack-revoke-acceptance":
        generation = PackPolicy(store).invalidate_acceptance(pack_id, reason=args.reason)
        return (f"{pack_id}: acceptance revoked (generation {generation});"
                f" state {store.get_pack(pack_id)['state']}")

    if args.command == "pack-revoke-writes":
        op = store.get_operation(args.op_id)
        quarantine = Path(f"{args.roots[0]}.revoked-{op['op_id']}")
        result = machine.revoke_write_capability(
            args.op_id, roots=args.roots, quarantine=quarantine,
            record_id=f"EV-{_uuid.uuid4()}")
        if result["ok"]:
            moved = ", ".join(entry["moved_to"] for entry in result["moved"])
            return f"{args.op_id}: write capability revoked; roots moved to {moved}"
        return (f"{args.op_id}: not revoked ({result['reason']});"
                f" the operation stays unknown")

    if args.command == "pack-resolve":
        op = store.get_operation(args.op_id)
        pack = store.get_pack(pack_id)
        outcome = machine.resolve_operation(
            pack_id, args.op_id, args.action,
            current_boot_id=revocation.boot_session_uuid(),
            op_boot_id=pack["host_boot_id"])
        return f"{args.op_id}: {outcome}"

    if args.command == "pack-rebind":
        op = store.get_operation(args.op_id)
        outcome = machine.rebind_review_session(
            pack_id, role="reviewer", attempt_id=op["attempt_id"],
            lost_op_id=args.op_id, stage=op["stage"] or "review",
            record_id=f"RT-{_uuid.uuid4()}")
        if outcome["held"]:
            return f"{pack_id}: not redispatched; held on {outcome['held']}"
        return (f"{pack_id}: session rebound, redispatch authorised;"
                f" state {store.get_pack(pack_id)['state']}")

    if args.command == "pack-allow-apply":
        pack = store.get_pack(pack_id)

        def recheck() -> str | None:
            # Approving says one thing only. Any other blocker that was
            # recorded stays until whatever owns it clears it - claiming
            # otherwise would let a grant wave through an unrelated failure.
            remaining = [b["reason"] for b in pack["blockers"]
                         if b.get("reason") not in {"approval_revoked", "approval_missing"}]
            return remaining[0] if remaining else None

        state = machine.allow_apply(pack_id, record_id=f"GRANT-{_uuid.uuid4()}",
                                    recheck=recheck)
        return f"{pack_id}: apply authorised; state {state}"

    raise AssertionError(f"unhandled operator action {args.command!r}")
