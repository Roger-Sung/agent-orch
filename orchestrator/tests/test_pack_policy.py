"""Verification gate, primitives and `orch pack status` (step 1)."""
from __future__ import annotations

import unittest

from orchestrator.pack.policy import (
    ALLOWED_OUTCOMES,
    PackPolicy,
    allowed_outcomes,
    is_pack_v1,
    pack_status,
    render_status,
)
from orchestrator.pack.verification import (
    GateResult,
    Sample,
    first_failure,
    run_gate,
    triage_event,
)
from orchestrator.tests.pack_stub import CONTRACT_H1, StubPack, envelope, high_finding


class VerificationGateTest(unittest.TestCase):
    def samples(self, **overrides) -> dict[str, Sample]:
        base = {
            "approval": Sample.of({"revoked": False}),
            "dependencies": Sample.of({}),
            "requirement": Sample.of("sha256:r"),
            "manifest": Sample.of("sha256:m"),
            "environment": Sample.of("sha256:e"),
            "candidate": Sample.of("sha256:c"),
        }
        base.update(overrides)
        return base

    def expected(self, **overrides) -> dict:
        base = {
            "requirement": "sha256:r",
            "manifest": "sha256:m",
            "environment": "sha256:e",
            "candidate_input": "sha256:c",
            "candidate_output": "sha256:c",
        }
        base.update(overrides)
        return base

    def test_clean_sample_passes(self) -> None:
        result = run_gate("compare_input", self.samples(), expected=self.expected())
        self.assertTrue(result.passed)

    # A' order is fixed so the reported code does not depend on dict order.
    def test_failure_priority_is_fixed(self) -> None:
        samples = self.samples(
            requirement=Sample.failed("requirement_unavailable"),
            manifest=Sample.failed("manifest_unreadable"),
            environment=Sample.failed("environment_unavailable"),
        )
        self.assertEqual(first_failure(samples), "manifest_unreadable")

    def test_candidate_rejection_ranks_after_the_three(self) -> None:
        samples = self.samples(
            candidate=Sample.failed("unsupported_entry"),
            requirement=Sample.failed("requirement_unavailable"),
        )
        self.assertEqual(first_failure(samples), "requirement_unavailable")
        samples = self.samples(candidate=Sample.failed("unsupported_entry"))
        self.assertEqual(first_failure(samples), "unsupported_entry")

    # N5-8: a sampling failure outranks a stale approval binding.
    def test_sampling_failure_beats_validity(self) -> None:
        samples = self.samples(
            requirement=Sample.failed("requirement_unavailable"),
            approval=Sample.of({"revoked": False, "requirement": "sha256:old"}),
        )
        result = run_gate("compare_input", samples, expected=self.expected())
        self.assertEqual(result.code, "requirement_unavailable")

    def test_revoked_approval_is_caught(self) -> None:
        result = run_gate(
            "compare_input", self.samples(approval=Sample.of({"revoked": True})),
            expected=self.expected(),
        )
        self.assertEqual(result.code, "approval_revoked")

    # baseline runs validity but has no contract baseline to compare against.
    def test_baseline_skips_comparison_but_keeps_validity(self) -> None:
        samples = self.samples(requirement=Sample.of("sha256:different"))
        self.assertTrue(run_gate("baseline", samples, expected=self.expected()).passed)
        revoked = self.samples(approval=Sample.of({"revoked": True}))
        self.assertEqual(run_gate("baseline", revoked).code, "approval_revoked")

    def test_candidate_drift_is_detected(self) -> None:
        samples = self.samples(candidate=Sample.of("sha256:moved"))
        result = run_gate("compare_output", samples, expected=self.expected())
        self.assertEqual(result.code, "candidate_changed")

    def test_dependency_expiry(self) -> None:
        samples = self.samples(dependencies=Sample.of({"P1": {"revoked": False, "expired": True}}))
        result = run_gate("compare_input", samples, expected=self.expected())
        self.assertEqual(result.code, "dependency_expired")

    # event_gate and reconcile are triaged in A0, never as a stage-B mode.
    def test_event_modes_are_rejected_here(self) -> None:
        for mode in ("event_gate", "reconcile"):
            with self.assertRaises(ValueError):
                run_gate(mode, self.samples())


