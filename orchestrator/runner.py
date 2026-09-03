from __future__ import annotations

import codecs
import json
import os
import queue
import re
import selectors
import shlex
import signal
import shutil
import subprocess
import threading
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .containment import ContainmentConfigError, ContainmentError, SandboxSetupError, prepare_sandbox


OUTCOME_RE = re.compile(r"^ORCHESTRATOR_OUTCOME:\s*([A-Za-z0-9_-]+)\s*$", re.MULTILINE)
RATE_LIMIT_SIGNATURES = (
    re.compile(r"\brate[_ -]?limit[_ -]?exceeded\b", re.IGNORECASE),
    re.compile(r"\byou(?:'|’)ve hit your (?:usage )?limit\b", re.IGNORECASE),
    re.compile(r"\busage limit (?:has been )?reached\b", re.IGNORECASE),
    re.compile(r"\btoo many requests\b", re.IGNORECASE),
    re.compile(r"\b(?:http(?: status)?\s*)?429\b.*\b(?:rate|quota|limit)\b", re.IGNORECASE),
)
SOCKET_SIGNATURES = (
    re.compile(r"\bFailedToOpenSocket\b", re.IGNORECASE),
    re.compile(r"\bConnectionRefused\b", re.IGNORECASE),
    re.compile(r"\bconnection refused\b", re.IGNORECASE),
    re.compile(r"\bECONNREFUSED\b", re.IGNORECASE),
    re.compile(r"\bfailed to connect\b", re.IGNORECASE),
)
LOCALHOST_SIGNATURE = re.compile(r"\b(localhost|127\.0\.0\.1|::1)\b", re.IGNORECASE)
DEFAULT_CODEX_SERVICE_TIERS = frozenset({"fast", "priority"})

# Identical to subprocess._communicate's read size, so the hand-rolled drain in
# SubprocessRunner._drain_pipe reads the provider pipe in exactly the chunks
# communicate() used to.
DRAIN_READ_BYTES = 32768
# Upper bound on one select() wait, so a silent child still wakes the drain.
# min(remaining, LIVE_POLL_SECONDS) is what stops it from extending the
# deadline the child runs under.
LIVE_POLL_SECONDS = 1.0

# Live stream: one run-local JSONL file beside the sealed stage log, evidence
# only. None of these has an environment override; an override would be a new
# operator knob with a new failure mode and nothing here needs one.
LIVE_SCHEMA_VERSION = 1
LIVE_MAX_BYTES = 8192              # whole-run footprint; never exceeded, never rewritten
LIVE_TERMINAL_RESERVE_BYTES = 512  # a stage_end line is under 200 bytes
LIVE_FRAGMENT_MAX_CHARS = 1024
LIVE_QUEUE_MAX_RECORDS = 256       # optional-record depth; the slot beyond it is the terminal slot
LIVE_CLOSE_JOIN_SECONDS = 1.0      # bounded, and taken only on a reaped-child path

# Worktree + git containment: pin the stage's working directory to a worktree and
# strip every push credential. This is enforcement, not an instruction in a
# prompt - an agent that decides to push has no credential and the hook rejects
# it anyway.
#
# This layer is not a sandbox on its own. Writes outside the workspace are
# prevented by L1 and detected by L2 (see containment.py); what remains
# unaddressed is process isolation and network egress - the agent still runs as
# the same UNIX user and can read whatever that user can read. See
# docs/threat-model.md for the layer boundaries and the residual risk.
CONTAINMENT_BLOCKED_ENV = frozenset(
    {
        "SSH_AUTH_SOCK",
        "SSH_AGENT_PID",
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "GITLAB_TOKEN",
        "GIT_ASKPASS",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_SYSTEM",
        "GIT_CONFIG_COUNT",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
    }
)
CONTAINMENT_PRE_PUSH_HOOK = """#!/bin/sh
echo "agent-orch containment: push is disabled for orchestrated tasks" >&2
exit 1
"""


def prepare_containment(workspace: Path, log_path: Path) -> dict[str, str]:
    """Prepare worktree + git containment for one stage run; return the child env.

    This function covers the git dimension only: work happens in the worktree,
    and a result cannot leave through git. Write confinement is L1 and L2 in
    containment.py; process and network isolation are not implemented at all.

    Push is blocked three ways, so that any one of them failing leaves two:
      1. no SSH agent or token in the environment (neither HTTPS nor SSH can
         find a credential)
      2. GIT_SSH_COMMAND / GIT_ASKPASS point at /usr/bin/false, so there is no
         interactive rescue
      3. a dedicated core.hooksPath whose pre-push rejects unconditionally

    Committing still works. The constraint is on letting a result leave the
    worktree through git, not on doing the job.
    """
    containment_root = log_path.parent / "containment"
    hooks_dir = containment_root / "hooks"
    try:
        hooks_dir.mkdir(parents=True, exist_ok=True)
        pre_push = hooks_dir / "pre-push"
        pre_push.write_text(CONTAINMENT_PRE_PUSH_HOOK, encoding="utf-8")
        pre_push.chmod(0o755)
    except OSError as exc:
        # Same class as a failed sandbox profile write: the environment is
        # broken, not the policy. Raising the environment-specific error keeps
        # the stop reason pointing at the disk rather than at the settings.
        raise SandboxSetupError(f"cannot prepare containment artifacts: {exc}") from exc

    gitconfig = containment_root / "gitconfig"
    identity = _git_identity()
    try:
        (containment_root / "identity-source").write_text(identity["source"] + "\n", encoding="utf-8")
        gitconfig.write_text(
            "[user]\n"
            f"\tname = {identity['name']}\n"
            f"\temail = {identity['email']}\n"
            "[core]\n"
            f"\thooksPath = {hooks_dir}\n"
            "[credential]\n"
            "\thelper =\n",
            encoding="utf-8",
        )
    except OSError as exc:
        raise SandboxSetupError(f"cannot write containment gitconfig: {exc}") from exc

    env = {key: value for key, value in os.environ.items() if key not in CONTAINMENT_BLOCKED_ENV}
    env.update(
        {
            "GIT_CONFIG_GLOBAL": str(gitconfig),
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "/usr/bin/false",
            "SSH_ASKPASS": "/usr/bin/false",
            "GIT_SSH_COMMAND": "/usr/bin/false",
            "ORCH_CONTAINMENT": "worktree+git",
            "ORCH_CONTAINMENT_WORKSPACE": str(workspace),
        }
    )
    return env


