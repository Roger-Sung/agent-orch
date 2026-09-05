from __future__ import annotations

import hashlib
import inspect
import json
import os
import shutil
import sqlite3
import sys
import shlex
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

from .containment import (
    DEFAULT_SENTINEL_EXCLUDES,
    Sentinel,
    Violation,
    protected_roots_from_env,
    sentinel_excludes_from_env,
)
from .db import connect
from .profile import Profile, ProfileError, canonical_json, load_profile, profile_from_snapshot, sha256_bytes
from .runner import (
    CONVERGENCE_BEGIN,
    CONVERGENCE_END,
    HOLD_OUTCOME,
    HOLD_STOP_REASON,
    ConvergenceError,
    EnvelopeError,
    ProviderPreflightResult,
    RunResult,
    SubprocessRunner,
    allowed_outcomes,
    classify_result,
    envelope_block_text,
    extract_convergence,
    extract_envelope,
    validate_convergence,
)
from .retained import inspect_retained


ACTIVE_STATUSES = {"queued", "running"}
RESUMABLE_STATUSES = {"waiting_user", "paused", "blocked"}


def _protected_roots_support(run: Any) -> str:
    """How a runner's run() can receive protected_roots: explicit, var_keyword, or none.

    Checked by signature rather than by catching TypeError: catching would also
    swallow a genuine TypeError raised *inside* the runner, turning a real bug
    into a silent fallback. The three answers get different treatment because
    they carry different guarantees — see the caller.
    """
    return _keyword_support(run, "protected_roots")


