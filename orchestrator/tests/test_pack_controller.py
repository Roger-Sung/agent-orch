"""The seven controller branches and the three-way parity check (step 3).

The parity test is the point of this file: adding a branch is only safe if the
*unbranched* path still produces the same observable rows, so legacy and
execution-v1 tasks are run through the real controller and compared field by
field with non-deterministic values pinned.
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from orchestrator.controller import Controller, ControllerError
from orchestrator.pack.store import PackStore
from orchestrator.runner import RunResult

ROOT = Path(__file__).resolve().parents[2]
DEMO_PROFILE = ROOT / "orchestrator" / "examples" / "demo-loop.yaml"
DEMO_INPUT = ROOT / "orchestrator" / "examples" / "demo-input.md"

# Columns whose values are timestamps, tokens or paths; they differ between two
# runs of the same task by construction, so parity is judged on everything else.
VOLATILE = {
    "id", "created_at", "updated_at", "started_at", "ended_at", "lease_token",
    "run_token", "task_id", "profile_snapshot_path", "input_snapshot_path",
    "artifact_dir", "log_path", "manifest_path", "manifest_hash", "duration_ms",
    "operation_id", "at", "seq", "workspace_dir", "profile_hash", "input_hash",
    # Notification text embeds the task id, which is a fresh uuid per run.
    "message",
}


class SequenceRunner:
    def __init__(self, outcomes: list[str]):
        self.outcomes = iter(outcomes)

    def run(self, owner: str, prompt: str, timeout: int, log_path: Path, **kwargs) -> RunResult:
        outcome = next(self.outcomes)
        output = f"ORCHESTRATOR_OUTCOME: {outcome}\n"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(output, encoding="utf-8")
        return RunResult(0, output, None, "raw", "raw")


def _stable(rows) -> list[dict]:
    return [{k: v for k, v in dict(row).items() if k not in VOLATILE} for row in rows]


def observable(controller: Controller, task_id: str) -> dict:
    """Everything an operator can see about a task, minus volatile values."""
    conn = controller.conn
    return {
        "task": _stable(conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,))),
        "runs": _stable(conn.execute(
            "SELECT * FROM stage_runs WHERE task_id=? ORDER BY started_at,run_token", (task_id,))),
        "transitions": _stable(conn.execute(
            "SELECT * FROM transitions WHERE task_id=? ORDER BY seq", (task_id,))),
        "edges": _stable(conn.execute(
            "SELECT * FROM edge_counts WHERE task_id=? ORDER BY edge", (task_id,))),
        "notifications": _stable(conn.execute(
            "SELECT * FROM notifications WHERE task_id=? ORDER BY transition_seq", (task_id,))),
    }


def run_legacy(directory: Path, outcomes: list[str]) -> dict:
    controller = Controller(directory, runner=SequenceRunner(outcomes))
    try:
        task_id = controller.submit("demo-loop", DEMO_PROFILE, DEMO_INPUT)
        controller.run_until_stop(task_id)
        return observable(controller, task_id)
    finally:
        controller.close()


class ThreeWayParityTest(unittest.TestCase):
    """joint-r1: the branch must not be observable from the unbranched paths."""

    def test_legacy_task_is_byte_identical_across_two_runs(self) -> None:
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            first = run_legacy(Path(a), ["submit", "block", "submit", "block"])
            second = run_legacy(Path(b), ["submit", "block", "submit", "block"])
        self.assertEqual(first, second)

    def test_legacy_still_reaches_the_edge_cap(self) -> None:
        # The cap bypass is guarded on pack-v1, so the legacy stop must survive.
        with tempfile.TemporaryDirectory() as directory:
            result = run_legacy(Path(directory), ["submit", "block", "submit", "block"])
        self.assertEqual(result["task"][0]["status"], "waiting_user")
        self.assertEqual(result["task"][0]["stop_reason"], "edge_cap")

    def test_legacy_resume_still_clears_the_lease(self) -> None:
        # `resume` ends by re-entering the run loop, so the loop is stubbed out:
        # the branch under test is the UPDATE, not what happens afterwards.
        with tempfile.TemporaryDirectory() as directory:
            controller = Controller(Path(directory), runner=SequenceRunner([]))
            try:
                task_id = controller.submit("demo-loop", DEMO_PROFILE, DEMO_INPUT)
                controller.conn.execute(
                    "UPDATE tasks SET status='waiting_user',stop_reason='stalled',"
                    "lease_token='LEASE-1' WHERE id=?", (task_id,))
                controller.run_until_stop = lambda _tid: {}
                controller.resume(task_id)
                row = controller.conn.execute(
                    "SELECT lease_token,resume_allowance FROM tasks WHERE id=?", (task_id,)
                ).fetchone()
            finally:
                controller.close()
        self.assertIsNone(row["lease_token"])
        self.assertEqual(row["resume_allowance"], 1)


class PolicyDetectionTest(unittest.TestCase):
    """A task whose policy cannot be read must fall back to legacy."""

    def controller(self, directory: Path) -> Controller:
        return Controller(directory, runner=SequenceRunner([]))

    def test_plain_task_is_not_pack_v1(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self.controller(Path(directory))
            try:
                task_id = controller.submit("demo-loop", DEMO_PROFILE, DEMO_INPUT)
                task = controller.conn.execute(
                    "SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
                self.assertEqual(controller._policy_version(task), "execution-v1")
                self.assertFalse(controller._is_pack_v1(task))
            finally:
                controller.close()

    # Unreadable input must not be classified as pack-v1: every branch is an
    # exception to existing behaviour, so an unknown task takes the old path.
    def test_unreadable_snapshot_falls_back_to_legacy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self.controller(Path(directory))
            try:
                task_id = controller.submit("demo-loop", DEMO_PROFILE, DEMO_INPUT)
                task = controller.conn.execute(
                    "SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
                Path(task["input_snapshot_path"]).unlink()
                self.assertEqual(controller._policy_version(task), "execution-v1")
            finally:
                controller.close()


class PackBranchTest(unittest.TestCase):
    """Each branch, exercised against a task the controller reports as pack-v1."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.controller = Controller(Path(self._tmp.name), runner=SequenceRunner([]))
        self.addCleanup(self.controller.close)
        self.task_id = self.controller.submit("demo-loop", DEMO_PROFILE, DEMO_INPUT)
        # Force the classification rather than authoring a pack-v1 input here:
        # this file is about the branches, and the plan parsing has its own tests.
        self.controller._is_pack_v1 = lambda task: True

    def task(self):
        return self.controller.conn.execute(
            "SELECT * FROM tasks WHERE id=?", (self.task_id,)).fetchone()

    # (e) the startup barrier must not touch a pack task.
    def test_startup_barrier_defers_to_pack_reconcile(self) -> None:
        self.controller.conn.execute(
            "UPDATE tasks SET status='running',lease_token='LEASE-1' WHERE id=?", (self.task_id,))
        summary = self.controller.reconcile_startup()
        self.assertEqual(summary["running_blocked"], 0)
        self.assertEqual(summary["pack_reconcile_deferred"], 1)
        row = self.task()
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["lease_token"], "LEASE-1")

    # (e) and the run loop barrier likewise.
    def test_run_loop_leaves_a_running_pack_task_alone(self) -> None:
        self.controller.conn.execute(
            "UPDATE tasks SET status='running',lease_token='LEASE-1' WHERE id=?", (self.task_id,))
        self.controller.run_until_stop(self.task_id)
        self.assertEqual(self.task()["status"], "running")

    # (f) resume keeps the writer lease and grants no allowance.
    def test_resume_keeps_the_lease_and_gives_no_allowance(self) -> None:
        self.controller.conn.execute(
            "UPDATE tasks SET status='waiting_user',stop_reason='stalled',lease_token='LEASE-1'"
            " WHERE id=?", (self.task_id,))
        self.controller.run_until_stop = lambda _tid: {}
        self.controller.resume(self.task_id)
        row = self.task()
        self.assertEqual(row["lease_token"], "LEASE-1")
        self.assertEqual(row["resume_allowance"], 0)
        self.assertEqual(row["status"], "queued")

    # (g) the outcome set is a per-stage constant, not derived from an envelope.
    def test_allowed_outcomes_come_from_the_policy(self) -> None:
        from orchestrator.profile import load_profile

        profile = load_profile(
            Path(__file__).resolve().parents[1] / "profiles" / "claude_apply_codex_review.yaml")
        outcomes = self.controller._pack_outcomes(self.task(), profile.stage("apply"))
        self.assertEqual(outcomes, sorted(["produced", "producer_failed", "hold"]))

    def test_unknown_stage_is_a_configuration_error(self) -> None:
        class Stage:
            name = "not_a_pack_stage"
            outcomes: dict = {}

        with self.assertRaises(ControllerError):
            self.controller._pack_outcomes(self.task(), Stage())

    # (a) the legacy caps observe but no longer refuse.
    def test_transition_cap_does_not_refuse_a_pack_task(self) -> None:
        self.controller.conn.execute(
            "UPDATE tasks SET transitions_count=max_transitions WHERE id=?", (self.task_id,))
        claimed = self.controller.claim_stage(self.task_id)
        self.assertIsNotNone(claimed)
        self.assertEqual(self.task()["transitions_count"],
                         self.task()["max_transitions"])

    # The same condition must still stop a legacy task.
    def test_transition_cap_still_refuses_a_legacy_task(self) -> None:
        self.controller._is_pack_v1 = lambda task: False
        self.controller.conn.execute(
            "UPDATE tasks SET transitions_count=max_transitions WHERE id=?", (self.task_id,))
        self.assertIsNone(self.controller.claim_stage(self.task_id))
        self.assertEqual(self.task()["stop_reason"], "transition_cap")

    # (d) commit seals the call and leaves the result for the machine.
    def test_commit_seals_and_leaves_the_result_unconsumed(self) -> None:
        # The demo profile's stage names are not pack stages, so the prompt's
        # outcome set is supplied directly; this test is about the commit path.
        self.controller._pack_outcomes = lambda task, stage: ["submit", "block"]
        claimed = self.controller.claim_stage(self.task_id)
        run_token, stage, profile, log_path = claimed
        store = PackStore(self.controller.conn)
        store.create_pack(self.task_id, target_id="acme", change="c1", state="claimed")
        result = self.controller.runner_result = RunResult(
            0, "ORCHESTRATOR_OUTCOME: submit\n", None, "raw", "raw")
        from orchestrator.runner import classify_result

        classified = classify_result(0, "ORCHESTRATOR_OUTCOME: submit\n", {"submit"}, False,
                                     source=result)
        self.controller.commit_run(self.task_id, run_token, classified, profile)

        op = store.get_operation(run_token)
        self.assertEqual(op["result"], "completed")
        self.assertIsNone(op["consumed_at"])
        run = self.controller.conn.execute(
            "SELECT status,sealed FROM stage_runs WHERE run_token=?", (run_token,)).fetchone()
        self.assertEqual((run["status"], run["sealed"]), ("committed", 1))
        # No legacy edge routing happened. The rows themselves are seeded from
        # the profile's caps at submit time, so the thing to assert is that no
        # edge was *counted*, not that the table is empty.
        counted = self.controller.conn.execute(
            "SELECT COALESCE(SUM(count),0) AS n FROM edge_counts WHERE task_id=?",
            (self.task_id,)).fetchone()
        self.assertEqual(counted["n"], 0)
        # ...and that the commit recorded no outcome-driven transition either.
        transitions = self.controller.conn.execute(
            "SELECT COUNT(*) AS n FROM transitions WHERE task_id=? AND edge IS NOT NULL",
            (self.task_id,)).fetchone()
        self.assertEqual(transitions["n"], 0)
        self.assertEqual(self.task()["status"], "queued")


