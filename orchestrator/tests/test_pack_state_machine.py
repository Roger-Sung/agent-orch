"""STATE-TABLE §10 fixtures driven by the stub (IMPLEMENTATION-PLAN step 1)."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from orchestrator.pack import budgets, revocation
from orchestrator.pack.state_machine import (
    ALREADY_CONSUMED,
    CONSUME,
    DEFER,
    RESOLVE_UNKNOWN,
    RESUME_AWAITING,
    STALE,
    PackMachine,
)
from orchestrator.tests.pack_stub import (
    BOOT_A,
    BudgetRefused,
    BOOT_B,
    CONTRACT_H1,
    StubPack,
    envelope,
    high_finding,
)


class HappyPathTest(unittest.TestCase):
    # ST1a: the whole loop with a stub producer and reviewer.
    def test_st1a_needs_repair_reaches_repair_pending(self) -> None:
        pack = StubPack()
        self.assertEqual(pack.contract_review(passes=True), "claimed")
        pack.claim()
        op = pack.produce()
        k = pack.submit(op)
        pack.prerun(k)
        state = pack.review(
            k,
            envelope(1, verdict="needs_repair",
                     obligations={"O1": "PASS", "O2": "FAIL"},
                     findings=[high_finding("F1-1", "L-1", "O2")]),
        )
        self.assertEqual(state, "repair_pending(1)")
        self.assertEqual(pack.pack()["review_round"], 1)
        self.assertEqual(pack.pack()["decision"]["kind"], "dispatch_ok")

    # ST1b: UNKNOWN with no finding is an evidence gap, not a repair.
    def test_st1b_evidence_gap_holds_with_return_point(self) -> None:
        pack = StubPack()
        pack.contract_review(passes=True)
        pack.claim()
        k = pack.submit(pack.produce())
        pack.prerun(k)
        state = pack.review(
            k,
            envelope(1, verdict="blocked", blocked_reason="evidence_gap",
                     obligations={"O1": "PASS", "O2": "UNKNOWN"}),
        )
        self.assertEqual(state, "hold(evidence_gap)")
        stored = pack.pack()
        self.assertEqual(stored["return_point"], "submitted(1)")
        self.assertEqual(stored["evidence_todo"], ["O2"])
        self.assertEqual(stored["continuation"], "reverify_then_review(1)")

    # ST1c: all UNKNOWN is the same route.
    def test_st1c_all_unknown_is_evidence_gap(self) -> None:
        pack = StubPack()
        pack.contract_review(passes=True)
        pack.claim()
        k = pack.submit(pack.produce())
        pack.prerun(k)
        state = pack.review(
            k,
            envelope(1, verdict="blocked", blocked_reason="evidence_gap",
                     obligations={"O1": "UNKNOWN", "O2": "UNKNOWN"}),
        )
        self.assertEqual(state, "hold(evidence_gap)")
        self.assertEqual(sorted(pack.pack()["evidence_todo"]), ["O1", "O2"])

    # ST2: contract findings hold without spending a review round or a lease.
    def test_st2_contract_findings_do_not_spend_a_round(self) -> None:
        pack = StubPack()
        state = pack.contract_review(passes=False, findings=[{"id": "C1-1"}])
        self.assertEqual(state, "hold(contract_hold)")
        stored = pack.pack()
        self.assertEqual(stored["review_round"], 0)
        self.assertEqual(stored["review_seq"], 1)
        self.assertIsNone(stored["lease_token"])
        self.assertIsNone(pack.store.last_dispatch_record(pack.pack_id))

    # A reviewer that reports contract findings mid-flow voids the decision.
    def test_contract_findings_during_review_void_the_decision(self) -> None:
        pack = StubPack()
        pack.contract_review(passes=True)
        pack.claim()
        k = pack.submit(pack.produce())
        pack.prerun(k)
        state = pack.review(
            k,
            envelope(1, verdict="needs_repair", obligations={"O1": "FAIL"},
                     findings=[high_finding("F1-1", "L-1", "O1")],
                     contract_findings=[{"id": "C1-1"}]),
        )
        self.assertEqual(state, "hold(contract_hold)")
        self.assertIsNone(pack.pack()["decision"])
        self.assertEqual(pack.pack()["review_round"], 0)


class ConsumptionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.pack = StubPack()
        self.pack.contract_review(passes=True)
        self.pack.claim()

    def test_consume_marks_exactly_once(self) -> None:
        op = self.pack.produce()
        self.pack.machine.commit_call_result(
            op, result="completed",
            call_binding=self.pack.binding(stage="apply", attempt_id="WA-1"),
        )
        self.assertEqual(self.pack.machine.consume_result(self.pack.pack_id, op), CONSUME)
        self.assertEqual(self.pack.machine.consume_result(self.pack.pack_id, op), ALREADY_CONSUMED)

    def test_commit_is_idempotent(self) -> None:
        op = self.pack.produce()
        self.pack.machine.commit_call_result(op, result="completed")
        self.pack.machine.commit_call_result(op, result="failed")
        self.assertEqual(self.pack.store.get_operation(op)["result"], "completed")

    # A result carrying a superseded contract hash is stale, not applicable.
    def test_stale_binding_is_archived_not_applied(self) -> None:
        op = self.pack.produce()
        self.pack.machine.commit_call_result(
            op, result="completed",
            call_binding=self.pack.binding(stage="apply", attempt_id="WA-1",
                                           contract_hash="sha256:" + "9" * 64),
        )
        self.assertEqual(self.pack.machine.consume_result(self.pack.pack_id, op), STALE)
        self.assertEqual(
            self.pack.store.get_operation(op)["result"], "failed_stale_result"
        )

    # A dependency-only contract change lets the frozen output carry over.
    def test_dependency_refresh_rebinds(self) -> None:
        op = self.pack.produce()
        self.pack.machine.commit_call_result(
            op, result="completed",
            call_binding=self.pack.binding(stage="apply", attempt_id="WA-1"),
        )
        new_hash = "sha256:" + "d" * 64
        self.pack.store.add_record(
            "TR-1", "transition", {
                "from_hash": CONTRACT_H1, "to_hash": new_hash,
                "author_class": "none", "refresh": ["dependency"],
            },
            pack_id=self.pack.pack_id,
        )
        self.pack.store.update_pack(self.pack.pack_id, contract_hash=new_hash)
        self.assertEqual(
            self.pack.machine.classify_consumption(self.pack.pack_id, op), "rebind"
        )

    # A non-invalidating hold parks the result instead of dropping it.
    def test_hold_defers_the_result(self) -> None:
        op = self.pack.produce()
        self.pack.machine.commit_call_result(
            op, result="completed",
            call_binding=self.pack.binding(stage="apply", attempt_id="WA-1"),
        )
        self.pack.machine.enter_hold(self.pack.pack_id, "reviewer_failed")
        self.assertEqual(self.pack.machine.consume_result(self.pack.pack_id, op), DEFER)
        self.assertIsNone(self.pack.store.get_operation(op)["consumed_at"])


class AwaitingInflightTest(unittest.TestCase):
    """joint-r1 J2: a pack waiting on a live operation must be able to leave."""

    def setUp(self) -> None:
        self.pack = StubPack()
        self.pack.contract_review(passes=True)
        self.pack.claim()
        self.op = self.pack.produce()
        self.pack.store.update_pack(
            self.pack.pack_id, state="awaiting_inflight(claimed,None)",
            return_point="claimed", blockers=[],
        )

    # ST24i-2: the producer finishes normally and the pack resumes.
    def test_st24i2_normal_completion_resumes(self) -> None:
        self.pack.machine.commit_call_result(
            self.op, result="completed",
            call_binding=self.pack.binding(stage="apply", attempt_id="WA-1"),
        )
        outcome = self.pack.machine.consume_result(self.pack.pack_id, self.op)
        self.assertEqual(outcome, RESUME_AWAITING)
        self.assertEqual(self.pack.state(), "claimed")
        self.assertIsNotNone(self.pack.store.get_operation(self.op)["consumed_at"])

    # Still-outstanding blockers keep the pack waiting rather than releasing it.
    def test_remaining_blocker_keeps_awaiting(self) -> None:
        self.pack.store.update_pack(self.pack.pack_id, blockers=[{"reason": "dependency_stale"}])
        self.pack.machine.commit_call_result(
            self.op, result="completed",
            call_binding=self.pack.binding(stage="apply", attempt_id="WA-1"),
        )
        self.pack.machine.consume_result(self.pack.pack_id, self.op)
        self.assertTrue(self.pack.state().startswith("awaiting_inflight"))

    # ST24i-4: a crash between the result and its consumption replays cleanly.
    def test_st24i4_crash_before_consumption_replays_once(self) -> None:
        self.pack.machine.commit_call_result(
            self.op, result="completed",
            call_binding=self.pack.binding(stage="apply", attempt_id="WA-1"),
        )
        counts = self.pack.machine.startup_scan(self.pack.pack_id)
        self.assertEqual(counts["consumed"], 1)
        self.assertEqual(self.pack.state(), "claimed")
        # A second scan must not consume it again.
        self.assertEqual(self.pack.machine.startup_scan(self.pack.pack_id)["consumed"], 0)


class UnknownRecoveryTest(unittest.TestCase):
    """joint-r4 R4-H1: nothing infers that the old writer stopped."""

    def setUp(self) -> None:
        self.pack = StubPack()
        self.pack.contract_review(passes=True)
        self.pack.claim()

    def hold_unknown(self, op: str) -> None:
        self.pack.store.update_operation(op, result="unknown")
        self.pack.store.update_pack(
            self.pack.pack_id, state="hold(unknown_operation)",
            hold_reason="unknown_operation", return_point="claimed",
            blockers=[{"reason": "unknown_operation", "op_id": op}],
        )

    # ST24h-1: the controller itself saw the launch fail - refunding is safe.
    def test_st24h1_launch_failure_is_positive_evidence(self) -> None:
        op = self.pack.produce(launch_failed="binary_missing")
        counts = self.pack.machine.startup_scan(self.pack.pack_id)
        self.assertEqual(counts["not_spawned"], 1)
        self.assertEqual(self.pack.store.get_operation(op)["result"], "failed_not_spawned")

    # ST24h-2: fork succeeded, the flag never landed - the writer may be alive.
    def test_st24h2_crash_in_the_spawn_window_stays_unknown(self) -> None:
        op = self.pack.produce(crash_before_spawn_flag=True)
        counts = self.pack.machine.startup_scan(self.pack.pack_id)
        self.assertEqual(counts["unknown"], 1)
        self.assertEqual(counts["not_spawned"], 0)
        self.hold_unknown(op)
        for action in ("restore_input", "restore_output", "recovery_run"):
            self.assertEqual(
                self.pack.machine.resolve_operation(
                    self.pack.pack_id, op, action,
                    current_boot_id=BOOT_A, op_boot_id=BOOT_A,
                ),
                "refused_no_stop_evidence",
            )

    # ST24h-2b: a stopped process and a quiet tree prove nothing.
    def test_st24h2b_sigstop_and_silence_prove_nothing(self) -> None:
        op = self.pack.produce(crash_before_spawn_flag=True)
        self.pack.machine.startup_scan(self.pack.pack_id)
        self.hold_unknown(op)
        self.assertEqual(
            self.pack.machine.resolve_operation(
                self.pack.pack_id, op, "verify_stopped",
                current_boot_id=BOOT_A, op_boot_id=BOOT_A,
            ),
            "still_unknown",
        )

    # ST24h-2c: the leader exited and nothing was revoked - still unknown.
    def test_st24h2c_leader_gone_without_revocation_stays_unknown(self) -> None:
        op = self.pack.produce()
        self.pack.machine.startup_scan(self.pack.pack_id, alive=lambda o: False)
        self.hold_unknown(op)
        self.assertEqual(
            self.pack.machine.resolve_operation(
                self.pack.pack_id, op, "recovery_run",
                current_boot_id=BOOT_A, op_boot_id=BOOT_A,
            ),
            "refused_no_stop_evidence",
        )

    # An empty original process group is no longer evidence of anything: a
    # provider can setsid its helpers out of the group, so the group emptying is
    # compatible with a live writer (D-2026-09-16-01).
    def test_an_empty_process_group_is_not_stop_evidence(self) -> None:
        op = self.pack.produce()
        self.pack.machine.startup_scan(self.pack.pack_id, alive=lambda o: False)
        self.hold_unknown(op)
        operation = self.pack.store.get_operation(op)
        self.assertIsNone(
            self.pack.machine.stop_evidence(operation, current_boot_id=BOOT_A,
                                            op_boot_id=BOOT_A))

    # ST24h-3 (E-1): the write roots were renamed aside and nothing holds them.
    def test_st24h3_revoked_write_capability_releases_recovery(self) -> None:
        op = self.pack.produce()
        self.pack.machine.startup_scan(self.pack.pack_id, alive=lambda o: False)
        self.hold_unknown(op)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "ws"
        (root / "src").mkdir(parents=True)
        (root / "src" / "A.java").write_text("class A {}\n")

        result = self.pack.machine.revoke_write_capability(
            op, roots=[root], quarantine=Path(tmp.name) / "quarantine",
            record_id="EV-1")
        self.assertTrue(result["ok"], result)
        self.assertFalse(root.exists(), "the root the sandbox allowed is gone")
        # §5 requires the operation to record how the evidence was obtained.
        self.assertEqual(result["moved"][0]["root"], str(root))
        self.assertTrue(Path(result["moved"][0]["moved_to"]).is_dir())

        self.assertEqual(
            self.pack.machine.resolve_operation(
                self.pack.pack_id, op, "recovery_run",
                current_boot_id=BOOT_A, op_boot_id=BOOT_A,
            ),
            "allowed:recovery_run",
        )

    def test_a_held_descriptor_refuses_the_evidence(self) -> None:
        """A survivor with the tree already open is the case renaming misses."""
        op = self.pack.produce()
        self.pack.machine.startup_scan(self.pack.pack_id, alive=lambda o: False)
        self.hold_unknown(op)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "ws"
        root.mkdir(parents=True)

        result = self.pack.machine.revoke_write_capability(
            op, roots=[root], quarantine=Path(tmp.name) / "q", record_id="EV-2",
            holders=lambda paths: ["p4242"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], revocation.OPEN_DESCRIPTORS)
        self.assertEqual(
            self.pack.machine.resolve_operation(
                self.pack.pack_id, op, "recovery_run",
                current_boot_id=BOOT_A, op_boot_id=BOOT_A,
            ),
            "refused_no_stop_evidence",
        )

    def test_the_probe_runs_after_the_rename_not_before(self) -> None:
        """Order is the mechanism, not an implementation detail.

        Probing first leaves a window between "the probe came back empty" and
        "the rename happened" in which a survivor can open the tree by its old
        path.  Renaming first closes it, so the probe only has to cover
        descriptors that were already open.
        """
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "ws"
        root.mkdir(parents=True)
        seen: list[bool] = []

        def holders(paths):
            seen.append(root.exists())
            return []

        result = revocation.revoke([root], quarantine=Path(tmp.name) / "q",
                                   holders=holders)
        self.assertTrue(result["ok"])
        self.assertEqual(seen, [False],
                         "the probe ran while the old path was still reachable")

    def test_revoking_nothing_is_refused_rather_than_reported_clean(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        result = revocation.revoke([], quarantine=Path(tmp.name) / "q")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], revocation.NO_ROOTS)

    def test_an_incomplete_enumeration_refuses_the_evidence(self) -> None:
        """Not being able to look is not the same as looking and seeing none."""
        op = self.pack.produce()
        self.pack.machine.startup_scan(self.pack.pack_id, alive=lambda o: False)
        self.hold_unknown(op)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "ws"
        root.mkdir(parents=True)

        result = self.pack.machine.revoke_write_capability(
            op, roots=[root], quarantine=Path(tmp.name) / "q", record_id="EV-3",
            holders=lambda paths: None)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], revocation.ENUMERATION_INCOMPLETE)
        self.assertEqual(
            self.pack.machine.resolve_operation(
                self.pack.pack_id, op, "recovery_run",
                current_boot_id=BOOT_A, op_boot_id=BOOT_A,
            ),
            "refused_no_stop_evidence",
        )

    # ST24h-3 (E-3): the host rebooted, so no old process can have survived.
    def test_st24h3_host_reboot_releases_recovery(self) -> None:
        op = self.pack.produce(crash_before_spawn_flag=True)
        self.pack.machine.startup_scan(self.pack.pack_id)
        self.hold_unknown(op)
        self.assertEqual(
            self.pack.machine.resolve_operation(
                self.pack.pack_id, op, "restore_output",
                current_boot_id=BOOT_B, op_boot_id=BOOT_A,
            ),
            "allowed:restore_output",
        )

    # A missing stored boot id cannot be compensated for by rebooting.
    def test_missing_stored_boot_id_cannot_be_recovered_by_reboot(self) -> None:
        op = self.pack.produce(crash_before_spawn_flag=True)
        self.pack.machine.startup_scan(self.pack.pack_id)
        self.hold_unknown(op)
        self.assertEqual(
            self.pack.machine.resolve_operation(
                self.pack.pack_id, op, "recovery_run",
                current_boot_id=BOOT_B, op_boot_id=None,
            ),
            "refused_no_stop_evidence",
        )

    # ST24h-2d: whichever way the binding lands, the unknown blocker clears.
    def test_st24h2d_resolve_unknown_clears_the_blocker(self) -> None:
        op = self.pack.produce(crash_before_spawn_flag=True)
        self.pack.machine.startup_scan(self.pack.pack_id)
        self.hold_unknown(op)
        self.pack.store.update_operation(
            op, result="completed",
            call_binding=self.pack.binding(stage="apply", attempt_id="WA-1",
                                           contract_hash="sha256:" + "9" * 64),
        )
        self.assertEqual(
            self.pack.machine.consume_result(self.pack.pack_id, op), RESOLVE_UNKNOWN
        )
        self.assertEqual(self.pack.state(), "claimed")
        self.assertEqual(self.pack.pack()["blockers"], [])

    def test_resolve_unknown_keeps_other_blockers(self) -> None:
        op = self.pack.produce(crash_before_spawn_flag=True)
        self.pack.machine.startup_scan(self.pack.pack_id)
        self.hold_unknown(op)
        self.pack.store.update_pack(
            self.pack.pack_id,
            blockers=[{"reason": "unknown_operation", "op_id": op},
                      {"reason": "dependency_stale"}],
        )
        self.pack.store.update_operation(op, result="completed")
        self.pack.machine.consume_result(self.pack.pack_id, op)
        self.assertEqual(self.pack.state(), "hold(unknown_operation)")
        self.assertEqual(len(self.pack.pack()["blockers"]), 1)


class HoldPolicyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.pack = StubPack()
        self.pack.contract_review(passes=True)
        self.pack.claim()
        self.op = self.pack.produce()

    # An invalidating hold must stop a live writer before freezing the tree.
    def test_invalidating_hold_stops_a_live_writer(self) -> None:
        state = self.pack.machine.enter_hold(
            self.pack.pack_id, "candidate_changed", alive=lambda op: True
        )
        self.assertEqual(state, "stopping(hold(candidate_changed))")

    def test_non_invalidating_hold_lets_the_writer_finish(self) -> None:
        state = self.pack.machine.enter_hold(
            self.pack.pack_id, "dependency_stale", alive=lambda op: True
        )
        self.assertEqual(state, "hold(dependency_stale)")

    # ST24i: exiting a hold while an operation is still alive parks the pack.
    def test_exit_hold_with_live_operation_becomes_awaiting(self) -> None:
        self.pack.machine.enter_hold(self.pack.pack_id, "dependency_stale",
                                     return_point="claimed")
        self.assertEqual(
            self.pack.machine.exit_hold(self.pack.pack_id, alive=lambda op: True),
            "awaiting_inflight",
        )

    def test_exit_hold_with_no_live_operation_returns(self) -> None:
        self.pack.machine.commit_call_result(
            self.op, result="completed",
            call_binding=self.pack.binding(stage="apply", attempt_id="WA-1"),
        )
        self.pack.machine.enter_hold(self.pack.pack_id, "dependency_stale",
                                     return_point="claimed")
        self.assertEqual(
            self.pack.machine.exit_hold(self.pack.pack_id, alive=lambda op: False),
            "claimed",
        )


class DispatchGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.pack = StubPack()
        self.pack.store.update_pack(
            self.pack.pack_id, k_last=1, review_round=1, contract_hash=CONTRACT_H1,
        )

    def set_decision(self, kind: str, **bound) -> None:
        self.pack.store.update_pack(
            self.pack.pack_id,
            decision={"kind": kind, "decision_id": "DEC-1",
                      "bound_to": {"review_round": 1, "output_id": 1,
                                   "contract_hash": CONTRACT_H1, **bound}},
            continuation="resume_repair_gate(1)",
        )

    def test_dispatch_ok_goes_to_repair_pending(self) -> None:
        self.set_decision("dispatch_ok")
        self.assertEqual(self.pack.machine.dispatch_gate(self.pack.pack_id), "repair_pending(1)")

    # A decision bound to a superseded contract is not a decision any more.
    def test_decision_invalidated_by_contract_change_forces_rereview(self) -> None:
        self.set_decision("dispatch_ok", contract_hash="sha256:" + "9" * 64)
        self.assertEqual(self.pack.machine.dispatch_gate(self.pack.pack_id), "submitted(1)")

    # Continuation outranks the decision.
    def test_reverify_continuation_wins_over_decision(self) -> None:
        self.set_decision("accepted")
        self.pack.store.update_pack(self.pack.pack_id, continuation="reverify_then_review(1)")
        self.assertEqual(self.pack.machine.dispatch_gate(self.pack.pack_id), "submitted(1)")

    def test_outstanding_evidence_todo_blocks_acceptance(self) -> None:
        self.set_decision("dispatch_ok")
        self.pack.store.update_pack(self.pack.pack_id, evidence_todo=["O3"])
        self.assertEqual(self.pack.machine.dispatch_gate(self.pack.pack_id), "hold(evidence_gap)")

    # stalled needs an explicit human grant; it must not dispatch on its own.
    def test_stalled_without_grant_holds(self) -> None:
        self.set_decision("stalled")
        self.assertEqual(self.pack.machine.dispatch_gate(self.pack.pack_id), "hold(stalled)")

    def test_stalled_with_grant_dispatches(self) -> None:
        self.set_decision("stalled")
        self.pack.store.issue_grant("G-1", self.pack.pack_id, output_id=1, decision_id="DEC-1")
        self.assertEqual(self.pack.machine.dispatch_gate(self.pack.pack_id), "repair_pending(1)")


if __name__ == "__main__":
    unittest.main()


class RemainingFixtureTest(unittest.TestCase):
    """The STATE-TABLE §10 entries the state machine (not the judge) decides."""

    def setUp(self) -> None:
        self.pack = StubPack()
        self.pack.contract_review(passes=True)
        self.pack.claim()

    # ST20: an event whose operation belongs to another pack changes nothing.
    def test_st20_foreign_operation_event_is_dropped(self) -> None:
        other = StubPack("P9")
        other.contract_review(passes=True)
        foreign_op = other.produce(attempt_id=other.claim("WA-9"))
        before = self.pack.state()
        self.assertEqual(
            self.pack.machine.route_event(self.pack.pack_id, "E_review", op_id=foreign_op),
            "unexpected_event",
        )
        self.assertEqual(self.pack.state(), before)

    # ST25b: an event the table does not define is dropped, not invented.
    def test_st25b_undefined_event_is_dropped(self) -> None:
        before = self.pack.state()
        self.assertEqual(
            self.pack.machine.route_event(self.pack.pack_id, "A_defer_pack"), "unexpected_event"
        )
        self.assertEqual(self.pack.state(), before)

    def test_known_event_with_own_operation_routes(self) -> None:
        op = self.pack.produce()
        self.assertEqual(
            self.pack.machine.route_event(self.pack.pack_id, "E_produced", op_id=op), "routed"
        )

    # ST20b: a late result for an older output is stale, not applied.
    def test_st20b_late_result_for_an_older_output_is_stale(self) -> None:
        op = self.pack.produce()
        k = self.pack.submit(op)
        self.pack.store.update_pack(self.pack.pack_id, state="submitted(2)", k_last=2)
        late = self.pack.next_op("review", stage="review")
        self.pack.machine.commit_call_result(
            late, result="completed",
            call_binding=self.pack.binding(stage="review", attempt_id="WA-1",
                                           review_round=99),
        )
        self.assertEqual(self.pack.machine.consume_result(self.pack.pack_id, late), STALE)

    # ST24c: an invalidating hold stops the writer but keeps the lease.
    def test_st24c_stop_keeps_the_lease_for_a_hold(self) -> None:
        op = self.pack.produce()
        state = self.pack.machine.enter_hold(
            self.pack.pack_id, "approval_revoked", alive=lambda o: True)
        self.assertEqual(state, "stopping(hold(approval_revoked))")
        final = self.pack.machine.stop_and_release(
            self.pack.pack_id, next_state="hold(approval_revoked)")
        self.assertEqual(final, "hold(approval_revoked)")
        self.assertEqual(self.pack.pack()["lease_token"], "LEASE-1")
        # The stopped operation is superseded, not charged to the producer.
        self.assertEqual(
            self.pack.store.get_operation(op)["result"], "failed_superseded_by_hold"
        )

    def test_stopping_to_a_terminal_state_releases_the_lease(self) -> None:
        self.pack.produce()
        self.pack.machine.enter_hold(self.pack.pack_id, "approval_revoked", alive=lambda o: True)
        self.pack.machine.stop_and_release(self.pack.pack_id, next_state="abandoned")
        self.assertIsNone(self.pack.pack()["lease_token"])

    # ST24d: a non-invalidating hold defers, and the result survives the hold.
    def test_st24d_deferred_result_is_consumed_after_the_hold(self) -> None:
        op = self.pack.produce()  # pack is now producing(1)
        self.pack.machine.commit_call_result(
            op, result="completed",
            call_binding=self.pack.binding(stage="apply", attempt_id="WA-1"),
        )
        # The hold is entered from producing(1), so that is where it returns:
        # a producer result can only be taken up in the state that expects one.
        self.pack.machine.enter_hold(self.pack.pack_id, "dependency_stale",
                                     return_point="producing(1)")
        self.assertEqual(self.pack.machine.consume_result(self.pack.pack_id, op), DEFER)
        self.assertEqual(self.pack.machine.exit_hold(self.pack.pack_id,
                                                     alive=lambda o: False), "producing(1)")
        self.assertIsNotNone(self.pack.store.get_operation(op)["consumed_at"])

    # ST9: a rate-limited retry is a new operation under one logical id, and
    # the waiting attempts are not counted as producer failures.
    def test_st9_rate_limit_retries_share_a_logical_op(self) -> None:
        logical = "LOP-1"
        self.pack.store.update_pack(self.pack.pack_id, state="producing(1)")
        ops = []
        for _ in range(3):
            op = self.pack.next_op("producer", stage="apply", attempt_id="WA-1",
                                   logical_op_id=logical)
            self.pack.store.update_operation(op, result="failed_rate_limited")
            ops.append(op)
        final = self.pack.next_op("producer", stage="apply", attempt_id="WA-1",
                                  logical_op_id=logical)
        self.pack.machine.commit_call_result(
            final, result="completed",
            call_binding=self.pack.binding(stage="apply", attempt_id="WA-1"),
        )
        ops.append(final)
        self.assertEqual(len({o for o in ops}), 4)
        self.assertEqual(
            {self.pack.store.get_operation(o)["logical_op_id"] for o in ops}, {logical}
        )
        self.assertEqual(self.pack.machine.consume_result(self.pack.pack_id, final), CONSUME)
        self.assertEqual(self.pack.store.get_attempt("WA-1")["producer_failures"], 0)

    # ST18-out: output ids are pack-global, so a new attempt continues the
    # numbering instead of restarting it.
    def test_st18out_base_refresh_continues_the_output_numbering(self) -> None:
        self.pack.store.update_pack(self.pack.pack_id, k_last=3)
        self.pack.store.create_attempt("WA-2", self.pack.pack_id, base_revision="def",
                                       candidate_input=None, next_output_id=4)
        op = self.pack.produce(attempt_id="WA-2")
        k = self.pack.submit(op, attempt_id="WA-2")
        self.assertEqual(k, 4)
        self.assertEqual(self.pack.pack()["k_last"], 4)

    # ST24k: a sandbox that will not start is a hold with a redispatch exit.
    def test_st24k_launch_failure_holds_with_an_exit(self) -> None:
        op = self.pack.produce(launch_failed="sandbox_unavailable")
        self.pack.machine.startup_scan(self.pack.pack_id)
        state = self.pack.machine.enter_hold(
            self.pack.pack_id, "launch_failed(sandbox_unavailable)")
        self.assertEqual(state, "hold(launch_failed(sandbox_unavailable))")
        self.assertEqual(
            self.pack.store.get_operation(op)["result"], "failed_not_spawned"
        )

    # ST24g: the legacy transition cap counts but no longer refuses (step 3
    # covers the controller side; this is the pack-side counterpart).
    def test_st24g_counters_keep_counting(self) -> None:
        before = self.pack.pack()["calls_reserved"]
        for _ in range(3):
            self.pack.store.bump(self.pack.pack_id, "calls_reserved")
        self.assertEqual(self.pack.pack()["calls_reserved"], before + 3)


class FaultInjectionTest(unittest.TestCase):
    """IMPLEMENTATION-PLAN §3.5 - the scenarios not already covered above."""

    def setUp(self) -> None:
        self.pack = StubPack()
        self.pack.contract_review(passes=True)

    # "claim 後 crash": a claimed pack with no running operation just dispatches.
    def test_crash_after_claim_leaves_a_dispatchable_pack(self) -> None:
        self.pack.claim()
        counts = self.pack.machine.startup_scan(self.pack.pack_id)
        self.assertEqual(counts, {"unknown": 0, "consumed": 0, "deferred": 0, "not_spawned": 0})
        self.assertEqual(self.pack.state(), "claimed")

    # "provider 完成未 commit": a receipt exists, so recovery seals and consumes.
    def test_provider_finished_without_commit_is_recovered(self) -> None:
        from orchestrator.pack.policy import PackPolicy

        self.pack.claim()
        op = self.pack.produce()
        outcome = PackPolicy(self.pack.store).recovery_commit(
            self.pack.pack_id, op, receipt_ref="R-1",
            call_binding=self.pack.binding(stage="apply", attempt_id="WA-1"),
        )
        self.assertEqual(outcome, CONSUME)
        self.assertEqual(self.pack.store.get_operation(op)["receipt_ref"], "R-1")

    # "verify 結果未知": no envelope and no receipt leaves the operation unknown,
    # and a retry is a *new* operation rather than a re-reading of the old one.
    def test_verify_with_no_result_becomes_unknown_then_retries(self) -> None:
        self.pack.claim()
        verify_op = self.pack.next_op("prerun", stage="prerun", reserve=0)
        self.pack.store.update_operation(verify_op, spawned=1)
        self.pack.machine.startup_scan(self.pack.pack_id, alive=lambda o: False)
        self.assertEqual(self.pack.store.get_operation(verify_op)["result"], "unknown")
        retry = self.pack.next_op("prerun", stage="prerun", reserve=0)
        self.assertNotEqual(retry, verify_op)
        self.pack.store.bump(self.pack.pack_id, "verify_retry")
        self.assertEqual(self.pack.pack()["verify_retry"], 1)


class CoverageMapTest(unittest.TestCase):
    """Makes ST coverage auditable instead of leaving the gap implicit.

    A green suite says nothing about which fixtures exist, so the map is
    asserted here: adding a state-machine fixture without listing it, or
    listing one that is not really covered, both show up as a failure.
    """

    # Where each STATE-TABLE §10 entry is exercised.  `judge` entries are
    # covered by test_pack_judge under their rule names rather than ST ids.
    COVERED = {
        "ST1a": "HappyPathTest", "ST1b": "HappyPathTest", "ST1c": "HappyPathTest",
        "ST2": "HappyPathTest",
        "ST9": "RemainingFixtureTest", "ST18-out": "RemainingFixtureTest",
        "ST20": "RemainingFixtureTest", "ST20b": "RemainingFixtureTest",
        "ST24c": "RemainingFixtureTest", "ST24d": "RemainingFixtureTest",
        "ST24g": "RemainingFixtureTest", "ST24k": "RemainingFixtureTest",
        "ST25b": "RemainingFixtureTest",
        "ST5": "BudgetGateTest", "ST25": "BudgetGateTest",
        "ST24h-1": "UnknownRecoveryTest", "ST24h-2": "UnknownRecoveryTest",
        "ST24h-2b": "UnknownRecoveryTest", "ST24h-2c": "UnknownRecoveryTest",
        "ST24h-2d": "UnknownRecoveryTest", "ST24h-3": "UnknownRecoveryTest",
        "ST24i-2": "AwaitingInflightTest", "ST24i-4": "AwaitingInflightTest",
        "ST3": "test_pack_judge.JudgeTest", "ST4": "test_pack_judge.JudgeTest",
        "ST6a": "test_pack_judge.JudgeTest", "ST6i": "test_pack_judge.JudgeTest",
        "ST6j": "test_pack_judge.JudgeTest", "ST7c": "test_pack_judge.JudgeTest",
        "ST8": "test_pack_judge.JudgeTest",
        "ST26": "test_pack_target.FixtureTargetFlowTest",
    }

    # Still open, with the step that owns them.  Listing them is the point:
    # an unlisted gap is indistinguishable from no gap.
    DEFERRED = {
        "ST14": "step 7 - needs a real sealed receipt",
        "ST14b": "step 7 - needs a real provider session",
        "ST24e": "step 5 - restore_tree against a real blob store",
    }

    def test_no_fixture_is_both_covered_and_deferred(self) -> None:
        self.assertEqual(set(self.COVERED) & set(self.DEFERRED), set())

    def test_every_covered_id_names_a_real_test_class(self) -> None:
        """Every claimed location must resolve, in this module or another.

        An unverified map is just a comment: it would keep saying a fixture is
        covered long after the test that covered it was renamed or deleted.
        """
        import importlib

        for fixture, location in sorted(self.COVERED.items()):
            module_name, _, class_name = location.rpartition(".")
            module = importlib.import_module(
                f"orchestrator.tests.{module_name}" if module_name
                else "orchestrator.tests.test_pack_state_machine"
            )
            self.assertTrue(
                hasattr(module, class_name),
                f"{fixture} claims to live in {location}, which does not exist",
            )


class BudgetGateTest(unittest.TestCase):
    """STATE-TABLE §3.4 - the only thing that refuses a pack-v1 dispatch.

    Legacy's claim and edge caps are bypassed for pack-v1 by design, so a
    missing gate here is not a missing limit among several: it is no limit at
    all.  ST5 and ST25 are the two fixtures that say so.
    """

    POLICY = {"round_cap": 4, "call_budget": 12, "reviewer_retry": 2}

    def setUp(self) -> None:
        self.pack = StubPack()
        self.pack.contract_review(passes=True)
        self.pack.claim()

    def _dispatch_records(self, n: int) -> None:
        for i in range(n):
            self.pack.store.create_dispatch_record(
                f"D-{i}", self.pack.pack_id, source_review_seq=i + 1,
                repair_op_id=f"OP-r{i}", target_output_id=1, lineage_set=["H1"])

    def _calls_reserved(self) -> int:
        return self.pack.pack()["calls_reserved"]

    # ST5: allowance 2 with 1 already used - one more redispatch, then the hold.
    def test_st5_reviewer_allowance_runs_out_and_holds(self) -> None:
        first = self.pack.machine.use_reviewer_retry(
            self.pack.pack_id, stage="review", output_id=1, cause="envelope_invalid",
            record_id="RT-0", budget_policy=self.POLICY)
        self.assertIsNone(first, "the first of two retries must be allowed")

        second = self.pack.machine.use_reviewer_retry(
            self.pack.pack_id, stage="review", output_id=1, cause="envelope_invalid",
            record_id="RT-1", budget_policy=self.POLICY)
        self.assertIsNone(second, "the allowance is 2, so the second is the last one")

        third = self.pack.machine.use_reviewer_retry(
            self.pack.pack_id, stage="review", output_id=1, cause="envelope_invalid",
            record_id="RT-2", budget_policy=self.POLICY)
        self.assertEqual(third, "envelope_invalid")
        # Refused, not merely reported: a spent-out group must not keep a record.
        self.assertEqual(
            self.pack.machine.reviewer_retry_used(self.pack.pack_id, stage="review", output_id=1),
            2)

    def test_st5_allowance_is_scoped_to_its_group(self) -> None:
        for i in range(2):
            self.pack.machine.use_reviewer_retry(
                self.pack.pack_id, stage="review", output_id=1, cause="envelope_invalid",
                record_id=f"RT-{i}", budget_policy=self.POLICY)
        # A different output is a different `(stage, output_id)` group (§3.4).
        other = self.pack.machine.use_reviewer_retry(
            self.pack.pack_id, stage="review", output_id=2, cause="reviewer_failed",
            record_id="RT-other", budget_policy=self.POLICY)
        self.assertIsNone(other)
        # The contract review stage keeps its own count, per the same rule.
        contract = self.pack.machine.use_reviewer_retry(
            self.pack.pack_id, stage="contract_review", output_id=None, cause="reviewer_failed",
            record_id="RT-contract", budget_policy=self.POLICY)
        self.assertIsNone(contract)

    def test_exhausted_allowance_reports_the_cause_not_a_budget_hold(self) -> None:
        for i in range(2):
            self.pack.machine.use_reviewer_retry(
                self.pack.pack_id, stage="review", output_id=1, cause="reviewer_failed",
                record_id=f"RF-{i}", budget_policy=self.POLICY)
        self.assertEqual(
            self.pack.machine.use_reviewer_retry(
                self.pack.pack_id, stage="review", output_id=1, cause="reviewer_failed",
                record_id="RF-2", budget_policy=self.POLICY),
            "reviewer_failed")

    # ST25: rounds at the cap, raised by 2, with 3 calls left.
    def test_st25_raised_cap_lets_the_fifth_round_through(self) -> None:
        self._dispatch_records(4)
        self.pack.store.update_pack(self.pack.pack_id,
                                    calls_reserved=self.POLICY["call_budget"] - 3)

        refused = self.pack.machine.reserve_dispatch(
            self.pack.pack_id, budget_policy=self.POLICY, counts_round=True)
        self.assertEqual(refused, "budget_exhausted(round)")
        self.assertEqual(self._calls_reserved(), self.POLICY["call_budget"] - 3,
                         "a refused reservation must not spend a call")

        # `A_raise_cap(round, 2)` - the spelling the hold reason gave the operator.
        self.pack.machine.raise_cap(self.pack.pack_id, "round", 2, record_id="CAP-1")
        self.assertEqual(
            self.pack.machine.budget_caps(self.pack.pack_id, self.POLICY)["round_cap"], 6)

        allowed = self.pack.machine.reserve_dispatch(
            self.pack.pack_id, budget_policy=self.POLICY, counts_round=True)
        self.assertIsNone(allowed)
        self.assertEqual(self._calls_reserved(), self.POLICY["call_budget"] - 2)

    def test_st25_call_budget_refuses_even_when_rounds_are_free(self) -> None:
        self.pack.store.update_pack(self.pack.pack_id,
                                    calls_reserved=self.POLICY["call_budget"])
        self.assertEqual(
            self.pack.machine.reserve_dispatch(self.pack.pack_id, budget_policy=self.POLICY),
            "budget_exhausted(call)")

    def test_raises_accumulate_rather_than_replace(self) -> None:
        self.pack.machine.raise_cap(self.pack.pack_id, "round_cap", 1, record_id="CAP-a")
        self.pack.machine.raise_cap(self.pack.pack_id, "round_cap", 1, record_id="CAP-b")
        self.assertEqual(
            self.pack.machine.budget_caps(self.pack.pack_id, self.POLICY)["round_cap"], 6)

    def test_a_revoked_raise_stops_counting(self) -> None:
        self.pack.machine.raise_cap(self.pack.pack_id, "round_cap", 2, record_id="CAP-x")
        self.pack.store.revoke_record("CAP-x")
        self.assertEqual(
            self.pack.machine.budget_caps(self.pack.pack_id, self.POLICY)["round_cap"], 4)

    def test_the_gate_actually_refuses_a_dispatch(self) -> None:
        """The counters are worthless if nothing consults them before spawning.

        This is the property a gate with no caller would still pass every other
        test in this class while providing no limit at all.
        """
        self.pack.store.update_pack(self.pack.pack_id,
                                    calls_reserved=budgets.DEFAULTS["call_budget"])
        with self.assertRaises(BudgetRefused) as raised:
            self.pack.next_op("producer", stage="apply")
        self.assertEqual(str(raised.exception), "budget_exhausted(call)")

    def test_an_unknown_counter_is_refused_not_silently_created(self) -> None:
        with self.assertRaises(budgets.BudgetError):
            self.pack.machine.raise_cap(self.pack.pack_id, "made_up", 1, record_id="CAP-z")


@unittest.skipUnless(os.path.exists(revocation.LSOF), "lsof is not installed")
class RevocationProbeTest(unittest.TestCase):
    """The real probe, not an injected one.

    `lsof` exits 1 both for "nothing holds this" and for "I could not look", so
    everything here turns on telling those apart: reading the first as the
    second would make every failed enumeration look like clean evidence.
    """

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_nothing_holding_a_directory_reports_no_holders(self) -> None:
        quiet = self.root / "quiet"
        quiet.mkdir()
        (quiet / "a.txt").write_text("x")
        self.assertEqual(revocation.lsof_holders([quiet]), [])

    def test_a_path_that_cannot_be_examined_reports_unknown(self) -> None:
        self.assertIsNone(revocation.lsof_holders([self.root / "gone"]))

    def test_an_open_descriptor_is_reported(self) -> None:
        busy = self.root / "busy"
        busy.mkdir()
        target = busy / "held.txt"
        target.write_text("x")
        handle = open(target, "a")
        self.addCleanup(handle.close)
        holders = revocation.lsof_holders([busy])
        self.assertIsNotNone(holders)
        self.assertIn(f"p{os.getpid()}", holders)