def _keyword_support(run: Any, name: str) -> str:
    try:
        parameters = inspect.signature(run).parameters
    except (TypeError, ValueError):  # builtins and C callables
        return "none"
    candidate = parameters.get(name)
    if candidate is not None:
        # A positional-only parameter carries the right name but cannot be
        # passed by keyword; treating it as support would raise TypeError at the
        # call site, which is the very failure this check exists to avoid.
        if candidate.kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        ):
            return "explicit"
        return "none"
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()):
        return "var_keyword"
    return "none"


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
        self._warned: set[str] = set()
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
        # read_only: query without taking over, skipping the orphan block, so
        # status is safe to run while the daemon owns the database.
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
            try:
                envelope = extract_envelope(input_text)
            except EnvelopeError as exc:
                # Framing is fail-closed on every read, not only at intake: the
                # stage is already claimed, so record the stop as a blocked run
                # rather than invoking a provider against an input whose
                # authoritative region cannot be identified.
                self._stop_claimed_run(
                    task_id, run_token, profile, stage, log_path,
                    "envelope_invalid", f"controller rejected envelope framing: {exc}",
                )
                return self.status(task_id)
            reports_dir, reports_location = self._reports_target_for(task)
            if reports_dir is not None:
                try:
                    reports_dir.mkdir(parents=True, exist_ok=True)
                except OSError:
                    # Must not escape: the stage is already claimed, and an
                    # exception here would leave it recorded as running until
                    # orphan reconciliation. The runner re-attempts creation
                    # for contained runs and classifies the failure properly;
                    # elsewhere the provider CLI can create the directory or
                    # fail visibly in its own output.
                    pass
            convergence = (
                self._convergence_context(task_id, stage, profile) if envelope is not None else None
            )
            prompt = self._build_prompt(
                task_id, stage, input_text, reports_location, envelope, convergence
            )
            try:
                raw_result = self._invoke_runner(task, stage, prompt, log_path, reports_dir)
            except BaseException as exc:
                message = f"controller observed runner interruption: {type(exc).__name__}: {exc}\n"
                log_path.parent.mkdir(parents=True, exist_ok=True)
                with log_path.open("a", encoding="utf-8") as handle:
                    handle.write(message)
                raw_result = RunResult(None, message, None, "raw", "raw")
            result = classify_result(
                raw_result.exit_code,
                raw_result.output,
                set(allowed_outcomes(stage.outcomes, envelope is not None)),
                raw_result.timed_out,
                source=raw_result,
            )
            if convergence is not None:
                result = self._apply_convergence(result, convergence)
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

    def _stop_claimed_run(
        self,
        task_id: str,
        run_token: str,
        profile: Profile,
        stage: Any,
        log_path: Path,
        reason: str,
        message: str,
    ) -> None:
        """Close an already-claimed stage as blocked without invoking a provider.

        A claimed stage cannot simply be abandoned — it would stay `running`
        until orphan reconciliation — so the stop travels the ordinary
        commit_run failure path, which seals a manifest and needs the log to
        exist.
        """
        text = message + "\n"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(text)
        self.commit_run(task_id, run_token, RunResult(None, text, None, "blocked", reason), profile)
        self._emit_event(
            "stage_finished",
            {
                "task_id": task_id,
                "run_token": run_token,
                "stage": stage.name,
                "owner": stage.owner,
                "classification": "blocked",
                "reason": reason,
                "outcome": None,
                "exit_code": None,
                "timed_out": False,
            },
        )

    def _reports_target_for(self, task: sqlite3.Row) -> tuple[Path | None, str]:
        """(path the runner should allowlist or None, location line for the prompt).

        Reports used to land at relative paths inside the workspace, which put
        orchestration artifacts into the target repository's diff — every
        deployment then re-invented an exclusion for them. The artifact
        directory is where run evidence already lives, so reports join it.

        For a contained (workspace) task the external path is only offered
        when the runner can accept it, because L1 must allowlist it; promising
        a path the sandbox denies would turn every report write into a
        violation. A runner that cannot take the path falls back to the old
        in-workspace location — and the prompt SAYS so, because the shipped
        profiles refer to "the reports directory named at the top of this
        prompt" and a dangling reference would leave the stage guessing.
        """
        reports = Path(task["artifact_dir"]) / "reports"
        try:
            raw = task["workspace_dir"]
        except (IndexError, KeyError):
            raw = None
        if not raw:
            return reports, str(reports)
        if _keyword_support(self.runner.run, "reports_dir") == "none":
            return None, (
                "reports/ relative to your working directory "
                "(this deployment's runner cannot expose an external reports directory)"
            )
        return reports, str(reports)

    def _invoke_runner(
        self, task: sqlite3.Row, stage: Any, prompt: str, log_path: Path, reports_dir: Path | None = None
    ) -> RunResult:
        """Containment applies only when the task has a workspace; without one the
        call shape stays as it was, so existing runners are unaffected.

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
        result = self._runner_run_contained(stage, prompt, log_path, workspace, reports_dir)
        if sentinel is None or before is None:
            return result
        violations = sentinel.compare(before)
        if not violations:
            return result
        return self._record_protected_root_drift(task, log_path, result, violations)

    def _runner_run_contained(
        self, stage: Any, prompt: str, log_path: Path, workspace: Path, reports_dir: Path | None = None
    ) -> RunResult:
        """Hand the runner this controller's protected roots.

        L1 needs them to reject a declared write root that overlaps something
        L2 is watching. Passing them explicitly keeps a controller constructed
        with roots in code consistent with one configured from the environment.
        """
        # reports_dir is optional for the runner the same way it is for the
        # prompt: passed only when the runner can accept it, never guessed.
        reports_kw: dict[str, Path] = {}
        if reports_dir is not None and _keyword_support(self.runner.run, "reports_dir") != "none":
            reports_kw = {"reports_dir": reports_dir}
        support = _protected_roots_support(self.runner.run)
        if support == "explicit":
            return self.runner.run(
                stage.owner, prompt, stage.timeout, log_path,
                workspace=workspace, protected_roots=self.protected_roots, **reports_kw,
            )
        if support == "var_keyword":
            # The roots do reach the runner, but whether a **kwargs wrapper
            # forwards them cannot be established from here. Say so once.
            self._warn_once(
                "runner_var_keyword_protected_roots",
                f"{type(self.runner).__name__}.run() accepts protected_roots only through "
                "**kwargs; the overlap guard depends on that wrapper forwarding them",
            )
            return self.runner.run(
                stage.owner, prompt, stage.timeout, log_path,
                workspace=workspace, protected_roots=self.protected_roots, **reports_kw,
            )
        # No support at all. The roots would simply be dropped, turning a
        # fail-closed guard into a fail-open one, so only proceed when nothing
        # is actually lost: the environment already declares the same roots and
        # the runner will read them there.
        environment_roots = {Path(os.path.realpath(root)) for root in protected_roots_from_env()}
        configured_roots = {Path(os.path.realpath(root)) for root in self.protected_roots}
        if configured_roots - environment_roots:
            missing = ", ".join(sorted(str(root) for root in configured_roots - environment_roots))
            return RunResult(
                None,
                f"runner_cannot_enforce_guard: {type(self.runner).__name__}.run() cannot accept "
                f"protected_roots, and these are not declared in the environment either: {missing}. "
                "Refusing rather than running a mutating stage with the write-root guard disabled.\n",
                None,
                "raw",
                "raw",
                containment_stop="runner_cannot_enforce_guard",
            )
        return self.runner.run(stage.owner, prompt, stage.timeout, log_path, workspace=workspace, **reports_kw)

    def _warn_once(self, key: str, message: str) -> None:
        if key in self._warned:
            return
        self._warned.add(key)
        print(f"[orchestrator] warning: {message}", file=sys.stderr)

    def _sentinel_for(self, workspace: Path) -> Sentinel | None:
        roots = self.protected_roots
        if not roots:
            return None
        return Sentinel(
            roots=tuple(roots),
            workspace=workspace,
            excludes=DEFAULT_SENTINEL_EXCLUDES + sentinel_excludes_from_env(),
        )

    def _record_protected_root_drift(
        self, task: sqlite3.Row, log_path: Path, result: RunResult, violations: list[Violation]
    ) -> RunResult:
        """Protected files changed during a stage; the writer is not established.

        The stage may well have "succeeded" by its own account — that is the
        dangerous case, and why this overrides the run's own classification
        instead of being folded into it.
        """
        reason = "protected_root_drift"
        payload = {
            "schema_version": 1,
            "task_id": task["id"],
            "log_path": str(log_path),
            "attribution": "unknown",
            "workspace_dir": task["workspace_dir"],
            "detected_at": _iso_now(),
            "violations": [item.as_dict() for item in violations],
        }
        evidence_path = log_path.with_suffix(".containment-drift.json")
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_write(
            evidence_path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        )
        self._quarantine(task["id"], None, evidence_path, reason)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(
                f"containment_violation_count={len(violations)}\n"
                f"containment_evidence={evidence_path}\n"
            )
        return replace(
            result,
            containment_stop=reason,
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
            # For an envelope task whose frozen profile graph can actually
            # score its own loops, convergence is the gate and the legacy caps
            # only observe: the counter keeps counting past `max_transitions`
            # and stays readable in `orch status`, but it no longer rejects.
            # Both conjuncts are read from this task's own frozen snapshots, so
            # a task submitted before this release and resumed after it meets
            # exactly the same test as a new one.
            envelope_uncapped = (
                self._envelope_for_task(task) is not None and self._gate_reachable(profile)
            )
            if (
                task["transitions_count"] >= task["max_transitions"]
                and not allowance
                and not envelope_uncapped
            ):
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
            # One hash-verified read of the frozen input per commit, used by
            # both the hold short-circuit below and the edge-cap test further
            # down. Hold routing stays keyed on envelope presence alone.
            envelope_present = self._envelope_for_task(task) is not None
            if outcome == HOLD_OUTCOME and envelope_present:
                # Before the outcome-to-target lookup and before the edge-cap
                # test, so a profile that never declared this outcome is fully
                # served and a held task neither consumes an edge nor loses its
                # stage. `stage.outcomes[outcome]` below would raise KeyError
                # for exactly those profiles (E-10).
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
                    None,
                    outcome,
                    "running",
                    "waiting_user",
                    HOLD_STOP_REASON,
                )
                updated = self.conn.execute(
                    """UPDATE tasks SET status='waiting_user',stop_reason=?,lease_token=NULL,
                       revision=revision+1,updated_at=? WHERE id=? AND revision=? AND lease_token=?""",
                    (HOLD_STOP_REASON, now, task_id, task["revision"], run["lease_token"]),
                )
                if updated.rowcount != 1:
                    raise ControllerError("commit_run CAS conflict")
                self._notify(
                    task_id,
                    seq,
                    HOLD_STOP_REASON,
                    f"task {task_id} waiting_user: user decision required ({result.reason})",
                )
                self._write_evidence_index(task_id)
                self.conn.execute("COMMIT")
                return

            target_name = stage.outcomes[outcome]
            target = profile.stage(target_name)
            edge = profile.edge_id(stage.name, outcome)
            edge_row = self.conn.execute(
                "SELECT count,cap FROM edge_counts WHERE task_id=? AND edge=?", (task_id, edge)
            ).fetchone()
            if not edge_row:
                raise ControllerError(f"missing seeded edge counter: {edge}")
            # Same lift as the transition cap: for an envelope task on a
            # gate-reachable profile the edge counter counts without gating, so
            # `edge_counts.count` may exceed `cap` and the count stays
            # observable rather than becoming the stop condition.
            edge_cap_hit = edge_row["count"] >= edge_row["cap"] and not (
                envelope_present and self._gate_reachable(profile)
            )
            next_status = target.terminal or "queued"
            stop_reason = None
            if edge_cap_hit:
                next_status = "waiting_user"
                stop_reason = "edge_cap"
            elif outcome == HOLD_OUTCOME:
                # An edge cap that has already fired wins: elif keeps today's
                # behaviour for a held outcome that is also over its cap.
                next_status = "waiting_user"
                stop_reason = HOLD_STOP_REASON
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
            elif stop_reason == HOLD_STOP_REASON:
                self._notify(
                    task_id,
                    seq,
                    HOLD_STOP_REASON,
                    f"task {task_id} waiting_user: user decision required",
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

    def resume(
        self, task_id: str, *, operation_id: str | None = None, rerun_stage: bool = False
    ) -> dict[str, Any]:
        if type(rerun_stage) is not bool:
            raise ControllerError("rerun_stage must be a boolean")
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
            if task["stop_reason"] in {"workspace_escape", "protected_root_drift"} and not rerun_stage:
                raise ControllerError(
                    "containment_review_required: preserved stage output must be inspected with "
                    f"containment-inspect {task_id}; --rerun-stage explicitly requests a new "
                    "provider attempt, not clearance or reuse of the interrupted run"
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
                "manual_rerun_stage" if rerun_stage else "manual_resume",
            )
            self.conn.execute("COMMIT")
        except BaseException:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise
        return self.run_until_stop(task_id)

    def containment_inspect(self, task_id: str) -> dict[str, Any]:
        """No state change, lease acquisition, provider invocation or clearance."""
        self.conn.execute("BEGIN")
        try:
            task = self._task(task_id)
            run = self.conn.execute(
                "SELECT * FROM stage_runs WHERE task_id=? AND stage=? ORDER BY started_at DESC,rowid DESC LIMIT 1",
                (task_id, task["current_stage"]),
            ).fetchone()
            if run is None:
                raise ControllerError("retained_run_missing")
            return inspect_retained(task, run)
        finally:
            self.conn.execute("ROLLBACK")

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

    # -- Interpretation envelope: repeat-review convergence (spec section 1.7) --

    @staticmethod
    def _stage_arity(profile: Profile, stage_name: str) -> str:
        """branching, correction, or neither, derived from the profile snapshot.

        E-12's definitions verbatim: a branching stage is a non-terminal stage
        with more than one outcome, a correction stage a non-terminal stage
        with exactly one. Nothing is added to the profile schema to say so.
        """
        try:
            stage = profile.stage(stage_name)
        except (ProfileError, KeyError):
            return "neither"
        if stage.terminal:
            return "neither"
        count = len(stage.outcomes or {})
        if count > 1:
            return "branching"
        if count == 1:
            return "correction"
        return "neither"

    @staticmethod
    def _reachable_stages(profile: Profile) -> set[str]:
        """The stages an execution of this profile can actually enter."""
        seen: set[str] = set()
        frontier = [profile.initial_stage]
        while frontier:
            name = frontier.pop()
            if name in seen:
                continue
            seen.add(name)
            try:
                stage = profile.stage(name)
            except (ProfileError, KeyError):
                continue
            frontier.extend(target for target in (stage.outcomes or {}).values())
        return seen

    @staticmethod
    def _has_cycle(profile: Profile, nodes: set[str]) -> bool:
        """Is there a cycle inside this node set? Iterative three-colour DFS.

        Edges leaving the set are ignored, so removing a node removes every
        cycle that ran through it — which is the whole point: what survives the
        removal is exactly what never needed the removed kind of stage.
        """
        def successors(name: str) -> list[str]:
            try:
                stage = profile.stage(name)
            except (ProfileError, KeyError):
                return []
            return [target for target in (stage.outcomes or {}).values() if target in nodes]

        WHITE, GREY, BLACK = 0, 1, 2
        colour = dict.fromkeys(nodes, WHITE)
        for root in sorted(nodes):
            if colour[root] != WHITE:
                continue
            colour[root] = GREY
            stack: list[tuple[str, list[str]]] = [(root, successors(root))]
            while stack:
                name, pending = stack[-1]
                if not pending:
                    colour[name] = BLACK
                    stack.pop()
                    continue
                target = pending.pop()
                if colour[target] == GREY:
                    return True
                if colour[target] == WHITE:
                    colour[target] = GREY
                    stack.append((target, successors(target)))
        return False

    def _gate_reachable(self, profile: Profile) -> bool:
        """Can convergence score every loop this profile can enter?

        `_convergence_context` scores a *repeat* branching run: one with a
        committed correction-arity run between it and the previous branching
        run. So a cycle is only gated if it carries both arities. Two shapes
        break that and would loop for ever once the caps stop rejecting — a
        cycle of single-outcome stages, which reaches no branching stage at
        all, and a cycle with no correction stage (`review --again--> review`,
        or a branching-only multi-stage cycle), which reaches the gate but
        never satisfies the repeat predicate, so every visit files another
        first-run record and no strict-subset comparison ever runs.

        This is that repeat predicate read off the frozen graph: drop the
        correction stages and require what remains acyclic (every cycle
        carries a correction), then drop the branching stages and require the
        same (every cycle carries a branching stage). Static, linear in the
        graph, and pure: no cycle enumeration, no stored state, and no number.
        A profile that fails the proof keeps the caps that already ship.
        """
        reachable = self._reachable_stages(profile)
        arity = {name: self._stage_arity(profile, name) for name in reachable}
        without_correction = {name for name in reachable if arity[name] != "correction"}
        if self._has_cycle(profile, without_correction):
            return False
        without_branching = {name for name in reachable if arity[name] != "branching"}
        return not self._has_cycle(profile, without_branching)

    def _committed_run_history(self, task_id: str) -> list[dict[str, Any]]:
        """The task's committed run history, totally ordered by commit sequence."""
        rows = self.conn.execute(
            """SELECT t.seq AS seq, t.run_token AS run_token, t.stage AS stage,
                      r.manifest_path AS manifest_path, r.manifest_hash AS manifest_hash
               FROM transitions t JOIN stage_runs r ON r.run_token = t.run_token
               WHERE t.task_id=? AND r.status='committed' ORDER BY t.seq""",
            (task_id,),
        )
        return [dict(row) for row in rows]

    def _read_sealed_convergence(self, run: dict[str, Any]) -> dict[str, Any]:
        """A prior branching run's convergence record, read through its seal.

        The record lives in the run's own provider output, which
        `_seal_run_manifest` already writes as `<log>.output.txt` under
        `output_hash`; both hashes are re-verified here, so an edited output
        is unreadable rather than quietly authoritative.
        """
        manifest_path = run.get("manifest_path")
        manifest_hash = run.get("manifest_hash")
        if not manifest_path or not manifest_hash:
            raise ConvergenceError(f"run {run['run_token']} is not sealed")
        self._verify_file(Path(manifest_path), manifest_hash, "prior run manifest")
        manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
        output_path = manifest.get("output_path")
        output_hash = manifest.get("output_hash")
        if not output_path or not output_hash:
            raise ConvergenceError(f"run {run['run_token']} manifest names no sealed output")
        self._verify_file(Path(output_path), output_hash, "prior run output")
        output = Path(output_path).read_text(encoding="utf-8", errors="replace")
        return extract_convergence(output)

    def _convergence_context(self, task_id: str, stage: Any, profile: Profile) -> dict[str, Any] | None:
        """What section 1.7 needs about this run, or None if it is not branching."""
        if self._stage_arity(profile, stage.name) != "branching":
            return None
        history = self._committed_run_history(task_id)
        arities = [(run, self._stage_arity(profile, run["stage"])) for run in history]
        branching = [(index, run) for index, (run, arity) in enumerate(arities) if arity == "branching"]
        correction_at = [index for index, (_, arity) in enumerate(arities) if arity == "correction"]
        # A committed branching run qualifies iff at least one committed
        # correction run lies between it and now; the prior branching run is
        # the last qualifying one, whatever the history's shape.
        qualifying = [
            (index, run) for index, run in branching
            if any(correction > index for correction in correction_at)
        ]
        context: dict[str, Any] = {
            "repeat": bool(qualifying),
            "prior_live": set(),
            "historical_resolved": set(),
            "prior_stage": None,
            "prior_run_token": None,
            "corrections_between": [],
            "error": None,
        }
        if not qualifying:
            return context
        prior_index, prior = qualifying[-1]
        context["prior_stage"] = prior["stage"]
        context["prior_run_token"] = prior["run_token"]
        context["corrections_between"] = [
            history[index]["stage"] for index in correction_at if index > prior_index
        ]
        try:
            context["prior_live"] = set(self._read_sealed_convergence(prior)["live"])
            resolved: set[str] = set()
            for _, run in branching:
                resolved |= set(self._read_sealed_convergence(run).get("resolved") or [])
            context["historical_resolved"] = resolved
        except (ConvergenceError, ControllerError, OSError, ValueError) as exc:
            context["error"] = f"prior convergence record unreadable: {exc}"
        return context

    def _apply_convergence(self, result: RunResult, convergence: dict[str, Any]) -> RunResult:
        """Validate the convergence record before the typed outcome is accepted.

        Every failure — an unreadable prior, a missing, malformed or
        contradicting current record, and a `stalled` or `oscillating` verdict
        — becomes the reserved hold outcome, which commit_run short-circuits
        before the outcome-to-target lookup and before the edge-cap test. The
        caps stay cost failsafes; they are not the convergence signal.

        The record obligation itself is unconditional (E-13), so a run that
        prints the reserved hold outcome directly is checked too: it is still a
        committed branching run, and accepting it without a record would leave
        a later repeat review with no baseline to be scored against.
        """
        if result.classification != "success":
            return result

        def hold(reason: str) -> RunResult:
            return replace(result, outcome=HOLD_OUTCOME, classification="success", reason=reason)

        if convergence["error"]:
            return hold(f"convergence_unverifiable: {convergence['error']}")
        try:
            record = extract_convergence(result.output, repeat=convergence["repeat"])
        except ConvergenceError as exc:
            return hold(f"convergence_record_invalid: {exc}")
        if not convergence["repeat"]:
            return result
        try:
            verdict = validate_convergence(
                record, convergence["prior_live"], convergence["historical_resolved"]
            )
        except ConvergenceError as exc:
            return hold(f"convergence_contradictory: {exc}")
        if result.outcome == HOLD_OUTCOME:
            # E-13 binds every branching run, so the record obligation above is
            # checked for a directly printed hold too; the verdict below only
            # decides whether an *ordinary* outcome may continue, so the
            # provider's own hold reason is kept rather than restated.
            return result
        if verdict != "improved":
            return hold(f"convergence_{verdict}")
        return result

    def _envelope_for_task(self, task: sqlite3.Row) -> dict[str, Any] | None:
        """Envelope presence, read from the hash-verified input snapshot."""
        snapshot = Path(task["input_snapshot_path"])
        self._verify_file(snapshot, task["input_hash"], "input snapshot")
        return extract_envelope(snapshot.read_text(encoding="utf-8"))

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
            "schema_version": 2,
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
            "profile_hash": task["profile_hash"],
            "input_hash": task["input_hash"],
            "candidate_outcome": result.candidate_outcome,
            "candidate_classification": result.candidate_classification,
            "candidate_reason": result.candidate_reason,
            "started_at": run["started_at"],
            "ended_at": ended_at_ms,
        }
        output_path = log_path.with_suffix(".output.txt")
        self._atomic_write(output_path, result.output.encode("utf-8", errors="replace"))
        payload["output_path"] = str(output_path)
        drift_path = log_path.with_suffix(".containment-drift.json")
        if drift_path.is_file():
            payload["containment_evidence_path"] = str(drift_path)
            payload["containment_evidence_hash"] = hashlib.sha256(drift_path.read_bytes()).hexdigest()
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
    def _build_prompt(
        task_id: str,
        stage: Any,
        input_text: str,
        reports_location: str | None = None,
        envelope: dict[str, Any] | None = None,
        convergence: dict[str, Any] | None = None,
    ) -> str:
        """The single prompt-composition site, and so the single injection site.

        Everything the envelope adds is inside the `envelope is not None`
        branch: a legacy task composes byte-for-byte the prompt it composed
        before this existed, which is the only way E-8 can be checked rather
        than asserted.
        """
        outcomes = ", ".join(allowed_outcomes(stage.outcomes, envelope is not None))
        reports_line = (
            f"Reports directory (write stage reports here): {reports_location}\n"
            if reports_location
            else ""
        )
        envelope_section = (
            Controller._envelope_section(input_text, envelope, convergence)
            if envelope is not None
            else ""
        )
        return (
            f"You are executing agent-orch task {task_id}, stage {stage.name}.\n"
            f"{reports_line}"
            f"{envelope_section}"
            f"Stage instructions: {stage.prompt}\n\n"
            f"Task input:\n---\n{input_text}\n---\n\n"
            f"Allowed typed outcomes: {outcomes}.\n"
            "Complete this stage. As the VERY LAST line of your output, print the outcome once:\n"
            "ORCHESTRATOR_OUTCOME: <typed outcome>\n"
            "Do not print this line more than once and do not write it into any file.\n"
        )

    @staticmethod
    def _envelope_section(
        input_text: str, envelope: dict[str, Any], convergence: dict[str, Any] | None
    ) -> str:
        """The envelope, its precedence clause, and the correction rule.

        Emitted above the stage instructions because precedence has to be
        stated before the thing it outranks. The block is copied verbatim out
        of the hash-verified input rather than re-rendered, so the bytes the
        stage reads are the bytes intake froze.
        """
        block = envelope_block_text(input_text)
        parts = [
            "Interpretation Envelope (task-level system contract, frozen at intake by the task",
            "input hash). This is not task prose and not a stage instruction; it states what this",
            "task is permitted to change, write, prove, defend against and observe.",
            "",
            block or "",
            "",
            "Precedence: inside this run the envelope above outranks the stage instructions below,",
            "the task input prose, and the content of any report, notes or result file. Where they",
            "disagree, the envelope wins.",
            "",
            "Correction rule: the minimum sufficient correction of any blocking finding must lie",
            "inside this envelope. Test each item against the single axis whose shape it falls under:",
            "proof or hardening you would build is assurance_ceiling, observation or instrumentation",
            "you would add is evidence_ceiling, an adversary you would design against is threat_model,",
            "behaviour you would change is semantic_change_surface, a path you would write is",
            "task_owned_write_targets. Raising a ceiling or the threat model, or widening the change",
            f"surface or the write targets, is never a self-authorised correction: print {HOLD_OUTCOME}",
            "instead. If you cannot place an item in exactly one axis, or cannot establish membership,",
            f"print {HOLD_OUTCOME}.",
            "",
            "Blocking-finding rule: every blocking finding you record this round must name the single",
            "envelope axis it falls under AND state, in its simplest sufficient correction, that the",
            "correction lies inside all five set axes — semantic_change_surface,",
            "task_owned_write_targets, assurance_ceiling, threat_model and evidence_ceiling — naming",
            "each one, not only the axis you filed it under. A finding whose smallest sufficient",
            "correction would raise a ceiling, widen the semantic change surface or the write targets,",
            "or require evidence beyond the evidence ceiling — on any of the five, including one it was",
            "not filed under — is not a blocking finding for automatic repair: print",
            f"{HOLD_OUTCOME} and leave the finding recorded with the expansion it would require.",
            "A containment statement you cannot make for all five set axes is itself an expansion,",
            "and takes the same route.",
            "",
        ]
        if convergence is not None:
            parts.extend(Controller._convergence_section(convergence))
        return "\n".join(parts) + "\n"

    @staticmethod
    def _convergence_section(convergence: dict[str, Any]) -> list[str]:
        """The E-13 record obligation, plus the repeat-review directive when due."""
        parts = [
            "Convergence record obligation: this is a branching stage, so your output must carry",
            "exactly one convergence record, delimited exactly like this and parsed from your own",
            "output:",
            "",
            CONVERGENCE_BEGIN,
            '{ "live": [...], "resolved": [...] }',
            CONVERGENCE_END,
            "",
            "`live` lists the failure-scenario identities of your current blocking findings: short",
            "strings you choose, reused verbatim whenever the same scenario recurs. Comparison is",
            "exact string equality; there is no fuzzy matching.",
            "",
        ]
        if not convergence.get("repeat"):
            parts.extend(
                [
                    "This is the first branching run of this task, so record `live` and `resolved` only,",
                    "with `resolved` empty, and no verdict.",
                    "",
                ]
            )
            return parts
        prior_live = sorted(convergence.get("prior_live") or [])
        historical = sorted(convergence.get("historical_resolved") or [])
        corrections = convergence.get("corrections_between") or []
        parts.extend(
            [
                "This is a REPEAT REVIEW. The prior branching run is the last committed branching-stage",
                "run with a committed correction-stage run between it and now:",
                f"stage {convergence.get('prior_stage')!r}, run {convergence.get('prior_run_token')}.",
                f"Correction runs since then: {', '.join(corrections) if corrections else '(none)'}.",
                "",
                f"prior_live (from that run's sealed record): {json.dumps(prior_live, ensure_ascii=False)}",
                f"historical_resolved (union over every earlier branching run): "
                f"{json.dumps(historical, ensure_ascii=False)}",
                "",
                "Record all five keys:",
                "",
                CONVERGENCE_BEGIN,
                '{ "live": [...], "resolved": [...], "new": [...], "repeated": [...],',
                '  "verdict": "improved" | "stalled" | "oscillating" }',
                CONVERGENCE_END,
                "",
                "with resolved = prior_live \\ live, repeated = prior_live ∩ live, new = live \\ prior_live,",
                "and the verdict from this total rule, in order: live ∩ historical_resolved non-empty →",
                "oscillating; otherwise live a strict subset of prior_live with new empty → improved;",
                "otherwise stalled.",
                "",
                "The engine recomputes all four before accepting your typed outcome. A missing, malformed",
                f"or contradicting record, and a verdict of stalled or oscillating, all end this run at",
                f"{HOLD_OUTCOME} regardless of the outcome you print.",
                "",
            ]
        )
        return parts

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
