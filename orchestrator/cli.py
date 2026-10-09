from __future__ import annotations

import argparse
import getpass
import json
import os
import sqlite3
import sys
import uuid
from pathlib import Path

from .config import ConfigFileError, load_config_into_env
from .containment import ContainmentError
from .controller import Controller, ControllerError
from .daemon import run_daemon, daemon_mode, PROGRESS_COMMANDS
from .doctor import run_doctor
from .runner import ALLOW_UNSANDBOXED_ENV, UnattendedConsentError
from .ipc import IPCError, daemon_is_running, enqueue_request, wait_for_result
from .profile import ProfileError
from .trajectory import TrajectoryError, trajectory_mode
from .trajectory_replay import projection_bytes, reduce_snapshot, render_projection
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
    daemon = subparsers.add_parser("daemon", help="run the always-on service that watches the inbox")
    daemon.add_argument("--mode", choices=("full", "progress-management"))
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

    trajectory_r0 = subparsers.add_parser(
        "trajectory-r0",
        help="pure R0 projection from an explicit frozen snapshot; stdout only",
    )
    trajectory_r0.add_argument(
        "--snapshot", default="-",
        help="snapshot JSON path, or - for stdin (default)",
    )
    trajectory_r0.add_argument(
        "--audience", action="append", choices=["public", "internal", "sensitive"],
        help="explicit render audience; repeatable (default: public + internal)",
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
    _add_kanban_parsers(subparsers)
    return parser


# ---------------------------------------------------------------------------
# kanban (T1.8 slices T1-T2): manual card and quota commands
# ---------------------------------------------------------------------------

#: CLI flag -> card column.  Spelled out rather than derived so adding a card
#: column never silently becomes an operator-writable field.
KANBAN_TEXT_FIELDS = (
    ("--title", "title"),
    ("--note", "note"),
    ("--priority", "priority"),
    ("--repo", "repo_path"),
    ("--worktree", "worktree_path"),
    ("--git-common-dir", "git_common_dir"),
    ("--change", "change_name"),
    ("--spec", "spec_path"),
    ("--acceptance", "acceptance_path"),
    ("--spec-review", "spec_review_pointer"),
    ("--spec-review-hash", "spec_review_hash"),
    ("--base-head", "base_head"),
    ("--candidate", "candidate_fingerprint"),
    ("--card-profile", "profile_name"),
    ("--provider", "provider"),
    ("--model", "model"),
    ("--effort", "effort"),
    ("--routing-digest", "routing_digest"),
    ("--config-digest", "config_digest"),
)
KANBAN_JSON_FIELDS = (
    ("--risk", "risk"),
    ("--estimate", "estimate_by_pool"),
    ("--allowed-commands", "allowed_commands"),
)


def _add_kanban_parsers(subparsers: argparse._SubParsersAction) -> None:
    kanban = subparsers.add_parser(
        "kanban",
        help="manual kanban card commands (metadata only; the daemon applies them)",
        description=(
            "Every card command is written to the daemon inbox and applied by the daemon's "
            "single writer, so a queued request is not a successful operation. Each mutating "
            "command carries --expected-revision (compare-and-swap) and an --operation-id "
            "(idempotency key): resending the same operation id with the same payload replays "
            "the recorded result, and resending it with a different payload is refused. "
            "Nothing here runs a provider, submits a task or claims a night."
        ),
    )
    commands = kanban.add_subparsers(dest="kanban_command", required=True)
    for action in ("list", "show", "render"):
        reader = commands.add_parser(action, help="read-only snapshot; no daemon")
        if action in {"list", "show"}:
            reader.add_argument("--json", action="store_true")
        if action == "show":
            reader.add_argument("--card", required=True)
        if action == "render":
            reader.add_argument("--output", type=Path, required=True)

    def common(parser: argparse.ArgumentParser, *, revision: bool = True) -> None:
        parser.add_argument(
            "--actor",
            help="operator identity recorded on the event; defaults to the local login user",
        )
        parser.add_argument(
            "--operation-id",
            help="idempotency key; defaults to this request's own id (one-shot use)",
        )
        parser.add_argument("--wait-timeout", type=float, default=_default_wait_timeout())
        if revision:
            parser.add_argument("--expected-revision", type=int, required=True)

    def fields(parser: argparse.ArgumentParser) -> None:
        for flag, dest in KANBAN_TEXT_FIELDS:
            parser.add_argument(flag, dest=dest)
        for flag, dest in KANBAN_JSON_FIELDS:
            parser.add_argument(flag, dest=dest, help="a JSON object or array, as appropriate")

    create = commands.add_parser("create", help="create a card in Inbox")
    create.add_argument("--card", required=True, help="card id, chosen by the caller so a resend is exact")
    common(create, revision=False)
    fields(create)

    edit = commands.add_parser(
        "edit",
        help="edit card fields; a scope-bearing field invalidates the approval",
    )
    edit.add_argument("--card", required=True)
    common(edit)
    fields(edit)

    from .kanban.commands import PROGRESS_STATUSES
    report = commands.add_parser("report-progress", help="append an assistant report; never change task/card lifecycle")
    report.add_argument("--card", required=True)
    common(report)
    report.add_argument("--report-status", required=True, choices=PROGRESS_STATUSES)
    report.add_argument("--summary", required=True)
    report.add_argument("--blocker")
    report.add_argument("--decision")
    report.add_argument("--next-step")
    report.add_argument("--source-ref", action="append", default=[], dest="source_refs",
                        help="source pointer as text only; repeat as needed")

    place = commands.add_parser("place", help="explicit USER placement; display metadata only")
    place.add_argument("--card", required=True)
    common(place)
    place.add_argument("--destination", required=True, choices=("board", "backlog"))
    place.add_argument("--user-request", required=True, help="the explicit user instruction selecting this card")

    approve = commands.add_parser(
        "approve", help="operator approval: freeze the scope and move the card to Ready")
    approve.add_argument("--card", required=True)
    common(approve)

    for name, help_text in (
        ("withdraw", "withdraw an approval; the card returns to Inbox"),
        ("return", "return the card for clarification; the approval is invalidated"),
        ("archive", "archive the card; this is not a success claim"),
        ("pause", "record a pause that applies at the next stage boundary"),
    ):
        parser = commands.add_parser(name, help=help_text)
        parser.add_argument("--card", required=True)
        common(parser)

    done = commands.add_parser(
        "done",
        help="accept the card as Done against its evidence and, when required, a manual gate ALLOW",
    )
    done.add_argument("--card", required=True)
    common(done)
    done.add_argument("--binding", type=Path, required=True,
                      help="JSON file holding the closeout binding (T7 generates it from committed rows)")
    done.add_argument("--final-candidate", required=True,
                      help="the final candidate fingerprint as observed now")
    done.add_argument("--gate-decision", type=Path,
                      help="path to the operator's <closeout>-gate-decision.yaml")
    done.add_argument("--gate-decision-hash", help="sha256 of that artifact")

    snapshot = commands.add_parser(
        "quota-snapshot",
        help="record a human-observed seven-day remaining quota snapshot",
    )
    common(snapshot, revision=False)
    snapshot.add_argument("--snapshot", required=True, help="caller-chosen stable ID for exact retry")
    snapshot.add_argument(
        "--pool",
        required=True,
        help="non-sensitive lowercase pool alias; never an email, token, key, or credential",
    )
    snapshot.add_argument("--remaining-bp", required=True, type=int)
    snapshot.add_argument("--observed-at-ms", required=True, type=int)
    snapshot.add_argument("--reset-at-ms", required=True, type=int)

    invalidate = commands.add_parser(
        "quota-invalidate", help="mark one exact manual snapshot stale without deleting it"
    )
    common(invalidate, revision=False)
    invalidate.add_argument("--snapshot", required=True)


def _kanban_payload(args: argparse.Namespace) -> dict:
    payload: dict = {"actor": args.actor or f"local:{getpass.getuser()}"}
    command = args.kanban_command
    if command not in {"create", "quota-snapshot", "quota-invalidate"}:
        payload["expected_revision"] = args.expected_revision
    if command in {"quota-snapshot", "quota-invalidate"}:
        payload["snapshot_id"] = args.snapshot
        if command == "quota-snapshot":
            payload.update(
                {
                    "pool_key": args.pool,
                    "weekly_remaining_bp": args.remaining_bp,
                    "observed_at": args.observed_at_ms,
                    "reset_at": args.reset_at_ms,
                }
            )
        return payload
    payload["card_id"] = args.card
    if command in {"create", "edit"}:
        collected: dict = {}
        for _flag, dest in KANBAN_TEXT_FIELDS:
            value = getattr(args, dest)
            if value is not None:
                collected[dest] = value
        for flag, dest in KANBAN_JSON_FIELDS:
            value = getattr(args, dest)
            if value is None:
                continue
            try:
                collected[dest] = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ControllerError(f"{flag} is not valid JSON: {exc}") from exc
        payload["fields"] = collected
    if command == "place":
        payload.update(destination=args.destination, user_request=args.user_request)
    if command == "report-progress":
        payload.update({key: getattr(args, key) for key in (
            "report_status", "summary", "blocker", "decision", "next_step", "source_refs")})
    if command == "done":
        try:
            binding = json.loads(args.binding.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ControllerError(f"cannot read --binding: {exc}") from exc
        payload["binding"] = binding
        payload["final_candidate_fingerprint"] = args.final_candidate
        if args.gate_decision is not None:
            payload["gate_decision_path"] = str(args.gate_decision.resolve())
        if args.gate_decision_hash is not None:
            payload["gate_decision_hash"] = args.gate_decision_hash
    return payload


def _kanban(home: Path, args: argparse.Namespace) -> int:
    if args.kanban_command in {"list", "show", "render"}:
        from .kanban.read import snapshot, Unavailable
        from .kanban.view import project, render
        try:
            data = snapshot(home, card_id=args.card if args.kanban_command == "show" else None)
            cards = project(data)
            if args.kanban_command == "show":
                cards = [c for c in cards if c["card"]["card_id"] == args.card]
                if not cards:
                    raise Unavailable("unknown card: " + args.card)
                task_id = cards[0]["card"].get("task_id")
                data = {**data, "cards": [cards[0]["card"]],
                        "tasks": [t for t in data["tasks"] if t["id"] == task_id],
                        "events": [e for e in data["events"] if e.get("card_id") == args.card],
                        "nights": [n for n in data["nights"] if n.get("card_id") == args.card]}
            if args.kanban_command == "render":
                # Reject state paths and aliases before opening the destination.
                state_files = [home / name for name in (
                    "orchestrator.db", "orchestrator.db-wal", "orchestrator.db-shm",
                    "orch.db", "orch.db-wal", "orch.db-shm")]
                destination = args.output.resolve()
                if destination.is_relative_to(home.resolve()) or any(
                    destination == state.resolve() or (
                        args.output.exists() and state.exists() and args.output.samefile(state)
                    ) for state in state_files
                ):
                    raise Unavailable("render output must be outside ORCH_HOME and not alias state")
                args.output.write_text(render(data), encoding="utf-8")
                print(str(args.output))
            else:
                print(json.dumps({**data, "projection": cards}, ensure_ascii=False, indent=2))
            return 0
        except (Unavailable, OSError) as exc:
            print(json.dumps({"available": False, "error": "unavailable: " + str(exc)}, ensure_ascii=False))
            return 2
    from .kanban.commands import KanbanError, build_request
    try:
        if daemon_mode() == "progress-management" and args.kanban_command not in PROGRESS_COMMANDS:
            raise KanbanError("command unavailable in progress-management")
    except (ValueError, KanbanError) as exc:
        print(f"orchestrator: {exc}", file=sys.stderr)
        return 2

    try:
        request = build_request(
            args.kanban_command,
            _kanban_payload(args),
            operation_id=args.operation_id,
        )
    except (ControllerError, KanbanError, OSError) as exc:
        print(f"orchestrator: {exc}", file=sys.stderr)
        return 2
    if not daemon_is_running(home):
        # A card command is only ever applied by the daemon's single writer;
        # there is deliberately no --in-process escape hatch, because a second
        # writer is exactly what the revision CAS cannot defend against.
        print(
            "orchestrator: orchestrator daemon is not running; a kanban command is applied "
            "only by the daemon",
            file=sys.stderr,
        )
        return 2
    try:
        path = enqueue_request(home, request)
        result = wait_for_result(home, path, args.wait_timeout)
    except (IPCError, OSError) as exc:
        print(f"orchestrator: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    kanban = result.get("kanban")
    if not isinstance(kanban, dict):
        return 2
    # A recorded rejection is a real answer, not a crash, but the operator
    # still needs a non-zero exit so a script does not treat it as applied.
    return 0 if kanban.get("result") == "accepted" else 2



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
    try:
        mode = daemon_mode(getattr(args, "mode", None))
    except ValueError as exc:
        print(f"orchestrator: {exc}", file=sys.stderr)
        return 2
    if mode == "progress-management" and args.command not in {"daemon", "kanban"}:
        print("orchestrator: command unavailable in progress-management", file=sys.stderr)
        return 2
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

    if args.command == "kanban":
        return _kanban(home, args)

    if args.command == "daemon":
        try:
            poll_interval = float(os.environ.get("ORCH_POLL_INTERVAL", "3"))
            run_daemon(home, poll_interval=poll_interval, mode=mode)
            return 0
        except UnattendedConsentError as exc:
            # Same wording and same exit code as the launcher check, so the two
            # gates are indistinguishable to whoever is reading the failure.
            print(f"orchestrator daemon: {exc}", file=sys.stderr)
            return 78  # EX_CONFIG
        except (ControllerError, ContainmentError, IPCError, OSError, ValueError, sqlite3.Error) as exc:
            print(f"orchestrator: {exc}", file=sys.stderr)
            return 2

    if args.command == "enqueue":
        try:
            print(json.dumps(_enqueue(home, args), ensure_ascii=False, indent=2))
            return 0
        except (ControllerError, IPCError, OSError) as exc:
            print(f"orchestrator: {exc}", file=sys.stderr)
            return 2

    if args.command == "trajectory-r0":
        try:
            if trajectory_mode() != "read":
                raise TrajectoryError("trajectory R0 is disabled; ORCH_TRAJECTORY_V1=read is required")
            if args.snapshot == "-":
                raw = sys.stdin.read()
            else:
                raw = Path(args.snapshot).read_text(encoding="utf-8")
            snapshot = json.loads(raw)
            audiences = tuple(args.audience or ("public", "internal"))
            result = reduce_snapshot(snapshot, audience_policy=audiences)
            rendered = render_projection(snapshot, result, audiences=audiences)
            sys.stdout.write(projection_bytes(rendered).decode("utf-8"))
            return 2 if result.integrity_status in {"corrupt", "mismatch"} else 0
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            KeyError,
            RecursionError,
            TrajectoryError,
            TypeError,
            ValueError,
        ):
            print(f"orchestrator: trajectory-r0 failed", file=sys.stderr)
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