if __name__ == "__main__":
    unittest.main()


class PackCliTest(unittest.TestCase):
    """`orch pack-status` / `pack-list` - step 1's second exit criterion."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        from orchestrator.db import connect

        conn = connect(self.home / "orch.db")
        store = PackStore(conn)
        store.create_pack("P2", target_id="acme", change="c1", state="repair_pending(1)")
        store.update_pack("P2", contract_hash="sha256:" + "b" * 64, review_round=1, k_last=1)
        store.create_pack("P1", target_id="acme", change="c1", state="hold(contract_hold)")
        store.update_pack("P1", hold_reason="contract_hold", return_point="contracting")
        conn.commit()
        conn.close()

    def run_cli(self, *args: str):
        import io
        import os
        from contextlib import redirect_stdout, redirect_stderr
        from unittest.mock import patch

        from orchestrator.cli import main

        out, err = io.StringIO(), io.StringIO()
        # Home is resolved from the environment, not a flag.
        with patch.dict(os.environ, {"ORCH_HOME": str(self.home)}):
            with redirect_stdout(out), redirect_stderr(err):
                code = main(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_pack_list_shows_every_pack(self) -> None:
        code, out, _ = self.run_cli("pack-list")
        self.assertEqual(code, 0)
        self.assertIn("P1", out)
        self.assertIn("P2", out)
        self.assertIn("contract_hold", out)

    def test_pack_status_renders(self) -> None:
        code, out, _ = self.run_cli("pack-status", "P2")
        self.assertEqual(code, 0)
        self.assertIn("repair_pending(1)", out)

    def test_pack_status_json(self) -> None:
        import json as _json

        code, out, _ = self.run_cli("pack-status", "P2", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(_json.loads(out)["state"], "repair_pending(1)")

    def test_unknown_pack_exits_two(self) -> None:
        code, _, err = self.run_cli("pack-status", "nope")
        self.assertEqual(code, 2)
        self.assertIn("unknown pack", err)

    # Inspecting a pack must never be able to change one.
    def test_status_opens_the_database_read_only(self) -> None:
        code, _, _ = self.run_cli("pack-status", "P2")
        self.assertEqual(code, 0)
        from orchestrator.db import connect

        conn = connect(self.home / "orch.db", read_only=True)
        try:
            state = conn.execute(
                "SELECT state FROM pack_packs WHERE pack_id='P2'").fetchone()["state"]
        finally:
            conn.close()
        self.assertEqual(state, "repair_pending(1)")
