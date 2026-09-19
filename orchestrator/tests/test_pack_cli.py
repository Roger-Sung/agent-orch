"""The operator's exits from a hold, driven through the CLI (STATE-TABLE §6).

Every hold in the state table names an `A_*` that leaves it.  Until these
existed the engine could put a pack into a hold and nobody could take it out,
which is as stuck as a pack that never stops - so these run the real command
against a real database rather than calling the machine directly.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from orchestrator.cli import main
from orchestrator.db import connect
from orchestrator.pack import budgets
from orchestrator.pack.store import PackStore


class OperatorActionCliTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        previous = os.environ.get("ORCH_HOME")
        os.environ["ORCH_HOME"] = str(self.home)
        self.addCleanup(lambda: os.environ.__setitem__("ORCH_HOME", previous)
                        if previous is not None else os.environ.pop("ORCH_HOME", None))

        conn = connect(self.home / "orch.db")
        store = PackStore(conn)
        store.create_pack("P2", target_id="acme", change="c1", state="claimed")
        conn.commit()
        conn.close()

    def store(self):
        conn = connect(self.home / "orch.db")
        self.addCleanup(conn.close)
        return conn, PackStore(conn, create=False)

    def test_raise_cap_changes_what_the_gate_allows(self) -> None:
        conn, store = self.store()
        conn.execute("UPDATE pack_packs SET calls_reserved=? WHERE pack_id='P2'",
                     (budgets.DEFAULTS["call_budget"],))
        conn.commit()

        self.assertEqual(main(["pack-raise-cap", "P2", "call", "3"]), 0)

        conn2, store2 = self.store()
        from orchestrator.pack.state_machine import PackMachine
        machine = PackMachine(store2)
        self.assertEqual(machine.budget_caps("P2")["call_budget"],
                         budgets.DEFAULTS["call_budget"] + 3)
        # The point of raising it: the gate now lets a dispatch through.
        self.assertIsNone(machine.reserve_dispatch("P2"))

    def test_an_unknown_counter_is_refused_with_a_non_zero_exit(self) -> None:
        self.assertEqual(main(["pack-raise-cap", "P2", "made_up", "1"]), 2)

    def test_revoke_acceptance_raises_the_floor_and_holds(self) -> None:
        conn, store = self.store()
        conn.execute("UPDATE pack_packs SET state='accepted', review_round=3 WHERE pack_id='P2'")
        conn.commit()

        self.assertEqual(main(["pack-revoke-acceptance", "P2", "--reason", "approval_revoked"]), 0)

        _, store2 = self.store()
        pack = store2.get_pack("P2")
        self.assertEqual(pack["state"], "hold(approval_revoked)")
        self.assertEqual(pack["revoked_generation"], 1)
        self.assertEqual(pack["acceptance_floor_round"], 3)

    def test_allow_apply_clears_only_the_approval_blocker(self) -> None:
        conn, store = self.store()
        store.update_pack("P2", state="hold(approval_revoked)", hold_reason="approval_revoked",
                          return_point="submitted(1)",
                          blockers=[{"reason": "approval_revoked"},
                                    {"reason": "environment_changed"}])
        conn.commit()

        self.assertEqual(main(["pack-allow-apply", "P2"]), 0)

        _, store2 = self.store()
        pack = store2.get_pack("P2")
        self.assertEqual(pack["state"], "hold(environment_changed)",
                         "a grant must not wave through an unrelated failure")

    def test_allow_apply_returns_to_the_return_point_when_nothing_else_blocks(self) -> None:
        conn, store = self.store()
        store.update_pack("P2", state="hold(approval_revoked)", hold_reason="approval_revoked",
                          return_point="submitted(1)", blockers=[{"reason": "approval_revoked"}])
        conn.commit()

        self.assertEqual(main(["pack-allow-apply", "P2"]), 0)

        _, store2 = self.store()
        self.assertEqual(store2.get_pack("P2")["state"], "submitted(1)")

    def test_resolve_refuses_a_tree_touching_action_without_stop_evidence(self) -> None:
        conn, store = self.store()
        store.create_operation("OP-1", "P2", type="producer", stage="apply")
        store.update_operation("OP-1", result="unknown")
        conn.commit()

        self.assertEqual(main(["pack-resolve", "P2", "OP-1", "recovery_run"]), 0)

        _, store2 = self.store()
        self.assertIsNone(store2.get_operation("OP-1")["consumed_at"])
        self.assertEqual(store2.get_operation("OP-1")["result"], "unknown")

    def test_revoke_writes_moves_the_root_and_records_the_evidence(self) -> None:
        conn, store = self.store()
        store.create_operation("OP-1", "P2", type="producer", stage="apply")
        store.update_operation("OP-1", result="unknown")
        conn.commit()
        workspace = self.home / "ws"
        (workspace / "src").mkdir(parents=True)
        (workspace / "src" / "A.java").write_text("class A {}\n")

        self.assertEqual(main(["pack-revoke-writes", "P2", "OP-1", "--root", str(workspace)]), 0)

        self.assertFalse(workspace.exists(), "the root the sandbox allowed is still there")
        _, store2 = self.store()
        records = [r for r in store2.records_of_kind("stop_evidence", "P2")
                   if r["payload"].get("ok")]
        self.assertEqual(len(records), 1)
        # And the evidence is what lets recovery proceed.
        self.assertEqual(main(["pack-resolve", "P2", "OP-1", "recovery_run"]), 0)

    def test_rebind_is_the_only_way_out_of_a_lost_review_session(self) -> None:
        from orchestrator.pack import sessions

        conn, store = self.store()
        store.create_operation("OP-R", "P2", type="review", stage="review")
        store.update_operation("OP-R", result="unknown")
        sessions.open_session(store, "P2", "reviewer", None, op_id="OP-R")
        store.update_pack("P2", state="hold(review_session_lost)",
                          hold_reason="review_session_lost", return_point="reviewing(1)")
        conn.commit()

        self.assertEqual(main(["pack-rebind", "P2", "OP-R"]), 0)

        _, store2 = self.store()
        rows = store2.session_rows("P2", "reviewer", None)
        self.assertEqual(rows[-2]["state"], "superseded")
        self.assertEqual(rows[-1]["state"], "new")
        self.assertNotEqual(store2.get_pack("P2")["state"], "hold(review_session_lost)")
        # One retry spent, as §3.4 requires of a redispatch.
        from orchestrator.pack.state_machine import PackMachine
        self.assertEqual(
            PackMachine(store2).reviewer_retry_used("P2", stage="review", output_id=0), 1)

    def test_rebind_is_refused_when_the_pack_is_not_held_for_it(self) -> None:
        conn, store = self.store()
        store.create_operation("OP-R", "P2", type="review", stage="review")
        conn.commit()
        self.assertEqual(main(["pack-rebind", "P2", "OP-R"]), 2)

    def test_restore_tree_puts_a_frozen_candidate_back(self) -> None:
        import json as _json

        from orchestrator.pack import trees
        from orchestrator.pack.blobs import BlobStore
        from orchestrator.profile import canonical_json

        workspace = self.home / "ws"
        (workspace / "src").mkdir(parents=True)
        (workspace / "src" / "A.java").write_text("class A {}\n")
        blobs = BlobStore(self.home / "pack-blobs")
        snapshot = trees.snapshot(blobs, workspace)
        digest = blobs.put(canonical_json(snapshot))

        conn, store = self.store()
        conn.execute(
            "INSERT INTO tasks(id,type,status,current_stage,profile_hash,input_hash,"
            "profile_snapshot_path,input_snapshot_path,artifact_dir,max_transitions,"
            "created_at,updated_at,workspace_dir)"
            " VALUES('P2','apply','waiting_user','apply','ph','ih','ps','is','ad',10,0,0,?)",
            (str(workspace),))
        store.add_record("SNAP-P2-1", "candidate_snapshot",
                         {"output_id": 1, "snapshot_sha256": digest,
                          "candidate_fingerprint": "sha256:" + "a" * 64}, pack_id="P2")
        store.update_pack("P2", state="hold(candidate_changed)",
                          hold_reason="candidate_changed", return_point="submitted(1)",
                          blockers=[{"reason": "candidate_changed"}])
        conn.commit()

        (workspace / "src" / "A.java").write_text("class A { broken }\n")
        (workspace / "stray.txt").write_text("left behind\n")

        self.assertEqual(main(["pack-restore-tree", "P2", "1"]), 0)

        self.assertEqual((workspace / "src" / "A.java").read_text(), "class A {}\n")
        self.assertFalse((workspace / "stray.txt").exists())
        _, store2 = self.store()
        self.assertEqual(store2.get_pack("P2")["state"], "submitted(1)")

    def test_restore_tree_is_refused_when_nothing_was_frozen(self) -> None:
        conn, store = self.store()
        store.update_pack("P2", state="hold(candidate_changed)",
                          hold_reason="candidate_changed")
        conn.commit()
        self.assertEqual(main(["pack-restore-tree", "P2", "1"]), 2)


@unittest.skipUnless((Path(__file__).resolve().parents[2] / "targets" / "_fixture_min"
                      / "profile.yaml").is_file(), "the fixture target is not present")
class PackStartTest(unittest.TestCase):
    """Intake is what makes every pack-v1 branch reachable from outside.

    `start_packs` built the pack rows and no task was ever created against them,
    so the daemon had nothing to pick up. The assertion that matters is not that
    a task exists but that the controller classifies it as pack-v1: a task the
    classifier reads as legacy runs the whole flow down the wrong path in
    silence.
    """

    TARGET = Path(__file__).resolve().parents[2] / "targets" / "_fixture_min"

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "home"
        previous = os.environ.get("ORCH_HOME")
        os.environ["ORCH_HOME"] = str(self.home)
        self.addCleanup(lambda: os.environ.__setitem__("ORCH_HOME", previous)
                        if previous is not None else os.environ.pop("ORCH_HOME", None))
        self.workspace = Path(tmp.name) / "ws"
        self.workspace.mkdir(parents=True)

    def _run(self) -> int:
        return main([
            "pack-start",
            "--target-dir", str(self.TARGET),
            "--change-dir", str(self.TARGET / "change"),
            "--workspace", str(self.workspace),
            "--base-revision", "abc",
            "--producer-model", "claude-opus-5",
            "--reviewer-model", "gpt-6-astra",
        ])

    def test_a_task_is_created_and_reads_as_pack_v1(self) -> None:
        from orchestrator.controller import Controller

        self.assertEqual(self._run(), 0)

        controller = Controller(self.home, runner=None)
        self.addCleanup(controller.close)
        tasks = list(controller.conn.execute("SELECT * FROM tasks"))
        self.assertEqual(len(tasks), 1)
        task = tasks[0]
        self.assertEqual(controller._policy_version(task), "pack-v1",
                         "a pack task the classifier reads as legacy runs the wrong flow")
        # The pack and its task share one identity, which is what the controller
        # looks the pack up by.
        self.assertEqual(controller.pack_store.get_pack(task["id"])["pack_id"], task["id"])

    def test_the_input_carries_the_contract_the_prompts_refer_to(self) -> None:
        self.assertEqual(self._run(), 0)
        intake = sorted((self.home / "pack-intake").glob("*.md"))
        self.assertEqual(len(intake), 1)
        text = intake[0].read_text(encoding="utf-8")
        self.assertIn("Dispatch contract", text)
        self.assertIn("files_writable", text)
        self.assertIn("Contract hash: sha256:", text)