ALLOW_UNSANDBOXED_ENV = "ORCH_ALLOW_UNSANDBOXED"
ALLOW_UNATTENDED_ENV = "ORCH_ALLOW_UNATTENDED"

#: Provider CLI flags that disable the approval prompt, i.e. that hand a stage
#: the operator's authority with nobody watching. Known names only — see
#: `unattended_flags_in_use` for why this is a detector and not a guarantee.
UNATTENDED_FLAGS = (
    "--dangerously-skip-permissions",
    "--approve-for-me",
)
PROVIDER_COMMAND_ENV = ("ORCH_CLAUDE_COMMAND", "ORCH_CODEX_COMMAND")

UNATTENDED_CONSENT_MESSAGE = """refusing to start.

The configured provider commands disable their approval prompts, so stages act
unattended with the full authority of this UNIX user: they can read anything
this account can read, and nothing isolates them at the process level.

Set ORCH_ALLOW_UNATTENDED=1 to confirm you intend that, then start again."""


class UnattendedConsentError(ValueError):
    """Unattended-capable provider commands without the operator's acknowledgement."""


def unattended_flags_in_use(env: dict[str, str] | None = None) -> list[tuple[str, str]]:
    """Which provider commands carry a known approval-disabling flag.

    Best-effort by construction: it recognises the flags of the CLIs this
    project actually drives, by name. A different CLI, a renamed flag, a wrapper
    script that adds one, or a config file that sets the same behaviour will not
    be seen — so a clean result means "no known flag was spelled out here", not
    "this deployment is attended". The launcher gate stays the first line
    precisely because it does not depend on recognising anything.
    """
    source = env if env is not None else os.environ
    found: list[tuple[str, str]] = []
    for variable in PROVIDER_COMMAND_ENV:
        command = source.get(variable, "")
        if not command:
            continue
        tokens = set(shlex.split(command)) if command.strip() else set()
        for flag in UNATTENDED_FLAGS:
            if flag in tokens or flag in command:
                found.append((variable, flag))
    return found


def require_unattended_consent(env: dict[str, str] | None = None) -> None:
    """Refuse to proceed when unattended execution was configured but not stated.

    The launcher performs the same check before exec, and this repeats it in the
    engine so that a deployment with its own launcher cannot skip it by accident.
    """
    source = env if env is not None else os.environ
    hits = unattended_flags_in_use(source)
    if not hits:
        return
    if source.get(ALLOW_UNATTENDED_ENV, "").strip() == "1":
        return
    detail = ", ".join(f"{variable} contains {flag}" for variable, flag in hits)
    raise UnattendedConsentError(f"{UNATTENDED_CONSENT_MESSAGE}\n\nDetected: {detail}")


