"""H3: one bounded, read-only window over H2's live JSONL stream.

Everything here is a *reader*. It opens the stream `O_RDONLY`, never
`O_CREAT`, holds no state past one call, and consults no run-liveness signal:
the whole answer is derived from one pre-read `fstat` snapshot and the bytes
inside it. Nothing in this module may write, truncate, rename or unlink
anything, and nothing in it may import a mutation path.

H2's writer contract (`orchestrator/runner.py` @ `cca82ed`) is fixed input:

* the file is opened `"ab"` and only ever appended;
* every record is `json.dumps(..., ensure_ascii=False).encode("utf-8") + b"\\n"`,
  written and flushed as one call pair by a single thread;
* therefore a `\\n` in the file always terminates a complete record - it is the
  only `\\n` the writer emits, because `json.dumps` escapes an in-string
  newline as `\\\\n`. A partial tail is exactly "the bytes after the last `\\n`".
"""

from __future__ import annotations

import base64
import os
import re
from dataclasses import dataclass
from pathlib import Path

from .runner import LIVE_MAX_BYTES

#: Envelope version. Bumped only when the response *shape* changes.
WATCH_SCHEMA_VERSION = 1

#: The largest line H2 can admit, including its trailing newline. Imported
#: rather than copied so the two bounds cannot drift: H2's own admission
#: arithmetic caps every optional line at `LIVE_MAX_BYTES` minus its terminal
#: reserve, caps the terminal line at `LIVE_MAX_BYTES`, and caps the whole
#: file at `LIVE_MAX_BYTES`. A framed line longer than this is external
#: corruption H2 could not have written.
WATCH_RECORD_MAX_BYTES = LIVE_MAX_BYTES

#: Smallest accepted window. Strictly greater than `WATCH_RECORD_MAX_BYTES`,
#: which is what makes bounded progress provable: a full window that frames no
#: admissible line cannot be a legitimate "come back later", so it is a named
#: failure instead of a silent non-advancing success. The factor of two is
#: round slack, not necessity.
WATCH_MIN_BYTES = 2 * WATCH_RECORD_MAX_BYTES

#: Default window. Four times the minimum, so the default and the minimum
#: genuinely produce different call counts over the same fixture.
WATCH_DEFAULT_BYTES = 65536

