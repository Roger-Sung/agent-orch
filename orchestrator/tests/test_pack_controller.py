"""The seven controller branches and the three-way parity check (step 3).

The parity test is the point of this file: adding a branch is only safe if the
*unbranched* path still produces the same observable rows, so legacy and
execution-v1 tasks are run through the real controller and compared field by
field with non-deterministic values pinned.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

from orchestrator.controller import Controller, ControllerError
from orchestrator.pack import budgets, trees
from orchestrator.pack.envelopes import REVIEW_BEGIN, REVIEW_END
from orchestrator.pack.policy import allowed_outcomes as pack_allowed_outcomes
from orchestrator.pack.store import PackStore
from orchestrator.tests.pack_stub import full_review_envelope
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

    # (e) the startup barrier hands a pack task to the pack's own reconcile.
    # Deferring without running it left an interrupted pack `running` for ever,
    # which is why this asserts the scan happened rather than that it was skipped.
    def test_startup_barrier_runs_the_pack_reconcile(self) -> None:
        self.controller.conn.execute(
            "UPDATE tasks SET status='running',lease_token='LEASE-1' WHERE id=?", (self.task_id,))
        store = self.controller.pack_store
        store.create_pack(self.task_id, target_id="acme", change="c1", state="producing(1)")
        store.create_operation("OP-1", self.task_id, type="producer", stage="apply")
        store.update_operation("OP-1", spawned=1,
                               process_identity={"pid": 999999, "pgid": 999999, "start": 1})

        summary = self.controller.reconcile_startup()
        self.assertEqual(summary["running_blocked"], 0)
        # A dead group with no receipt is unknown, not blocked and not recovered.
        self.assertEqual(summary["pack_unknown"], 1)
        self.assertEqual(store.get_operation("OP-1")["result"], "unknown")
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


class PackReceiptRecoveryTest(unittest.TestCase):
    """The window §5 exists for: the provider finished, the controller did not.

    Sealing inside `commit_run` would put the receipt on the far side of the
    crash it is meant to survive, so it is written first and its hash recorded
    in its own transaction - otherwise a crash in between leaves a receipt with
    nothing to verify it against, which reconcile must refuse.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.controller = Controller(Path(self._tmp.name), runner=SequenceRunner([]))
        self.addCleanup(self.controller.close)
        self.task_id = self.controller.submit("demo-loop", DEMO_PROFILE, DEMO_INPUT)
        self.controller._is_pack_v1 = lambda task: True
        self.store = self.controller.pack_store
        self.store.create_pack(self.task_id, target_id="acme", change="c1",
                               state="reviewing(1)")
        self.store.create_operation("OP-R", self.task_id, type="review", stage="review")
        self.store.update_operation(
            "OP-R", spawned=1,
            process_identity={"pid": 999999, "pgid": 999999, "start": 1})
        self.task = self.controller.conn.execute(
            "SELECT * FROM tasks WHERE id=?", (self.task_id,)).fetchone()

    def _result(self, envelope: dict, outcome: str = "needs_repair") -> RunResult:
        body = json.dumps(envelope)
        text = f"{REVIEW_BEGIN}\n{body}\n{REVIEW_END}\nORCHESTRATOR_OUTCOME: {outcome}\n"
        return RunResult(0, text, outcome, "success", "ok")

    def test_a_sealed_receipt_is_recovered_after_the_crash(self) -> None:
        self.controller._seal_pack_receipt(self.task, "OP-R", self._result(full_review_envelope(1)))
        ref = self.store.get_operation("OP-R")["receipt_ref"]
        self.assertTrue(ref and ref.startswith("sha256:"), "the expectation must be durable")
        self.assertTrue(self.controller._pack_receipt_path(self.task, "OP-R").is_file())

        counts = self.controller.reconcile_pack(self.task)
        self.assertEqual(counts["recovered"], 1)
        self.assertEqual(counts["unknown"], 0)
        self.assertEqual(self.store.get_operation("OP-R")["result"], "completed")

    def test_a_call_with_no_envelope_seals_nothing(self) -> None:
        self.controller._seal_pack_receipt(
            self.task, "OP-R", RunResult(0, "no envelope here", None, "success", "ok"))
        self.assertIsNone(self.store.get_operation("OP-R")["receipt_ref"])
        counts = self.controller.reconcile_pack(self.task)
        self.assertEqual(counts["unknown"], 1)
        self.assertEqual(counts["recovered"], 0)

    def test_a_receipt_rewritten_after_sealing_is_refused(self) -> None:
        self.controller._seal_pack_receipt(self.task, "OP-R", self._result(full_review_envelope(1)))
        path = self.controller._pack_receipt_path(self.task, "OP-R")
        path.write_bytes(path.read_bytes().replace(b"needs_repair", b"accepted"))
        counts = self.controller.reconcile_pack(self.task)
        self.assertEqual(counts["unknown"], 1)
        self.assertEqual(counts["recovered"], 0)

    def test_a_live_process_group_is_waited_for_not_reconciled(self) -> None:
        self.store.update_operation(
            "OP-R", process_identity={"pid": os.getpid(), "pgid": os.getpgid(0), "start": 1})
        counts = self.controller.reconcile_pack(self.task)
        self.assertEqual(counts, {"unknown": 0, "consumed": 0, "deferred": 0,
                                  "not_spawned": 0, "recovered": 0, "session_lost": 0})
        self.assertIsNone(self.store.get_operation("OP-R")["result"])


