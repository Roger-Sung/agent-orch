"""STATE-TABLE §4 convergence decisions (judge-v1) - step 0a."""
from __future__ import annotations

import unittest

from orchestrator.pack.judge import (
    Decision,
    JudgeInputMissing,
    blocking_lineages,
    closure,
    fold_dispositions,
    judge_v1,
    oscillation_hits,
)


def finding(fid: str, lineages: list[str], severity: str = "High", **extra) -> dict:
    base = {
        "id": fid,
        "severity": severity,
        "blocking": True,
        "lineage_ids": lineages,
        "prior_refs": [],
        "recurrence_of": None,
    }
    base.update(extra)
    return base


def envelope(round_no: int, findings: list[dict], *, verdict: str = "needs_repair",
             dispositions: dict | None = None) -> dict:
    return {
        "review_round": round_no,
        "verdict": verdict,
        "findings": findings,
        "prior_round": None if dispositions is None else {"dispositions": dispositions},
    }


def resolved(basis: str = "OBS-3") -> dict:
    return {"disposition": "resolved", "successors": [], "basis": [basis], "reason": None}


def residual(successor: str) -> dict:
    return {"disposition": "residual", "successors": [successor], "basis": [], "reason": None}


class JudgeTest(unittest.TestCase):
    def test_step0_incomplete_input_raises(self) -> None:
        with self.assertRaises(JudgeInputMissing):
            judge_v1(envelope(1, []), history=[], dispatch_record=None, inputs_complete=False)

    # Step 3: the first sealed review has nothing to compare against.
    def test_step3_baseline(self) -> None:
        decision = judge_v1(envelope(1, [finding("F1-1", ["L-1"])]), history=[], dispatch_record=None)
        self.assertEqual(decision.kind, "baseline")
        self.assertTrue(decision.dispatch_ok)

    # Step 2: acceptance, and the floor that stops an old round re-granting it.
    def test_step2_accepted(self) -> None:
        decision = judge_v1(
            envelope(3, [], verdict="accepted"),
            history=[envelope(1, []), envelope(2, [])],
            dispatch_record={"lineage_set": ["L-1"]},
            acceptance_floor_round=2,
        )
        self.assertEqual(decision.kind, "accepted")

    def test_step2_accepted_below_floor_is_stalled(self) -> None:
        decision = judge_v1(
            envelope(2, [], verdict="accepted"),
            history=[envelope(1, [])],
            dispatch_record={"lineage_set": ["L-1"]},
            acceptance_floor_round=3,
        )
        self.assertEqual(decision.kind, "stalled")

    # Step 1 outranks step 2: a regression cannot be accepted away.
    def test_step1_oscillation_beats_acceptance(self) -> None:
        history = [envelope(1, [finding("F1-1", ["L-1"])]),
                   envelope(2, [], dispositions={"F1-1": {"L-1": resolved()}})]
        current = envelope(3, [finding("F3-1", ["L-1"])], verdict="accepted")
        decision = judge_v1(current, history=history, dispatch_record={"lineage_set": ["L-1"]})
        self.assertEqual(decision.kind, "oscillating")
        self.assertEqual(decision.detail["lineages"], ["L-1"])

    # Step 4b: findings that first appear in round >= 2 with no dispatch record
    # were never handed to the producer, so there is nothing to have improved.
    def test_step4b_no_dispatch_and_no_origin(self) -> None:
        decision = judge_v1(
            envelope(2, [finding("F2-1", ["L-1"])]),
            history=[envelope(1, [])],
            dispatch_record=None,
        )
        self.assertEqual(decision.kind, "stalled")

    # Step 4a: a round-1 contract hold can still authorise the first dispatch,
    # provided every current lineage traces back to that origin.
    def test_step4a_first_dispatch_within_closure(self) -> None:
        r1 = envelope(1, [finding("F1-1", ["L-1"])])
        current = envelope(
            2,
            [finding("F2-1", ["L-1"], prior_refs=[{"finding_id": "F1-1", "lineage_id": "L-1"}])],
            dispositions={"F1-1": {"L-1": residual("F2-1")}},
        )
        decision = judge_v1(
            current, history=[r1], dispatch_record=None,
            first_dispatch_origin={"lineage_set": ["L-1"]},
        )
        self.assertEqual(decision.kind, "first_dispatch")
        self.assertTrue(decision.dispatch_ok)

    def test_step4a_rejects_lineage_outside_the_origin(self) -> None:
        r1 = envelope(1, [finding("F1-1", ["L-1"])])
        current = envelope(2, [finding("F2-9", ["L-9"])],
                           dispositions={"F1-1": {"L-1": resolved()}})
        decision = judge_v1(
            current, history=[r1], dispatch_record=None,
            first_dispatch_origin={"lineage_set": ["L-1"]},
        )
        self.assertEqual(decision.kind, "stalled")
        self.assertEqual(decision.detail["outside"], ["L-9"])

    # Step 5: a lineage the producer never received is not its failure.
    def test_step5_new_lineage_versus_dispatch(self) -> None:
        decision = judge_v1(
            envelope(2, [finding("F2-1", ["L-1"]), finding("F2-2", ["L-7"])]),
            history=[envelope(1, [finding("F1-1", ["L-1"])])],
            dispatch_record={"lineage_set": ["L-1"]},
        )
        self.assertEqual(decision.kind, "stalled")
        self.assertEqual(decision.detail["new_vs_dispatch"], ["L-7"])

    # Step 7: the top severity fell.
    def test_step7_improved_by_severity(self) -> None:
        decision = judge_v1(
            envelope(2, [finding("F2-1", ["L-1"], severity="Medium")]),
            history=[envelope(1, [finding("F1-1", ["L-1"], severity="High")])],
            dispatch_record={"lineage_set": ["L-1", "L-2"]},
            rounds_since_dispatch=[],
        )
        self.assertEqual(decision.kind, "improved")
        self.assertTrue(decision.dispatch_ok)

    # Step 7: same top severity but fewer at that level.
    def test_step7_improved_by_count(self) -> None:
        decision = judge_v1(
            envelope(2, [finding("F2-1", ["L-1"])]),
            history=[envelope(1, [finding("F1-1", ["L-1"]), finding("F1-2", ["L-2"])])],
            dispatch_record={"lineage_set": ["L-1", "L-2"]},
        )
        self.assertEqual(decision.kind, "improved")

    # Step 8: the same count at the same level is not progress, whatever the
    # reviewer's prose says (D-2026-09-14-11).
    def test_step8_same_count_same_severity_is_stalled(self) -> None:
        decision = judge_v1(
            envelope(2, [finding("F2-1", ["L-1"]), finding("F2-2", ["L-2"])]),
            history=[envelope(1, [finding("F1-1", ["L-1"]), finding("F1-2", ["L-2"])])],
            dispatch_record={"lineage_set": ["L-1", "L-2"]},
        )
        self.assertEqual(decision.kind, "stalled")

    # A withdrawn lineage leaves the baseline, so withdrawing is not progress -
    # it shrinks both sides.
    def test_withdrawn_lineage_does_not_count_as_progress(self) -> None:
        current = envelope(
            2,
            [finding("F2-1", ["L-1"])],
            dispositions={"F1-2": {"L-2": {"disposition": "withdrawn", "successors": [],
                                            "basis": [], "reason": "misread"}}},
        )
        decision = judge_v1(
            current,
            history=[envelope(1, [finding("F1-1", ["L-1"]), finding("F1-2", ["L-2"])])],
            dispatch_record={"lineage_set": ["L-1", "L-2"]},
        )
        self.assertEqual(decision.kind, "stalled")

    # A resolved lineage stays in the baseline; progress shows up as its
    # absence from the current set.
    def test_resolved_lineage_shows_as_improvement(self) -> None:
        current = envelope(
            2,
            [finding("F2-1", ["L-1"])],
            dispositions={"F1-2": {"L-2": resolved()}},
        )
        decision = judge_v1(
            current,
            history=[envelope(1, [finding("F1-1", ["L-1"]), finding("F1-2", ["L-2"])])],
            dispatch_record={"lineage_set": ["L-1", "L-2"]},
        )
        self.assertEqual(decision.kind, "improved")


