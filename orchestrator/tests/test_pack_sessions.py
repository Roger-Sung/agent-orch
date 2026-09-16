"""pack_sessions lifecycle (STATE-TABLE §8) - step 2."""
from __future__ import annotations

import unittest

from orchestrator.pack.sessions import (
    FRESH,
    NEEDS_REBIND,
    RESUME,
    confirm_session,
    open_session,
    rebind_session,
    return_state,
    role_for_stage,
    select_session,
)
from orchestrator.tests.pack_stub import StubPack

BINDING = {"provider": "codex", "session_id": "S-1", "model_invoked": "gpt-6-astra",
           "cwd": "/w", "uid": 501, "host": "mac"}


class SessionSelectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.pack = StubPack()
        self.store = self.pack.store
        self.pid = self.pack.pack_id

    # contract_review is a reviewer stage, not a third identity.
    def test_stage_to_role_mapping(self) -> None:
        self.assertEqual(role_for_stage("contract_review"), "reviewer")
        self.assertEqual(role_for_stage("review"), "reviewer")
        self.assertEqual(role_for_stage("apply"), "producer")
        self.assertEqual(role_for_stage("repair"), "producer")
        with self.assertRaises(KeyError):
            role_for_stage("prerun")

    # joint-r4: the very first call has no predecessor to rebind.
    def test_first_call_is_fresh(self) -> None:
        mode, latest = select_session(self.store, self.pid, "reviewer", None)
        self.assertEqual(mode, FRESH)
        self.assertIsNone(latest)

    # joint-r4: the first final_review continues the contract-review session.
    def test_ready_session_is_resumed(self) -> None:
        seq = open_session(self.store, self.pid, "reviewer", None, op_id="OP-1")
        confirm_session(self.store, self.pid, "reviewer", None, seq, BINDING)
        mode, latest = select_session(self.store, self.pid, "reviewer", None)
        self.assertEqual(mode, RESUME)
        self.assertEqual(latest["provider_binding"]["session_id"], "S-1")

    # An unconfirmed session must not be routed around by starting a fresh one.
    def test_unconfirmed_session_demands_a_rebind(self) -> None:
        seq = open_session(self.store, self.pid, "reviewer", None, op_id="OP-1")
        confirm_session(self.store, self.pid, "reviewer", None, seq, None)
        mode, _ = select_session(self.store, self.pid, "reviewer", None)
        self.assertEqual(mode, NEEDS_REBIND)
        with self.assertRaises(ValueError):
            open_session(self.store, self.pid, "reviewer", None, op_id="OP-2")

    # The pending row is written before the spawn so a crash is visible.
    def test_pending_is_written_before_the_call(self) -> None:
        open_session(self.store, self.pid, "reviewer", None, op_id="OP-1")
        latest = self.store.latest_session(self.pid, "reviewer", None)
        self.assertEqual(latest["state"], "pending")
        self.assertEqual(latest["pending_op_id"], "OP-1")

    # Producer sessions are per work attempt; a base refresh gets a new one.
    def test_producer_sessions_are_per_attempt(self) -> None:
        seq = open_session(self.store, self.pid, "producer", "WA-1", op_id="OP-1")
        confirm_session(self.store, self.pid, "producer", "WA-1", seq, BINDING)
        self.assertEqual(select_session(self.store, self.pid, "producer", "WA-1")[0], RESUME)
        self.assertEqual(select_session(self.store, self.pid, "producer", "WA-2")[0], FRESH)


class RebindTest(unittest.TestCase):
    def setUp(self) -> None:
        self.pack = StubPack()
        self.store = self.pack.store
        self.pid = self.pack.pack_id
        seq = open_session(self.store, self.pid, "reviewer", None, op_id="OP-1")
        confirm_session(self.store, self.pid, "reviewer", None, seq, None)

    # joint-r3: a sealed success is kept; re-running would waste the work.
    def test_sealed_success_is_not_rerun_and_costs_no_retry(self) -> None:
        result = rebind_session(self.store, self.pid, "reviewer", None,
                                sealed_result="completed")
        self.assertEqual(result["branch"], "sealed_success")
        self.assertFalse(result["rerun"])
        self.assertFalse(result["charge_retry"])

    def test_sealed_failure_reruns_and_charges(self) -> None:
        result = rebind_session(self.store, self.pid, "reviewer", None,
                                sealed_result="failed")
        self.assertEqual(result["branch"], "sealed_failure")
        self.assertTrue(result["rerun"])
        self.assertTrue(result["charge_retry"])

    def test_no_result_reruns_and_charges(self) -> None:
        result = rebind_session(self.store, self.pid, "reviewer", None, sealed_result=None)
        self.assertEqual(result["branch"], "no_result")
        self.assertTrue(result["rerun"])

    # joint-r5: the successor must be usable by the next inspect.
    def test_successor_row_is_new_not_superseded(self) -> None:
        rebind_session(self.store, self.pid, "reviewer", None, sealed_result="completed")
        rows = self.store.session_rows(self.pid, "reviewer", None)
        self.assertEqual(rows[-2]["state"], "superseded")
        self.assertEqual(rows[-1]["state"], "new")
        self.assertEqual(select_session(self.store, self.pid, "reviewer", None)[0], FRESH)

    def test_predecessor_is_kept(self) -> None:
        seq = self.store.latest_session(self.pid, "reviewer", None)["seq"]
        confirm_session(self.store, self.pid, "reviewer", None, seq, BINDING)
        rebind_session(self.store, self.pid, "reviewer", None, sealed_result="failed")
        rows = self.store.session_rows(self.pid, "reviewer", None)
        self.assertEqual(rows[-1]["predecessor"]["session_id"], "S-1")


class ReturnStateTest(unittest.TestCase):
    # The resume position comes from the pending op's stage (joint-r4).
    def test_return_state_per_stage(self) -> None:
        self.assertEqual(return_state("apply", 3), "producing(3)")
        self.assertEqual(return_state("repair", 3), "producing(3)")
        self.assertEqual(return_state("review", 3), "reviewing(3)")
        self.assertEqual(return_state("contract_review", 3), "contracting")


if __name__ == "__main__":
    unittest.main()