class EnvelopeRunner:
    """A runner whose output carries a framed review envelope."""

    def __init__(self, envelope: dict, outcome: str = "needs_repair") -> None:
        body = json.dumps(envelope)
        self.text = (f"{REVIEW_BEGIN}\n{body}\n{REVIEW_END}\n"
                     f"ORCHESTRATOR_OUTCOME: {outcome}\n")

    def run(self, owner: str, prompt: str, timeout: int, log_path: Path, **kwargs) -> RunResult:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(self.text, encoding="utf-8")
        return RunResult(0, self.text, None, "raw", "raw")


class ReceiptCallSiteTest(unittest.TestCase):
    """The seal has to happen on the real path, not only when called directly.

    Testing `_seal_pack_receipt` on its own says nothing about whether anything
    invokes it. This drives one real dispatch and stops the loop at the tick,
    because the post-commit driver that would end it is still step 7 work.
    """

    def test_a_real_run_seals_before_it_commits(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        controller = Controller(Path(tmp.name),
                                runner=EnvelopeRunner(full_review_envelope(1)))
        self.addCleanup(controller.close)
        task_id = controller.submit("demo-loop", DEMO_PROFILE, DEMO_INPUT)
        controller._is_pack_v1 = lambda task: True
        controller._pack_outcomes = lambda task, stage: sorted(
            pack_allowed_outcomes("review"))
        controller.pack_store.create_pack(task_id, target_id="acme", change="c1",
                                          state="reviewing(1)")

        order: list[str] = []
        seal = controller._seal_pack_receipt
        controller._seal_pack_receipt = lambda *a, **k: (order.append("seal"), seal(*a, **k))[1]
        commit = controller._commit_pack_run
        controller._commit_pack_run = lambda *a, **k: (order.append("commit"),
                                                       commit(*a, **k))[1]

        controller.run_until_stop(task_id)

        self.assertEqual(order[:2], ["seal", "commit"],
                         "the receipt must be sealed before the commit it survives")
        receipts_dir = Path(controller.conn.execute(
            "SELECT artifact_dir FROM tasks WHERE id=?", (task_id,)
        ).fetchone()["artifact_dir"]) / "pack-receipts"
        self.assertTrue(sorted(receipts_dir.glob("*.json")),
                        "a pack-v1 run sealed no receipt, so nothing is recoverable")
        self.assertTrue(any(row["receipt_ref"] for row in controller.conn.execute(
            "SELECT receipt_ref FROM pack_operations WHERE pack_id=?", (task_id,))),
            "the receipt's hash was never made durable")


class PackRunLoopTerminatesTest(unittest.TestCase):
    """A pack-v1 task must stop for a stated reason rather than loop.

    `_commit_pack_run` puts the task back to `queued` for the pack machine to
    advance. Until something advanced it the loop re-dispatched the same stage
    for ever, and with the legacy caps bypassed there was no limit to stop it
    either - so this asserts both that the loop ends and why.
    """

    def test_the_loop_ends_when_the_call_budget_runs_out(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        controller = Controller(Path(tmp.name),
                                runner=EnvelopeRunner(full_review_envelope(1)))
        self.addCleanup(controller.close)
        task_id = controller.submit("demo-loop", DEMO_PROFILE, DEMO_INPUT)
        controller._is_pack_v1 = lambda task: True
        controller._pack_outcomes = lambda task, stage: sorted(
            pack_allowed_outcomes("review"))
        controller.pack_store.create_pack(task_id, target_id="acme", change="c1",
                                          state="reviewing(1)")

        controller.run_until_stop(task_id)

        row = controller.conn.execute(
            "SELECT status, stop_reason FROM tasks WHERE id=?", (task_id,)).fetchone()
        self.assertEqual(row["status"], "waiting_user")
        self.assertEqual(row["stop_reason"], "budget_exhausted(call)")
        # Every reserved call is accounted for; the cap is what stopped it.
        pack = controller.pack_store.get_pack(task_id)
        self.assertEqual(pack["calls_reserved"], budgets.DEFAULTS["call_budget"])
        self.assertEqual(pack["state"], "hold(budget_exhausted(call))")


class FreezeCandidateTest(unittest.TestCase):
    """The producer's output is frozen with its content, not just its fingerprint.

    A fingerprint notices that a candidate changed and cannot put it back, so
    `A_restore_tree` had nothing to restore from and `submitted(k)` was never
    reached on the real path at all.
    """

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.workspace = self.root / "ws"
        self.workspace.mkdir()
        for argv in (["git", "init", "-q"],
                     ["git", "config", "user.email", "t@example.com"],
                     ["git", "config", "user.name", "t"]):
            subprocess.run(argv, cwd=self.workspace, check=True, capture_output=True)
        (self.workspace / "src").mkdir()
        (self.workspace / "src" / "A.java").write_text("class A {}\n")
        subprocess.run(["git", "add", "-A"], cwd=self.workspace, check=True,
                       capture_output=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=self.workspace, check=True,
                       capture_output=True)

        self.controller = Controller(self.root / "home", runner=SequenceRunner([]))
        self.addCleanup(self.controller.close)
        self.task_id = self.controller.submit("demo-loop", DEMO_PROFILE, DEMO_INPUT)
        self.controller._is_pack_v1 = lambda task: True
        self.controller.conn.execute(
            "UPDATE tasks SET workspace_dir=? WHERE id=?", (str(self.workspace), self.task_id))
        store = self.controller.pack_store
        store.create_pack(self.task_id, target_id="acme", change="c1", state="producing(1)")
        store.create_attempt("WA-1", self.task_id, base_revision="abc",
                             candidate_input="sha256:" + "a" * 64, next_output_id=1)
        store.create_operation("OP-A", self.task_id, type="producer", stage="apply",
                               attempt_id="WA-1")
        self.controller.pack_store.update_operation("OP-A", result="completed")
        self.controller.conn.commit()
        self.task = self.controller.conn.execute(
            "SELECT * FROM tasks WHERE id=?", (self.task_id,)).fetchone()

    def test_consuming_the_apply_freezes_the_output_and_its_content(self) -> None:
        self.controller._advance_pack(self.task)

        pack = self.controller.pack_store.get_pack(self.task_id)
        self.assertEqual(pack["state"], "submitted(1)")
        self.assertEqual(pack["k_last"], 1)
        self.assertEqual(
            self.controller.pack_store.get_operation("OP-A")["produced_output_id"], 1)
        self.assertEqual(
            self.controller.pack_store.get_attempt("WA-1")["next_output_id"], 2)

        snapshot = self.controller.pack_candidate_snapshot(self.task_id, 1)
        self.assertIsNotNone(snapshot, "a fingerprint with no content cannot be restored")
        self.assertIn("src/A.java", [entry["path"] for entry in snapshot["files"]])

    def test_the_frozen_content_is_enough_to_restore_the_tree(self) -> None:
        """The payoff: A_restore_tree now has something to restore from."""
        self.controller._advance_pack(self.task)
        snapshot = self.controller.pack_candidate_snapshot(self.task_id, 1)

        (self.workspace / "src" / "A.java").write_text("class A { broken }\n")
        (self.workspace / "stray.txt").write_text("left behind\n")
        trees.restore(self.controller.pack_blobs(), self.workspace, snapshot)

        self.assertEqual((self.workspace / "src" / "A.java").read_text(), "class A {}\n")
        self.assertFalse((self.workspace / "stray.txt").exists())

    def test_a_task_with_no_workspace_freezes_nothing(self) -> None:
        self.controller.conn.execute(
            "UPDATE tasks SET workspace_dir=NULL WHERE id=?", (self.task_id,))
        task = self.controller.conn.execute(
            "SELECT * FROM tasks WHERE id=?", (self.task_id,)).fetchone()

        self.controller._advance_pack(task)

        self.assertIsNone(self.controller.pack_candidate_snapshot(self.task_id, 1))
        self.assertEqual(self.controller.pack_store.get_pack(self.task_id)["k_last"], 0)