class FoldingTest(unittest.TestCase):
    def test_blocking_lineages_ignores_non_blocking(self) -> None:
        env = envelope(1, [finding("F1-1", ["L-1"]), finding("F1-2", ["L-2"], blocking=False)])
        self.assertEqual(blocking_lineages(env), {"L-1"})

    def test_fold_takes_the_latest_state(self) -> None:
        rounds = [
            envelope(2, [], dispositions={"F1-1": {"L-1": resolved()}}),
            envelope(3, [finding("F3-1", ["L-1"])]),
        ]
        self.assertEqual(fold_dispositions(rounds)["L-1"], "continuing")

    def test_oscillation_detects_explicit_recurrence(self) -> None:
        history = [envelope(2, [], dispositions={"F1-1": {"L-1": resolved()}})]
        current = envelope(
            3,
            [finding("F3-1", ["L-9"], recurrence_of={"lineage_id": "L-1"})],
        )
        self.assertEqual(oscillation_hits(history, current), {"L-1"})

    def test_closure_follows_split_and_merge(self) -> None:
        rounds = [
            envelope(2, [finding("F2-1", ["L-2"], prior_refs=[{"lineage_id": "L-1"}]),
                         finding("F2-2", ["L-3"], prior_refs=[{"lineage_id": "L-1"}])]),
            envelope(3, [finding("F3-1", ["L-4"], prior_refs=[{"lineage_id": "L-3"}])]),
        ]
        self.assertEqual(closure({"L-1"}, rounds), {"L-1", "L-2", "L-3", "L-4"})


