"""H3 acceptance: bounded watch window and cursor (spec section 7, E1-E12).

Case ids in the test names are the spec's. Fixtures are hand-built byte files
except where a real writer is named: crash-truncated tails and oversized
corruption are by definition states the H2 writer cannot produce, so building
them by hand is the only way to observe the reader's behaviour on them.

Because H2 caps the whole file below `WATCH_MIN_BYTES`, a real writer's stream
always drains in one window at any accepted bound. Multi-window iteration
therefore runs against synthetic fixtures, and the real-writer single-window
property is asserted separately (E10c) rather than quietly assumed.
"""

from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import re
import shlex
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

from orchestrator.cli import build_parser
from orchestrator.cli import main as cli_main
from orchestrator.controller import Controller
from orchestrator.runner import (
    LIVE_FRAGMENT_MAX_CHARS,
    LIVE_MAX_BYTES,
    ProviderPreflightResult,
    RunResult,
    SubprocessRunner,
)
from orchestrator.tests.test_runner_lifecycle_parity import _FAKE_CHILD_SOURCE
from orchestrator.watch import (
    WATCH_DEFAULT_BYTES,
    WATCH_ERROR_CODES,
    WATCH_MIN_BYTES,
    WATCH_RECORD_MAX_BYTES,
    WATCH_RESPONSE_KEYS,
    WATCH_SCHEMA_VERSION,
    Window,
    WatchError,
    decode_record,
    encode_record,
    format_cursor,
    parse_cursor,
    read_window,
)

ROOT = Path(__file__).resolve().parents[2]
WATCH_SOURCE = ROOT / "orchestrator" / "watch.py"
TEST_SOURCE = Path(__file__).resolve()

# The frozen closed set, written out literally so a code added to the module
# without a decision here fails rather than silently widening the contract.
FROZEN_ERROR_CODES = frozenset(
    {
        "task_not_found",
        "no_stage_run",
        "no_live_stream",
        "live_path_outside_artifact_dir",
        "window_too_small",
        "cursor_malformed",
        "cursor_run_token_unknown",
        "cursor_out_of_range",
        "cursor_interior_record",
        "oversized_record",
        "unterminated_tail",
        "stream_truncated",
    }
)


# --------------------------------------------------------------------------
# Real-writer fixtures. The fake-child generator is imported from the H2
# parity harness rather than re-declared, so "what a real writer emits" has
# one definition in the suite.
# --------------------------------------------------------------------------
_MODULE_TMP: tempfile.TemporaryDirectory | None = None
_CHILD_SCRIPT: Path


def setUpModule() -> None:
    global _MODULE_TMP, _CHILD_SCRIPT
    _MODULE_TMP = tempfile.TemporaryDirectory(prefix="h3-watch-")
    _CHILD_SCRIPT = Path(_MODULE_TMP.name) / "fake_child.py"
    _CHILD_SCRIPT.write_text(_FAKE_CHILD_SOURCE, encoding="utf-8")


def tearDownModule() -> None:
    if _MODULE_TMP is not None:
        _MODULE_TMP.cleanup()


def child_command(child_spec: str) -> str:
    return shlex.join([sys.executable, str(_CHILD_SCRIPT), child_spec])


def run_real_writer(log_path: Path, child_spec: str, *, owner: str = "claude", timeout: int = 30) -> Path:
    """Drive H2's shipped runner against a fake child and return the live path.

    Nothing here is hand-encoded: the record schema, the incremental
    `errors="replace"` decoder, the fragmenting and the byte budget are all
    H2's, exercised through `SubprocessRunner.run`.
    """
    command = child_command(child_spec)
    env = {"ORCH_CLAUDE_COMMAND": command, "ORCH_CODEX_COMMAND": command}
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with mock.patch.dict(os.environ, env):
        SubprocessRunner().run(owner, "h3-watch-prompt", timeout, log_path)
    return log_path.with_suffix(".live.jsonl")


def raw_lines(path: Path) -> list[bytes]:
    """Every complete `\n`-terminated line of a live stream, in file order."""
    raw = path.read_bytes()
    lines = [line + b"\n" for line in raw.split(b"\n")[:-1]]
    return lines


# --------------------------------------------------------------------------
# Fixture generation. `_line` encodes exactly the way H2's writer does, so a
# hand-built worst-case line is byte-comparable with one H2 would emit.
# --------------------------------------------------------------------------
def _line(record: dict) -> bytes:
    return json.dumps(record, ensure_ascii=False).encode("utf-8") + b"\n"


def _stage_start(seq: int) -> bytes:
    return _line(
        {
            "event": "stage_start",
            "schema_version": 1,
            "seq": seq,
            "ts_ms": 1_760_000_000_000,
            "owner": "claude",
            "child_pid": 41207,
            "timeout_seconds": 1800,
            "encoding": "UTF-8",
        }
    )


def _fragment(seq: int, text: str) -> bytes:
    return _line({"event": "output_fragment", "seq": seq, "ts_ms": 1_760_000_000_001, "text": text})


def _heartbeat(seq: int) -> bytes:
    return _line({"event": "heartbeat", "seq": seq, "ts_ms": 1_760_000_000_002})


def _stage_end(seq: int) -> bytes:
    return _line(
        {
            "event": "stage_end",
            "seq": seq,
            "ts_ms": 1_760_000_000_003,
            "exit_code": 0,
            "timed_out": False,
            "live_complete": True,
        }
    )


#: Worst-case `output_fragment.text` payloads, at H2's own per-record
#: character bound. Each entry names the per-code-point encoded cost under
#: `json.dumps(..., ensure_ascii=False)` that makes it worst-case.
WORST_CASE_TEXTS = {
    # C0 control other than \b \t \n \f \r: escaped \uXXXX, 6 bytes - the
    # maximum any single code point can cost.
    "c0_control": "\x01" * LIVE_FRAGMENT_MAX_CHARS,
    # What H2's errors="replace" incremental decoder emits for an invalid
    # byte: 3 bytes each.
    "replacement": "�" * LIVE_FRAGMENT_MAX_CHARS,
    # Astral plane: 4 bytes each.
    "astral": "\U0001f600" * LIVE_FRAGMENT_MAX_CHARS,
    # Escaped structural characters: 2 bytes each.
    "quote": '"' * LIVE_FRAGMENT_MAX_CHARS,
    "backslash": "\\" * LIVE_FRAGMENT_MAX_CHARS,
}


def worst_case_lines() -> dict[str, bytes]:
    """One raw JSONL line per worst-case payload, plus a `stage_start`."""
    lines = {"stage_start": _stage_start(0)}
    for name, text in WORST_CASE_TEXTS.items():
        lines[name] = _fragment(1, text)
    return lines


