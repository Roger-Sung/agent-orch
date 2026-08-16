from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import shlex
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

from .containment import Sentinel, Violation, protected_roots_from_env
from .db import connect
from .profile import Profile, ProfileError, canonical_json, load_profile, profile_from_snapshot, sha256_bytes
from .runner import ProviderPreflightResult, RunResult, SubprocessRunner, classify_result


ACTIVE_STATUSES = {"queued", "running"}
RESUMABLE_STATUSES = {"waiting_user", "paused", "blocked"}


class ControllerError(RuntimeError):
    pass


class Controller:
    """Single-process, single-writer orchestrator for the frozen MVP."""

    def __init__(
        self,
        home: Path,
        runner: Any | None = None,
        read_only: bool = False,
        event_callback: Any | None = None,
        protected_roots: tuple[Path, ...] | None = None,
    ):
        self.home = home.resolve()
        # L2 watches these; empty means detection is off. The engine ships with
        # no opinion about which directories on someone else's machine matter,
        # so a deployment declares them (ORCH_PROTECTED_ROOTS) or passes them in.
        self.protected_roots = (
            tuple(protected_roots) if protected_roots is not None else protected_roots_from_env()
        )
        self.tasks_dir = self.home / "tasks"
        self.conn = connect(self.home / "orchestrator.db", read_only=read_only)
        self.runner = runner or SubprocessRunner()
        self.event_callback = event_callback
        # read_only：只查詢、不接管，跳過 orphan-block（讓 daemon 跑著時也能安全 status）。
        if read_only:
            return
        self.reconcile_startup()

    def close(self) -> None:
        self.conn.close()

    def reconcile_startup(self) -> dict[str, Any]:
        """Startup barrier: classify DB/artifact residue before intake is ready."""
        summary: dict[str, Any] = {"running_blocked": 0, "artifact_quarantined": 0}
        # With the single-daemon assumption, a running row observed at startup is
        # an interrupted prior controller. Do this before daemon intake scans.
        orphaned = list(self.conn.execute("SELECT * FROM tasks WHERE status='running' ORDER BY created_at"))
        for task in orphaned:
            self._block_orphaned_running(task)
            summary["running_blocked"] += 1
        for task in self.conn.execute("SELECT * FROM tasks ORDER BY created_at"):
            artifact_dir = Path(task["artifact_dir"])
            if not artifact_dir.exists():
                self._quarantine(task["id"], None, artifact_dir, "missing_artifact_dir")
                summary["artifact_quarantined"] += 1
                continue
            for path_label in ("profile_snapshot_path", "input_snapshot_path"):
                path = Path(task[path_label])
                if not path.is_file():
                    self._quarantine(task["id"], None, path, f"missing_{path_label}")
                    summary["artifact_quarantined"] += 1
        return summary

    def submit(
        self,
        task_type: str,
        profile_path: Path,
        input_path: Path,
        *,
        task_id: str | None = None,
        operation_id: str | None = None,
        workspace: Path | None = None,
    ) -> str:
        profile = load_profile(profile_path)
        if profile.type != task_type:
            raise ControllerError(f"--type {task_type!r} does not match profile type {profile.type!r}")
        if not input_path.is_file():
            raise ControllerError(f"input file does not exist: {input_path}")

        profile_bytes = canonical_json(profile.to_dict())
        input_bytes = input_path.read_bytes()
        profile_hash = sha256_bytes(profile_bytes)
        input_hash = hashlib.sha256(input_bytes).hexdigest()
        idempotent_request = task_id is not None
        if idempotent_request:
            existing = self.conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if existing:
                if (
                    existing["type"] != task_type
                    or existing["profile_hash"] != profile_hash
                    or existing["input_hash"] != input_hash
                ):
                    raise ControllerError(f"request task id {task_id} was already used with different input")
                return task_id
        else:
            task_id = str(uuid.uuid4())

        artifact_dir = self.tasks_dir / task_id
        if artifact_dir.exists():
            # A process may have died after creating snapshots but before the DB
            # transaction committed. A caller-supplied task id is the idempotency
            # key, so this directory cannot belong to another request.
            if not idempotent_request:
                raise ControllerError(f"generated task artifact already exists: {artifact_dir}")
            shutil.rmtree(artifact_dir)
        artifact_dir.mkdir(parents=True, exist_ok=False)
        profile_snapshot = artifact_dir / "profile.snapshot.json"
        input_snapshot = artifact_dir / ("input.snapshot" + input_path.suffix)
        self._atomic_write(profile_snapshot, profile_bytes)
        self._atomic_write(input_snapshot, input_bytes)
        self._verify_file(profile_snapshot, profile_hash, "profile snapshot")
        self._verify_file(input_snapshot, input_hash, "input snapshot")

        now = _now()
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            self.conn.execute(
                """INSERT INTO tasks(
                    id,type,status,current_stage,owner,profile_hash,input_hash,
                    profile_snapshot_path,input_snapshot_path,artifact_dir,max_transitions,
                    workspace_dir,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    task_id,
                    task_type,
                    "queued",
                    profile.initial_stage,
                    profile.stage(profile.initial_stage).owner,
                    profile_hash,
                    input_hash,
                    str(profile_snapshot),
                    str(input_snapshot),
                    str(artifact_dir),
                    profile.max_transitions,
                    str(workspace.resolve()) if workspace else None,
                    now,
                    now,
                ),
            )
            self.conn.executemany(
                "INSERT INTO edge_counts(task_id,edge,cap) VALUES(?,?,?)",
                [(task_id, edge, cap) for edge, cap in profile.edge_caps.items()],
            )
            self._insert_transition(
                task_id,
                operation_id or str(uuid.uuid4()),
                None,
                None,
                None,
                None,
                None,
                None,
                "queued",
                "submitted",
            )
            self.conn.execute("COMMIT")
        except BaseException:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            shutil.rmtree(artifact_dir, ignore_errors=True)
            raise
        return task_id

    def run_until_stop(self, task_id: str) -> dict[str, Any]:
        while True:
            task = self._task(task_id)
            if task["status"] not in ACTIVE_STATUSES:
                return self.status(task_id)
            if task["status"] == "running":
                self._block_orphaned_running(task)
                return self.status(task_id)
            profile = self._profile_for(task)
            stage = profile.stage(task["current_stage"])
            if not stage.terminal:
                preflight = self._provider_preflight(stage.owner)
                if preflight.status != "pass":
                    self._record_provider_preflight_stop(task_id, stage, preflight)
                    return self.status(task_id)
            claim = self.claim_stage(task_id)
            if claim is None:
                return self.status(task_id)
            run_token, stage, profile, log_path = claim
            self._emit_event(
                "stage_started",
                {
                    "task_id": task_id,
                    "run_token": run_token,
                    "stage": stage.name,
                    "owner": stage.owner,
                    "model": self._runner_model(stage.owner) or "unspecified",
                    "timeout": stage.timeout,
                    "log_path": str(log_path),
                },
            )
            input_text = self._read_verified_input(task_id)
            prompt = self._build_prompt(task_id, stage, input_text)
            try:
                raw_result = self._invoke_runner(task, stage, prompt, log_path)
            except BaseException as exc:
                message = f"controller observed runner interruption: {type(exc).__name__}: {exc}\n"
                log_path.parent.mkdir(parents=True, exist_ok=True)
                with log_path.open("a", encoding="utf-8") as handle:
                    handle.write(message)
                raw_result = RunResult(None, message, None, "raw", "raw")
            result = classify_result(
                raw_result.exit_code,
                raw_result.output,
                set(stage.outcomes),
                raw_result.timed_out,
                source=raw_result,
            )
            self.commit_run(task_id, run_token, result, profile)
            self._emit_event(
                "stage_finished",
                {
                    "task_id": task_id,
                    "run_token": run_token,
                    "stage": stage.name,
                    "owner": stage.owner,
                    "classification": result.classification,
                    "reason": result.reason,
                    "outcome": result.outcome,
                    "exit_code": result.exit_code,
                    "timed_out": result.timed_out,
                },
            )

    def _invoke_runner(self, task: sqlite3.Row, stage: Any, prompt: str, log_path: Path) -> RunResult:
        """有 workspace 才走 containment；沒有就維持原本的呼叫方式（既有 runner 不受影響）。

        L1 (the sandbox) lives in the runner, because it has to wrap the child
        process. L2 (detection) lives here, because it has to bracket the run
        and because the consequence of a hit — quarantine the task, do not
        advance it — is the controller's decision to make.
        """
        workspace = self._workspace_for(task)
        if workspace is None:
            return self.runner.run(stage.owner, prompt, stage.timeout, log_path)

        sentinel = self._sentinel_for(workspace)
        before = sentinel.snapshot() if sentinel is not None else None
        result = self.runner.run(stage.owner, prompt, stage.timeout, log_path, workspace=workspace)
        if sentinel is None or before is None:
            return result
        violations = sentinel.compare(before)
        if not violations:
            return result
        return self._record_workspace_escape(task, log_path, result, violations)

    def _sentinel_for(self, workspace: Path) -> Sentinel | None:
        roots = self.protected_roots
        if not roots:
            return None
        return Sentinel(roots=tuple(roots), workspace=workspace)

    def _record_workspace_escape(
        self, task: sqlite3.Row, log_path: Path, result: RunResult, violations: list[Violation]
    ) -> RunResult:
        """A stage wrote outside its workspace. Stop, quarantine, keep the evidence.

        The stage may well have "succeeded" by its own account — that is the
        dangerous case, and why this overrides the run's own classification
        instead of being folded into it.
        """
        payload = {
            "task_id": task["id"],
            "workspace_dir": task["workspace_dir"],
            "detected_at": _iso_now(),
            "violations": [item.as_dict() for item in violations],
        }
        evidence_path = log_path.parent / "containment-violations.json"
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_write(
            evidence_path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        )
        self._quarantine(task["id"], None, evidence_path, "workspace_escape")
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(
                f"containment_violation_count={len(violations)}\n"
                f"containment_evidence={evidence_path}\n"
            )
        return replace(
            result,
            containment_stop="workspace_escape",
            containment_violations=tuple(item.as_dict() for item in violations),
        )

    def _workspace_for(self, task: sqlite3.Row) -> Path | None:
        try:
            raw = task["workspace_dir"]
        except (IndexError, KeyError):
            return None
        if not raw:
            return None
        workspace = Path(raw)
        if not workspace.is_dir():
            raise ControllerError(f"task workspace no longer exists: {workspace}")
        return workspace

    def claim_stage(self, task_id: str):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            task = self._task(task_id)
            if task["status"] != "queued":
                raise ControllerError(f"task {task_id} is not queued")
            profile = self._profile_for(task)
            stage = profile.stage(task["current_stage"])
            if stage.terminal:
                raise ControllerError(f"queued task points at terminal stage {stage.name}")
            allowance = task["resume_allowance"]
            if task["transitions_count"] >= task["max_transitions"] and not allowance:
                self._stop_for_cap(task, "transition_cap", f"max_transitions={task['max_transitions']} reached")
                self.conn.execute("COMMIT")
                return None

            cycle, attempt = self._next_attempt(task_id, stage.name)
            if attempt > stage.attempt_cap and not allowance:
                self._stop_for_cap(task, "attempt_cap", f"{stage.name}.attempt_cap={stage.attempt_cap} reached")
                self.conn.execute("COMMIT")
                return None

            run_token = str(uuid.uuid4())
            lease_token = str(uuid.uuid4())
            log_path = Path(task["artifact_dir"]) / "runs" / f"{self._next_seq(task_id):04d}-{stage.name}-{run_token}.log"
            now = _now()
            self._write_stage_start_log(
                log_path=log_path,
                task_id=task_id,
                run_token=run_token,
                stage=stage.name,
                owner=stage.owner,
                cycle=cycle,
                attempt=attempt,
                timeout=stage.timeout,
                started_at_ms=now,
            )
            self.conn.execute(
                """INSERT INTO stage_runs(
                    run_token,task_id,stage,cycle,attempt,owner,status,lease_token,log_path,model,
                    provider_preflight_status,provider_preflight_reason,started_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_token,
                    task_id,
                    stage.name,
                    cycle,
                    attempt,
                    stage.owner,
                    "running",
                    lease_token,
                    str(log_path),
                    self._runner_model(stage.owner),
                    "pass",
                    "provider_preflight_pass",
                    now,
                ),
            )
            updated = self.conn.execute(
                """UPDATE tasks SET status='running',owner=?,resume_allowance=0,lease_token=?,
                    revision=revision+1,updated_at=? WHERE id=? AND revision=?""",
                (stage.owner, lease_token, now, task_id, task["revision"]),
            )
            if updated.rowcount != 1:
                raise ControllerError("claim_stage CAS conflict")
            self.conn.execute("COMMIT")
            return run_token, stage, profile, log_path
        except BaseException:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise

    def commit_run(self, task_id: str, run_token: str, result: RunResult, profile: Profile) -> None:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            task = self._task(task_id)
            run = self.conn.execute("SELECT * FROM stage_runs WHERE run_token=?", (run_token,)).fetchone()
            if not run or run["task_id"] != task_id or run["status"] != "running" or task["status"] != "running":
                raise ControllerError("run/task state conflict")
            if run["lease_token"] != task["lease_token"]:
                raise ControllerError("run/task lease conflict")
            stage = profile.stage(run["stage"])
            now = _now()
            duration_ms = self._duration_ms(run, result, now)
            manifest_path, manifest_hash = self._seal_run_manifest(task, run, result, stage.name, now)
            if result.classification != "success":
                status = result.classification
                self.conn.execute(
                    """UPDATE stage_runs SET status=?,exit_code=?,outcome=?,ended_at=?,
                       duration_ms=?,model=?,usage_input_tokens=?,usage_output_tokens=?,
                       usage_total_tokens=?,usage_unavailable_reason=?,manifest_path=?,manifest_hash=?,sealed=1
                       WHERE run_token=?""",
                    (
                        status,
                        result.exit_code,
                        result.outcome,
                        now,
                        duration_ms,
                        result.model or run["model"] or "unspecified",
                        result.usage_input_tokens,
                        result.usage_output_tokens,
                        result.usage_total_tokens,
                        self._usage_unavailable_reason(result),
                        str(manifest_path),
                        manifest_hash,
                        run_token,
                    ),
                )
                seq = self._insert_transition(
                    task_id,
                    str(uuid.uuid4()),
                    run_token,
                    stage.name,
                    stage.owner,
                    None,
                    result.outcome,
                    "running",
                    status,
                    result.reason,
                )
                updated = self.conn.execute(
                    """UPDATE tasks SET status=?,stop_reason=?,lease_token=NULL,
                       revision=revision+1,updated_at=? WHERE id=? AND revision=? AND lease_token=?""",
                    (status, result.reason, now, task_id, task["revision"], run["lease_token"]),
                )
                if updated.rowcount != 1:
                    raise ControllerError("commit_run CAS conflict")
                self._notify(task_id, seq, result.reason, f"task {task_id} {status}: {result.reason}")
                self._write_evidence_index(task_id)
                self.conn.execute("COMMIT")
                return

            outcome = result.outcome
            assert outcome is not None
            target_name = stage.outcomes[outcome]
            target = profile.stage(target_name)
            edge = profile.edge_id(stage.name, outcome)
            edge_row = self.conn.execute(
                "SELECT count,cap FROM edge_counts WHERE task_id=? AND edge=?", (task_id, edge)
            ).fetchone()
            if not edge_row:
                raise ControllerError(f"missing seeded edge counter: {edge}")
            edge_cap_hit = edge_row["count"] >= edge_row["cap"]
            next_status = target.terminal or "queued"
            stop_reason = None
            if edge_cap_hit:
                next_status = "waiting_user"
                stop_reason = "edge_cap"
            next_owner = None if target.terminal else target.owner
            if not edge_cap_hit:
                self.conn.execute(
                    "UPDATE edge_counts SET count=count+1 WHERE task_id=? AND edge=?",
                    (task_id, edge),
                )
            self.conn.execute(
                """UPDATE stage_runs SET status='committed',exit_code=?,outcome=?,ended_at=?,
                   duration_ms=?,model=?,usage_input_tokens=?,usage_output_tokens=?,
                   usage_total_tokens=?,usage_unavailable_reason=?,manifest_path=?,manifest_hash=?,sealed=1
                   WHERE run_token=?""",
                (
                    result.exit_code,
                    outcome,
                    now,
                    duration_ms,
                    result.model or run["model"] or "unspecified",
                    result.usage_input_tokens,
                    result.usage_output_tokens,
                    result.usage_total_tokens,
                    self._usage_unavailable_reason(result),
                    str(manifest_path),
                    manifest_hash,
                    run_token,
                ),
            )
            seq = self._insert_transition(
                task_id,
                str(uuid.uuid4()),
                run_token,
                stage.name,
                stage.owner,
                edge,
                outcome,
                "running",
                next_status,
                stop_reason or "stage_completed",
            )
            updated = self.conn.execute(
                """UPDATE tasks SET status=?,stop_reason=?,current_stage=?,owner=?,
                    lease_token=NULL,
                    transitions_count=transitions_count+1,
                    revision=revision+1,updated_at=? WHERE id=? AND revision=? AND lease_token=?""",
                (next_status, stop_reason, target_name, next_owner, now, task_id, task["revision"], run["lease_token"]),
            )
            if updated.rowcount != 1:
                raise ControllerError("commit_run CAS conflict")
            if edge_cap_hit:
                self._notify(
                    task_id,
                    seq,
                    "edge_cap",
                    f"task {task_id} waiting_user: {edge} cap={edge_row['cap']} exceeded",
                )
                self._write_evidence_index(task_id)
            elif next_status in {"done", "failed"}:
                self._notify(task_id, seq, next_status, f"task {task_id} {next_status}: terminal stage {target_name}")
                self._write_evidence_index(task_id)
            self.conn.execute("COMMIT")
        except BaseException:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise

    def resume(self, task_id: str, *, operation_id: str | None = None) -> dict[str, Any]:
        if operation_id:
            existing = self.conn.execute(
                "SELECT task_id FROM transitions WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if existing:
                if existing["task_id"] != task_id:
                    raise ControllerError(f"resume operation {operation_id} belongs to another task")
                return self.run_until_stop(task_id)
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            task = self._task(task_id)
            if task["status"] not in RESUMABLE_STATUSES:
                raise ControllerError(
                    f"task {task_id} cannot resume from {task['status']}; "
                    "expected waiting_user, paused, or blocked"
                )
            allowance = 1 if task["status"] == "waiting_user" else 0
            now = _now()
            resumed_edge = None
            resumed_outcome = None
            if task["status"] == "waiting_user" and task["stop_reason"] == "edge_cap":
                capped = self.conn.execute(
                    """SELECT edge,outcome FROM transitions
                       WHERE task_id=? AND reason='edge_cap' ORDER BY seq DESC LIMIT 1""",
                    (task_id,),
                ).fetchone()
                if not capped or not capped["edge"]:
                    raise ControllerError("edge_cap task is missing its capped transition")
                resumed_edge = capped["edge"]
                resumed_outcome = capped["outcome"]
                self.conn.execute(
                    "UPDATE edge_counts SET count=count+1 WHERE task_id=? AND edge=?",
                    (task_id, resumed_edge),
                )
            self.conn.execute(
                "UPDATE tasks SET status='queued',stop_reason=NULL,lease_token=NULL,resume_allowance=?,revision=revision+1,updated_at=? WHERE id=?",
                (allowance, now, task_id),
            )
            self._insert_transition(
                task_id,
                operation_id or str(uuid.uuid4()),
                None,
                task["current_stage"],
                task["owner"],
                resumed_edge,
                resumed_outcome,
                task["status"],
                "queued",
                "manual_resume",
            )
            self.conn.execute("COMMIT")
        except BaseException:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise
        return self.run_until_stop(task_id)

    def status(self, task_id: str) -> dict[str, Any]:
        task = self._task(task_id)
        edges = [dict(row) for row in self.conn.execute(
            "SELECT edge,count,cap FROM edge_counts WHERE task_id=? ORDER BY edge", (task_id,)
        )]
        runs = [dict(row) for row in self.conn.execute(
            f"""SELECT {self._stage_run_select_columns()}
                FROM stage_runs WHERE task_id=? ORDER BY started_at,rowid""",
            (task_id,),
        )]
        self._annotate_running_runs(task, runs)
        transitions = [dict(row) for row in self.conn.execute(
            """SELECT seq,run_token,stage,owner,edge,outcome,from_status,to_status,reason,at
               FROM transitions WHERE task_id=? ORDER BY seq""",
            (task_id,),
        )]
        notifications = [dict(row) for row in self.conn.execute(
            "SELECT transition_seq,reason,message,created_at FROM notifications WHERE task_id=? ORDER BY transition_seq",
            (task_id,),
        )]
        return {
            "task": dict(task),
            "edge_counts": edges,
            "stage_runs": runs,
            "transitions": transitions,
            "notifications": notifications,
        }

    def _task(self, task_id: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            raise ControllerError(f"task not found: {task_id}")
        return row

    def _profile_for(self, task: sqlite3.Row) -> Profile:
        return profile_from_snapshot(Path(task["profile_snapshot_path"]), task["profile_hash"])

    def _read_verified_input(self, task_id: str) -> str:
        task = self._task(task_id)
        path = Path(task["input_snapshot_path"])
        self._verify_file(path, task["input_hash"], "input snapshot")
        return path.read_text(encoding="utf-8", errors="replace")

    def _next_attempt(self, task_id: str, stage: str) -> tuple[int, int]:
        row = self.conn.execute(
            "SELECT cycle,attempt,status FROM stage_runs WHERE task_id=? AND stage=? ORDER BY started_at DESC,run_token DESC LIMIT 1",
            (task_id, stage),
        ).fetchone()
        if not row:
            return 1, 1
        if row["status"] == "paused":
            return row["cycle"], row["attempt"] + 1
        return row["cycle"] + 1, 1

    def _stop_for_cap(self, task: sqlite3.Row, reason: str, detail: str) -> None:
        seq = self._insert_transition(
            task["id"],
            str(uuid.uuid4()),
            None,
            task["current_stage"],
            task["owner"],
            None,
            None,
            "queued",
            "waiting_user",
            reason,
        )
        now = _now()
        self.conn.execute(
            "UPDATE tasks SET status='waiting_user',stop_reason=?,lease_token=NULL,revision=revision+1,updated_at=? WHERE id=?",
            (reason, now, task["id"]),
        )
        self._notify(task["id"], seq, reason, f"task {task['id']} waiting_user: {detail}")
        self._write_evidence_index(task["id"])

    def _block_orphaned_running(self, task: sqlite3.Row) -> None:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            run = self.conn.execute(
                "SELECT * FROM stage_runs WHERE task_id=? AND status='running'", (task["id"],)
            ).fetchone()
            reason = "orphaned_running"
            now = _now()
            if run:
                path = Path(run["log_path"])
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as handle:
                    handle.write("\ncontroller detected an unknown interruption while task was running\n")
                self.conn.execute(
                    "UPDATE stage_runs SET status='blocked',ended_at=? WHERE run_token=?", (now, run["run_token"])
                )
            seq = self._insert_transition(
                task["id"], str(uuid.uuid4()), run["run_token"] if run else None,
                task["current_stage"], task["owner"], None, None, "running", "blocked", reason,
            )
            self.conn.execute(
                "UPDATE tasks SET status='blocked',stop_reason=?,lease_token=NULL,revision=revision+1,updated_at=? WHERE id=?",
                (reason, now, task["id"]),
            )
            self._notify(task["id"], seq, reason, f"task {task['id']} blocked: unknown runner interruption")
            self._write_evidence_index(task["id"])
            self.conn.execute("COMMIT")
        except BaseException:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise

    def _insert_transition(
        self,
        task_id: str,
        operation_id: str,
        run_token: str | None,
        stage: str | None,
        owner: str | None,
        edge: str | None,
        outcome: str | None,
        from_status: str | None,
        to_status: str,
        reason: str,
    ) -> int:
        seq = self._next_seq(task_id)
        self.conn.execute(
            """INSERT INTO transitions(
                task_id,seq,operation_id,run_token,stage,owner,edge,outcome,from_status,to_status,reason,at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (task_id, seq, operation_id, run_token, stage, owner, edge, outcome, from_status, to_status, reason, _now()),
        )
        return seq

    def _next_seq(self, task_id: str) -> int:
        return self.conn.execute(
            "SELECT COALESCE(MAX(seq),0)+1 FROM transitions WHERE task_id=?", (task_id,)
        ).fetchone()[0]

    def _notify(self, task_id: str, seq: int, reason: str, message: str) -> None:
        self.conn.execute(
            "INSERT INTO notifications(task_id,transition_seq,reason,message,created_at) VALUES(?,?,?,?,?)",
            (task_id, seq, reason, message, _now()),
        )
        artifact_dir = Path(self._task(task_id)["artifact_dir"])
        with (artifact_dir / "notifications.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"transition_seq": seq, "reason": reason, "message": message}, ensure_ascii=False) + "\n")
        with (self.home / "inbox.md").open("a", encoding="utf-8") as handle:
            handle.write(
                f"- {_iso_now()} type={reason} sev={_notification_severity(reason)} "
                f"ref={artifact_dir}: {message}\n"
            )

    def _write_evidence_index(self, task_id: str) -> None:
        task = self._task(task_id)
        artifact_dir = Path(task["artifact_dir"])
        transitions = [dict(row) for row in self.conn.execute(
            """SELECT seq,run_token,stage,owner,edge,outcome,from_status,to_status,reason,at
               FROM transitions WHERE task_id=? ORDER BY seq""",
            (task_id,),
        )]
        transition_by_run = {
            transition["run_token"]: transition
            for transition in transitions
            if transition.get("run_token")
        }
        runs = [dict(row) for row in self.conn.execute(
            f"""SELECT {self._stage_run_select_columns()}
                FROM stage_runs WHERE task_id=? ORDER BY started_at,rowid""",
            (task_id,),
        )]
        notifications = [dict(row) for row in self.conn.execute(
            "SELECT transition_seq,reason,message,created_at FROM notifications WHERE task_id=? ORDER BY transition_seq",
            (task_id,),
        )]
        stages = []
        for run in runs:
            transition = transition_by_run.get(run["run_token"], {})
            stages.append(
                {
                    "seq": transition.get("seq"),
                    "run_token": run["run_token"],
                    "stage": run["stage"],
                    "owner": run["owner"],
                    "model": run.get("model") or "unspecified",
                    "duration_ms": run.get("duration_ms"),
                    "preflight_status": run.get("provider_preflight_status"),
                    "preflight_reason": run.get("provider_preflight_reason"),
                    "status": run["status"],
                    "exit_code": run.get("exit_code"),
                    "outcome": run.get("outcome"),
                    "usage": {
                        "input_tokens": run.get("usage_input_tokens"),
                        "output_tokens": run.get("usage_output_tokens"),
                        "total_tokens": run.get("usage_total_tokens"),
                        "unavailable_reason": run.get("usage_unavailable_reason"),
                    },
                    "lease_token": run.get("lease_token"),
                    "sealed": bool(run.get("sealed")),
                    "manifest_path": run.get("manifest_path"),
                    "manifest_hash": run.get("manifest_hash"),
                    "log_path": run["log_path"],
                    "started_at": run["started_at"],
                    "ended_at": run.get("ended_at"),
                }
            )
        payload = {
            "schema_version": 1,
            "task_id": task_id,
            "status": task["status"],
            "stop_reason": task["stop_reason"],
            "profile_hash": task["profile_hash"],
            "input_hash": task["input_hash"],
            "profile_snapshot_path": task["profile_snapshot_path"],
            "input_snapshot_path": task["input_snapshot_path"],
            "artifact_dir": task["artifact_dir"],
            "created_at": task["created_at"],
            "ended_at": task["updated_at"],
            "stages": stages,
            "transitions": transitions,
            "notifications": notifications,
        }
        self._atomic_write(
            artifact_dir / "evidence.json",
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8"),
        )

    def _seal_run_manifest(
        self,
        task: sqlite3.Row,
        run: sqlite3.Row,
        result: RunResult,
        stage_name: str,
        ended_at_ms: int,
    ) -> tuple[Path, str]:
        log_path = Path(run["log_path"])
        artifact_dir = Path(task["artifact_dir"]).resolve()
        try:
            resolved_log = log_path.resolve()
        except OSError as exc:
            raise ControllerError(f"cannot resolve run log for sealing: {exc}") from exc
        if not resolved_log.is_relative_to(artifact_dir):
            raise ControllerError(f"run log outside artifact dir: {log_path}")
        if not log_path.is_file():
            raise ControllerError(f"run log missing for sealed manifest: {log_path}")
        log_hash = hashlib.sha256(log_path.read_bytes()).hexdigest()
        output_hash = hashlib.sha256(result.output.encode("utf-8", errors="replace")).hexdigest()
        payload = {
            "schema_version": 1,
            "task_id": task["id"],
            "run_token": run["run_token"],
            "lease_token": run["lease_token"],
            "stage": stage_name,
            "owner": run["owner"],
            "classification": result.classification,
            "reason": result.reason,
            "outcome": result.outcome,
            "exit_code": result.exit_code,
            "timed_out": result.timed_out,
            "log_path": str(log_path),
            "log_hash": log_hash,
            "output_hash": output_hash,
            "started_at": run["started_at"],
            "ended_at": ended_at_ms,
        }
        manifest_path = log_path.with_suffix(log_path.suffix + ".manifest.json")
        encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        self._atomic_write(manifest_path, encoded)
        manifest_hash = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        return manifest_path, manifest_hash

    def _quarantine(
        self,
        task_id: str | None,
        request_path: Path | None,
        artifact_path: Path | None,
        reason: str,
    ) -> Path:
        now = _now()
        quarantine_id = str(uuid.uuid4())
        destination_dir = self.home / "quarantine"
        destination_dir.mkdir(parents=True, exist_ok=True)
        moved_path: Path | None = None
        if request_path is not None and request_path.exists():
            moved_path = destination_dir / f"{now}-{quarantine_id}-{request_path.name}"
            os.replace(request_path, moved_path)
        self.conn.execute(
            """INSERT INTO quarantine(id,task_id,request_path,artifact_path,reason,created_at)
               VALUES(?,?,?,?,?,?)""",
            (
                quarantine_id,
                task_id,
                str(moved_path or request_path) if request_path is not None else None,
                str(artifact_path) if artifact_path is not None else None,
                reason,
                now,
            ),
        )
        notice = {
            "id": quarantine_id,
            "task_id": task_id,
            "request_path": str(moved_path or request_path) if request_path is not None else None,
            "artifact_path": str(artifact_path) if artifact_path is not None else None,
            "reason": reason,
            "created_at": now,
        }
        self._atomic_write(
            destination_dir / f"{now}-{quarantine_id}.json",
            json.dumps(notice, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8"),
        )
        with (self.home / "inbox.md").open("a", encoding="utf-8") as handle:
            handle.write(
                f"- {_iso_now()} type=quarantine sev=error ref={notice.get('request_path') or notice.get('artifact_path')}: "
                f"{reason}\n"
            )
        return moved_path or artifact_path or destination_dir

    @staticmethod
    def _build_prompt(task_id: str, stage: Any, input_text: str) -> str:
        outcomes = ", ".join(stage.outcomes)
        return (
            f"You are executing agent-orch task {task_id}, stage {stage.name}.\n"
            f"Stage instructions: {stage.prompt}\n\n"
            f"Task input:\n---\n{input_text}\n---\n\n"
            f"Allowed typed outcomes: {outcomes}.\n"
            "Complete this stage. As the VERY LAST line of your output, print the outcome once:\n"
            "ORCHESTRATOR_OUTCOME: <typed outcome>\n"
            "Do not print this line more than once and do not write it into any file.\n"
        )

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        temp = path.with_name(path.name + f".tmp-{uuid.uuid4()}")
        with temp.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    @staticmethod
    def _verify_file(path: Path, expected_hash: str, label: str) -> None:
        try:
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            raise ControllerError(f"cannot verify {label}: {exc}") from exc
        if actual != expected_hash:
            raise ControllerError(f"{label} hash mismatch: expected {expected_hash}, got {actual}")

    def _provider_preflight(self, owner: str | None) -> ProviderPreflightResult:
        if owner is None:
            now = _now()
            return ProviderPreflightResult("pass", "terminal_stage", "", None, [], None, now, now)
        preflight = getattr(self.runner, "preflight", None)
        if preflight is None:
            now = _now()
            return ProviderPreflightResult("pass", "runner_preflight_not_supported", "", None, [], None, now, now)
        return preflight(owner)

    def _record_provider_preflight_stop(self, task_id: str, stage: Any, preflight: ProviderPreflightResult) -> None:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            task = self._task(task_id)
            if task["status"] != "queued":
                self.conn.execute("ROLLBACK")
                return
            run_token = str(uuid.uuid4())
            cycle, attempt = self._next_attempt(task_id, stage.name)
            log_path = Path(task["artifact_dir"]) / "runs" / f"{self._next_seq(task_id):04d}-{stage.name}-{run_token}.preflight.log"
            self._write_provider_preflight_log(log_path, stage.owner, preflight)
            status = "paused" if preflight.status == "paused" else "blocked"
            self.conn.execute(
                """INSERT INTO stage_runs(
                    run_token,task_id,stage,cycle,attempt,owner,status,exit_code,log_path,model,
                    duration_ms,usage_unavailable_reason,provider_preflight_status,
                    provider_preflight_reason,started_at,ended_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_token,
                    task_id,
                    stage.name,
                    cycle,
                    attempt,
                    stage.owner,
                    status,
                    preflight.exit_code,
                    str(log_path),
                    preflight.model or self._runner_model(stage.owner) or "unspecified",
                    max(0, preflight.ended_at_ms - preflight.started_at_ms),
                    "not_applicable_provider_preflight_failed",
                    preflight.status,
                    preflight.reason,
                    preflight.started_at_ms,
                    preflight.ended_at_ms,
                ),
            )
            seq = self._insert_transition(
                task_id,
                str(uuid.uuid4()),
                run_token,
                stage.name,
                stage.owner,
                None,
                None,
                "queued",
                status,
                preflight.reason,
            )
            now = _now()
            self.conn.execute(
                "UPDATE tasks SET status=?,stop_reason=?,revision=revision+1,updated_at=? WHERE id=?",
                (status, preflight.reason, now, task_id),
            )
            self._notify(task_id, seq, preflight.reason, f"task {task_id} {status}: provider preflight {preflight.reason}")
            self._write_evidence_index(task_id)
            self.conn.execute("COMMIT")
        except BaseException:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise

    @staticmethod
    def _write_provider_preflight_log(path: Path, owner: str, preflight: ProviderPreflightResult) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        header = [
            f"started_at_ms={preflight.started_at_ms}",
            f"ended_at_ms={preflight.ended_at_ms}",
            f"duration_ms={max(0, preflight.ended_at_ms - preflight.started_at_ms)}",
            f"owner={owner}",
            f"model={preflight.model or 'unspecified'}",
            f"command={' '.join(preflight.command) if preflight.command else 'unavailable'}",
            f"exit_code={preflight.exit_code}",
            f"timed_out={str(preflight.timed_out).lower()}",
            f"provider_preflight_status={preflight.status}",
            f"provider_preflight_reason={preflight.reason}",
            "usage_unavailable_reason=not_applicable_provider_preflight_failed",
        ]
        path.write_text("\n".join(header) + "\n\n--- output ---\n" + preflight.output, encoding="utf-8")

    def _runner_model(self, owner: str) -> str | None:
        command = getattr(self.runner, "_command", None)
        model_from_command = getattr(self.runner, "_model_from_command", None)
        if command is None or model_from_command is None:
            return None
        try:
            return model_from_command(command(owner)) or "unspecified"
        except (OSError, ValueError):
            return None

    def _runner_command_preview(self, owner: str) -> str:
        command = getattr(self.runner, "_command", None)
        if command is None:
            return "unavailable"
        try:
            return shlex.join(command(owner))
        except (OSError, ValueError):
            return "unavailable"

    def _write_stage_start_log(
        self,
        *,
        log_path: Path,
        task_id: str,
        run_token: str,
        stage: str,
        owner: str,
        cycle: int,
        attempt: int,
        timeout: int,
        started_at_ms: int,
    ) -> None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            "stage_status=claimed",
            f"task_id={task_id}",
            f"run_token={run_token}",
            f"stage={stage}",
            f"owner={owner}",
            f"model={self._runner_model(owner) or 'unspecified'}",
            f"cycle={cycle}",
            f"attempt={attempt}",
            f"timeout_seconds={timeout}",
            f"started_at_ms={started_at_ms}",
            f"command={self._runner_command_preview(owner)}",
            "",
            "--- live status ---",
            "provider process not spawned yet",
        ]
        log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _annotate_running_runs(self, task: sqlite3.Row, runs: list[dict[str, Any]]) -> None:
        now = _now()
        profile: Profile | None = None
        for run in runs:
            if run.get("status") != "running":
                continue
            if profile is None:
                try:
                    profile = self._profile_for(task)
                except (ControllerError, ProfileError, OSError, ValueError):
                    profile = None
            timeout_seconds = None
            if profile is not None:
                try:
                    timeout_seconds = profile.stage(run["stage"]).timeout
                except (KeyError, ValueError):
                    timeout_seconds = None
            elapsed_ms = max(0, now - int(run["started_at"]))
            run["running_elapsed_ms"] = elapsed_ms
            run["running_elapsed_seconds"] = round(elapsed_ms / 1000, 3)
            if timeout_seconds is not None:
                timeout_ms = timeout_seconds * 1000
                run["timeout_ms"] = timeout_ms
                run["timeout_seconds"] = timeout_seconds
                run["timeout_remaining_ms"] = max(0, timeout_ms - elapsed_ms)

    def _emit_event(self, event: str, payload: dict[str, Any]) -> None:
        if self.event_callback is None:
            return
        try:
            self.event_callback(event, payload)
        except Exception:
            pass

    @staticmethod
    def _duration_ms(run: sqlite3.Row, result: RunResult, now: int) -> int:
        if result.duration_ms is not None:
            return result.duration_ms
        return max(0, now - int(run["started_at"]))

    @staticmethod
    def _usage_unavailable_reason(result: RunResult) -> str | None:
        if any(
            value is not None
            for value in (result.usage_input_tokens, result.usage_output_tokens, result.usage_total_tokens)
        ):
            return None
        return result.usage_unavailable_reason or "runner_usage_unavailable"

    def _stage_run_select_columns(self) -> str:
        existing = {row["name"] for row in self.conn.execute("PRAGMA table_info(stage_runs)")}
        columns = [
            "run_token",
            "lease_token",
            "stage",
            "cycle",
            "attempt",
            "owner",
            "status",
            "exit_code",
            "outcome",
            "log_path",
            "manifest_path",
            "manifest_hash",
            "sealed",
            "model",
            "duration_ms",
            "usage_input_tokens",
            "usage_output_tokens",
            "usage_total_tokens",
            "usage_unavailable_reason",
            "provider_preflight_status",
            "provider_preflight_reason",
            "started_at",
            "ended_at",
        ]
        return ",".join(column for column in columns if column in existing)


def _now() -> int:
    return time.time_ns() // 1_000_000


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _notification_severity(reason: str) -> str:
    if reason in {"done"}:
        return "info"
    if reason in {"edge_cap", "transition_cap", "attempt_cap", "rate_limited"}:
        return "warn"
    return "error"