if __name__ == "__main__":
    unittest.main()


class RealHistoryTest(unittest.TestCase):
    """judge-v1 against a recorded four-round spec review history.

    This is the case D-2026-09-14-11 was derived from, so it is the one place
    the rule can be checked against a decision a human actually made rather
    than against a scenario invented to suit it.  The lineage is real: r1 raises
    a dependency cycle and a lock gap, the cycle is fixed, and the lock gap
    survives two more rounds under new occurrence ids before being resolved.
    """

    def setUp(self) -> None:
        self.r1 = envelope(1, [finding("F1-1", ["L-1"]), finding("F1-2", ["L-2"])])
        self.r2 = envelope(
            2,
            [finding("F2-1", ["L-2"],
                     prior_refs=[{"finding_id": "F1-2", "lineage_id": "L-2",
                                  "relation": "residual"}])],
            dispositions={"F1-1": {"L-1": resolved()},
                          "F1-2": {"L-2": residual("F2-1")}},
        )
        self.r3 = envelope(
            3,
            [finding("F3-1", ["L-2"],
                     prior_refs=[{"finding_id": "F2-1", "lineage_id": "L-2",
                                  "relation": "residual"}])],
            dispositions={"F2-1": {"L-2": residual("F3-1")}},
        )

    def test_r1_is_the_baseline(self) -> None:
        self.assertEqual(judge_v1(self.r1, history=[], dispatch_record=None).kind, "baseline")

    # Two High down to one is progress at the same severity level.
    def test_r2_improved(self) -> None:
        decision = judge_v1(self.r2, history=[self.r1],
                            dispatch_record={"lineage_set": ["L-1", "L-2"]})
        self.assertEqual(decision.kind, "improved")

    # One High to one High is not, whatever the review prose says - this is the
    # round the rule was written from, and it really did stop here.
    def test_r3_stalls(self) -> None:
        decision = judge_v1(self.r3, history=[self.r1, self.r2],
                            dispatch_record={"lineage_set": ["L-2"]},
                            rounds_since_dispatch=[self.r2])
        self.assertEqual(decision.kind, "stalled")
        self.assertEqual(decision.detail["current"], ["L-2"])
        self.assertEqual(decision.detail["prior_eff"], ["L-2"])

    def test_r4_accepts(self) -> None:
        r4 = envelope(4, [], verdict="accepted",
                      dispositions={"F3-1": {"L-2": resolved()}})
        decision = judge_v1(r4, history=[self.r1, self.r2, self.r3],
                            dispatch_record={"lineage_set": ["L-2"]},
                            rounds_since_dispatch=[self.r2, self.r3])
        self.assertEqual(decision.kind, "accepted")

    # A finding the producer never saw stops at step 5, with that reason - not
    # at step 8, which would misattribute it as a failure to improve.
    def test_a_new_lineage_stalls_for_the_right_reason(self) -> None:
        r3 = envelope(3, [finding("F3-9", ["L-9"])],
                      dispositions={"F2-1": {"L-2": resolved()}})
        decision = judge_v1(r3, history=[self.r1, self.r2],
                            dispatch_record={"lineage_set": ["L-2"]},
                            rounds_since_dispatch=[self.r2])
        self.assertEqual(decision.kind, "stalled")
        self.assertEqual(decision.detail["new_vs_dispatch"], ["L-9"])

    def test_a_resolved_lineage_returning_is_oscillation(self) -> None:
        r3 = envelope(3, [finding("F3-8", ["L-1"])],
                      dispositions={"F2-1": {"L-2": resolved()}})
        decision = judge_v1(r3, history=[self.r1, self.r2],
                            dispatch_record={"lineage_set": ["L-1", "L-2"]},
                            rounds_since_dispatch=[self.r2])
        self.assertEqual(decision.kind, "oscillating")
        self.assertEqual(decision.detail["lineages"], ["L-1"])