# --------------------------------------------------------------------------
# E1 - record envelope and codec contract (H3.1)
# --------------------------------------------------------------------------
class E1RecordBoundTests(unittest.TestCase):
    def test_e1_guard_imported_h2_bound_is_below_the_window_floor(self):
        # The guard is against the *imported* H2 constant, so raising
        # LIVE_MAX_BYTES fails here rather than silently costing the reader
        # its bounded-progress guarantee.
        self.assertEqual(WATCH_RECORD_MAX_BYTES, LIVE_MAX_BYTES)
        self.assertLess(LIVE_MAX_BYTES, WATCH_MIN_BYTES)
        self.assertLess(WATCH_RECORD_MAX_BYTES, WATCH_MIN_BYTES)
        self.assertLessEqual(WATCH_MIN_BYTES, WATCH_DEFAULT_BYTES)
        self.assertEqual(WATCH_SCHEMA_VERSION, 1)

    def test_e1_no_hardcoded_h2_constant_and_no_identifier_length_assumption(self):
        source = WATCH_SOURCE.read_text(encoding="utf-8")
        self.assertIn("from .runner import LIVE_MAX_BYTES", source)
        self.assertIn("WATCH_RECORD_MAX_BYTES = LIVE_MAX_BYTES", source)
        forbidden = {
            "LIVE_MAX_BYTES": LIVE_MAX_BYTES,
            "LIVE_FRAGMENT_MAX_CHARS": LIVE_FRAGMENT_MAX_CHARS,
            # A UUID's textual length. Nothing in the byte proof, the
            # constants or the fixtures may assume it.
            "uuid_text_length": len("00000000-0000-0000-0000-000000000000"),
        }
        for name, path in (("watch", WATCH_SOURCE), ("test", TEST_SOURCE)):
            module_source = path.read_text(encoding="utf-8")
            for label, value in forbidden.items():
                # A whole numeric literal, so an unrelated digit run such as a
                # window size or a timestamp is not a false positive.
                pattern = rf"(?<![0-9]){value}(?![0-9])"
                with self.subTest(module=name, constant=label):
                    self.assertIsNone(
                        re.search(pattern, module_source),
                        f"{name} module hardcodes {label} as the literal {value}",
                    )

    def test_e1a_handbuilt_worst_case_lines_are_within_both_bounds(self):
        lines = worst_case_lines()
        self.assertIn("c0_control", lines)
        for name, raw in lines.items():
            with self.subTest(payload=name):
                self.assertEqual(raw.count(b"\n"), 1)
                self.assertTrue(raw.endswith(b"\n"))
                self.assertLessEqual(len(raw), WATCH_RECORD_MAX_BYTES)
                self.assertLess(len(raw), WATCH_MIN_BYTES)
        # The most expensive payload really is the C0-control one: six bytes
        # per code point. Asserted so a future edit cannot quietly drop the
        # worst case and keep the bound looking satisfied.
        widest = max(lines, key=lambda name: len(lines[name]))
        self.assertEqual(widest, "c0_control")
        self.assertGreater(len(lines["c0_control"]), 5 * LIVE_FRAGMENT_MAX_CHARS)

    def test_e1_codec_round_trips_arbitrary_and_invalid_bytes(self):
        for raw in (b"", b"{}\n", bytes(range(256)), b"\xff" * 64 + b"\n"):
            with self.subTest(length=len(raw)):
                self.assertEqual(decode_record(encode_record(raw)), raw)
        self.assertIsInstance(encode_record(b"x"), str)

    def test_e1b_writer_emitted_maximum_fragment_is_within_both_bounds(self):
        # 0xff is invalid UTF-8 at every position, so H2's incremental
        # errors="replace" decoder emits exactly one U+FFFD per byte. Two
        # fragments' worth of bytes, so its fragmenting must produce at least
        # one fragment at the full per-record character bound.
        payload = b"\xff" * (2 * LIVE_FRAGMENT_MAX_CHARS)
        spec = f"b64:{base64.b64encode(payload).decode('ascii')};exit:0"
        with tempfile.TemporaryDirectory() as directory:
            live = run_real_writer(Path(directory) / "e1b.log", spec)
            self.assertTrue(live.is_file(), "H2 wrote no live stream")
            lines = raw_lines(live)
            self.assertTrue(lines)
            widest = 0
            max_fragment_chars = 0
            for raw in lines:
                # Every line H2 admitted, as written: metadata, JSON escaping
                # and trailing newline included.
                self.assertLessEqual(len(raw), WATCH_RECORD_MAX_BYTES)
                self.assertLess(len(raw), WATCH_MIN_BYTES)
                widest = max(widest, len(raw))
                record = json.loads(raw.decode("utf-8"))
                if record.get("event") == "output_fragment":
                    text = record["text"]
                    self.assertEqual(set(text), {"\ufffd"})
                    max_fragment_chars = max(max_fragment_chars, len(text))
            self.assertEqual(max_fragment_chars, LIVE_FRAGMENT_MAX_CHARS)
            # Whole-file cap is H2's too, and it is what makes a real stream
            # drain in one window at any accepted bound (E10c).
            self.assertLessEqual(live.stat().st_size, LIVE_MAX_BYTES)
            self.assertGreater(widest, 3 * LIVE_FRAGMENT_MAX_CHARS)

    def test_e1_frozen_error_code_set(self):
        self.assertEqual(set(WATCH_ERROR_CODES), set(FROZEN_ERROR_CODES))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


# --------------------------------------------------------------------------
# Caller iteration protocol (spec section 5.4), used verbatim by every case
# that iterates. It stops on the composite predicate, never on `eof` alone.
# --------------------------------------------------------------------------
#: A walk over any accepted window advances by at least one complete record
#: per successful non-EOF call, so this only ever trips on a real livelock.
WALK_CALL_CAP = 4096


class WalkStep:
    """One call of a walk: either a window or the named failure that ended it."""

    def __init__(self, window: Window | None, error: WatchError | None) -> None:
        self.window = window
        self.error = error


def walk(live_path: Path, window_bytes: int, *, cursor_offset: int = 0) -> list[WalkStep]:
    cursor = cursor_offset
    steps: list[WalkStep] = []
    while True:
        if len(steps) > WALK_CALL_CAP:  # pragma: no cover - livelock guard
            raise AssertionError(f"walk did not terminate within {WALK_CALL_CAP} calls")
        try:
            window = read_window(live_path, cursor_offset=cursor, window_bytes=window_bytes)
        except WatchError as exc:
            steps.append(WalkStep(None, exc))
            return steps
        steps.append(WalkStep(window, None))
        cursor = window.next_offset
        if window.eof and window.next_offset == window.snapshot_bytes:
            return steps
        # eof true with next_offset below snapshot_bytes: bytes are withheld.
        # Loop again at the SAME returned cursor.


def walk_bytes(steps: list[WalkStep]) -> bytes:
    raw = b""
    for step in steps:
        if step.window is not None:
            raw += b"".join(decode_record(record) for record in step.window.records)
    return raw


def walk_records(steps: list[WalkStep]) -> list[bytes]:
    out: list[bytes] = []
    for step in steps:
        if step.window is not None:
            out.extend(decode_record(record) for record in step.window.records)
    return out


def mixed_stream(target_bytes: int) -> bytes:
    """A synthetic stream of mixed record types ending in `stage_end`.

    Larger than any real H2 stream on purpose: H2 caps the whole file below
    `WATCH_MIN_BYTES`, so a real stream can never exercise multi-window
    iteration (E10c asserts that property instead).
    """
    seq = 0
    raw = bytearray(_stage_start(seq))
    seq += 1
    texts = list(WORST_CASE_TEXTS.values()) + ["plain output line", "tab\tand\nnewline"]
    index = 0
    while len(raw) < target_bytes:
        if index % 4 == 3:
            raw += _heartbeat(seq)
        else:
            raw += _fragment(seq, texts[index % len(texts)])
        seq += 1
        index += 1
    raw += _stage_end(seq)
    return bytes(raw)


class _FdCalls:
    """Ordered `(op, fd)` log for one live path, plus the flags it was opened with.

    Only calls naming the watched path or an fd derived from it are recorded,
    so an unrelated open elsewhere in the process cannot pollute the
    assertions. Used both for the call-order proof (E4) and the read-only
    proof (E10b).
    """

    def __init__(self, live_path: Path) -> None:
        # realpath, because the DB stores a resolved artifact path while a
        # test may hold the unresolved one; /var vs /private/var on macOS
        # would otherwise silently match nothing.
        self.live_path = os.path.realpath(live_path)
        self.opens: list[int] = []
        self.flags: list[int] = []
        self.calls: list[tuple[str, int]] = []
        self.pread_count = 0
        self.mutations: list[tuple[str, object]] = []

    def _watched(self, fd: object) -> bool:
        return isinstance(fd, int) and fd in self.opens

    def install(self, stack, *, on_fstat=None, on_pread=None):
        real_open, real_fstat, real_pread = os.open, os.fstat, os.pread
        real_write, real_ftruncate = os.write, os.ftruncate
        real_truncate, real_rename, real_unlink = os.truncate, os.rename, os.unlink
        recorder = self

        def fake_open(path, flags, *args, **kwargs):
            fd = real_open(path, flags, *args, **kwargs)
            if isinstance(path, (str, bytes, os.PathLike)) and os.path.realpath(path) == recorder.live_path:
                recorder.opens.append(fd)
                recorder.flags.append(flags)
            return fd

        def fake_fstat(fd, *args, **kwargs):
            result = real_fstat(fd, *args, **kwargs)
            if recorder._watched(fd):
                recorder.calls.append(("fstat", fd))
                if on_fstat is not None:
                    on_fstat(len(recorder.calls))
            return result

        def fake_pread(fd, length, offset, *args, **kwargs):
            if recorder._watched(fd) and on_pread is not None:
                # 1-based ordinal among preads on the watched fd, so a hook
                # can name "the first read" without counting the fstat.
                override = on_pread(recorder.pread_count + 1)
                if override is not None:
                    recorder.pread_count += 1
                    recorder.calls.append(("pread", fd))
                    return override
            result = real_pread(fd, length, offset, *args, **kwargs)
            if recorder._watched(fd):
                recorder.pread_count += 1
                recorder.calls.append(("pread", fd))
            return result

        def guard(name, real):
            def wrapper(target, *args, **kwargs):
                named = isinstance(target, (str, bytes, os.PathLike)) and os.path.realpath(target) == recorder.live_path
                if recorder._watched(target) or named:
                    recorder.mutations.append((name, target))
                return real(target, *args, **kwargs)

            return wrapper

        stack.enter_context(mock.patch("os.open", fake_open))
        stack.enter_context(mock.patch("os.fstat", fake_fstat))
        stack.enter_context(mock.patch("os.pread", fake_pread))
        stack.enter_context(mock.patch("os.write", guard("write", real_write)))
        stack.enter_context(mock.patch("os.ftruncate", guard("ftruncate", real_ftruncate)))
        stack.enter_context(mock.patch("os.truncate", guard("truncate", real_truncate)))
        stack.enter_context(mock.patch("os.rename", guard("rename", real_rename)))
        stack.enter_context(mock.patch("os.unlink", guard("unlink", real_unlink)))
        return self

    def assert_read_only(self, case: unittest.TestCase) -> None:
        case.assertTrue(self.opens, "the reader never opened the live stream")
        forbidden = os.O_CREAT | os.O_TRUNC | os.O_APPEND | os.O_WRONLY | os.O_RDWR
        for flags in self.flags:
            case.assertEqual(flags & forbidden, 0, f"live stream opened with flags {flags:#o}")
            case.assertEqual(flags & (os.O_RDONLY | forbidden), os.O_RDONLY)
        case.assertEqual(self.mutations, [])