def allow_unsandboxed_requested(env: dict[str, str] | None = None) -> bool:
    """True only when the operator explicitly accepted running without L1.

    Set by `--allow-unsandboxed` on the CLI, or directly in the environment for
    a service launcher. Anything other than an explicit truthy value means no:
    the whole point is that an unconfined mutating stage has to be asked for.
    """
    raw = (env if env is not None else os.environ).get(ALLOW_UNSANDBOXED_ENV, "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


DEFAULT_GIT_IDENTITY = {"name": "agent-orch", "email": "orchestrator@orch.invalid"}
GIT_IDENTITY_ENV = "ORCH_GIT_IDENTITY"
_IDENTITY_LITERAL_RE = re.compile(r"^\s*(?P<name>[^<>]+?)\s*<(?P<email>[^<>@\s]+@[^<>\s]+)>\s*$")


def _git_identity(env: dict[str, str] | None = None) -> dict[str, str]:
    """Resolve the commit identity for a contained stage.

    Containment replaces the global git config, and a commit with no identity
    fails outright, so one has to be written. The question is *whose*.

    Default: a fixed synthetic identity. The previous behaviour — read the
    machine's global `user.name`/`user.email` and use them — silently stamped
    the operator's real name and address onto every commit an agent made,
    including any evidence later published. See docs/decisions/0001.

    Opt in explicitly with ORCH_GIT_IDENTITY:
      * ``global``            resolve from `git config --global` (old behaviour)
      * ``Name <a@b.example>``  use this literal

    A malformed value fails the stage rather than falling back to `global`;
    silently using the real identity is the failure mode being designed out.
    """
    source = (env if env is not None else os.environ).get(GIT_IDENTITY_ENV, "").strip()
    if not source:
        return {**DEFAULT_GIT_IDENTITY, "source": "default"}
    if source == "global":
        identity = dict(DEFAULT_GIT_IDENTITY)
        for key in ("name", "email"):
            try:
                result = subprocess.run(
                    ["git", "config", "--global", f"user.{key}"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                continue
            value = result.stdout.strip()
            if value:
                identity[key] = value
        # The resolved value is deliberately not recorded anywhere: only the
        # fact that a global identity was used travels into the stage record,
        # so evidence stays sanitizable by construction.
        return {**identity, "source": "global"}
    match = _IDENTITY_LITERAL_RE.match(source)
    if match is None:
        raise ContainmentError(
            f"{GIT_IDENTITY_ENV} must be 'global' or 'Name <address@host>'; refusing to guess"
        )
    return {"name": match.group("name"), "email": match.group("email"), "source": "env-literal"}


@dataclass(frozen=True)
class ProviderPreflightResult:
    status: str
    reason: str
    output: str
    exit_code: int | None
    command: list[str]
    model: str | None
    started_at_ms: int
    ended_at_ms: int
    timed_out: bool = False


@dataclass(frozen=True)
class RunResult:
    exit_code: int | None
    output: str
    outcome: str | None
    classification: str
    reason: str
    timed_out: bool = False
    #: Set when containment itself stopped the stage (sandbox unavailable,
    #: identity misconfigured, or a write outside the workspace was detected).
    #: It short-circuits classification so the stop reason stays specific
    #: instead of collapsing into a generic non-zero exit.
    containment_stop: str | None = None
    containment_violations: tuple[dict[str, str], ...] = ()
    started_at_ms: int | None = None
    ended_at_ms: int | None = None
    duration_ms: int | None = None
    model: str | None = None
    usage_input_tokens: int | None = None
    usage_output_tokens: int | None = None
    usage_total_tokens: int | None = None
    usage_unavailable_reason: str | None = None


def classify_result(
    exit_code: int | None,
    output: str,
    allowed_outcomes: set[str],
    timed_out: bool = False,
    *,
    source: RunResult | None = None,
) -> RunResult:
    telemetry = _telemetry_from(source)
    if source is not None and source.containment_stop is not None:
        # Containment outranks every other signal: a stage that escaped its
        # workspace, or never got a sandbox, must not be reported as a plain
        # non-zero exit — the operator needs to know which of the two happened.
        return RunResult(
            exit_code,
            output,
            None,
            "blocked",
            source.containment_stop,
            timed_out,
            containment_stop=source.containment_stop,
            containment_violations=source.containment_violations,
            **telemetry,
        )
    if timed_out:
        return RunResult(exit_code, output, None, "blocked", "timeout", True, **telemetry)
    if exit_code != 0:
        if any(pattern.search(output) for pattern in RATE_LIMIT_SIGNATURES):
            return RunResult(exit_code, output, None, "paused", "rate_limited", False, **telemetry)
        if any(pattern.search(output) for pattern in SOCKET_SIGNATURES):
            return RunResult(exit_code, output, None, "blocked", "provider_socket_error", False, **telemetry)
        return RunResult(exit_code, output, None, "blocked", "runner_nonzero", False, **telemetry)
    matches = OUTCOME_RE.findall(output)
    final_outcome = _final_outcome_marker(output)
    if final_outcome is not None:
        if final_outcome not in allowed_outcomes:
            return RunResult(exit_code, output, final_outcome, "blocked", "unknown_outcome", False, **telemetry)
        return RunResult(exit_code, output, final_outcome, "success", "success", False, **telemetry)
    distinct = set(matches)
    if not matches:
        return RunResult(exit_code, output, None, "blocked", "missing_outcome", False, **telemetry)
    if len(distinct) > 1:
        # Only genuinely conflicting outcomes are ambiguous; a repeated identical
        # value is common from agents, so take the last one.
        return RunResult(exit_code, output, None, "blocked", "ambiguous_outcome", False, **telemetry)
    outcome = matches[-1]
    if outcome not in allowed_outcomes:
        return RunResult(exit_code, output, outcome, "blocked", "unknown_outcome", False, **telemetry)
    return RunResult(exit_code, output, outcome, "success", "success", False, **telemetry)


def _decode_authoritative(raw: bytes, encoding: str, errors: str) -> str:
    """Verbatim replica of subprocess.Popen._translate_newlines.

    This is the authoritative decode: whole buffer, strict errors, run once
    after the child is reaped. The live stream decodes the same bytes with
    replacement instead, which is exactly what makes it non-authoritative.
    """
    return raw.decode(encoding, errors).replace("\r\n", "\n").replace("\r", "\n")


_LIVE_TERMINAL = object()


class _LiveStream:
    """Non-authoritative live JSONL for one stage run.

    Live rendering is evidence. It cannot kill, retry, resume, classify,
    extend a deadline or mutate task state, and two properties make that true
    rather than merely intended:

    - Renderer failure cannot raise into the child's lifecycle. Every public
      method contains `Exception` only. Process-control `BaseException` is
      deliberately left on its legacy path, so a KeyboardInterrupt arriving
      inside `fragment()` still reaches `run()`'s handler and still terminates
      the child.
    - Renderer *slowness* cannot stall the lifecycle either. All filesystem
      I/O, file creation included, happens on one daemon writer thread. The
      lifecycle thread only ever does `put_nowait`, and the single blocking
      step - a bounded join in `close()` - is taken only once the runner has
      already reaped the child, so it cannot delay the paths where the runner
      returns immediately with a child still alive.
    """

    _THREAD_NAME = "orch-live-writer"

    def __init__(self, path: Path, owner: str, timeout_seconds: int) -> None:
        self._path = path
        self._owner = owner
        self._timeout_seconds = timeout_seconds
        # One slot beyond the optional-record depth. That slot is the terminal
        # record's, which is why put_nowait in close() cannot raise Full.
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=LIVE_QUEUE_MAX_RECORDS + 1)
        self._closing = threading.Event()
        # Set from either thread, read by the writer, so it is an Event rather
        # than a bool: "is this live evidence complete" is the one thing a
        # later reader must not get wrong.
        self._incomplete = threading.Event()
        self._thread: threading.Thread | None = None
        self._decoder: Any = None
        self._terminal: dict[str, Any] | None = None
        # Writer thread only, from here down.
        self._handle: Any = None
        self._written = 0
        self._seq = 0

    @classmethod
    def open(cls, log_path: Path, *, owner: str, timeout_seconds: int) -> "_LiveStream":
        """Start the writer thread. Total against renderer failure.

        Touches no path and enqueues nothing, and is called before
        `started = time.time()`, so no live filesystem I/O and no blocking wait
        happens inside the authoritative duration interval. Because it raises
        no `Exception`, one call binds the handle ahead of `run()`'s try, and a
        spawn `OSError` still reaches the convergence-point `close()` against a
        bound name.
        """
        try:
            stream = cls(log_path.with_suffix(".live.jsonl"), owner, timeout_seconds)
        except Exception:
            return cls._inert()
        try:
            thread = threading.Thread(target=stream._writer, name=cls._THREAD_NAME, daemon=True)
            thread.start()
        except Exception:
            # Same inert no-op state a failed file create leaves behind, not a
            # second code path.
            stream._incomplete.set()
            return stream
        stream._thread = thread
        return stream

    @classmethod
    def _inert(cls) -> "_LiveStream":
        stream = cls.__new__(cls)
        stream._path = None  # type: ignore[assignment]
        stream._owner = ""
        stream._timeout_seconds = 0
        stream._queue = queue.Queue(maxsize=1)
        stream._closing = threading.Event()
        stream._incomplete = threading.Event()
        stream._incomplete.set()
        stream._thread = None
        stream._decoder = None
        stream._terminal = None
        stream._handle = None
        stream._written = 0
        stream._seq = 0
        return stream

    # -- lifecycle-thread side: in-memory only, never blocking --------------
    def stage_start(self, *, child_pid: int, encoding: str) -> None:
        try:
            # Replacement errors, so invalid bytes render as U+FFFD in live
            # evidence while the authoritative decode still raises. That
            # divergence is the point of "non-authoritative".
            self._decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
            self._offer(
                {
                    "event": "stage_start",
                    "schema_version": LIVE_SCHEMA_VERSION,
                    "seq": None,
                    "ts_ms": _ms(time.time()),
                    "owner": self._owner,
                    "child_pid": child_pid,
                    "timeout_seconds": self._timeout_seconds,
                    "encoding": encoding,
                }
            )
        except Exception:
            self._decoder = None
            self._degrade()

    def fragment(self, chunk: bytes) -> None:
        try:
            if self._decoder is None:
                self._incomplete.set()
                return
            self._emit_text(self._decoder.decode(chunk))
        except Exception:
            self._degrade()

    def heartbeat(self) -> None:
        try:
            self._offer({"event": "heartbeat", "seq": None, "ts_ms": _ms(time.time())})
        except Exception:
            self._degrade()

    def eof(self) -> None:
        try:
            if self._decoder is None:
                return
            self._emit_text(self._decoder.decode(b"", final=True))
        except Exception:
            self._degrade()

    def close(self, *, process: subprocess.Popen[str] | None, timed_out: bool) -> None:
        """Admit the terminal record always; wait for it only when free to.

        Takes the `Popen` rather than a pre-computed exit code so the reaped
        test, the `returncode` read and the join decision all happen inside
        this method's exception wrapper - the call site at the convergence
        point stays as exception-free as it was before H2.
        """
        try:
            exit_code: int | None = None
            reaped = False
            if process is not None and process.poll() is not None:
                reaped = True
                exit_code = process.returncode
            self._terminal = {
                "event": "stage_end",
                "seq": None,
                "ts_ms": _ms(time.time()),
                # Explicitly non-authoritative: on the decode-failure paths the
                # authoritative exit_code is None while poll() returns the
                # child's real code, and with no reaped child this is null.
                "exit_code": exit_code,
                "timed_out": bool(timed_out),
                "live_complete": None,
            }
            self._closing.set()
            try:
                self._queue.put_nowait(_LIVE_TERMINAL)
            except queue.Full:
                self._incomplete.set()
            thread = self._thread
            # Only a path where the runner already reaped the child may wait.
            # On a spawn OSError, a mid-drain OSError, or the unreaped variant
            # of the interrupt path, the runner returns to the controller
            # immediately - in the mid-drain case with an unowned child still
            # running - and a slow writer must not add latency to exactly
            # those. There, live evidence is best effort: the daemon writer may
            # still persist stage_end, but nothing waits for it.
            if reaped and thread is not None and thread.is_alive():
                thread.join(LIVE_CLOSE_JOIN_SECONDS)
        except Exception:
            self._degrade()

    def _emit_text(self, text: str) -> None:
        if not text:
            return
        # Bounded per-record size. Not newline-normalized: normalization is
        # stateful across chunk boundaries and belongs to the authoritative
        # path only.
        for start in range(0, len(text), LIVE_FRAGMENT_MAX_CHARS):
            self._offer(
                {
                    "event": "output_fragment",
                    "seq": None,
                    "ts_ms": _ms(time.time()),
                    "text": text[start : start + LIVE_FRAGMENT_MAX_CHARS],
                }
            )

    def _offer(self, record: dict[str, Any]) -> None:
        if self._thread is None:
            self._incomplete.set()
            return
        # The lifecycle thread is the sole producer, so admission needs no
        # lock. Optional records stop one short of the queue's capacity, which
        # keeps the last slot for the terminal record - the same
        # terminal-reserve arithmetic the byte budget uses, applied to depth.
        if self._queue.qsize() >= LIVE_QUEUE_MAX_RECORDS:
            self._incomplete.set()
            return
        self._queue.put_nowait(record)

    def _degrade(self) -> None:
        self._incomplete.set()

    # -- writer thread side: the only place that touches the filesystem -----
    def _writer(self) -> None:
        try:
            try:
                self._handle = self._create_handle(self._path)
            except BaseException:
                self._handle = None
                self._incomplete.set()
            while True:
                item = self._queue.get()
                if self._closing.is_set():
                    # Discard the optional backlog rather than write it, so
                    # close()'s bounded join is not spent on records that are
                    # no longer worth having.
                    if item is not _LIVE_TERMINAL or not self._queue.empty():
                        self._incomplete.set()
                    self._finalize()
                    return
                self._write_record(item)
        except BaseException:
            # This thread has no lifecycle thread to propagate into, so it
            # contains everything, including process-control exceptions.
            self._incomplete.set()
            self._close_handle()

    def _write_record(self, record: dict[str, Any]) -> None:
        if self._handle is None:
            self._incomplete.set()
            return
        try:
            record["seq"] = self._seq
            line = json.dumps(record, ensure_ascii=False).encode("utf-8") + b"\n"
        except Exception:
            self._incomplete.set()
            return
        if self._written + len(line) + LIVE_TERMINAL_RESERVE_BYTES > LIVE_MAX_BYTES:
            # An optional record may never eat into the terminal reserve. A
            # continuous writer that exhausts the budget therefore also stops
            # producing heartbeats and the stream looks stalled - which is what
            # stage_end.live_complete=false tells the reader.
            self._incomplete.set()
            return
        try:
            self._handle.write(line)
            self._handle.flush()
        except Exception:
            self._incomplete.set()
            self._close_handle()
            return
        self._written += len(line)
        self._seq += 1

    def _finalize(self) -> None:
        record = self._terminal or {
            "event": "stage_end",
            "seq": None,
            "ts_ms": _ms(time.time()),
            "exit_code": None,
            "timed_out": False,
            "live_complete": None,
        }
        try:
            record["seq"] = self._seq
            record["live_complete"] = not self._incomplete.is_set()
            line = json.dumps(record, ensure_ascii=False).encode("utf-8") + b"\n"
        except Exception:
            self._close_handle()
            return
        # stage_end may consume the reserve. Because the reserve is larger than
        # any stage_end line, rejection by byte-budget exhaustion is impossible
        # here by arithmetic rather than by a runtime check.
        if self._handle is not None and self._written + len(line) <= LIVE_MAX_BYTES:
            try:
                self._handle.write(line)
                self._handle.flush()
                self._written += len(line)
                self._seq += 1
            except Exception:
                pass
        self._close_handle()

    @staticmethod
    def _create_handle(path: Path) -> Any:
        path.parent.mkdir(parents=True, exist_ok=True)
        # No fsync: a same-host reader sees flushed bytes, and a per-record
        # fsync would add I/O latency for no gain.
        return path.open("ab")

    def _close_handle(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            handle.close()
        except Exception:
            pass


class SubprocessRunner:
    def preflight(self, owner: str, timeout: int = 5) -> ProviderPreflightResult:
        command = self._command(owner)
        started = time.time()
        started_ms = _ms(started)
        model = self._model_from_command(command)
        env_issue = self._environment_issue(owner)
        if env_issue is not None:
            ended_ms = _ms(time.time())
            return ProviderPreflightResult(
                "blocked",
                env_issue,
                f"{env_issue}: provider environment/config check failed\n",
                None,
                command,
                model,
                started_ms,
                ended_ms,
            )

        executable = command[0]
        resolved = _resolve_executable(executable)
        if resolved is None:
            ended_ms = _ms(time.time())
            return ProviderPreflightResult(
                "blocked",
                "provider_cli_unavailable",
                f"provider CLI not found or not executable: {executable}\n",
                127,
                command,
                model,
                started_ms,
                ended_ms,
            )

        probe = [resolved, "--version"]
        exit_code: int | None = None
        output = ""
        timed_out = False
        try:
            completed = subprocess.run(
                probe,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=timeout,
                check=False,
            )
            exit_code = completed.returncode
            output = completed.stdout
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            output = (exc.stdout or "") + (exc.stderr or "")
        except OSError as exc:
            output = f"provider preflight spawn failed: {exc}\n"
            exit_code = 127
        ended_ms = _ms(time.time())
        if timed_out:
            return ProviderPreflightResult(
                "blocked", "provider_preflight_timeout", output, exit_code, command, model, started_ms, ended_ms, True
            )
        if any(pattern.search(output) for pattern in RATE_LIMIT_SIGNATURES):
            return ProviderPreflightResult(
                "paused", "rate_limited", output, exit_code, command, model, started_ms, ended_ms
            )
        if any(pattern.search(output) for pattern in SOCKET_SIGNATURES):
            return ProviderPreflightResult(
                "blocked", "provider_socket_error", output, exit_code, command, model, started_ms, ended_ms
            )
        if exit_code != 0:
            return ProviderPreflightResult(
                "blocked", "provider_preflight_failed", output, exit_code, command, model, started_ms, ended_ms
            )
        return ProviderPreflightResult("pass", "provider_preflight_pass", output, exit_code, command, model, started_ms, ended_ms)

    def run(
        self,
        owner: str,
        prompt: str,
        timeout: int,
        log_path: Path,
        *,
        workspace: Path | None = None,
        protected_roots: tuple[Path, ...] | None = None,
        reports_dir: Path | None = None,
    ) -> RunResult:
        command = self._command(owner) + [prompt]
        model_command = command
        containment_env = None
        if workspace is not None:
            try:
                containment_env = prepare_containment(workspace, log_path)
                if reports_dir is not None:
                    # Reports live in the task's artifact area, not the
                    # workspace: the sandbox must allow the directory (an
                    # allowlist entry only exists for paths that exist), and
                    # the stage gets its location in the environment too.
                    reports_dir.mkdir(parents=True, exist_ok=True)
                    containment_env["ORCH_REPORTS_DIR"] = str(reports_dir)
            except SandboxSetupError as exc:
                return self._containment_stop(
                    log_path, owner, command, "sandbox_setup_failed", str(exc)
                )
            except ContainmentError as exc:
                return self._containment_stop(
                    log_path, owner, command, "containment_identity_invalid", str(exc)
                )
            except OSError as exc:
                return self._containment_stop(
                    log_path, owner, command, "sandbox_setup_failed", f"cannot create reports directory: {exc}"
                )
            try:
                decision = prepare_sandbox(
                    workspace,
                    log_path.parent / "containment",
                    allow_unsandboxed=allow_unsandboxed_requested(),
                    extra_allow=(reports_dir,) if reports_dir is not None else (),
                    protected_roots=protected_roots,
                )
            except ContainmentConfigError as exc:
                # A declared write root that overlaps a protected root is a
                # contradiction, not a preference. Refuse the stage and say so.
                return self._containment_stop(
                    log_path, owner, command, "containment_config_conflict", str(exc)
                )
            except SandboxSetupError as exc:
                # A full disk or an unwritable artifact directory is an
                # environment problem. Reporting it as a config conflict would
                # send the operator to edit settings that are already correct.
                return self._containment_stop(
                    log_path, owner, command, "sandbox_setup_failed", str(exc)
                )
            if decision.blocks_run:
                return self._containment_stop(
                    log_path,
                    owner,
                    command,
                    "sandbox_unavailable",
                    "L1 sandbox is unavailable on this host and --allow-unsandboxed was not given; "
                    "refusing to run a mutating stage unconfined",
                )
            containment_env["ORCH_CONTAINMENT_SANDBOX"] = decision.mode
            model_command = command
            command = decision.wrap(command)
        # Started before `started = time.time()` so the writer thread's
        # creation cost can reach neither the deadline window nor the
        # authoritative duration interval. open() raises no Exception, so the
        # handle is bound on every path that reaches the convergence point -
        # including a spawn OSError.
        live = _LiveStream.open(log_path, owner=owner, timeout_seconds=timeout)
        started = time.time()
        exit_code: int | None = None
        output = ""
        timed_out = False
        error: str | None = None
        child_pid: int | None = None
        process: subprocess.Popen[str] | None = None
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,  # without this, claude -p waits on stdin EOF until the timeout
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
                cwd=str(workspace) if workspace is not None else None,
                env=containment_env,
            )
            child_pid = process.pid
            containment_line = f"containment_workspace={workspace}\n" if workspace is not None else ""
            self._append_live_status(
                log_path, f"{containment_line}provider_child_pid={child_pid}\nstage_status=running\n"
            )
            stdout_encoding = process.stdout.encoding
            stdout_errors = process.stdout.errors
            # communicate() started its clock here; keeping the origin in the
            # same statement position keeps the deadline the child runs under
            # byte-for-byte the one it ran under before.
            deadline = time.monotonic() + timeout
            live.stage_start(child_pid=child_pid, encoding=stdout_encoding)
            chunks: list[bytes] = []
            try:
                self._drain_pipe(process, chunks, live, deadline, timeout)
                process.wait(timeout=deadline - time.monotonic())
            except subprocess.TimeoutExpired:
                timed_out = True
                self._terminate_group(process)
                # The final drain has no deadline, exactly as the second
                # communicate() call had none. A grandchild still holding the
                # pipe blocks the worker here as it always did.
                self._drain_pipe(process, chunks, live, None, timeout)
                process.wait()
            live.eof()
            # One whole-buffer strict decode, after the child is reaped and
            # before exit_code is read: a UnicodeDecodeError therefore leaves
            # exit_code at None, which is what the sealed log has always shown.
            output = _decode_authoritative(b"".join(chunks), stdout_encoding, stdout_errors)
            exit_code = process.returncode
        except OSError as exc:
            error = f"failed to spawn runner: {exc}"
            output = error + "\n"
        except BaseException as exc:
            if process is not None and process.poll() is None:
                self._terminate_group(process)
                exit_code = process.returncode
            error = f"runner interrupted: {type(exc).__name__}: {exc}"
            output += error + "\n"
        ended = time.time()
        live.close(process=process, timed_out=timed_out)
        self._write_log(log_path, owner, command, started, ended, exit_code, timed_out, output, error, child_pid)
        usage = _extract_usage(output)
        return RunResult(
            exit_code,
            output,
            None,
            "raw",
            "raw",
            timed_out,
            started_at_ms=_ms(started),
            ended_at_ms=_ms(ended),
            duration_ms=max(0, _ms(ended) - _ms(started)),
            model=SubprocessRunner._model_from_command(model_command) or "unspecified",
            usage_input_tokens=usage["input_tokens"],
            usage_output_tokens=usage["output_tokens"],
            usage_total_tokens=usage["total_tokens"],
            usage_unavailable_reason=usage["unavailable_reason"],
        )

    def _containment_stop(
        self, log_path: Path, owner: str, command: list[str], reason: str, message: str
    ) -> RunResult:
        """Report a stage that was never allowed to start, with its own reason.

        The log still gets written, because "why did nothing run" is exactly
        the question an operator asks next.
        """
        now = time.time()
        text = f"{reason}: {message}\n"
        try:
            self._write_log(log_path, owner, command, now, now, None, False, text, message, None)
        except OSError:
            # The reason this stage stopped may *be* that the directory is
            # unwritable. Reporting must not depend on the thing that failed;
            # the stop reason travels in the result either way.
            pass
        return RunResult(
            None,
            text,
            None,
            "raw",
            "raw",
            False,
            containment_stop=reason,
            started_at_ms=_ms(now),
            ended_at_ms=_ms(now),
            duration_ms=0,
            model=SubprocessRunner._model_from_command(command) or "unspecified",
        )

    @staticmethod
    def _command(owner: str) -> list[str]:
        if owner == "claude":
            raw = os.environ.get("ORCH_CLAUDE_COMMAND", "claude -p")
        elif owner == "codex":
            raw = os.environ.get("ORCH_CODEX_COMMAND", "codex exec")
        else:
            raise ValueError(f"unsupported owner: {owner}")
        command = shlex.split(raw)
        if not command:
            raise ValueError(f"empty command for owner {owner}")
        return command

    @staticmethod
    def _drain_pipe(
        process: subprocess.Popen[str],
        chunks: list[bytes],
        live: "_LiveStream",
        deadline: float | None,
        orig_timeout: float,
    ) -> None:
        """Read the provider pipe to EOF, or until the deadline expires.

        Structured after subprocess.Popen._communicate on purpose: the parity
        argument for the provider's lifecycle has to be readable line by line
        against the loop this replaces. The only differences are that raw bytes
        are accumulated for one whole-buffer decode later, that select() is
        bounded so a silent child still produces evidence, and that each chunk
        is offered to the live stream - an in-memory step that cannot raise or
        block into this loop.
        """
        stdout = process.stdout
        if stdout is None or stdout.closed:
            return  # EOF was already reached in the first phase
        with selectors.DefaultSelector() as selector:
            selector.register(stdout, selectors.EVENT_READ)
            while True:
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(process.args, orig_timeout)
                    wait_for = min(remaining, LIVE_POLL_SECONDS)
                else:
                    wait_for = LIVE_POLL_SECONDS
                ready = selector.select(wait_for)
                # Checked again before any ready event is consumed, exactly
                # where _check_timeout sits in _communicate. min() above is
                # also what keeps the poll bound from extending the deadline.
                if deadline is not None and time.monotonic() > deadline:
                    raise subprocess.TimeoutExpired(process.args, orig_timeout)
                if not ready:
                    live.heartbeat()
                    continue
                key = ready[0][0]
                # An OSError here lands in run()'s OSError handler, which
                # reports "failed to spawn runner", does not terminate, and
                # returns while the child keeps running unowned. Misleading,
                # and current behaviour.
                data = os.read(key.fd, DRAIN_READ_BYTES)
                if not data:
                    selector.unregister(stdout)
                    stdout.close()
                    return
                chunks.append(data)
                live.fragment(data)

    @staticmethod
    def _terminate_group(process: subprocess.Popen[str]) -> None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=3)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    @staticmethod
    def _write_log(
        path: Path,
        owner: str,
        command: list[str],
        started: float,
        ended: float,
        exit_code: int | None,
        timed_out: bool,
        output: str,
        error: str | None,
        child_pid: int | None,
    ) -> None:
        usage = _extract_usage(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        header = [
            f"started_at={started:.6f}",
            f"ended_at={ended:.6f}",
            f"duration_seconds={ended - started:.3f}",
            f"owner={owner}",
            f"model={SubprocessRunner._model_from_command(command) or 'unspecified'}",
            f"command={shlex.join(command)}",
            f"child_pid={_format_unavailable(child_pid)}",
            f"exit_code={exit_code}",
            f"timed_out={str(timed_out).lower()}",
            f"usage_input_tokens={_format_unavailable(usage['input_tokens'])}",
            f"usage_output_tokens={_format_unavailable(usage['output_tokens'])}",
            f"usage_total_tokens={_format_unavailable(usage['total_tokens'])}",
            f"usage_unavailable_reason={usage['unavailable_reason'] or 'none'}",
        ]
        if error:
            header.append(f"controller_error={error}")
        path.write_text("\n".join(header) + "\n\n--- output ---\n" + output, encoding="utf-8")

    @staticmethod
    def _append_live_status(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(text)

    @staticmethod
    def _model_from_command(command: list[str]) -> str | None:
        for index, value in enumerate(command):
            if value in {"--model", "-m"} and index + 1 < len(command):
                return command[index + 1]
            if value.startswith("--model="):
                return value.split("=", 1)[1]
        return None

    @staticmethod
    def _environment_issue(owner: str) -> str | None:
        if owner == "claude":
            base_url = os.environ.get("ANTHROPIC_BASE_URL", "")
            if base_url and LOCALHOST_SIGNATURE.search(base_url):
                return "provider_socket_misconfigured"
        if owner == "codex":
            return _codex_config_issue()
        return None


def _allowed_codex_service_tiers() -> frozenset[str]:
    """Codex service tiers accepted by provider preflight.

    Defaults to the tiers real Codex configs use ("fast", "priority"). This
    check validates whether the current `~/.codex/config.toml` is usable, so
    the default must not reject a normal Codex setup. Override with
    ORCH_CODEX_SERVICE_TIERS (comma-separated) to enforce a narrower cost
    guardrail without editing `~/.codex/config.toml`.
    """
    raw = os.environ.get("ORCH_CODEX_SERVICE_TIERS")
    if raw:
        return frozenset(part.strip() for part in raw.split(",") if part.strip())
    return DEFAULT_CODEX_SERVICE_TIERS


def _codex_config_issue() -> str | None:
    config_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    config_path = config_home / "config.toml"
    if not config_path.exists():
        return None
    try:
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return "provider_config_invalid"
    service_tier = data.get("service_tier")
    if service_tier is not None and service_tier not in _allowed_codex_service_tiers():
        return "provider_config_invalid"
    return None


def _resolve_executable(value: str) -> str | None:
    path = Path(value)
    if path.parent != Path("."):
        return str(path) if path.is_file() and os.access(path, os.X_OK) else None
    return shutil.which(value)


def _extract_usage(output: str) -> dict[str, int | str | None]:
    found: dict[str, int | None] = {"input_tokens": None, "output_tokens": None, "total_tokens": None}
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped or stripped[:1] not in "{[":
            continue
        try:
            parsed = json.loads(stripped)
        except ValueError:
            continue
        _merge_usage(found, _usage_from_json(parsed))

    regex_pairs = {
        "input_tokens": (
            re.compile(r"\binput[_ -]?tokens?\b\s*[:=]\s*(\d+)", re.IGNORECASE),
            re.compile(r"\bprompt[_ -]?tokens?\b\s*[:=]\s*(\d+)", re.IGNORECASE),
        ),
        "output_tokens": (
            re.compile(r"\boutput[_ -]?tokens?\b\s*[:=]\s*(\d+)", re.IGNORECASE),
            re.compile(r"\bcompletion[_ -]?tokens?\b\s*[:=]\s*(\d+)", re.IGNORECASE),
        ),
        "total_tokens": (re.compile(r"\btotal[_ -]?tokens?\b\s*[:=]\s*(\d+)", re.IGNORECASE),),
    }
    for key, patterns in regex_pairs.items():
        if found[key] is not None:
            continue
        for pattern in patterns:
            match = pattern.search(output)
            if match:
                found[key] = int(match.group(1))
                break
    if found["total_tokens"] is None and found["input_tokens"] is not None and found["output_tokens"] is not None:
        found["total_tokens"] = int(found["input_tokens"]) + int(found["output_tokens"])
    unavailable = None if any(value is not None for value in found.values()) else "provider_cli_usage_not_reported"
    return {**found, "unavailable_reason": unavailable}


def _usage_from_json(value: Any) -> dict[str, int | None]:
    found: dict[str, int | None] = {"input_tokens": None, "output_tokens": None, "total_tokens": None}
    if isinstance(value, list):
        for item in value:
            _merge_usage(found, _usage_from_json(item))
        return found
    if not isinstance(value, dict):
        return found
    candidates = [value]
    for key in ("usage", "token_usage", "tokens"):
        child = value.get(key)
        if isinstance(child, dict):
            candidates.append(child)
    aliases = {
        "input_tokens": ("input_tokens", "prompt_tokens", "input", "prompt"),
        "output_tokens": ("output_tokens", "completion_tokens", "output", "completion"),
        "total_tokens": ("total_tokens", "total"),
    }
    for candidate in candidates:
        for normalized, keys in aliases.items():
            if found[normalized] is not None:
                continue
            for key in keys:
                token_value = candidate.get(key)
                if isinstance(token_value, int):
                    found[normalized] = token_value
                    break
                if isinstance(token_value, str) and token_value.isdigit():
                    found[normalized] = int(token_value)
                    break
    return found


def _merge_usage(target: dict[str, int | None], source: dict[str, int | None]) -> None:
    for key, value in source.items():
        if target.get(key) is None and value is not None:
            target[key] = value


def _telemetry_from(source: RunResult | None) -> dict[str, int | str | None]:
    if source is None:
        usage = _extract_usage("")
        return {
            "started_at_ms": None,
            "ended_at_ms": None,
            "duration_ms": None,
            "model": None,
            "usage_input_tokens": usage["input_tokens"],
            "usage_output_tokens": usage["output_tokens"],
            "usage_total_tokens": usage["total_tokens"],
            "usage_unavailable_reason": usage["unavailable_reason"],
        }
    usage = {
        "input_tokens": source.usage_input_tokens,
        "output_tokens": source.usage_output_tokens,
        "total_tokens": source.usage_total_tokens,
        "unavailable_reason": source.usage_unavailable_reason,
    }
    if not any(usage[key] is not None for key in ("input_tokens", "output_tokens", "total_tokens")) and not usage[
        "unavailable_reason"
    ]:
        usage = _extract_usage(source.output)
    return {
        "started_at_ms": source.started_at_ms,
        "ended_at_ms": source.ended_at_ms,
        "duration_ms": source.duration_ms,
        "model": source.model,
        "usage_input_tokens": usage["input_tokens"],
        "usage_output_tokens": usage["output_tokens"],
        "usage_total_tokens": usage["total_tokens"],
        "usage_unavailable_reason": usage["unavailable_reason"],
    }


def _format_unavailable(value: int | None) -> str:
    return str(value) if value is not None else "unavailable"


def _ms(value: float) -> int:
    return int(value * 1000)


def _final_outcome_marker(output: str) -> str | None:
    for line in reversed(output.splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        match = OUTCOME_RE.match(stripped)
        return match.group(1) if match else None
    return None
