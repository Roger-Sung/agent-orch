"""Differential provider-lifecycle parity harness for H2 (spec section 7).

Every case runs the same fake child twice: once through a frozen verbatim copy
of the pre-H2 `SubprocessRunner.run` lifecycle (`_legacy_lifecycle`, the
oracle) and once through the live `SubprocessRunner.run`. The two observations
are then compared field by field. A recorded fixture is deliberately not used:
the required comparisons include child lifetime and `_terminate_group` call
count, which are host- and load-dependent.

Cases marked `H2.3` in the spec assert live-JSONL behaviour. They stay
declared here and skip themselves with a reason naming H2.3 until the real
`_LiveStream` exists, so no case is ever silently absent.
"""

from __future__ import annotations

import base64
import contextlib
import errno
import json
import os
import re
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

from orchestrator import runner as runner_module
from orchestrator.runner import RunResult, SubprocessRunner, _extract_usage, classify_result

# One definition, used by every timing assertion (spec section 7). 150 ms is
# 15% of LIVE_POLL_SECONDS, so neither a poll-sized drift (1000 ms) nor a full
# LIVE_CLOSE_JOIN_SECONDS join (1000 ms) can pass.
DEADLINE_TOLERANCE_MS = 150
TIMING_REPEATS = 3
# C16's return time is not anchored to a deadline: it is dominated by the
# fake child's own startup, whose distribution is bimodal on a loaded host.
# The minimum is the unstable statistic there, so those cases compare the
# median of a few more runs instead (see the report's deviations).
RETURN_TIME_REPEATS = 5

H2_3_REASON = "activated at H2.3 (real _LiveStream not present yet)"

LEGACY_READ_SIZE = 32768
PROMPT = "parity-prompt"
ALLOWED_OUTCOMES = frozenset({"submit"})

# Fields of RunResult compared across implementations. started_at_ms,
# ended_at_ms and duration_ms are wall-clock and are asserted separately by the
# timing protocol instead of by equality.
COMPARED_RESULT_FIELDS = (
    "exit_code",
    "output",
    "outcome",
    "classification",
    "reason",
    "timed_out",
    "containment_stop",
    "containment_violations",
    "model",
    "usage_input_tokens",
    "usage_output_tokens",
    "usage_total_tokens",
    "usage_unavailable_reason",
)

VOLATILE_LOG_KEYS = frozenset({"started_at", "ended_at", "duration_seconds", "child_pid"})
LOG_BODY_SEPARATOR = "\n\n--- output ---\n"

_CHILD_PID_RE = re.compile(r"(?:provider_)?child_pid=(\d+)")

# Every pid the harness ever spawned, so tearDownModule can prove no orphan
# survived the module (spec section 7, "Orphan hygiene").
_RECORDED_PIDS: set[int] = set()

_FAKE_CHILD_SOURCE = r'''
import base64
import os
import sys
import time

out = sys.stdout.buffer


def _detach_stdout():
    """Close the pipe write end for real, but keep fd 1 valid."""
    out.flush()
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    os.close(devnull)


for op in sys.argv[1].split(";"):
    if not op:
        continue
    kind, _, arg = op.partition(":")
    if kind == "text":
        out.write(arg.encode("utf-8"))
        out.flush()
    elif kind == "b64":
        out.write(base64.b64decode(arg))
        out.flush()
    elif kind == "pad":
        out.write(b"a" * int(arg))
        out.flush()
    elif kind == "bulk":
        total = int(arg)
        block = b"z" * 4096
        written = 0
        while written < total:
            take = min(len(block), total - written)
            out.write(block[:take])
            written += take
        out.flush()
    elif kind == "sleep":
        time.sleep(float(arg))
    elif kind == "loop":
        end = time.monotonic() + float(arg)
        while time.monotonic() < end:
            out.write(b"x" * 512)
            out.flush()
            time.sleep(0.002)
    elif kind == "close":
        _detach_stdout()
    elif kind == "exit":
        out.flush()
        os._exit(int(arg))
    else:
        raise SystemExit("unknown fake-child op: " + kind)
out.flush()
os._exit(0)
'''

_MODULE_TMP: tempfile.TemporaryDirectory | None = None
_CHILD_SCRIPT: Path


def setUpModule() -> None:
    global _MODULE_TMP, _CHILD_SCRIPT
    _MODULE_TMP = tempfile.TemporaryDirectory(prefix="h2-parity-")
    _CHILD_SCRIPT = Path(_MODULE_TMP.name) / "fake_child.py"
    _CHILD_SCRIPT.write_text(_FAKE_CHILD_SOURCE, encoding="utf-8")


def tearDownModule() -> None:
    # The C16 cases deliberately leave a live, unowned child under *both*
    # implementations (spec section 4.6, path G), so the sweep kills them. A
    # pid that is still *running* afterwards would be a real leak; a pid that
    # is only waiting to be reaped is not, because this process reaps it.
    survivors = sorted(pid for pid in _RECORDED_PIDS if _running(pid))
    for pid in survivors:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pid, signal.SIGKILL)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and any(_running(pid) for pid in survivors):
        with contextlib.suppress(Exception):
            subprocess._cleanup()  # type: ignore[attr-defined]
        time.sleep(0.05)
    still_running = [pid for pid in survivors if _running(pid)]
    if _MODULE_TMP is not None:
        _MODULE_TMP.cleanup()
    assert not still_running, f"orphan children survived: {still_running}"