class EventTriageTest(unittest.TestCase):
    """joint-r1 J3: the repair must be reachable while sampling still fails."""

    def test_a0_runs_before_any_sampling(self) -> None:
        executed: list[str] = []
        result = triage_event(
            {"event_id": "E-1", "kind": "A_restore_tree"},
            verify_record=lambda e: None,
            execute=lambda e: executed.append(e["kind"]),
        )
        self.assertTrue(result.passed)
        self.assertEqual(executed, ["A_restore_tree"])

    # ID41c: a scope-mismatched event is refused before anything is written.
    def test_scope_mismatch_refuses_before_execution(self) -> None:
        executed: list[str] = []
        result = triage_event(
            {"event_id": "E-1", "kind": "A_restore_tree"},
            verify_record=lambda e: "approval_scope_mismatch",
            execute=lambda e: executed.append(e["kind"]),
        )
        self.assertEqual(result.code, "approval_scope_mismatch")
        self.assertEqual(executed, [])

    # ID41c-2: a stale `expected` is `event_stale`, not a dependency expiry.
    def test_stale_expected_uses_event_stale(self) -> None:
        result = triage_event(
            {"event_id": "E-1", "kind": "A_restore_tree"},
            verify_record=lambda e: "event_stale",
            execute=lambda e: None,
        )
        self.assertEqual(result.code, "event_stale")


class PrimitivesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.pack = StubPack()
        self.policy = PackPolicy(self.pack.store)
        self.pack.contract_review(passes=True)
        self.pack.claim()

    def test_is_pack_v1_defaults_to_legacy(self) -> None:
        self.assertFalse(is_pack_v1({}))
        self.assertFalse(is_pack_v1({"policy_version": "execution-v1"}))
        self.assertTrue(is_pack_v1({"policy_version": "pack-v1"}))

    # §3.2a: the allowed set is a constant, not derived from envelope presence.
    def test_allowed_outcomes_are_per_stage_constants(self) -> None:
        self.assertIn("produced", allowed_outcomes("apply"))
        self.assertIn("contract_pass", allowed_outcomes("contract_review"))
        self.assertNotIn("produced", allowed_outcomes("review"))
        with self.assertRaises(KeyError):
            allowed_outcomes("not_a_stage")

    def test_recovery_commit_is_idempotent(self) -> None:
        op = self.pack.produce()
        binding = self.pack.binding(stage="apply", attempt_id="WA-1")
        first = self.policy.recovery_commit(
            self.pack.pack_id, op, receipt_ref="R-1", call_binding=binding
        )
        second = self.policy.recovery_commit(
            self.pack.pack_id, op, receipt_ref="R-1", call_binding=binding
        )
        self.assertEqual(first, "consume")
        self.assertEqual(second, "already_consumed")

    # Revoking an acceptance must also raise the floor, or the same round would
    # simply grant it again.
    def test_invalidate_acceptance_raises_the_floor(self) -> None:
        self.pack.store.update_pack(self.pack.pack_id, state="accepted", review_round=3)
        generation = self.policy.invalidate_acceptance(
            self.pack.pack_id, reason="revoked_by_upstream"
        )
        stored = self.pack.pack()
        self.assertEqual(generation, 1)
        self.assertEqual(stored["state"], "hold(revoked_by_upstream)")
        self.assertEqual(stored["acceptance_floor_round"], 3)
        self.assertIsNone(stored["decision"])

    def test_transition_pack_task_needs_no_active_run(self) -> None:
        self.policy.transition_pack_task(
            self.pack.pack_id, "hold(dependency_stale)",
            hold_reason="dependency_stale", return_point="claimed",
        )
        self.assertEqual(self.pack.state(), "hold(dependency_stale)")


class StatusTest(unittest.TestCase):
    def test_status_is_readable_after_a_full_round(self) -> None:
        pack = StubPack()
        pack.contract_review(passes=True)
        pack.claim()
        k = pack.submit(pack.produce())
        pack.prerun(k)
        pack.review(
            k,
            envelope(1, verdict="needs_repair", obligations={"O1": "FAIL"},
                     findings=[high_finding("F1-1", "L-1", "O1")]),
        )
        status = pack_status(pack.store, pack.pack_id)
        self.assertEqual(status["state"], "repair_pending(1)")
        self.assertEqual(status["review_round"], 1)
        self.assertEqual(status["output_id"], 1)
        self.assertEqual(status["attempt"], "WA-1")
        self.assertEqual(status["decision"]["kind"], "dispatch_ok")

        text = render_status(status)
        self.assertIn("repair_pending(1)", text)
        self.assertIn("WA-1", text)
        self.assertIn("dispatch_ok", text)

    def test_status_surfaces_a_hold_and_its_exit(self) -> None:
        pack = StubPack()
        pack.contract_review(passes=False)
        status = pack_status(pack.store, pack.pack_id)
        self.assertEqual(status["hold_reason"], "contract_hold")
        self.assertIn("hold reason", render_status(status))


if __name__ == "__main__":
    unittest.main()
