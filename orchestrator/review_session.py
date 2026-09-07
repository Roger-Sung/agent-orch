"""Small same-host reviewer binding, with crash-visible in-flight ownership.

No model call in registration or inspection. The explicit import receipt is
last-known evidence, not a promise the CLI can still resume. A pending call is
not replayed automatically, even after the OS releases its advisory lock.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import socket
import uuid
from contextlib import contextmanager
from pathlib import Path

from .execution import ExecutionConfigError, IDENTITY, _unique_object
from .ipc import atomic_write_text


def session_path(home: Path, series: str) -> Path:
    if not isinstance(series, str) or IDENTITY.fullmatch(series) is None:
        raise ExecutionConfigError("unsafe session series")
    return home / "review-sessions" / f"{series}.json"


@contextmanager
def locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ExecutionConfigError("review_session_busy") from exc
        yield
    finally:
        os.close(fd)


def _write(path: Path, data: dict) -> None:
    atomic_write_text(path, json.dumps(data, sort_keys=True, indent=2) + "\n")


def register(home: Path, series: str, cwd: Path, *, receipt: Path | None = None) -> dict:
    path = session_path(home, series)
    cwd = cwd.resolve(strict=True)
    if not cwd.is_dir():
        raise ExecutionConfigError("review session cwd is not a directory")
    sid = str(uuid.uuid4())
    state = "new"
    evidence = None
    if receipt is not None:
        raw = receipt.read_bytes()
        payload = json.loads(raw)
        if payload.get("type") != "result" or payload.get("is_error") is not False or payload.get("subtype") != "success":
            raise ExecutionConfigError("session import needs a successful provider receipt")
        sid = str(uuid.UUID(payload["session_id"]))
        usage = payload.get("modelUsage", {})
        if usage.get("claude-fable-5-1", {}).get("canonicalModel") != "claude-fable-5-1":
            raise ExecutionConfigError("session import model mismatch")
        evidence = {"receipt_sha256": hashlib.sha256(raw).hexdigest()}
        store = Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))) / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(cwd)) / f"{sid}.jsonl"
        if store.is_symlink() or not store.is_file():
            raise ExecutionConfigError("session import needs local metadata for this canonical cwd")
        state = "ready"
    record = {"schema_version": 1, "spec_series_id": series, "session_id": sid,
              "model": "claude-fable-5-1", "cwd": str(cwd), "uid": os.getuid(),
              "host": socket.gethostname(), "state": state, "pending": None,
              "last_receipt": evidence}
    # Registration uses a registry lock as well as the per-series lock, so
    # importing the same provider session into two series cannot race.
    with locked(path.parent / "registry"):
        if path.exists():
            raise ExecutionConfigError("review session series already registered")
        for other in path.parent.glob("*.json"):
            if json.loads(other.read_text()).get("session_id") == sid:
                raise ExecutionConfigError("provider session already belongs to another series")
        _write(path, record)
    return record


def inspect(home: Path, series: str, expected: dict | None = None, *, allow_pending: bool = False,
            allow_missing_cwd: bool = False) -> dict:
    path = session_path(home, series)
    if path.is_symlink() or not path.is_file():
        raise ExecutionConfigError("review_session_missing")
    record = json.loads(path.read_text(encoding="utf-8"))
    if record.get("schema_version") != 1 or record.get("spec_series_id") != series:
        raise ExecutionConfigError("review session binding mismatch")
    if record.get("uid") != os.getuid() or record.get("host") != socket.gethostname():
        raise ExecutionConfigError("review session belongs to another runtime owner")
    if record.get("model") != "claude-fable-5-1" or str(uuid.UUID(record["session_id"])) != record["session_id"]:
        raise ExecutionConfigError("invalid review session identity")
    cwd = Path(record["cwd"])
    if not allow_missing_cwd and (not cwd.is_dir() or str(cwd.resolve()) != record["cwd"]):
        raise ExecutionConfigError("review session canonical cwd unavailable")
    if expected is not None and binding(record) != expected:
        raise ExecutionConfigError("review session changed since task intake")
    if record.get("pending") is not None and not allow_pending:
        raise ExecutionConfigError("review_session_interrupted_unknown: reconcile the sealed run before resuming")
    if record.get("state") not in {"new", "ready"}:
        raise ExecutionConfigError("review session state is not usable")
    return record


def binding(record: dict) -> dict:
    return {k: record[k] for k in ("spec_series_id", "session_id", "model", "cwd", "uid", "host")}


def rehydrate(home: Path, series: str, *, expected_session: str, cwd: Path,
              reason: str, checkpoint: Path) -> dict:
    """Explicit operator recovery, not a retry or proof the old call failed.

    Old task snapshots keep the old binding and cannot use the replacement.
    The full predecessor record (including unknown pending work) is retained.
    The operator must stop/reconcile the old worker before this action; a live
    provider call still holds the series lock and rejects replacement.
    """
    if not reason.strip():
        raise ExecutionConfigError("rehydration requires an explicit reason")
    cwd = cwd.resolve(strict=True)
    if not cwd.is_dir() or checkpoint.stat().st_size > 1024 * 1024:
        raise ExecutionConfigError("invalid rehydration cwd/checkpoint")
    evidence = json.loads(checkpoint.read_text(), object_pairs_hook=_unique_object)
    if not isinstance(evidence, dict) or set(evidence) != {"spec_series_id", "current_spec", "decisions", "live_findings", "resolved_findings"}:
        raise ExecutionConfigError("checkpoint requires series, current_spec, decisions, live_findings, resolved_findings")
    if evidence["spec_series_id"] != series or not isinstance(evidence["current_spec"], str) or not evidence["current_spec"].strip():
        raise ExecutionConfigError("checkpoint series/spec mismatch")
    if any(not isinstance(evidence[k], list) for k in ("decisions", "live_findings", "resolved_findings")):
        raise ExecutionConfigError("checkpoint decisions/findings must be explicit lists")
    path = session_path(home, series)
    with locked(path):
        previous = inspect(home, series, allow_pending=True, allow_missing_cwd=True)
        if previous["session_id"] != expected_session:
            raise ExecutionConfigError("rehydration predecessor changed")
        record = {**previous, "session_id": str(uuid.uuid4()), "cwd": str(cwd),
                  "state": "new", "pending": None, "last_receipt": None,
                  "context_rehydrated": True, "predecessor": previous,
                  "rehydration_reason": reason, "checkpoint": evidence}
        _write(path, record)
        return record


@contextmanager
def call(home: Path, series: str, expected: dict, log_path: Path, expected_state: str | None = None):
    path = session_path(home, series)
    with locked(path):
        record = inspect(home, series, expected)
        if expected_state is not None and record["state"] != expected_state:
            raise ExecutionConfigError("review session state changed since preflight")
        record["pending"] = {"log_path": str(log_path)}
        _write(path, record)  # Before spawn; a crash remains visible.
        yield record
        # Do not clear here. Only a sealed controller result completes handoff.


def finish(home: Path, series: str, log_path: Path, manifest_path: Path, manifest_hash: str) -> None:
    path = session_path(home, series)
    with locked(path):
        record = json.loads(path.read_text())
        raw = manifest_path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != manifest_hash:
            raise ExecutionConfigError("session completion manifest hash mismatch")
        manifest = json.loads(raw)
        receipt = manifest.get("execution_receipt") or {}
        if record.get("pending") != {"log_path": str(log_path)} or manifest.get("log_path") != str(log_path):
            raise ExecutionConfigError("session completion run mismatch")
        if receipt.get("provider_session_id") != record["session_id"] or receipt.get("provider_reported_model") != record["model"]:
            raise ExecutionConfigError("resume_failed: provider session/model was not confirmed")
        record.update(pending=None, state="ready", last_receipt={"manifest_path": str(manifest_path), "manifest_hash": manifest_hash})
        _write(path, record)


def reconcile(home: Path, task_id: str) -> dict:
    """Explicit recovery, only from a DB-committed seal, never a loose file."""
    from .controller import Controller
    controller = Controller(home, read_only=True)
    try:
        status = controller.status(task_id)
        for run in reversed(status["stage_runs"]):
            if run["status"] == "running" or not run.get("manifest_path") or not run.get("manifest_hash"):
                continue
            path = Path(run["manifest_path"])
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != run["manifest_hash"]:
                raise ExecutionConfigError("reconciliation manifest was modified")
            manifest = json.loads(raw)
            receipt = manifest.get("execution_receipt") or {}
            session = receipt.get("session_binding")
            if session is None:
                continue
            if manifest.get("task_id") != task_id or manifest.get("run_token") != run["run_token"]:
                raise ExecutionConfigError("reconciliation task/run binding mismatch")
            finish(home, session["spec_series_id"], Path(run["log_path"]), path, run["manifest_hash"])
            return inspect(home, session["spec_series_id"])
        raise ExecutionConfigError("no committed review receipt; do not replay an unknown provider call")
    finally:
        controller.close()