# --------------------------------------------------------------------------
# E2, E3, E4, E5, E6, E7a, E7b, E8 - immutable-snapshot reader (H3.2)
# --------------------------------------------------------------------------
class ReaderTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="h3-reader-")
        self.addCleanup(self._tmp.cleanup)
        self.workdir = Path(self._tmp.name)

    def _fixture(self, name: str, raw: bytes) -> Path:
        path = self.workdir / name
        path.write_bytes(raw)
        return path

    # -- E2 ----------------------------------------------------------------
    def test_e2_repeated_windows_reach_terminal_eof_at_both_bounds(self):
        raw = mixed_stream(40 * WATCH_RECORD_MAX_BYTES // 8)
        path = self._fixture("e2.jsonl", raw)
        walks = {}
        for bound in (WATCH_DEFAULT_BYTES, WATCH_MIN_BYTES):
            steps = walk(path, bound)
            walks[bound] = steps
            with self.subTest(bound=bound):
                self.assertIsNone(steps[-1].error)
                last = steps[-1].window
                self.assertTrue(last.eof)
                self.assertEqual(last.next_offset, last.snapshot_bytes)
                cursor = 0
                for step in steps:
                    window = step.window
                    self.assertIsNotNone(window)
                    if not window.eof:
                        # Strict progress: a successful non-EOF window always
                        # advances by at least one complete record.
                        self.assertGreaterEqual(len(window.records), 1)
                        self.assertGreater(window.next_offset, cursor)
                    cursor = window.next_offset
        self.assertGreater(len(walks[WATCH_MIN_BYTES]), len(walks[WATCH_DEFAULT_BYTES]))
        self.assertEqual(walk_bytes(walks[WATCH_MIN_BYTES]), raw)
        self.assertEqual(walk_bytes(walks[WATCH_DEFAULT_BYTES]), raw)
        self.assertEqual(
            walk_records(walks[WATCH_MIN_BYTES]), walk_records(walks[WATCH_DEFAULT_BYTES])
        )
        records = walk_records(walks[WATCH_MIN_BYTES])
        # No record appears twice or is missing: the returned sequence IS the
        # file's line sequence, and its seq values are gap-free.
        self.assertEqual(records, raw_lines(path))
        seqs = [json.loads(record.decode("utf-8"))["seq"] for record in records]
        self.assertEqual(seqs, list(range(len(seqs))))
        self.assertEqual(len(set(seqs)), len(seqs))

    # -- E3 ----------------------------------------------------------------
    def test_e3_append_between_snapshot_and_read_belongs_to_the_next_call(self):
        raw = mixed_stream(3 * WATCH_RECORD_MAX_BYTES)
        path = self._fixture("e3.jsonl", raw)
        appended = _fragment(9999, "appended after the snapshot")
        control = read_window(path, cursor_offset=0, window_bytes=WATCH_DEFAULT_BYTES)

        recorder = _FdCalls(path)
        with contextlib.ExitStack() as stack:
            def on_fstat(_index: int) -> None:
                with path.open("ab") as handle:
                    handle.write(appended)
                    handle.flush()

            recorder.install(stack, on_fstat=on_fstat)
            observed = read_window(path, cursor_offset=0, window_bytes=WATCH_DEFAULT_BYTES)

        self.assertEqual(observed.snapshot_bytes, len(raw))
        self.assertEqual(observed, control)
        self.assertNotIn(encode_record(appended), observed.records)
        follow_up = read_window(
            path, cursor_offset=observed.next_offset, window_bytes=WATCH_DEFAULT_BYTES
        )
        self.assertEqual([decode_record(r) for r in follow_up.records], [appended])
        self.assertTrue(follow_up.eof)
        self.assertEqual(follow_up.next_offset, follow_up.snapshot_bytes)

    # -- E4 ----------------------------------------------------------------
    def test_e4_no_size_is_consulted_after_the_read(self):
        raw = mixed_stream(3 * WATCH_RECORD_MAX_BYTES)
        path = self._fixture("e4.jsonl", raw)
        control = read_window(path, cursor_offset=0, window_bytes=WATCH_DEFAULT_BYTES)

        recorder = _FdCalls(path)
        with contextlib.ExitStack() as stack:
            def on_pread(_index: int):
                with path.open("ab") as handle:
                    handle.write(_fragment(9999, "appended after a pread"))
                    handle.flush()
                return None

            recorder.install(stack, on_pread=on_pread)
            observed = read_window(path, cursor_offset=0, window_bytes=WATCH_DEFAULT_BYTES)
        self.assertEqual(observed, control)

    def test_e4_exactly_one_fstat_and_it_precedes_every_pread(self):
        raw = mixed_stream(3 * WATCH_RECORD_MAX_BYTES)
        path = self._fixture("e4-order.jsonl", raw)
        for cursor_offset in (0, len(_stage_start(0))):
            with self.subTest(cursor_offset=cursor_offset):
                recorder = _FdCalls(path)
                with contextlib.ExitStack() as stack:
                    recorder.install(stack)
                    read_window(
                        path, cursor_offset=cursor_offset, window_bytes=WATCH_DEFAULT_BYTES
                    )
                ops = [op for op, _ in recorder.calls]
                self.assertEqual(ops.count("fstat"), 1)
                self.assertIn("pread", ops)
                self.assertEqual(ops.index("fstat"), 0)
                self.assertLess(ops.index("fstat"), ops.index("pread"))
                recorder.assert_read_only(self)

    # -- E5 ----------------------------------------------------------------
    def _assert_exact_reconstruction(self, path: Path, steps: list[WalkStep]) -> None:
        snapshot = steps[-1].window.snapshot_bytes
        self.assertEqual(walk_bytes(steps), path.read_bytes()[:snapshot])
        seqs = []
        events = []
        for record in walk_records(steps):
            self.assertTrue(record.endswith(b"\n"))
            self.assertEqual(record.count(b"\n"), 1)
            parsed = json.loads(record.decode("utf-8"))
            seqs.append(parsed["seq"])
            events.append(parsed["event"])
        self.assertEqual(seqs, list(range(seqs[0], seqs[0] + len(seqs))))
        self.assertEqual(events.count("stage_end"), 1)
        self.assertEqual(events[-1], "stage_end")

    def test_e5_byte_for_byte_reconstruction_over_a_synthetic_stream(self):
        raw = mixed_stream(40 * WATCH_RECORD_MAX_BYTES // 8)
        path = self._fixture("e5.jsonl", raw)
        for bound in (WATCH_MIN_BYTES, WATCH_DEFAULT_BYTES):
            with self.subTest(bound=bound):
                self._assert_exact_reconstruction(path, walk(path, bound))

    def test_e5_byte_for_byte_reconstruction_over_a_real_writer_stream(self):
        live = run_real_writer(self.workdir / "e5-real.log", "text:hello;sleep:0.05;text:world;exit:0")
        self._assert_exact_reconstruction(live, walk(live, WATCH_MIN_BYTES))

    # -- E6 ----------------------------------------------------------------
    def test_e6_malformed_cursors_are_rejected_and_never_repaired(self):
        for raw_cursor in ("-1", "+5", "abc", "5", "tok:", "tok:1e3", "tok:-1", "tok: 4", ":0", ""):
            with self.subTest(cursor=raw_cursor):
                with self.assertRaises(WatchError) as caught:
                    parse_cursor(raw_cursor)
                self.assertEqual(caught.exception.code, "cursor_malformed")
                self.assertIsNone(caught.exception.snapshot_bytes)

    def test_e6_offset_zero_is_accepted_and_not_special_cased(self):
        token = "8e2c1a44-0000-4000-8000-000000000001"
        self.assertEqual(parse_cursor(f"{token}:0"), (token, 0))
        self.assertEqual(format_cursor(token, 0), f"{token}:0")

    def test_e6_out_of_range_and_interior_cursors_are_rejected(self):
        raw = mixed_stream(3 * WATCH_RECORD_MAX_BYTES)
        path = self._fixture("e6.jsonl", raw)
        with self.assertRaises(WatchError) as caught:
            read_window(path, cursor_offset=len(raw) + 1, window_bytes=WATCH_MIN_BYTES)
        self.assertEqual(caught.exception.code, "cursor_out_of_range")
        self.assertEqual(caught.exception.snapshot_bytes, len(raw))

        with self.assertRaises(WatchError) as caught:
            read_window(path, cursor_offset=1, window_bytes=WATCH_MIN_BYTES)
        self.assertEqual(caught.exception.code, "cursor_interior_record")
        self.assertEqual(caught.exception.snapshot_bytes, len(raw))

    def test_e6_window_below_the_floor_is_refused_and_not_clamped(self):
        raw = mixed_stream(3 * WATCH_RECORD_MAX_BYTES)
        path = self._fixture("e6-window.jsonl", raw)
        for bound in (0, 1, WATCH_RECORD_MAX_BYTES, WATCH_MIN_BYTES - 1):
            with self.subTest(bound=bound):
                with self.assertRaises(WatchError) as caught:
                    read_window(path, cursor_offset=0, window_bytes=bound)
                self.assertEqual(caught.exception.code, "window_too_small")
                self.assertIsNone(caught.exception.snapshot_bytes)

    # -- E7a ---------------------------------------------------------------
    def test_e7a_empty_file_is_terminal_eof(self):
        path = self._fixture("e7a-empty.jsonl", b"")
        for bound in (WATCH_MIN_BYTES, WATCH_DEFAULT_BYTES):
            with self.subTest(bound=bound):
                window = read_window(path, cursor_offset=0, window_bytes=bound)
                self.assertEqual(window.records, ())
                self.assertEqual(window.next_offset, 0)
                self.assertEqual(window.snapshot_bytes, 0)
                self.assertTrue(window.eof)

    def test_e7a_cursor_at_a_newline_terminated_snapshot_end_holds(self):
        raw = mixed_stream(3 * WATCH_RECORD_MAX_BYTES)
        path = self._fixture("e7a-boundary.jsonl", raw)
        self.assertTrue(raw.endswith(b"\n"))
        window = read_window(path, cursor_offset=len(raw), window_bytes=WATCH_MIN_BYTES)
        self.assertEqual(window.records, ())
        self.assertEqual(window.next_offset, len(raw))
        self.assertTrue(window.eof)
        self.assertEqual(window.snapshot_bytes, len(raw))

    def test_e7a_record_of_exactly_the_record_maximum_is_valid_data(self):
        filler = "x" * (WATCH_RECORD_MAX_BYTES - len(_fragment(0, "")))
        line = _fragment(0, filler)
        self.assertEqual(len(line), WATCH_RECORD_MAX_BYTES)
        path = self._fixture("e7a-exact.jsonl", line)
        window = read_window(path, cursor_offset=0, window_bytes=WATCH_MIN_BYTES)
        self.assertEqual([decode_record(r) for r in window.records], [line])
        self.assertTrue(window.eof)
        self.assertEqual(window.next_offset, window.snapshot_bytes)

    def test_e7a_composite_predicate_resolves_a_withheld_partial_tail(self):
        completion = _stage_end(99)
        for count in (1, 3):
            with self.subTest(records=count):
                complete = b"".join([_stage_start(0)] + [_heartbeat(i) for i in range(1, count)])
                self.assertEqual(complete.count(b"\n"), count)
                partial = completion[: len(completion) // 2]
                path = self._fixture(f"e7a-tail-{count}.jsonl", complete + partial)

                steps = walk(path, WATCH_MIN_BYTES)
                first = steps[0].window
                self.assertIsNotNone(first)
                self.assertEqual(len(first.records), count)
                self.assertTrue(first.eof)
                # eof is true, yet bytes are withheld: not terminal EOF.
                self.assertLess(first.next_offset, first.snapshot_bytes)
                self.assertIsNotNone(steps[-1].error)
                self.assertEqual(steps[-1].error.code, "unterminated_tail")

                # The rejected `while not eof` loop stops at the first
                # response and never issues the call that reports the tail -
                # so it also never retrieves the record a later append
                # completes. This is the only place that loop may appear.
                naive: list[bytes] = []
                cursor = 0
                eof = False
                while not eof:
                    window = read_window(path, cursor_offset=cursor, window_bytes=WATCH_MIN_BYTES)
                    naive.extend(decode_record(r) for r in window.records)
                    cursor = window.next_offset
                    eof = window.eof
                self.assertEqual(len(naive), count)

                with path.open("ab") as handle:
                    handle.write(completion[len(completion) // 2 :])
                resumed = read_window(
                    path, cursor_offset=first.next_offset, window_bytes=WATCH_MIN_BYTES
                )
                self.assertEqual([decode_record(r) for r in resumed.records], [completion])
                self.assertTrue(resumed.eof)
                self.assertEqual(resumed.next_offset, resumed.snapshot_bytes)
                self.assertNotIn(completion, naive)

    # -- E7b ---------------------------------------------------------------
    def test_e7b_unterminated_tail_is_terminal_and_repeatable(self):
        line = _stage_end(1)
        raw = _stage_start(0) + line[: len(line) // 2]
        path = self._fixture("e7b-tail.jsonl", raw)
        cursor = len(_stage_start(0))
        for bound in (WATCH_MIN_BYTES, WATCH_DEFAULT_BYTES):
            for attempt in (1, 2):
                with self.subTest(bound=bound, attempt=attempt):
                    with self.assertRaises(WatchError) as caught:
                        read_window(path, cursor_offset=cursor, window_bytes=bound)
                    self.assertEqual(caught.exception.code, "unterminated_tail")
                    self.assertEqual(caught.exception.snapshot_bytes, len(raw))

    def test_e7b_oversized_corruption_is_window_independent(self):
        oversized = b"{" + b"c" * (WATCH_RECORD_MAX_BYTES - 1) + b"\n"
        self.assertEqual(len(oversized), WATCH_RECORD_MAX_BYTES + 1)
        cases = {
            # (c) an over-long line WITH a trailing newline
            "terminated": (oversized, 0),
            # (d) the same line WITHOUT a trailing newline at end of file
            "unterminated": (oversized[:-1], 0),
            # (e) an unterminated corrupt span above the window floor
            "above_window_floor": (b"z" * (WATCH_MIN_BYTES + 1), 0),
            # (f) two good records, then case (c)
            "after_good_records": (
                _stage_start(0) + _heartbeat(1) + oversized,
                len(_stage_start(0) + _heartbeat(1)),
            ),
        }
        for name, (raw, failing_cursor) in cases.items():
            path = self._fixture(f"e7b-{name}.jsonl", raw)
            observed = {}
            for bound in (WATCH_MIN_BYTES, WATCH_DEFAULT_BYTES):
                with self.subTest(case=name, bound=bound):
                    steps = walk(path, bound)
                    self.assertIsNotNone(steps[-1].error)
                    self.assertEqual(steps[-1].error.code, "oversized_record")
                    returned = b"".join(walk_records(steps))
                    self.assertNotIn(b"c" * WATCH_RECORD_MAX_BYTES, returned)
                    self.assertNotIn(b"z" * WATCH_MIN_BYTES, returned)
                    self.assertEqual(len(returned), failing_cursor)
                    with self.assertRaises(WatchError) as caught:
                        read_window(path, cursor_offset=failing_cursor, window_bytes=bound)
                    self.assertEqual(caught.exception.code, "oversized_record")
                    observed[bound] = caught.exception.code
            self.assertEqual(len(set(observed.values())), 1, f"{name} classified differently")

    def test_e7b_failure_codes_are_pairwise_distinct(self):
        self.assertEqual(
            len({"unterminated_tail", "oversized_record", "stream_truncated"}), 3
        )
        for code in ("unterminated_tail", "oversized_record", "stream_truncated"):
            self.assertIn(code, WATCH_ERROR_CODES)

    def test_e7b_admissible_line_with_raw_invalid_bytes_is_returned_verbatim(self):
        # (g) external corruption H2 cannot write: raw invalid bytes injected
        # after JSON encoding. The reader parses no JSON, so it frames and
        # returns the line as data.
        corrupt = _fragment(0, "prefix").replace(b"prefix", b"pre\xff\xfefix")
        self.assertLessEqual(len(corrupt), WATCH_RECORD_MAX_BYTES)
        path = self._fixture("e7b-invalid-bytes.jsonl", corrupt)
        with self.assertRaises(UnicodeDecodeError):
            corrupt.decode("utf-8")
        for bound in (WATCH_MIN_BYTES, WATCH_DEFAULT_BYTES):
            with self.subTest(bound=bound):
                window = read_window(path, cursor_offset=0, window_bytes=bound)
                self.assertEqual([decode_record(r) for r in window.records], [corrupt])
                self.assertEqual(b"".join(decode_record(r) for r in window.records), path.read_bytes())

    # -- E8 ----------------------------------------------------------------
    def test_e8_file_shrinking_under_the_snapshot_is_terminal(self):
        raw = mixed_stream(3 * WATCH_RECORD_MAX_BYTES)
        path = self._fixture("e8.jsonl", raw)
        recorder = _FdCalls(path)
        with contextlib.ExitStack() as stack:
            def on_pread(index: int):
                if index == 1:
                    with path.open("r+b") as handle:
                        handle.truncate(0)
                    return b""
                return None

            recorder.install(stack, on_pread=on_pread)
            with self.assertRaises(WatchError) as caught:
                read_window(path, cursor_offset=0, window_bytes=WATCH_DEFAULT_BYTES)
        self.assertEqual(caught.exception.code, "stream_truncated")
        self.assertEqual(caught.exception.snapshot_bytes, len(raw))
        self.assertNotEqual(caught.exception.code, "oversized_record")
        # One read attempt, no retry loop at a larger size.
        self.assertEqual([op for op, _ in recorder.calls], ["fstat", "pread"])

    def test_e8_missing_stream_is_named_and_never_created(self):
        path = self.workdir / "absent.live.jsonl"
        with self.assertRaises(WatchError) as caught:
            read_window(path, cursor_offset=0, window_bytes=WATCH_MIN_BYTES)
        self.assertEqual(caught.exception.code, "no_live_stream")
        self.assertFalse(path.exists())


# --------------------------------------------------------------------------
# CLI harness for E9-E12. A real `Controller` drives a real stage through
# H2's shipped `SubprocessRunner`, so every live stream below is written by
# the writer H3 reads, not by this test.
# --------------------------------------------------------------------------
ONE_STAGE_PROFILE = """version: 1
type: demo-loop
initial_stage: work
max_transitions: 6
stages:
  work:
    owner: claude
    attempt_cap: 2
    timeout: 30
    prompt: "produce output"
    outcomes:
      submit: done
  done:
    terminal: done
edge_caps:
  work.submit: 3
"""

TWO_STAGE_PROFILE = """version: 1
type: demo-loop
initial_stage: first
max_transitions: 8
stages:
  first:
    owner: claude
    attempt_cap: 2
    timeout: 30
    prompt: "first stage"
    outcomes:
      submit: second
  second:
    owner: claude
    attempt_cap: 2
    timeout: 30
    prompt: "second stage"
    outcomes:
      submit: done
  done:
    terminal: done
edge_caps:
  first.submit: 3
  second.submit: 3
"""


def text_spec(text: str) -> str:
    """A fake-child op that writes `text` verbatim, newlines included."""
    return f"b64:{base64.b64encode(text.encode('utf-8')).decode('ascii')}"


def outcome_spec(outcome: str) -> str:
    """A fake-child op that emits the orchestrator outcome sentinel."""
    payload = f"ORCHESTRATOR_OUTCOME: {outcome}\n".encode("utf-8")
    return f"b64:{base64.b64encode(payload).decode('ascii')}"


class _PassPreflightRunner(SubprocessRunner):
    """H2's runner, with only the provider probe short-circuited.

    `run` - the drain loop, the live writer, the byte budget - is untouched:
    it is the input H3 reads. The probe is replaced because it would spawn the
    fake child an extra time and classify its output as a provider handshake,
    which is not what these cases are about.
    """

    def preflight(self, owner: str, timeout: int = 5) -> ProviderPreflightResult:
        now = int(time.time() * 1000)
        return ProviderPreflightResult("pass", "h3_test_harness", "", 0, [], None, now, now)


class _NoLiveStreamRunner:
    """Writes the sealed log but no live stream, for E9's missing-stream case."""

    def run(self, owner: str, prompt: str, timeout: int, log_path: Path) -> RunResult:
        output = "ORCHESTRATOR_OUTCOME: submit\n"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(output, encoding="utf-8")
        return RunResult(0, output, None, "raw", "raw")


class StageHarness:
    """One temporary ORCH_HOME with one submitted task."""

    def __init__(self, case: unittest.TestCase, *, profile: str = ONE_STAGE_PROFILE, task_id: str | None = None):
        tmp = tempfile.TemporaryDirectory(prefix="h3-cli-")
        case.addCleanup(tmp.cleanup)
        self.case = case
        self.root = Path(tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        # `Controller` resolves its home, so the DB stores resolved paths.
        # Resolve here too, or every path comparison below compares
        # /var/... against /private/var/... on macOS.
        self.home = self.home.resolve()
        self.profile_path = self.root / "profile.yaml"
        self.profile_path.write_text(profile, encoding="utf-8")
        self.input_path = self.root / "input.md"
        self.input_path.write_text("h3 watch harness input\n", encoding="utf-8")
        # Own config file, so the host's ~/.config/agent-orch/orch.toml cannot
        # change what this test observes.
        self.config_path = self.root / "orch.toml"
        self.config_path.write_text("[env]\n", encoding="utf-8")
        self.env = {
            "ORCH_HOME": str(self.home),
            "ORCH_CONFIG": str(self.config_path),
            "ORCH_PROTECTED_ROOTS": "",
        }
        with mock.patch.dict(os.environ, self.env):
            controller = Controller(self.home, runner=_PassPreflightRunner())
            try:
                self.task_id = controller.submit(
                    "demo-loop", self.profile_path, self.input_path, task_id=task_id
                )
            finally:
                controller.close()
        self.db_path = self.home / "orchestrator.db"
        self.artifact_dir = self.home / "tasks" / self.task_id

    # -- driving a stage ---------------------------------------------------
    def _child_env(self, child_spec: str) -> dict[str, str]:
        command = child_command(child_spec)
        return {**self.env, "ORCH_CLAUDE_COMMAND": command, "ORCH_CODEX_COMMAND": command}

    def run_foreground(self, child_spec: str, *, runner: object | None = None) -> None:
        with mock.patch.dict(os.environ, self._child_env(child_spec)):
            controller = Controller(self.home, runner=runner or _PassPreflightRunner())
            try:
                controller.run_until_stop(self.task_id)
            finally:
                controller.close()

    def run_background(self, child_spec: str) -> threading.Thread:
        """Run the stage on its own thread, with its own DB connection.

        sqlite connections are thread-bound, so the worker builds its own
        `Controller` rather than sharing one with the observing thread. That
        is also the shape the daemon has in production: a separate process
        owns the write connection while `watch` opens `mode=ro`.
        """
        env = self._child_env(child_spec)
        failures: list[BaseException] = []

        def target() -> None:
            try:
                with mock.patch.dict(os.environ, env):
                    controller = Controller(self.home, runner=_PassPreflightRunner())
                    try:
                        controller.run_until_stop(self.task_id)
                    finally:
                        controller.close()
            except BaseException as exc:  # pragma: no cover - surfaced by the case
                failures.append(exc)

        thread = threading.Thread(target=target, name="h3-stage")
        thread.start()
        self.case.addCleanup(lambda: thread.join(timeout=60))
        self._failures = failures
        return thread

    # -- observation (its own read-only connection) ------------------------
    def observe(self) -> dict:
        uri = self.db_path.resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            task = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (self.task_id,)).fetchone())
            runs = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM stage_runs WHERE task_id=? ORDER BY started_at,rowid",
                    (self.task_id,),
                )
            ]
            transitions = [
                dict(row)
                for row in conn.execute(
                    "SELECT seq,to_status,reason FROM transitions WHERE task_id=? ORDER BY seq",
                    (self.task_id,),
                )
            ]
            user_version = conn.execute("PRAGMA user_version").fetchone()[0]
            counts = {
                name: conn.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
                for name in ("tasks", "stage_runs", "transitions", "notifications")
            }
        finally:
            conn.close()
        return {
            "task": task,
            "runs": runs,
            "transitions": transitions,
            "user_version": user_version,
            "counts": counts,
        }

    def live_paths(self) -> list[Path]:
        return sorted((self.artifact_dir / "runs").glob("*.live.jsonl"))

    def await_live_records(self, minimum: int, *, timeout: float = 30.0) -> Path:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            paths = self.live_paths()
            if paths and len(raw_lines(paths[-1])) >= minimum:
                return paths[-1]
            time.sleep(0.02)
        self.case.fail(f"live stream did not reach {minimum} records within {timeout}s")

    # -- the CLI under test ------------------------------------------------
    def watch(self, *argv: str) -> tuple[int, dict | None, str]:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, self.env):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli_main(["watch", *argv])
        text = out.getvalue()
        envelope = json.loads(text) if text.strip() else None
        return code, envelope, err.getvalue()

    def walk_cli(self, *argv: str, cursor: str | None = None) -> list[tuple[int, dict]]:
        """Section 5.4's composite-predicate loop, driven through `cli.main`."""
        steps: list[tuple[int, dict]] = []
        while True:
            if len(steps) > WALK_CALL_CAP:  # pragma: no cover - livelock guard
                raise AssertionError("CLI walk did not terminate")
            extra = ["--cursor", cursor] if cursor is not None else []
            code, envelope, _ = self.watch(self.task_id, *argv, *extra)
            steps.append((code, envelope))
            if envelope["error"] is not None:
                return steps
            cursor = envelope["next_cursor"]
            if envelope["eof"] and envelope["next_cursor"].endswith(f":{envelope['snapshot_bytes']}"):
                return steps


def envelope_bytes(steps: list[tuple[int, dict]]) -> bytes:
    return b"".join(
        decode_record(record) for _, envelope in steps for record in envelope["records"]
    )


# --------------------------------------------------------------------------
# E9, E10a, E10b, E10c, E11, E12 - read-only `orch watch` (H3.3)
# --------------------------------------------------------------------------
#: Which named case observes each frozen code. Asserted against
#: WATCH_ERROR_CODES, so a code can be neither added without coverage nor
#: covered by a case that has been renamed away.
CODE_COVERAGE = {
    "task_not_found": ("WatchCliTests", "test_e9_unknown_task_id"),
    "no_stage_run": ("WatchCliTests", "test_e9_task_with_no_stage_runs"),
    "no_live_stream": ("ReaderTests", "test_e8_missing_stream_is_named_and_never_created"),
    "live_path_outside_artifact_dir": (
        "WatchCliTests",
        "test_e9_log_path_outside_the_artifact_dir_is_refused",
    ),
    "window_too_small": ("ReaderTests", "test_e6_window_below_the_floor_is_refused_and_not_clamped"),
    "cursor_malformed": ("ReaderTests", "test_e6_malformed_cursors_are_rejected_and_never_repaired"),
    "cursor_run_token_unknown": ("WatchCliTests", "test_e11_cursor_from_another_task_is_rejected"),
    "cursor_out_of_range": ("ReaderTests", "test_e6_out_of_range_and_interior_cursors_are_rejected"),
    "cursor_interior_record": (
        "ReaderTests",
        "test_e6_out_of_range_and_interior_cursors_are_rejected",
    ),
    "oversized_record": ("ReaderTests", "test_e7b_oversized_corruption_is_window_independent"),
    "unterminated_tail": ("ReaderTests", "test_e7b_unterminated_tail_is_terminal_and_repeatable"),
    "stream_truncated": ("ReaderTests", "test_e8_file_shrinking_under_the_snapshot_is_terminal"),
}


class WatchCliTests(unittest.TestCase):
    # -- E9 ----------------------------------------------------------------
    def test_e9_unknown_task_id(self):
        harness = StageHarness(self)
        code, envelope, err = harness.watch("no-such-task-id")
        self.assertEqual(code, 2)
        self.assertEqual(envelope["error"], "task_not_found")
        self.assertEqual(envelope["task_id"], "no-such-task-id")
        self.assertIsNone(envelope["run_token"])
        self.assertIsNone(envelope["snapshot_bytes"])
        self.assertIn("orchestrator:", err)
        # A failed lookup never creates the file it did not find.
        self.assertEqual(harness.live_paths(), [])

    def test_e9_task_with_no_stage_runs(self):
        harness = StageHarness(self)
        code, envelope, _ = harness.watch(harness.task_id)
        self.assertEqual(code, 2)
        self.assertEqual(envelope["error"], "no_stage_run")
        self.assertIsNone(envelope["run_token"])
        self.assertEqual(harness.live_paths(), [])

    def test_e9_missing_live_stream_is_named_and_never_created(self):
        harness = StageHarness(self)
        harness.run_foreground("exit:0", runner=_NoLiveStreamRunner())
        runs = harness.observe()["runs"]
        self.assertTrue(runs)
        live = Path(runs[-1]["log_path"]).with_suffix(".live.jsonl")
        self.assertFalse(live.exists())
        code, envelope, _ = harness.watch(harness.task_id)
        self.assertEqual(code, 2)
        self.assertEqual(envelope["error"], "no_live_stream")
        self.assertEqual(envelope["run_token"], runs[-1]["run_token"])
        self.assertIsNone(envelope["snapshot_bytes"])
        # The reader never creates the file it failed to find.
        self.assertFalse(live.exists())

    def test_e9_log_path_outside_the_artifact_dir_is_refused(self):
        harness = StageHarness(self)
        harness.run_foreground(f"{text_spec(chr(10).join(['hi', '']))};{outcome_spec('submit')};exit:0")
        outside = harness.root / "outside"
        outside.mkdir()
        stray = outside / "0001-work-stray.log"
        stray.write_text("stray\n", encoding="utf-8")
        (outside / "0001-work-stray.live.jsonl").write_bytes(_stage_start(0))
        conn = sqlite3.connect(harness.db_path)
        try:
            conn.execute(
                "UPDATE stage_runs SET log_path=? WHERE task_id=?", (str(stray), harness.task_id)
            )
            conn.commit()
        finally:
            conn.close()
        code, envelope, _ = harness.watch(harness.task_id)
        self.assertEqual(code, 2)
        self.assertEqual(envelope["error"], "live_path_outside_artifact_dir")
        self.assertIsNone(envelope["snapshot_bytes"])

    def test_e9_symlinked_live_stream_escaping_the_artifact_dir_is_refused(self):
        # Containment resolves BOTH sides before comparing, so a live path
        # that is lexically inside the artifact dir but symlinked out is
        # refused rather than read.
        harness = StageHarness(self)
        harness.run_foreground("exit:0", runner=_NoLiveStreamRunner())
        runs = harness.observe()["runs"]
        live = Path(runs[-1]["log_path"]).with_suffix(".live.jsonl")
        outside = harness.root / "escape.live.jsonl"
        outside.write_bytes(_stage_start(0))
        live.symlink_to(outside)
        self.assertTrue(live.is_symlink())
        self.assertTrue(str(live).startswith(str(harness.artifact_dir)))
        code, envelope, _ = harness.watch(harness.task_id)
        self.assertEqual(code, 2)
        self.assertEqual(envelope["error"], "live_path_outside_artifact_dir")
        self.assertEqual(envelope["records"], [])
        self.assertIsNone(envelope["next_cursor"])

    # -- E10a --------------------------------------------------------------
    def test_e10a_live_read_does_not_orphan_block_the_running_task(self):
        harness = StageHarness(self)
        thread = harness.run_background(f"{text_spec(chr(10).join(['hello', '']))};sleep:6;{outcome_spec('submit')};exit:0")
        try:
            live = harness.await_live_records(2)
            before = harness.observe()
            self.assertEqual(before["task"]["status"], "running")
            child_pid = json.loads(raw_lines(live)[0].decode("utf-8"))["child_pid"]
            os.kill(child_pid, 0)  # provably alive

            code, envelope, _ = harness.watch(harness.task_id)

            after = harness.observe()
            os.kill(child_pid, 0)  # still alive: the reader touched no process
        finally:
            thread.join(timeout=60)
        self.assertEqual(code, 0, envelope)
        self.assertIsNone(envelope["error"])
        events = [json.loads(decode_record(r).decode("utf-8"))["event"] for r in envelope["records"]]
        self.assertEqual(events[0], "stage_start")
        self.assertTrue(set(events[1:]) & {"output_fragment", "heartbeat"}, events)

        self.assertEqual(after["task"]["status"], "running")
        self.assertEqual([run["status"] for run in after["runs"]], ["running"])
        self.assertEqual(len(after["transitions"]), len(before["transitions"]))
        self.assertEqual(after["counts"], before["counts"])
        self.assertNotIn("blocked", [row["to_status"] for row in after["transitions"]])
        # And no blocked transition appeared once the stage finished either.
        final = harness.observe()
        self.assertNotIn("blocked", [row["to_status"] for row in final["transitions"]])
        self.assertEqual(final["task"]["status"], "done")

    # -- E10b --------------------------------------------------------------
    def test_e10b_reader_is_read_only_while_the_writer_appends(self):
        harness = StageHarness(self)
        thread = harness.run_background(f"{text_spec(chr(10).join(['hello', '']))};sleep:6;{outcome_spec('submit')};exit:0")
        recorder = None
        try:
            live = harness.await_live_records(2)
            before_bytes = live.read_bytes()
            before_size = live.stat().st_size
            recorder = _FdCalls(live)
            with contextlib.ExitStack() as stack:
                recorder.install(stack)
                code, envelope, _ = harness.watch(harness.task_id)
            after_bytes = live.read_bytes()
            after_size = live.stat().st_size
        finally:
            thread.join(timeout=60)
        self.assertEqual(code, 0, envelope)
        # The only permitted difference is an append by the writer. Exact
        # equality is deliberately NOT asserted: a heartbeat landing between
        # the two observations would fail it on correct code. Concurrent
        # append semantics are covered deterministically by E3/E4.
        self.assertTrue(after_bytes.startswith(before_bytes))
        self.assertGreaterEqual(after_size, before_size)
        recorder.assert_read_only(self)

    def test_e10b_watching_leaves_the_sealed_manifest_and_db_rows_byte_identical(self):
        harness = StageHarness(self)
        harness.run_foreground(f"{text_spec(chr(10).join(['hello', '']))};{outcome_spec('submit')};exit:0")
        before = harness.observe()
        run = before["runs"][-1]
        manifest = Path(run["manifest_path"])
        self.assertTrue(manifest.is_file())
        manifest_before = manifest.read_bytes()
        live = Path(run["log_path"]).with_suffix(".live.jsonl")
        live_before = live.read_bytes()

        recorder = _FdCalls(live)
        with contextlib.ExitStack() as stack:
            recorder.install(stack)
            code, envelope, _ = harness.watch(harness.task_id)
        self.assertEqual(code, 0, envelope)

        after = harness.observe()
        # Byte-for-byte, not merely "contains no reference".
        self.assertEqual(manifest.read_bytes(), manifest_before)
        self.assertNotIn(".live.jsonl", manifest_before.decode("utf-8"))
        self.assertEqual(live.read_bytes(), live_before)
        self.assertEqual(after["task"], before["task"])
        self.assertEqual(after["runs"], before["runs"])
        self.assertEqual(after["transitions"], before["transitions"])
        self.assertEqual(after["user_version"], before["user_version"])
        self.assertEqual(after["counts"], before["counts"])
        recorder.assert_read_only(self)

    # -- E10c --------------------------------------------------------------
    def test_e10c_a_real_stream_drains_in_one_window_at_both_bounds(self):
        harness = StageHarness(self)
        harness.run_foreground(f"{text_spec(chr(10).join(['hello', 'more output', '']))};{outcome_spec('submit')};exit:0")
        live = harness.live_paths()[-1]
        self.assertLessEqual(live.stat().st_size, LIVE_MAX_BYTES)
        for bound in (str(WATCH_DEFAULT_BYTES), str(WATCH_MIN_BYTES)):
            with self.subTest(bound=bound):
                code, envelope, _ = harness.watch(harness.task_id, "--max-bytes", bound)
                self.assertEqual(code, 0, envelope)
                self.assertTrue(envelope["eof"])
                self.assertEqual(
                    envelope["next_cursor"], f"{envelope['run_token']}:{envelope['snapshot_bytes']}"
                )
                self.assertEqual(
                    b"".join(decode_record(r) for r in envelope["records"]),
                    live.read_bytes()[: envelope["snapshot_bytes"]],
                )
                held_code, held, _ = harness.watch(
                    harness.task_id, "--max-bytes", bound, "--cursor", envelope["next_cursor"]
                )
                self.assertEqual(held_code, 0, held)
                self.assertEqual(held["records"], [])
                self.assertEqual(held["next_cursor"], envelope["next_cursor"])
                self.assertTrue(held["eof"])

    # -- E11 ---------------------------------------------------------------
    def test_e11_run_selection_is_by_cursor_not_by_recency(self):
        harness = StageHarness(self, profile=TWO_STAGE_PROFILE)
        harness.run_foreground(f"{text_spec(chr(10).join(['stage output', '']))};{outcome_spec('submit')};exit:0")
        runs = harness.observe()["runs"]
        self.assertEqual(len(runs), 2)
        first, second = runs[0], runs[1]
        self.assertNotEqual(first["run_token"], second["run_token"])
        files = {
            row["run_token"]: Path(row["log_path"]).with_suffix(".live.jsonl") for row in runs
        }
        self.assertNotEqual(files[first["run_token"]], files[second["run_token"]])

        # No cursor: the latest run.
        code, envelope, _ = harness.watch(harness.task_id)
        self.assertEqual(code, 0, envelope)
        self.assertEqual(envelope["run_token"], second["run_token"])

        # A cursor from the earlier run keeps watching the earlier run to its
        # terminal EOF, even though a later run exists.
        for row in (first, second):
            with self.subTest(run_token=row["run_token"]):
                steps = harness.walk_cli(cursor=f"{row['run_token']}:0")
                self.assertIsNone(steps[-1][1]["error"])
                for _, envelope in steps:
                    self.assertEqual(envelope["run_token"], row["run_token"])
                snapshot = steps[-1][1]["snapshot_bytes"]
                self.assertEqual(
                    envelope_bytes(steps), files[row["run_token"]].read_bytes()[:snapshot]
                )

    def test_e11_cursor_from_another_task_is_rejected(self):
        harness = StageHarness(self)
        harness.run_foreground(f"{text_spec(chr(10).join(['hello', '']))};{outcome_spec('submit')};exit:0")
        other = StageHarness(self)
        other.run_foreground(f"{text_spec(chr(10).join(['hello', '']))};{outcome_spec('submit')};exit:0")
        foreign = other.observe()["runs"][-1]["run_token"]
        for cursor in (f"{foreign}:0", f"{uuid.uuid4()}:0"):
            with self.subTest(cursor=cursor):
                code, envelope, _ = harness.watch(harness.task_id, "--cursor", cursor)
                self.assertEqual(code, 2)
                self.assertEqual(envelope["error"], "cursor_run_token_unknown")
                self.assertEqual(envelope["cursor"], cursor)
                self.assertIsNone(envelope["next_cursor"])
                self.assertIsNone(envelope["run_token"])

    # -- E5, E6, E7a, E7b through the CLI ----------------------------------
    def test_e5_cli_walk_reconstructs_the_real_stream_exactly(self):
        harness = StageHarness(self)
        harness.run_foreground(f"{text_spec(chr(10).join(['hello', 'and more', '']))};{outcome_spec('submit')};exit:0")
        live = harness.live_paths()[-1]
        steps = harness.walk_cli("--max-bytes", str(WATCH_MIN_BYTES))
        self.assertIsNone(steps[-1][1]["error"])
        snapshot = steps[-1][1]["snapshot_bytes"]
        self.assertEqual(envelope_bytes(steps), live.read_bytes()[:snapshot])
        parsed = [
            json.loads(decode_record(record).decode("utf-8"))
            for _, envelope in steps
            for record in envelope["records"]
        ]
        seqs = [record["seq"] for record in parsed]
        self.assertEqual(seqs, list(range(len(seqs))))
        self.assertEqual([record["event"] for record in parsed].count("stage_end"), 1)
        self.assertEqual(parsed[-1]["event"], "stage_end")

    def test_e6_cli_cursor_matrix_returns_exact_codes_and_echoes_verbatim(self):
        harness = StageHarness(self)
        harness.run_foreground(f"{text_spec(chr(10).join(['hello', '']))};{outcome_spec('submit')};exit:0")
        run = harness.observe()["runs"][-1]
        token = run["run_token"]
        live = harness.live_paths()[-1]
        size = live.stat().st_size
        cases = {
            "-1": "cursor_malformed",
            "+5": "cursor_malformed",
            "abc": "cursor_malformed",
            "5": "cursor_malformed",
            f"{token}:": "cursor_malformed",
            f"{token}:1e3": "cursor_malformed",
            f"{uuid.uuid4()}:0": "cursor_run_token_unknown",
            f"{token}:{size + 1}": "cursor_out_of_range",
            f"{token}:1": "cursor_interior_record",
        }
        observed = set()
        for cursor, expected in cases.items():
            with self.subTest(cursor=cursor):
                code, envelope, err = harness.watch(harness.task_id, "--cursor", cursor)
                self.assertEqual(code, 2)
                self.assertEqual(envelope["error"], expected)
                self.assertEqual(envelope["records"], [])
                self.assertIsNone(envelope["next_cursor"])
                # Verbatim echo: never rounded, clamped or repaired to a
                # boundary. In particular the interior cursor 1 does not come
                # back as 0 or as the next record boundary.
                self.assertEqual(envelope["cursor"], cursor)
                self.assertIn("orchestrator:", err)
                observed.add(expected)
        code, envelope, _ = harness.watch(harness.task_id, "--max-bytes", str(WATCH_MIN_BYTES - 1))
        self.assertEqual(code, 2)
        self.assertEqual(envelope["error"], "window_too_small")
        self.assertIsNone(envelope["cursor"])
        observed.add("window_too_small")
        self.assertLessEqual(observed, set(WATCH_ERROR_CODES))

    def test_e6_every_frozen_code_is_reachable_and_no_other_code_exists(self):
        # Two halves of "the set of codes the module can emit equals the
        # frozen closed set exactly":
        #   (1) no code outside the set is raised anywhere in the reader or
        #       its CLI branch, and every code in the set is raised somewhere;
        #   (2) every code in the set is actually observed by a named case in
        #       this module, so none of them is unreachable in practice.
        raised = set()
        for source in (WATCH_SOURCE, ROOT / "orchestrator" / "cli.py"):
            raised |= set(re.findall(r'WatchError\(\s*"([a-z_]+)"', source.read_text(encoding="utf-8")))
        self.assertEqual(raised, set(WATCH_ERROR_CODES))

        self.assertEqual(set(CODE_COVERAGE), set(WATCH_ERROR_CODES))
        for code, (class_name, method) in CODE_COVERAGE.items():
            with self.subTest(code=code):
                case_class = globals()[class_name]
                self.assertTrue(
                    hasattr(case_class, method), f"{class_name}.{method} no longer exists"
                )

    def test_e7a_cli_composite_predicate_resolves_a_withheld_tail(self):
        harness = StageHarness(self)
        harness.run_foreground(f"{text_spec(chr(10).join(['hello', '']))};{outcome_spec('submit')};exit:0")
        live = harness.live_paths()[-1]
        whole = live.read_bytes()
        lines = raw_lines(live)
        # Truncate the real stream mid-record: a crash-truncated tail the H2
        # writer cannot itself produce.
        kept = b"".join(lines[:-1])
        live.write_bytes(kept + lines[-1][: len(lines[-1]) // 2])

        steps = harness.walk_cli("--max-bytes", str(WATCH_MIN_BYTES))
        first = steps[0][1]
        self.assertIsNone(first["error"])
        self.assertTrue(first["eof"])
        self.assertEqual(first["next_cursor"], f"{first['run_token']}:{len(kept)}")
        self.assertLess(len(kept), first["snapshot_bytes"])
        self.assertEqual(steps[-1][1]["error"], "unterminated_tail")
        self.assertEqual(steps[-1][0], 2)

        # Completing the record makes an explicit call at the SAME cursor
        # return it whole and reach terminal EOF.
        live.write_bytes(whole)
        code, envelope, _ = harness.watch(
            harness.task_id, "--max-bytes", str(WATCH_MIN_BYTES), "--cursor", first["next_cursor"]
        )
        self.assertEqual(code, 0, envelope)
        self.assertEqual([decode_record(r) for r in envelope["records"]], [lines[-1]])
        self.assertTrue(envelope["eof"])
        self.assertEqual(envelope["next_cursor"], f"{envelope['run_token']}:{len(whole)}")

    def test_e7b_unterminated_tail_is_terminal_whatever_the_run_row_says(self):
        # (a) the stale-`running` crash seam and (b) the same tail with the row
        # already committed. The reader consults no run status at all, which
        # is why both answer identically.
        for row_status in ("running", "committed"):
            with self.subTest(row_status=row_status):
                harness = StageHarness(self)
                harness.run_foreground(f"{text_spec(chr(10).join(['hello', '']))};{outcome_spec('submit')};exit:0")
                live = harness.live_paths()[-1]
                lines = raw_lines(live)
                kept = b"".join(lines[:-1])
                live.write_bytes(kept + lines[-1][: len(lines[-1]) // 2])
                run = harness.observe()["runs"][-1]
                conn = sqlite3.connect(harness.db_path)
                try:
                    conn.execute(
                        "UPDATE stage_runs SET status=? WHERE run_token=?",
                        (row_status, run["run_token"]),
                    )
                    conn.commit()
                finally:
                    conn.close()
                self.assertEqual(harness.observe()["runs"][-1]["status"], row_status)
                cursor = f"{run['run_token']}:{len(kept)}"
                for attempt in (1, 2):
                    code, envelope, _ = harness.watch(
                        harness.task_id, "--max-bytes", str(WATCH_MIN_BYTES), "--cursor", cursor
                    )
                    self.assertEqual(code, 2, f"attempt {attempt}")
                    self.assertEqual(envelope["error"], "unterminated_tail")
                    self.assertEqual(envelope["records"], [])
                    self.assertIsNone(envelope["next_cursor"])

    # -- E12 ---------------------------------------------------------------
    def test_e12_envelope_shape_is_frozen_on_both_paths(self):
        harness = StageHarness(self)
        harness.run_foreground(f"{text_spec(chr(10).join(['hello', '']))};{outcome_spec('submit')};exit:0")
        code, success, _ = harness.watch(harness.task_id)
        self.assertEqual(code, 0, success)
        fail_code, failure, _ = harness.watch(harness.task_id, "--cursor", "nope")
        self.assertEqual(fail_code, 2)
        for envelope in (success, failure):
            self.assertEqual(sorted(envelope), sorted(WATCH_RESPONSE_KEYS))
            self.assertEqual(envelope["schema_version"], WATCH_SCHEMA_VERSION)
            self.assertIsInstance(envelope["records"], list)
            for record in envelope["records"]:
                self.assertIsInstance(record, str)
        self.assertIsNone(failure["next_cursor"])
        self.assertIs(failure["eof"], False)
        self.assertEqual(failure["records"], [])
        self.assertEqual(failure["cursor"], "nope")
        self.assertIsNone(failure["snapshot_bytes"])
        self.assertIsNone(success["cursor"])
        self.assertIsNotNone(success["snapshot_bytes"])

    def test_e12_a_long_task_id_round_trips_without_entering_the_window_arithmetic(self):
        long_id = "h3-watch-long-task-id-" + "z" * 200
        harness = StageHarness(self, task_id=long_id)
        harness.run_foreground(f"{text_spec(chr(10).join(['hello', '']))};{outcome_spec('submit')};exit:0")
        live = harness.live_paths()[-1]
        code, envelope, _ = harness.watch(long_id)
        self.assertEqual(code, 0, envelope)
        self.assertEqual(envelope["task_id"], long_id)
        self.assertEqual(envelope["snapshot_bytes"], live.stat().st_size)
        self.assertEqual(envelope["next_cursor"], f"{envelope['run_token']}:{live.stat().st_size}")
        self.assertTrue(envelope["eof"])

        control = StageHarness(self)
        control.run_foreground(f"{text_spec(chr(10).join(['hello', '']))};{outcome_spec('submit')};exit:0")
        _, control_envelope, _ = control.watch(control.task_id)
        # The envelope is not windowed, so the identifier length changed
        # nothing about the byte arithmetic.
        self.assertEqual(
            len(decode_record(control_envelope["records"][0])),
            len(decode_record(envelope["records"][0])),
        )

        fail_code, failure, _ = harness.watch(long_id, "--cursor", "nope")
        self.assertEqual(fail_code, 2)
        self.assertEqual(failure["task_id"], long_id)
        self.assertIsNone(failure["snapshot_bytes"])

    def test_e12_database_open_failure_has_no_envelope(self):
        harness = StageHarness(self)
        harness.db_path.unlink()
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, harness.env):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli_main(["watch", harness.task_id])
        self.assertEqual(code, 2)
        self.assertEqual(out.getvalue().strip(), "")
        self.assertIn("orchestrator:", err.getvalue())

    def test_e12_database_query_failure_has_no_envelope_or_traceback(self):
        harness = StageHarness(self)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, harness.env):
            with mock.patch(
                "orchestrator.cli.Controller.status",
                side_effect=sqlite3.OperationalError("unable to open database file"),
            ):
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    code = cli_main(["watch", harness.task_id])
        self.assertEqual(code, 2)
        self.assertEqual(out.getvalue().strip(), "")
        self.assertEqual(
            err.getvalue().strip(),
            "orchestrator: unable to open database file",
        )
        self.assertNotIn("Traceback", err.getvalue())

    def test_e12_help_states_the_composite_predicate_not_the_rejected_loop(self):
        parser = build_parser()
        actions = {
            action.dest: action
            for action in parser._subparsers._group_actions  # type: ignore[union-attr]
        }
        watch_parser = actions["command"].choices["watch"]  # type: ignore[attr-defined]
        text = watch_parser.format_help()
        self.assertIn("eof is true AND next_cursor equals", text)
        for rejected in ("while not eof", "while (not eof)"):
            self.assertNotIn(rejected, text)
        self.assertNotIn("while not eof", (ROOT / "orchestrator" / "cli.py").read_text(encoding="utf-8"))
        self.assertNotIn("while not eof", WATCH_SOURCE.read_text(encoding="utf-8"))