#: Closed set. Every failure this reader and its CLI branch can name is here;
#: a failure that is not here (a `mode=ro` database open failure, an
#: unreadable home, an argparse usage error) keeps the CLI's pre-existing
#: stderr-only path and gets no envelope, because inventing a code for a
#: generic pre-existing failure would be new vocabulary for a risk H3 does
#: not introduce.
WATCH_ERROR_CODES = frozenset(
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

#: Frozen envelope key set, identical on the success and the failure path.
WATCH_RESPONSE_KEYS = (
    "schema_version",
    "task_id",
    "run_token",
    "cursor",
    "next_cursor",
    "eof",
    "snapshot_bytes",
    "records",
    "error",
)


class WatchError(Exception):
    """A named, terminal failure from the closed set.

    Carries the snapshot size when the pre-read `fstat` had already been
    taken, because the envelope reports it on the failure path too and a
    caller cannot otherwise tell "no snapshot" from "snapshot of zero bytes".
    """

    def __init__(self, code: str, message: str | None = None, *, snapshot_bytes: int | None = None) -> None:
        if code not in WATCH_ERROR_CODES:
            raise AssertionError(f"error code outside the frozen closed set: {code}")
        super().__init__(message or code)
        self.code = code
        self.snapshot_bytes = snapshot_bytes


#: `offset := [0-9]+`. Anchored on both ends, so a sign, a leading `+`, an
#: exponent or surrounding whitespace all fail the grammar rather than being
#: coerced into a number.
_OFFSET_RE = re.compile(r"\A[0-9]+\Z")


@dataclass(frozen=True)
class Window:
    """One window over one snapshot.

    `records` are base64 strings in file order, each covering one complete
    record's raw bytes including its trailing newline, so
    `b"".join(decode_record(r) for r in records)` is exactly the snapshot
    bytes from the input cursor to `next_offset`.
    """

    records: tuple[str, ...]
    next_offset: int
    eof: bool
    snapshot_bytes: int


def encode_record(raw: bytes) -> str:
    """Standard base64 (RFC 4648) of one record's raw bytes, newline included.

    Base64 rather than a decoded object or a text field: the response is JSON
    on stdout, the file may hold externally corrupted non-UTF-8 bytes, and any
    lossy or re-serialising representation would break the byte-for-byte
    reconstruction identity this module promises.
    """
    return base64.b64encode(raw).decode("ascii")


def decode_record(text: str) -> bytes:
    """Inverse of `encode_record`. The identity callers verify against."""
    return base64.b64decode(text)


def validate_window_bytes(window_bytes: int) -> None:
    """Reject a window that cannot guarantee progress. Never clamps.

    A window at or below `WATCH_RECORD_MAX_BYTES` could return zero complete
    records forever while the file keeps growing. Raising the floor for the
    caller would hide that from them, so the floor is refused instead.
    """
    if window_bytes < WATCH_MIN_BYTES:
        raise WatchError(
            "window_too_small",
            f"--max-bytes must be at least {WATCH_MIN_BYTES}, got {window_bytes}",
        )


def parse_cursor(raw: str) -> tuple[str, int]:
    """`<run_token>:<offset>` -> `(run_token, offset)`.

    The grammar is `offset := [0-9]+`: no sign, no leading `+`, no exponent,
    no whitespace. Nothing is clamped, defaulted, rounded or repaired - a
    cursor that does not parse is `cursor_malformed`, and the envelope echoes
    the rejected input verbatim so an operator can see what they typed.
    """
    token, separator, offset = raw.partition(":")
    if not separator or not token or not _OFFSET_RE.match(offset):
        raise WatchError("cursor_malformed", f"cursor must be <run_token>:<offset>, got {raw!r}")
    return token, int(offset)


def format_cursor(run_token: str, offset: int) -> str:
    """The inverse of `parse_cursor`. Deliberately transparent, not opaque."""
    return f"{run_token}:{offset}"


def read_window(live_path: Path, *, cursor_offset: int, window_bytes: int) -> Window:
    """One bounded window of complete records, over one immutable snapshot.

    The snapshot is `st_size` read once, before any read, and nothing
    downstream consults the file's current size again. That ordering - not a
    lock, not a size field in the stream, not a second index - is what makes a
    concurrent append belong to the *next* call by construction, so `eof` and
    `next_cursor` cannot depend on bytes that landed mid-read.

    Raises `WatchError` with a code from `WATCH_ERROR_CODES`. Every loop below
    terminates on every input: the read loop either grows the buffer or exits,
    and the framing scan advances past a newline each iteration.
    """
    validate_window_bytes(window_bytes)
    try:
        fd = os.open(live_path, os.O_RDONLY)
    except FileNotFoundError as exc:
        # Never O_CREAT: a reader that created the file it failed to find
        # would leave a zero-byte stream behind for the next caller to
        # misread as a completed run.
        raise WatchError("no_live_stream", f"no live stream at {live_path}") from exc
    try:
        # Step 2. The snapshot. Taken once, before any read, never re-taken.
        size = os.fstat(fd).st_size

        if cursor_offset > size:
            raise WatchError(
                "cursor_out_of_range",
                f"cursor offset {cursor_offset} is past the snapshot end {size}",
                snapshot_bytes=size,
            )

        # Boundary probe: exact, because the only newline the writer emits is
        # a record terminator. One byte, no index, no scan.
        if cursor_offset > 0 and os.pread(fd, 1, cursor_offset - 1) != b"\n":
            raise WatchError(
                "cursor_interior_record",
                f"cursor offset {cursor_offset} is inside a record",
                snapshot_bytes=size,
            )

        to_read = min(window_bytes, size - cursor_offset)
        buf = b""
        while len(buf) < to_read:
            chunk = os.pread(fd, to_read - len(buf), cursor_offset + len(buf))
            if not chunk:
                # The file shrank under the snapshot. H2 never truncates, so
                # this is external; either way it is terminal, not a retry.
                raise WatchError(
                    "stream_truncated",
                    f"stream shrank below the {size}-byte snapshot while reading",
                    snapshot_bytes=size,
                )
            buf += chunk

        # Frame and length-validate, left to right from the cursor. Framing
        # stops at the first line that violates H2's enforced per-line
        # maximum, and never returns such a line as data.
        pos = 0
        frames: list[bytes] = []
        oversized_at_pos = False
        while True:
            newline = buf.find(b"\n", pos)
            if newline < 0:
                break
            line_len = newline - pos + 1
            if line_len > WATCH_RECORD_MAX_BYTES:
                oversized_at_pos = True
                break
            frames.append(buf[pos : newline + 1])
            pos = newline + 1
        remainder = to_read - pos
        reached_snapshot_end = cursor_offset + to_read == size

        if frames:
            # Row 1. Good records win over a corrupt line that follows them:
            # next_cursor points at the corrupt line's first byte, and the
            # caller's next call at that cursor gets the named failure.
            next_offset = cursor_offset + pos
            assert next_offset > cursor_offset
            return Window(
                records=tuple(encode_record(frame) for frame in frames),
                next_offset=next_offset,
                eof=reached_snapshot_end,
                snapshot_bytes=size,
            )
        if oversized_at_pos:
            # Row 2. A terminated line longer than any H2 could admit.
            raise WatchError(
                "oversized_record",
                f"record at offset {cursor_offset} exceeds {WATCH_RECORD_MAX_BYTES} bytes",
                snapshot_bytes=size,
            )
        if remainder == 0:
            # Row 3. The only success path that holds the cursor, and it is
            # gated on to_read == 0, i.e. cursor_offset == size.
            return Window(records=(), next_offset=cursor_offset, eof=True, snapshot_bytes=size)
        if remainder >= WATCH_RECORD_MAX_BYTES:
            # Row 4. Once terminated this span would be at least
            # remainder + 1 bytes, so no line H2 can admit could complete it.
            # Window-independent: a bigger window cannot make it admissible.
            raise WatchError(
                "oversized_record",
                f"unterminated span of {remainder} bytes at offset {cursor_offset} "
                f"cannot be completed within {WATCH_RECORD_MAX_BYTES} bytes",
                snapshot_bytes=size,
            )
        if reached_snapshot_end:
            # Row 5. Short enough that an admissible record could still
            # complete it. Terminal for this call, not for the stream: an
            # explicit later call at the same cursor succeeds once an append
            # completes the record. No liveness signal is consulted, because
            # a killed runner leaves the row at 'running' with no writer left.
            raise WatchError(
                "unterminated_tail",
                f"{remainder} unterminated bytes at the {size}-byte snapshot end",
                snapshot_bytes=size,
            )
        # Row 6. Unreachable: not reached_snapshot_end forces
        # to_read == window_bytes >= WATCH_MIN_BYTES > WATCH_RECORD_MAX_BYTES,
        # and empty frames force remainder == to_read, contradicting
        # remainder < WATCH_RECORD_MAX_BYTES.
        raise AssertionError(
            f"unreachable window state: remainder={remainder} to_read={to_read} size={size}"
        )
    finally:
        os.close(fd)
