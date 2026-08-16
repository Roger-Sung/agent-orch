from __future__ import annotations

import fcntl
import json
import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, TextIO


class IPCError(RuntimeError):
    pass


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        with temporary.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    atomic_write_bytes(path, json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))


def atomic_write_text(path: Path, value: str) -> None:
    atomic_write_bytes(path, value.encode("utf-8"))


def enqueue_request(home: Path, request: dict[str, Any]) -> Path:
    request_id = request.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        raise IPCError("request_id is required")
    try:
        uuid.UUID(request_id)
    except ValueError as exc:
        raise IPCError(f"invalid request_id: {request_id!r}") from exc

    timestamp = time.strftime("%Y%m%d-%H%M%S")
    path = home / "inbox" / f"{timestamp}-{request_id}.json"
    atomic_write_json(path, request)
    return path


def daemon_is_running(home: Path) -> bool:
    home.mkdir(parents=True, exist_ok=True)
    lock_path = home / "service.lock"
    with lock_path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False


@contextmanager
def hold_daemon_lock(home: Path) -> Iterator[None]:
    home.mkdir(parents=True, exist_ok=True)
    lock_path = home / "service.lock"
    handle: TextIO = lock_path.open("a+", encoding="utf-8")
    acquired = False
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise IPCError(f"orchestrator daemon already holds {lock_path}") from exc
        acquired = True
        yield
    finally:
        try:
            if acquired:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def wait_for_result(
    home: Path,
    request_path: Path,
    timeout: float,
    poll_interval: float = 0.1,
) -> dict[str, Any]:
    if timeout <= 0:
        raise IPCError("wait timeout must be positive")
    deadline = time.monotonic() + timeout
    pattern = f"{request_path.stem}.*.result.json"
    processed = home / "processed"
    while True:
        matches = sorted(processed.glob(pattern))
        if matches:
            try:
                return json.loads(matches[0].read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise IPCError(f"cannot read daemon result {matches[0]}: {exc}") from exc
        if not daemon_is_running(home):
            raise IPCError(
                f"orchestrator daemon stopped before completing request {request_path.name}; "
                "the queued request remains available for recovery"
            )
        if time.monotonic() >= deadline:
            raise IPCError(
                f"timed out after {timeout:g}s waiting for daemon request {request_path.name}; "
                "use status/processed logs to inspect it"
            )
        time.sleep(poll_interval)