def _child_command(child_spec: str) -> str:
    return shlex.join([sys.executable, str(_CHILD_SCRIPT), child_spec])


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _process_state(pid: int) -> str:
    """`ps` state letter for a pid, or "" when it no longer exists."""
    try:
        completed = subprocess.run(
            ["/bin/ps", "-o", "state=", "-p", str(pid)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return completed.stdout.strip()


def _running(pid: int) -> bool:
    """True only for a process that still has a runnable state.

    A zombie answers `os.kill(pid, 0)` but is not a surviving child; it is a
    child this process has not reaped yet, which is exactly what legacy leaves
    behind on path G too.
    """
    state = _process_state(pid)
    return bool(state) and not state.startswith("Z")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# --------------------------------------------------------------------------
# The oracle: verbatim copy of orchestrator/runner.py `SubprocessRunner.run`
# lines 494-551 as of d977b84 (the H2 implementation base), reduced to the
# workspace=None case every harness case uses. Nothing below this comment is
# allowed to drift; it is the baseline the new drain is compared against.
# --------------------------------------------------------------------------
def _legacy_lifecycle(
    runner: SubprocessRunner, owner: str, prompt: str, timeout: float, log_path: Path
) -> RunResult:
    command = runner._command(owner) + [prompt]
    model_command = command
    containment_env = None
    workspace = None

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
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
            cwd=str(workspace) if workspace is not None else None,
            env=containment_env,
        )
        child_pid = process.pid
        containment_line = f"containment_workspace={workspace}\n" if workspace is not None else ""
        runner._append_live_status(
            log_path, f"{containment_line}provider_child_pid={child_pid}\nstage_status=running\n"
        )
        try:
            output, _ = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            runner._terminate_group(process)
            output, _ = process.communicate()
        exit_code = process.returncode
    except OSError as exc:
        error = f"failed to spawn runner: {exc}"
        output = error + "\n"
    except BaseException as exc:
        if process is not None and process.poll() is None:
            runner._terminate_group(process)
            exit_code = process.returncode
        error = f"runner interrupted: {type(exc).__name__}: {exc}"
        output += error + "\n"
    ended = time.time()
    runner._write_log(
        log_path, owner, command, started, ended, exit_code, timed_out, output, error, child_pid
    )
    usage = _extract_usage(output)
    return RunResult(
        exit_code,
        output,
        None,
        "raw",
        "raw",
        timed_out,
        started_at_ms=runner_module._ms(started),
        ended_at_ms=runner_module._ms(ended),
        duration_ms=max(0, runner_module._ms(ended) - runner_module._ms(started)),
        model=SubprocessRunner._model_from_command(model_command) or "unspecified",
        usage_input_tokens=usage["input_tokens"],
        usage_output_tokens=usage["output_tokens"],
        usage_total_tokens=usage["total_tokens"],
        usage_unavailable_reason=usage["unavailable_reason"],
    )


# --------------------------------------------------------------------------
# Observation
# --------------------------------------------------------------------------
@dataclass
class Observation:
    impl: str
    escaped_exception: str | None
    result_fields: dict[str, object] | None
    result_duration_ms: int | None
    classification: tuple[object, ...] | None
    log_text: str
    log_duration_seconds: float | None
    error_text: str | None
    child_pid: int | None
    child_lifetime_ms: int | None
    alive_at_return: bool
    return_elapsed_ms: float
    returned_at_ms: int | None
    result_ended_at_ms: int | None
    terminate_calls: int
    log_path: Path
    live_path: Path

    @property
    def post_ended_ms(self) -> float | None:
        """Wall-clock time spent after `ended = time.time()` was taken.

        `ended` precedes `close()`, so this interval contains close() plus
        `_write_log` and nothing else. Comparing it against the oracle isolates
        the close join from the fake child's own startup variance, which is
        what makes the C16 join assertions stable without widening the
        tolerance.
        """
        if self.returned_at_ms is None or self.result_ended_at_ms is None:
            return None
        return float(self.returned_at_ms - self.result_ended_at_ms)

    def live_records(self) -> list[dict]:
        if not self.live_path.is_file():
            return []
        records = []
        for line in self.live_path.read_text(encoding="utf-8").splitlines():
            if line:
                records.append(json.loads(line))
        return records


class _TerminateCounter:
    def __init__(self) -> None:
        self.count = 0
        self._real = SubprocessRunner._terminate_group

    def __call__(self, process: subprocess.Popen[str]) -> None:
        self.count += 1
        self._real(process)


class _ChildWatcher(threading.Thread):
    """Observe when the runner stops owning the child, without touching it."""

    def __init__(self, log_path: Path, origin: float) -> None:
        super().__init__(daemon=True)
        self._log_path = log_path
        self._origin = origin
        self._stop = threading.Event()
        self.pid: int | None = None
        self.gone_at: float | None = None

    def run(self) -> None:
        deadline = time.monotonic() + 180
        while not self._stop.is_set() and time.monotonic() < deadline:
            pid = _read_child_pid(self._log_path)
            if pid is not None:
                self.pid = pid
                _RECORDED_PIDS.add(pid)
                break
            time.sleep(0.002)
        if self.pid is None:
            return
        while not self._stop.is_set() and time.monotonic() < deadline:
            if not _alive(self.pid):
                self.gone_at = time.monotonic()
                return
            time.sleep(0.002)

    def stop(self) -> None:
        self._stop.set()
        self.join(timeout=2.0)

    def lifetime_ms(self) -> int | None:
        if self.gone_at is None:
            return None
        return int(round((self.gone_at - self._origin) * 1000))


def _read_child_pid(log_path: Path) -> int | None:
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = _CHILD_PID_RE.search(text)
    return int(match.group(1)) if match else None


def _mask_log(text: str) -> str:
    header, separator, body = text.partition(LOG_BODY_SEPARATOR)
    masked = []
    for line in header.splitlines():
        key = line.split("=", 1)[0]
        masked.append(f"{key}=<masked>" if key in VOLATILE_LOG_KEYS else line)
    return "\n".join(masked) + separator + body


def _log_field(text: str, key: str) -> str | None:
    header = text.partition(LOG_BODY_SEPARATOR)[0]
    for line in header.splitlines():
        name, _, value = line.partition("=")
        if name == key:
            return value
    return None


OUTPUT_DEPENDENT_FIELDS = frozenset({"output"})


def _without_output(fields: dict[str, object] | None) -> dict[str, object] | None:
    if fields is None:
        return None
    return {name: value for name, value in fields.items() if name not in OUTPUT_DEPENDENT_FIELDS}


def _classification_reason(observation: Observation) -> tuple[object, ...] | None:
    if observation.classification is None:
        return None
    keep = ("exit_code", "outcome", "classification", "reason", "timed_out", "containment_stop")
    index = {name: position for position, name in enumerate(COMPARED_RESULT_FIELDS)}
    return tuple(observation.classification[index[name]] for name in keep)


def _log_header(text: str) -> str:
    return text.partition(LOG_BODY_SEPARATOR)[0]


def _live_path_for(log_path: Path) -> Path:
    return log_path.with_suffix(".live.jsonl")


def _live_emission_available() -> bool:
    """True once H2.3's real `_LiveStream` exists.

    H2.2 lands the drain with a placeholder emitter and no live constants, so
    the presence of `LIVE_SCHEMA_VERSION` is the capability signal rather than
    a flag the production code carries for the tests.
    """
    return hasattr(runner_module, "_LiveStream") and hasattr(runner_module, "LIVE_SCHEMA_VERSION")


class _ParityCase(unittest.TestCase):
    """Shared observation helper plus the per-field comparison."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="h2-case-")
        self.addCleanup(self._tmp.cleanup)
        self.workdir = Path(self._tmp.name)
        self._counter = 0

    # -- observation -------------------------------------------------------
    def observe(
        self,
        *,
        impl: str,
        child_spec: str,
        timeout: float,
        owner: str = "claude",
        popen_patch: object | None = None,
        command_override: str | None = None,
        run_hook: object | None = None,
        log_name: str | None = None,
    ) -> Observation:
        self._counter += 1
        log_path = self.workdir / (log_name or f"{impl}-{self._counter:03d}.log")
        counter = _TerminateCounter()
        command = command_override or _child_command(child_spec)
        env = {"ORCH_CLAUDE_COMMAND": command, "ORCH_CODEX_COMMAND": command}
        runner = SubprocessRunner()
        watcher = _ChildWatcher(log_path, time.monotonic())
        escaped: str | None = None
        result: RunResult | None = None

        stack = contextlib.ExitStack()
        with stack:
            stack.enter_context(mock.patch.dict(os.environ, env))
            stack.enter_context(
                mock.patch.object(SubprocessRunner, "_terminate_group", staticmethod(counter))
            )
            if popen_patch is not None:
                stack.enter_context(mock.patch.object(subprocess, "Popen", popen_patch))
            if run_hook is not None:
                hooks = run_hook if isinstance(run_hook, (list, tuple)) else [run_hook]
                for hook in hooks:
                    stack.enter_context(hook)
            watcher._origin = time.monotonic()
            watcher.start()
            started = time.monotonic()
            try:
                if impl == "legacy":
                    result = _legacy_lifecycle(runner, owner, PROMPT, timeout, log_path)
                else:
                    result = runner.run(owner, PROMPT, timeout, log_path)
            except BaseException as exc:  # noqa: BLE001 - C15 asserts nothing escapes
                escaped = f"{type(exc).__name__}: {exc}"
            returned_at_ms = runner_module._ms(time.time())
            elapsed_ms = (time.monotonic() - started) * 1000
            alive_at_return = watcher.pid is not None and _alive(watcher.pid)
            watcher.stop()

        if watcher.pid is not None:
            _RECORDED_PIDS.add(watcher.pid)
            self.addCleanup(self._reap, watcher.pid)
        log_text = log_path.read_text(encoding="utf-8") if log_path.is_file() else ""
        duration = _log_field(log_text, "duration_seconds")
        return Observation(
            impl=impl,
            escaped_exception=escaped,
            result_fields=(
                {name: getattr(result, name) for name in COMPARED_RESULT_FIELDS}
                if result is not None
                else None
            ),
            result_duration_ms=result.duration_ms if result is not None else None,
            classification=(
                tuple(
                    getattr(
                        classify_result(
                            result.exit_code,
                            result.output,
                            set(ALLOWED_OUTCOMES),
                            result.timed_out,
                            source=result,
                        ),
                        name,
                    )
                    for name in COMPARED_RESULT_FIELDS
                )
                if result is not None
                else None
            ),
            log_text=_mask_log(log_text),
            log_duration_seconds=float(duration) if duration is not None else None,
            error_text=_log_field(log_text, "controller_error"),
            child_pid=watcher.pid,
            child_lifetime_ms=watcher.lifetime_ms(),
            alive_at_return=alive_at_return,
            return_elapsed_ms=elapsed_ms,
            returned_at_ms=returned_at_ms,
            result_ended_at_ms=result.ended_at_ms if result is not None else None,
            terminate_calls=counter.count,
            log_path=log_path,
            live_path=_live_path_for(log_path),
        )

    @staticmethod
    def _reap(pid: int) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pid, signal.SIGKILL)

    # -- comparisons -------------------------------------------------------
    def assert_lifecycle_parity(
        self, legacy: Observation, new: Observation, *, compare_output: bool = True
    ) -> None:
        """Compare the two observations field by field.

        `compare_output=False` is for a child whose byte count is inherently
        non-reproducible across two separate spawns - C3's continuous writer is
        killed at a deadline, so how much it managed to emit is not a parity
        property. Everything else, including the sealed log header, the
        classification and the termination count, is still compared exactly.
        """
        self.assertEqual(legacy.escaped_exception, new.escaped_exception, "escaped exception differs")
        self.assertIsNone(new.escaped_exception, "no exception may escape run()")
        self.assertEqual(legacy.error_text, new.error_text, "recorded runner error differs")
        if compare_output:
            self.assertEqual(legacy.result_fields, new.result_fields, "RunResult fields differ")
            self.assertEqual(
                legacy.classification, new.classification, "classify_result output differs"
            )
            self.assertEqual(legacy.log_text, new.log_text, "sealed log differs under the shared mask")
        else:
            self.assertEqual(
                _without_output(legacy.result_fields),
                _without_output(new.result_fields),
                "RunResult fields differ",
            )
            self.assertEqual(
                _classification_reason(legacy),
                _classification_reason(new),
                "classify_result verdict differs",
            )
            self.assertEqual(
                _log_header(legacy.log_text),
                _log_header(new.log_text),
                "sealed log header differs under the shared mask",
            )
        self.assertEqual(
            legacy.terminate_calls, new.terminate_calls, "_terminate_group call count differs"
        )

    def observe_pair(self, **kwargs: object) -> tuple[Observation, Observation]:
        legacy = self.observe(impl="legacy", **kwargs)  # type: ignore[arg-type]
        new = self.observe(impl="new", **kwargs)  # type: ignore[arg-type]
        return legacy, new

    def assert_parity(
        self, *, compare_output: bool = True, **kwargs: object
    ) -> tuple[Observation, Observation]:
        legacy, new = self.observe_pair(**kwargs)
        self.assert_lifecycle_parity(legacy, new, compare_output=compare_output)
        return legacy, new

    def observe_best(
        self, *, repeats: int = TIMING_REPEATS, hook_factory=None, **kwargs: object
    ) -> Observation:
        """Take the fastest of `repeats` runs, per the spec's jitter protocol.

        A single sample of a sub-second run cannot be held to
        DEADLINE_TOLERANCE_MS on a loaded host; the minimum can, without
        widening the tolerance.
        """
        runs: list[Observation] = []
        for _ in range(repeats):
            hook = hook_factory() if hook_factory is not None else None
            runs.append(self.observe(run_hook=hook, **kwargs))  # type: ignore[arg-type]
        for observation in runs:
            self.assertEqual(
                observation.terminate_calls,
                runs[0].terminate_calls,
                "_terminate_group count unstable across repetitions",
            )
        return min(runs, key=lambda observation: observation.return_elapsed_ms)

    def observe_typical(
        self, *, repeats: int = RETURN_TIME_REPEATS, hook_factory=None, **kwargs: object
    ) -> Observation:
        """Take the median-elapsed run of `repeats` runs."""
        runs: list[Observation] = []
        for _ in range(repeats):
            hook = hook_factory() if hook_factory is not None else None
            runs.append(self.observe(run_hook=hook, **kwargs))  # type: ignore[arg-type]
        for observation in runs:
            self.assertEqual(
                observation.terminate_calls,
                runs[0].terminate_calls,
                "_terminate_group count unstable across repetitions",
            )
        runs.sort(key=lambda observation: observation.return_elapsed_ms)
        return runs[len(runs) // 2]

    def assert_timing_parity(
        self,
        *,
        child_spec: str,
        timeout: float,
        absolute: bool = True,
        compare_lifetime: bool = True,
        compare_output: bool = True,
        owner: str = "claude",
    ) -> tuple[Observation, Observation]:
        """Run both implementations TIMING_REPEATS times and take the minimum.

        The minimum absorbs load jitter without widening the tolerance, which
        is what the spec's "over 3 repetitions taking the minimum" means.
        """
        samples: dict[str, list[Observation]] = {"legacy": [], "new": []}
        for _ in range(TIMING_REPEATS):
            for impl in ("legacy", "new"):
                samples[impl].append(
                    self.observe(impl=impl, child_spec=child_spec, timeout=timeout, owner=owner)
                )
        best = {
            impl: min(runs, key=lambda obs: obs.return_elapsed_ms) for impl, runs in samples.items()
        }
        legacy, new = best["legacy"], best["new"]
        for observation in (*samples["legacy"], *samples["new"]):
            self.assertEqual(
                observation.terminate_calls,
                legacy.terminate_calls,
                f"_terminate_group count unstable in {observation.impl}",
            )
        self.assert_lifecycle_parity(legacy, new, compare_output=compare_output)
        timeout_ms = timeout * 1000
        if absolute:
            # A legacy baseline that cannot meet the absolute bound is an
            # environment error, reported as such rather than widened away.
            self.assertLessEqual(
                abs(legacy.return_elapsed_ms - timeout_ms),
                DEADLINE_TOLERANCE_MS,
                "environment error: the legacy oracle itself missed the absolute deadline bound "
                f"({legacy.return_elapsed_ms:.1f} ms vs {timeout_ms:.1f} ms)",
            )
            self.assertLessEqual(
                abs(new.return_elapsed_ms - timeout_ms),
                DEADLINE_TOLERANCE_MS,
                f"absolute deadline drift: {new.return_elapsed_ms:.1f} ms vs {timeout_ms:.1f} ms",
            )
        self.assertLessEqual(
            abs(new.return_elapsed_ms - legacy.return_elapsed_ms),
            DEADLINE_TOLERANCE_MS,
            f"differential deadline drift: {new.return_elapsed_ms:.1f} ms vs "
            f"{legacy.return_elapsed_ms:.1f} ms",
        )
        if compare_lifetime and legacy.child_lifetime_ms is not None and new.child_lifetime_ms is not None:
            self.assertLessEqual(
                abs(new.child_lifetime_ms - legacy.child_lifetime_ms),
                DEADLINE_TOLERANCE_MS,
                f"child lifetime drift: {new.child_lifetime_ms} ms vs {legacy.child_lifetime_ms} ms",
            )
        return legacy, new

    def read_in_flight(self, owner: str) -> None:
        """C13/C14L: read the live stream while the child is provably alive."""
        log_path = self.workdir / f"inflight-{owner}.log"
        live_path = _live_path_for(log_path)
        command = _child_command(SPEC_C13)
        runner = SubprocessRunner()
        box: dict[str, object] = {}

        def target() -> None:
            box["result"] = runner.run(owner, PROMPT, 30, log_path)

        with mock.patch.dict(
            os.environ, {"ORCH_CLAUDE_COMMAND": command, "ORCH_CODEX_COMMAND": command}
        ):
            thread = threading.Thread(target=target, daemon=True)
            thread.start()
            deadline = time.monotonic() + 6
            records: list[dict] = []
            while time.monotonic() < deadline:
                if live_path.is_file():
                    lines = [
                        line
                        for line in live_path.read_text(encoding="utf-8").splitlines()
                        if line
                    ]
                    try:
                        parsed = [json.loads(line) for line in lines]
                    except json.JSONDecodeError:
                        time.sleep(0.01)
                        continue
                    if any(record["event"] == "stage_start" for record in parsed) and any(
                        record["event"] in {"output_fragment", "heartbeat"} for record in parsed
                    ):
                        records = parsed
                        break
                time.sleep(0.01)
            self.assertTrue(records, "live stream was not readable before the worker exited")
            start = next(record for record in records if record["event"] == "stage_start")
            pid = int(start["child_pid"])
            _RECORDED_PIDS.add(pid)
            self.addCleanup(self._reap, pid)
            self.assertTrue(_alive(pid), "child was already gone; readability was not in flight")
            thread.join(timeout=30)
        self.assertIsNotNone(box.get("result"))

    def require_live_emission(self) -> None:
        if not _live_emission_available():
            self.skipTest(H2_3_REASON)


# --------------------------------------------------------------------------
# Fault injection
# --------------------------------------------------------------------------
def _is_pipe(fd: int) -> bool:
    try:
        return stat.S_ISFIFO(os.fstat(fd).st_mode)
    except OSError:
        return False


class ReadFaultInjector(contextlib.AbstractContextManager):
    """Raise on the Nth provider-pipe `os.read`, symmetrically for both paths.

    Both implementations read the child pipe with `os.read(fd, 32768)` -
    `subprocess._communicate` for the oracle (section 4.5 explains why the
    one-pipe fast path is never taken) and `_drain_pipe` for the new path - so
    counting reads of exactly that size on a FIFO fd hits both identically
    without disturbing unrelated I/O in this process.
    """

    def __init__(self, nth: int, exc_factory) -> None:
        self._nth = nth
        self._exc_factory = exc_factory
        self._real = os.read
        self.calls = 0
        self._patch: object | None = None

    def _read(self, fd: int, size: int):
        if size == LEGACY_READ_SIZE and _is_pipe(fd):
            self.calls += 1
            if self.calls == self._nth:
                raise self._exc_factory()
        return self._real(fd, size)

    def __enter__(self) -> "ReadFaultInjector":
        self._patch = mock.patch.object(os, "read", self._read)
        self._patch.__enter__()  # type: ignore[union-attr]
        return self

    def __exit__(self, *exc_info) -> None:
        self._patch.__exit__(*exc_info)  # type: ignore[union-attr]


def measured_join_cost_ms() -> float:
    """What a bounded `Thread.join(LIVE_CLOSE_JOIN_SECONDS)` actually costs here.

    CPython's `Thread.join(timeout)` bottoms out in a timed lock acquire whose
    granularity on this host overshoots the requested timeout by roughly
    150 ms, independently of anything H2 does. Calibrating against the
    primitive keeps the C16(c) bound an assertion about the implementation
    rather than about the platform's timer resolution, without widening
    DEADLINE_TOLERANCE_MS.
    """
    join = runner_module.LIVE_CLOSE_JOIN_SECONDS
    blocked = threading.Event()
    thread = threading.Thread(target=lambda: blocked.wait(join * 4), daemon=True)
    thread.start()
    try:
        start = time.monotonic()
        thread.join(join)
        return (time.monotonic() - start) * 1000
    finally:
        blocked.set()
        thread.join(timeout=join * 4)


def _eio() -> OSError:
    return OSError(errno.EIO, os.strerror(errno.EIO))


@contextlib.contextmanager
def writer_thread_start_hook(*, delay: float = 0.0, raise_exc: BaseException | None = None):
    """Delay or fail the live writer thread's `start()`, and nothing else's."""
    real = threading.Thread.start
    stream = getattr(runner_module, "_LiveStream", None)
    target_name = getattr(stream, "_THREAD_NAME", "orch-live-writer")

    def start(self):  # noqa: ANN001
        if getattr(self, "name", "") == target_name:
            if raise_exc is not None:
                raise raise_exc
            if delay:
                time.sleep(delay)
        return real(self)

    with mock.patch.object(threading.Thread, "start", start):
        yield


class FakeHandle:
    """Stand-in for the live file handle, injected on the writer thread."""

    def __init__(
        self,
        *,
        write_error: BaseException | None = None,
        flush_error_after: int | None = None,
        first_write_sleep: float = 0.0,
    ) -> None:
        self.write_error = write_error
        self.flush_error_after = flush_error_after
        self.first_write_sleep = first_write_sleep
        self.writes = 0
        self.flushes = 0
        self.closed = False
        self.data = bytearray()

    def write(self, payload: bytes) -> int:
        self.writes += 1
        if self.writes == 1 and self.first_write_sleep:
            time.sleep(self.first_write_sleep)
        if self.write_error is not None:
            raise self.write_error
        self.data.extend(payload)
        return len(payload)

    def flush(self) -> None:
        self.flushes += 1
        if self.flush_error_after is not None and self.writes > self.flush_error_after:
            raise OSError(errno.EIO, "injected flush failure")

    def close(self) -> None:
        self.closed = True


@contextlib.contextmanager
def live_handle(factory):
    """Patch the writer thread's file-creation seam."""
    stream = runner_module._LiveStream
    created: list[object] = []

    def create(path):  # noqa: ANN001
        handle = factory(path)
        created.append(handle)
        return handle

    with mock.patch.object(stream, "_create_handle", staticmethod(create)):
        yield created


# --------------------------------------------------------------------------
# Child scripts
# --------------------------------------------------------------------------
INVALID_BYTES = _b64(b"\xff\xfe")
CR_MIX = _b64(b"line1\r\nline2\rline3\n")
# A multibyte character split across a read boundary. The pad puts the first
# two bytes of U+6F22 at offset 32766-32767 so a full 32768-byte read ends
# mid-character; the flush boundary between those two bytes and the third
# guarantees the split even when the reader wakes early and reads less.
SPLIT_MULTIBYTE = (
    f"pad:32766;sleep:0.15;b64:{_b64('漢'.encode()[:2])};sleep:0.15;"
    f"b64:{_b64('漢'.encode()[2:])};text:tail;exit:0"
)

SPEC_C1 = f"text:hello ;b64:{INVALID_BYTES};text: world;exit:0"
SPEC_C2 = "text:before-close;close;sleep:30"
SPEC_C3 = "loop:30"
SPEC_C4 = "sleep:30"
SPEC_C6 = f"text:partial;b64:{INVALID_BYTES};sleep:30"
SPEC_C8 = f"b64:{CR_MIX};exit:0"
SPEC_C9 = "exit:0"
SPEC_C10 = "bulk:4194304;exit:0"
SPEC_C13 = "text:first-chunk;sleep:8;exit:0"
SPEC_C16 = "text:chunk-one;sleep:0.4;text:chunk-two;sleep:6"
SPEC_INTERRUPT = "text:hello;sleep:12;exit:0"
SPEC_FAST = "text:hi;exit:0"


# ==========================================================================
# C1, C6, C7, C8, C9, C10 - authoritative decode and finalization parity
# ==========================================================================
class DecodeAndFinalizeParityTests(_ParityCase):
    def test_c1_invalid_bytes_between_valid_output_reproduces_path_c(self):
        legacy, new = self.assert_parity(child_spec=SPEC_C1, timeout=30)
        self.assertIsNotNone(legacy.error_text)
        self.assertIn("runner interrupted: UnicodeDecodeError", legacy.error_text or "")
        self.assertEqual(legacy.result_fields["exit_code"], None)  # type: ignore[index]
        self.assertIs(legacy.result_fields["timed_out"], False)  # type: ignore[index]
        self.assertEqual((legacy.terminate_calls, new.terminate_calls), (0, 0))

    def test_c6_timeout_path_with_invalid_bytes_reproduces_path_d(self):
        legacy, new = self.assert_parity(child_spec=SPEC_C6, timeout=1.0)
        self.assertIn("runner interrupted: UnicodeDecodeError", legacy.error_text or "")
        self.assertIs(legacy.result_fields["timed_out"], True)  # type: ignore[index]
        self.assertIsNone(legacy.result_fields["exit_code"])  # type: ignore[index]
        self.assertEqual((legacy.terminate_calls, new.terminate_calls), (1, 1))

    def test_c7_multibyte_split_across_read_boundary_keeps_authoritative_output(self):
        legacy, new = self.assert_parity(child_spec=SPLIT_MULTIBYTE, timeout=30)
        self.assertIn("漢tail", str(legacy.result_fields["output"]))  # type: ignore[index]

    def test_c7l_live_fragments_have_no_replacement_char_from_the_split(self):
        self.require_live_emission()
        legacy, new = self.assert_parity(child_spec=SPLIT_MULTIBYTE, timeout=30)
        fragments = [
            record["text"] for record in new.live_records() if record["event"] == "output_fragment"
        ]
        self.assertTrue(fragments, "live fragments were not emitted")
        self.assertNotIn("�", "".join(fragments))

    def test_c8_crlf_and_lone_cr_normalize_identically(self):
        legacy, new = self.assert_parity(child_spec=SPEC_C8, timeout=30)
        self.assertEqual(legacy.result_fields["output"], "line1\nline2\nline3\n")  # type: ignore[index]

    def test_c8l_live_fragments_are_not_newline_normalized(self):
        self.require_live_emission()
        legacy, new = self.assert_parity(child_spec=SPEC_C8, timeout=30)
        fragments = "".join(
            record["text"] for record in new.live_records() if record["event"] == "output_fragment"
        )
        self.assertIn("\r", fragments)

    def test_c9_empty_output_exit_zero(self):
        self.assert_parity(child_spec=SPEC_C9, timeout=30)

    def test_c10_output_larger_than_live_cap_stays_byte_identical(self):
        legacy, new = self.assert_parity(child_spec=SPEC_C10, timeout=60)
        self.assertEqual(len(str(legacy.result_fields["output"])), 4 * 1024 * 1024)  # type: ignore[index]

    def test_c10l_live_file_stays_inside_the_whole_run_cap(self):
        self.require_live_emission()
        samples: list[int] = []
        stop = threading.Event()

        def poll(path: Path) -> None:
            while not stop.is_set():
                if path.is_file():
                    samples.append(path.stat().st_size)
                time.sleep(0.005)

        log_path = self.workdir / "c10l.log"
        watcher = threading.Thread(target=poll, args=(_live_path_for(log_path),), daemon=True)
        watcher.start()
        try:
            new = self.observe(
                impl="new", child_spec=SPEC_C10, timeout=60, log_name=log_path.name
            )
        finally:
            stop.set()
            watcher.join(timeout=2)
        cap = runner_module.LIVE_MAX_BYTES
        for size in samples:
            self.assertLessEqual(size, cap, "live file exceeded LIVE_MAX_BYTES while the child ran")
        self.assertLessEqual(new.live_path.stat().st_size, cap)
        ends = [record for record in new.live_records() if record["event"] == "stage_end"]
        self.assertEqual(len(ends), 1)
        self.assertIs(ends[0]["live_complete"], False)


# ==========================================================================
# C2, C3, C4, C5 - deadline parity
# ==========================================================================
class DeadlineParityTests(_ParityCase):
    def test_c2_child_closes_stdout_then_sleeps_past_timeout(self):
        legacy, new = self.assert_timing_parity(child_spec=SPEC_C2, timeout=1.0)
        self.assertIs(legacy.result_fields["timed_out"], True)  # type: ignore[index]
        self.assertIs(new.result_fields["timed_out"], True)  # type: ignore[index]
        self.assertEqual((legacy.terminate_calls, new.terminate_calls), (1, 1))

    def test_c3_continuous_writer_does_not_starve_the_deadline(self):
        legacy, new = self.assert_timing_parity(
            child_spec=SPEC_C3, timeout=1.0, compare_output=False
        )
        self.assertIs(new.result_fields["timed_out"], True)  # type: ignore[index]
        self.assertEqual((legacy.terminate_calls, new.terminate_calls), (1, 1))

    def test_c4_silent_child_times_out_identically(self):
        legacy, new = self.assert_timing_parity(child_spec=SPEC_C4, timeout=2.5)
        self.assertIs(new.result_fields["timed_out"], True)  # type: ignore[index]

    def test_c4l_silent_child_still_produces_a_heartbeat(self):
        self.require_live_emission()
        new = self.observe(impl="new", child_spec=SPEC_C4, timeout=2.5)
        beats = [record for record in new.live_records() if record["event"] == "heartbeat"]
        self.assertGreaterEqual(len(beats), 1)

    def test_c5_timeout_shorter_than_the_poll_bound_has_no_poll_sized_drift(self):
        self.assert_timing_parity(child_spec=SPEC_C4, timeout=0.2)


# ==========================================================================
# C11 - injected live failure
# ==========================================================================
class LiveFailureContainmentTests(_ParityCase):
    def test_c11a_writer_write_oserror_is_contained(self):
        self.require_live_emission()
        legacy = self.observe(impl="legacy", child_spec=SPEC_C1, timeout=30)
        with live_handle(lambda path: FakeHandle(write_error=_eio())) as created:
            new = self.observe(impl="new", child_spec=SPEC_C1, timeout=30)
        self.assert_lifecycle_parity(legacy, new)
        self.assertEqual((legacy.terminate_calls, new.terminate_calls), (0, 0))
        self.assertTrue(created and created[0].writes >= 1)

    def test_c11a2_writer_flush_oserror_degrades_and_stops_writing(self):
        self.require_live_emission()
        legacy = self.observe(impl="legacy", child_spec=SPEC_FAST, timeout=30)
        with live_handle(lambda path: FakeHandle(flush_error_after=0)) as created:
            new = self.observe(impl="new", child_spec=SPEC_FAST, timeout=30)
        self.assert_lifecycle_parity(legacy, new)
        handle = created[0]
        self.assertEqual(handle.writes, 1, "live writes continued after a flush failure")
        self.assertTrue(handle.closed, "degraded handle was not closed")

    def test_c11b_file_create_failure_is_contained(self):
        self.require_live_emission()
        legacy = self.observe(impl="legacy", child_spec=SPEC_C1, timeout=30)

        def boom(path):  # noqa: ANN001
            raise OSError(errno.EACCES, "injected create failure")

        with mock.patch.object(runner_module._LiveStream, "_create_handle", staticmethod(boom)):
            new = self.observe(impl="new", child_spec=SPEC_C1, timeout=30)
        self.assert_lifecycle_parity(legacy, new)
        self.assertFalse(new.live_path.exists())

    def test_c11c_queue_exhaustion_drops_optional_records_and_keeps_stage_end(self):
        self.require_live_emission()
        stream_cls = runner_module._LiveStream
        depth = runner_module.LIVE_QUEUE_MAX_RECORDS
        release = threading.Event()

        class PausedHandle(FakeHandle):
            def write(self, payload: bytes) -> int:
                if self.writes == 0:
                    release.wait(timeout=10)
                return super().write(payload)

        handles: list[PausedHandle] = []
        path = self.workdir / "c11c.live.jsonl"
        with mock.patch.object(
            stream_cls,
            "_create_handle",
            staticmethod(lambda p: handles[-1]),
        ):
            handles.append(PausedHandle())
            stream = stream_cls.open(self.workdir / "c11c.log", owner="claude", timeout_seconds=30)
            stream.stage_start(child_pid=os.getpid(), encoding="UTF-8")
            for index in range(depth * 2):
                stream.fragment(f"fragment-{index}\n".encode())
            self.assertLessEqual(
                stream._queue.qsize(),
                depth,
                "optional records were admitted past the reserved terminal slot",
            )
            release.set()
            closed_at = time.monotonic()
            stream.close(process=_ReapedStub(0), timed_out=False)
            close_ms = (time.monotonic() - closed_at) * 1000
        self.assertLessEqual(close_ms, runner_module.LIVE_CLOSE_JOIN_SECONDS * 1000 + DEADLINE_TOLERANCE_MS)
        lines = [line for line in bytes(handles[0].data).decode("utf-8").splitlines() if line]
        records = [json.loads(line) for line in lines]
        ends = [record for record in records if record["event"] == "stage_end"]
        self.assertEqual(len(ends), 1)
        self.assertIs(ends[0]["live_complete"], False)

    def test_c11c_integration_bulk_output_keeps_the_drain_running(self):
        self.require_live_emission()
        legacy = self.observe(impl="legacy", child_spec=SPEC_C10, timeout=60)
        with live_handle(lambda path: FakeHandle(first_write_sleep=0.4)) as created:
            new = self.observe(impl="new", child_spec=SPEC_C10, timeout=60)
        self.assert_lifecycle_parity(legacy, new)
        self.assertTrue(created)

    def test_c11d_delayed_writer_thread_start_is_outside_the_authoritative_interval(self):
        """The 500 ms `Thread.start` delay is paid before `started = time.time()`.

        It must therefore appear in neither `RunResult.duration_ms` nor the
        sealed log's `duration_seconds`. `stage_runs.duration_ms` is covered by
        the same assertion because `controller._duration_ms` returns
        `result.duration_ms` verbatim.
        """
        self.require_live_emission()
        legacy = self.observe(impl="legacy", child_spec=SPEC_FAST, timeout=30)
        new = self.observe(
            impl="new",
            child_spec=SPEC_FAST,
            timeout=30,
            run_hook=writer_thread_start_hook(delay=0.5),
        )
        self.assert_lifecycle_parity(legacy, new)
        self.assertIsNotNone(new.result_duration_ms)
        self.assertLessEqual(
            abs((new.result_duration_ms or 0) - (legacy.result_duration_ms or 0)),
            DEADLINE_TOLERANCE_MS,
            "the injected writer-start delay leaked into RunResult.duration_ms",
        )
        self.assertLessEqual(
            abs(_log_duration_ms(new) - _log_duration_ms(legacy)),
            DEADLINE_TOLERANCE_MS,
            "the injected writer-start delay leaked into the sealed log duration",
        )

    def test_c11e_keyboardinterrupt_in_fragment_is_not_contained(self):
        self.require_live_emission()
        with ReadFaultInjector(1, KeyboardInterrupt):
            legacy = self.observe(impl="legacy", child_spec=SPEC_INTERRUPT, timeout=30)
        with _fragment_interrupt(1):
            new = self.observe(impl="new", child_spec=SPEC_INTERRUPT, timeout=30)
        self.assert_lifecycle_parity(legacy, new)
        self.assertIn("runner interrupted: KeyboardInterrupt", legacy.error_text or "")
        self.assertEqual((legacy.terminate_calls, new.terminate_calls), (1, 1))
        self.assertFalse(legacy.alive_at_return)
        self.assertFalse(new.alive_at_return)


def _log_duration_ms(observation: Observation) -> float:
    return (observation.log_duration_seconds or 0.0) * 1000


@contextlib.contextmanager
def _fragment_interrupt(nth: int):
    stream_cls = runner_module._LiveStream
    real = stream_cls.fragment
    state = {"calls": 0}

    def fragment(self, chunk):  # noqa: ANN001
        state["calls"] += 1
        if state["calls"] == nth:
            raise KeyboardInterrupt
        return real(self, chunk)

    with mock.patch.object(stream_cls, "fragment", fragment):
        yield


class _ReapedStub:
    """Minimal stand-in for a reaped Popen, for direct `_LiveStream` tests."""

    def __init__(self, code: int) -> None:
        self.returncode = code

    def poll(self) -> int:
        return self.returncode


# ==========================================================================
# C12 - sealed manifest shape
# ==========================================================================
class SealedManifestShapeTests(_ParityCase):
    # Frozen key set of controller._seal_run_manifest at the H2 base
    # (d977b84). The spec cites a schema_version 2 payload from a newer
    # engine snapshot; at this base the payload is schema_version 1 and this
    # is the key set that must not move.
    FROZEN_MANIFEST_KEYS = (
        "classification",
        "ended_at",
        "exit_code",
        "lease_token",
        "log_hash",
        "log_path",
        "outcome",
        "output_hash",
        "owner",
        "reason",
        "run_token",
        "schema_version",
        "stage",
        "started_at",
        "task_id",
        "timed_out",
    )

    def test_c12_manifest_key_set_is_unchanged_and_never_names_the_live_file(self):
        from orchestrator.controller import Controller

        root = Path(__file__).resolve().parents[2]
        profile = root / "orchestrator" / "examples" / "demo-loop.yaml"
        task_input = root / "orchestrator" / "examples" / "demo-input.md"
        command = _child_command(f"b64:{_b64(b'ORCHESTRATOR_OUTCOME: submit\n')};exit:0")
        home = self.workdir / "runtime"
        with mock.patch.dict(
            os.environ, {"ORCH_CLAUDE_COMMAND": command, "ORCH_CODEX_COMMAND": command}
        ):
            controller = Controller(home, runner=SubprocessRunner())
            try:
                task_id = controller.submit("demo-loop", profile, task_input)
                controller.run_until_stop(task_id)
                rows = controller.conn.execute(
                    "SELECT log_path,manifest_path FROM stage_runs WHERE manifest_path IS NOT NULL"
                    " ORDER BY started_at"
                ).fetchall()
                self.assertTrue(rows, "no sealed manifest was produced")
                manifest_text = Path(rows[0]["manifest_path"]).read_text(encoding="utf-8")
                log_path = Path(rows[0]["log_path"])
            finally:
                controller.close()
        manifest = json.loads(manifest_text)
        self.assertEqual(tuple(sorted(manifest.keys())), self.FROZEN_MANIFEST_KEYS)
        self.assertEqual(manifest["schema_version"], 1)
        self.assertNotIn(".live.jsonl", manifest_text)
        evidence = (log_path.parent.parent / "evidence.json")
        if evidence.is_file():
            self.assertNotIn(".live.jsonl", evidence.read_text(encoding="utf-8"))
        for pid in [_read_child_pid(log_path)] if log_path.is_file() else []:
            if pid is not None:
                _RECORDED_PIDS.add(pid)

    def test_c12_live_sibling_exists_but_is_not_sealed(self):
        self.require_live_emission()
        new = self.observe(impl="new", child_spec=SPEC_FAST, timeout=30)
        self.assertTrue(new.live_path.is_file(), "live sibling was not created")
        self.assertNotIn(".live.jsonl", new.log_text)


# ==========================================================================
# C13 - in-flight readability
# ==========================================================================
class InFlightReadabilityTests(_ParityCase):
    def test_c13_live_stream_is_readable_while_the_child_runs(self):
        self.require_live_emission()
        self.read_in_flight("claude")


# ==========================================================================
# C14 - provider lifecycle parity across owners
# ==========================================================================
class ProviderLifecycleParityTests(_ParityCase):
    OWNERS = ("claude", "codex")

    def test_c14_core_cases_are_identical_across_owners(self):
        for spec, timeout, name in (
            (SPEC_C1, 30, "c1"),
            (SPEC_C2, 1.0, "c2"),
            (SPEC_C6, 1.0, "c6"),
            (SPEC_C9, 30, "c9"),
        ):
            per_owner: dict[str, dict[str, object]] = {}
            for owner in self.OWNERS:
                with self.subTest(case=name, owner=owner):
                    legacy, new = self.assert_parity(
                        child_spec=spec, timeout=timeout, owner=owner
                    )
                    per_owner[owner] = dict(new.result_fields or {})
            claude = per_owner["claude"]
            codex = per_owner["codex"]
            # command/model provenance is the only permitted difference, and
            # both fake owners are pointed at the same child, so even model
            # matches; output and lifecycle fields must match exactly.
            with self.subTest(case=name, owner="cross"):
                self.assertEqual(claude, codex)

    def test_c14l_in_flight_readability_holds_for_both_owners(self):
        self.require_live_emission()
        for owner in self.OWNERS:
            with self.subTest(owner=owner):
                self.read_in_flight(owner)


# ==========================================================================
# C15 - spawn OSError
# ==========================================================================
class SpawnFailureParityTests(_ParityCase):
    def test_c15_patched_popen_oserror_reproduces_path_e(self):
        def boom(*args, **kwargs):
            raise OSError(errno.ENOENT, "injected spawn failure")

        legacy, new = self.assert_parity(child_spec=SPEC_FAST, timeout=30, popen_patch=boom)
        self.assertIn("failed to spawn runner", legacy.error_text or "")
        self.assertIsNone(legacy.result_fields["exit_code"])  # type: ignore[index]
        self.assertEqual((legacy.terminate_calls, new.terminate_calls), (0, 0))

    def test_c15_missing_executable_reproduces_path_e(self):
        missing = str(self.workdir / "definitely-not-here")
        legacy, new = self.assert_parity(
            child_spec=SPEC_FAST, timeout=30, command_override=shlex.join([missing])
        )
        self.assertIn("failed to spawn runner", legacy.error_text or "")

    def test_c15l_writer_thread_start_failure_keeps_the_run_legacy_identical(self):
        self.require_live_emission()

        def boom(*args, **kwargs):
            raise OSError(errno.ENOENT, "injected spawn failure")

        legacy = self.observe(impl="legacy", child_spec=SPEC_FAST, timeout=30, popen_patch=boom)
        new = self.observe(
            impl="new",
            child_spec=SPEC_FAST,
            timeout=30,
            popen_patch=boom,
            run_hook=writer_thread_start_hook(raise_exc=RuntimeError("injected thread start")),
        )
        self.assert_lifecycle_parity(legacy, new)


# ==========================================================================
# C16 - path G return timing and the reaped-child join gate
# ==========================================================================
class PathGAndCloseJoinTests(_ParityCase):
    def test_c16a_mid_drain_read_oserror_reproduces_path_g_on_both(self):
        injectors: list[ReadFaultInjector] = []

        def injector():
            injectors.append(ReadFaultInjector(2, _eio))
            return injectors[-1]

        legacy = self.observe_typical(
            impl="legacy", child_spec=SPEC_C16, timeout=30, hook_factory=injector
        )
        legacy_injectors = len(injectors)
        new = self.observe_typical(
            impl="new", child_spec=SPEC_C16, timeout=30, hook_factory=injector
        )
        for index, one in enumerate(injectors):
            side = "oracle" if index < legacy_injectors else "new drain"
            self.assertEqual(one.calls, 2, f"the {side} did not reach the injected read")
        self.assert_lifecycle_parity(legacy, new)
        self.assertIn("failed to spawn runner", legacy.error_text or "")
        self.assertIsNone(legacy.result_fields["exit_code"])  # type: ignore[index]
        self.assertIs(legacy.result_fields["timed_out"], False)  # type: ignore[index]
        self.assertEqual((legacy.terminate_calls, new.terminate_calls), (0, 0))
        self.assertTrue(legacy.alive_at_return, "the oracle did not leave a live child")
        self.assertTrue(new.alive_at_return, "the new path did not leave a live child")
        self.assertLessEqual(
            abs(new.return_elapsed_ms - legacy.return_elapsed_ms), DEADLINE_TOLERANCE_MS
        )

    def test_c16b_slow_writer_adds_no_join_on_a_live_child_path(self):
        self.require_live_emission()
        join = runner_module.LIVE_CLOSE_JOIN_SECONDS
        legacy = self.observe_typical(
            impl="legacy",
            child_spec=SPEC_C16,
            timeout=30,
            hook_factory=lambda: ReadFaultInjector(2, _eio),
        )
        new = self.observe_typical(
            impl="new",
            child_spec=SPEC_C16,
            timeout=30,
            hook_factory=lambda: [
                live_handle(lambda path: FakeHandle(first_write_sleep=join * 2)),
                ReadFaultInjector(2, _eio),
            ],
        )
        self.assert_lifecycle_parity(legacy, new)
        self.assertEqual((legacy.terminate_calls, new.terminate_calls), (0, 0))
        self.assertTrue(legacy.alive_at_return)
        self.assertTrue(new.alive_at_return)
        self.assertLessEqual(
            abs(new.return_elapsed_ms - legacy.return_elapsed_ms),
            DEADLINE_TOLERANCE_MS,
            "a join was taken on a path where legacy returns immediately",
        )
        self.assertLessEqual(
            abs((new.post_ended_ms or 0.0) - (legacy.post_ended_ms or 0.0)),
            DEADLINE_TOLERANCE_MS,
            "close() waited on a path where legacy returns immediately",
        )

    def test_c16c_reaped_path_still_takes_the_bounded_join(self):
        self.require_live_emission()
        join_seconds = runner_module.LIVE_CLOSE_JOIN_SECONDS
        join_ms = join_seconds * 1000
        legacy = self.observe_typical(impl="legacy", child_spec=SPEC_FAST, timeout=30)
        new = self.observe_typical(
            impl="new",
            child_spec=SPEC_FAST,
            timeout=30,
            hook_factory=lambda: live_handle(
                lambda path: FakeHandle(first_write_sleep=join_seconds * 2)
            ),
        )
        self.assert_lifecycle_parity(legacy, new)
        legacy_post = legacy.post_ended_ms
        new_post = new.post_ended_ms
        self.assertIsNotNone(legacy_post)
        self.assertIsNotNone(new_post)
        cost = (new_post or 0.0) - (legacy_post or 0.0)
        self.assertGreaterEqual(
            cost, join_ms - DEADLINE_TOLERANCE_MS, "the reaped-path join was not taken"
        )
        # The writer's first write sleeps for twice the join, so an unbounded
        # wait would land near 2000 ms. The upper bound is calibrated against
        # this host's own `Thread.join` granularity, which overshoots a 1000 ms
        # request by about 150 ms regardless of the implementation.
        self.assertLessEqual(
            cost,
            measured_join_cost_ms() + DEADLINE_TOLERANCE_MS,
            "the join exceeded LIVE_CLOSE_JOIN_SECONDS",
        )
        self.assertLessEqual(
            abs(_log_duration_ms(new) - _log_duration_ms(legacy)),
            DEADLINE_TOLERANCE_MS,
            "the join leaked into the sealed log duration",
        )


if __name__ == "__main__":
    unittest.main()
