"""The pack-v1 profile has to agree with the machine that drives it.

Two couplings break silently. `_advance_pack` sets the task's `current_stage`
from the pack state, so a state whose stage the profile does not define makes
the run loop raise instead of dispatching. And pack-v1 refuses any outcome
outside its stage's allowed set, so a stage declaring one the policy does not
know is a dispatch that can only ever be blocked.
"""
from __future__ import annotations

import unittest
from pathlib import Path

from orchestrator.pack.envelopes import OUTCOME_PREFIX, REVIEW_BEGIN, REVIEW_END
from orchestrator.pack.policy import PackPolicy, allowed_outcomes
from orchestrator.profile import load_profile

PROFILE = Path(__file__).resolve().parents[1] / "profiles" / "pack_v1.yaml"

PACK_STATES = ("contracting", "claimed", "producing(1)", "submitted(1)",
               "reviewing(1)", "judging(1)", "repair_pending(1)")


class PackProfileTest(unittest.TestCase):
    def setUp(self) -> None:
        self.profile = load_profile(PROFILE)

    def test_every_pack_state_names_a_stage_the_profile_defines(self) -> None:
        for state in PACK_STATES:
            stage = PackPolicy.stage_for_state(state)
            self.assertIsNotNone(stage, f"{state} maps to no stage")
            self.assertIn(stage, self.profile.stages,
                          f"{state} -> {stage}, which the profile does not define")

    def test_every_stage_outcome_is_one_the_policy_allows(self) -> None:
        for name, stage in self.profile.stages.items():
            if stage.terminal:
                continue
            allowed = allowed_outcomes(name)
            for outcome in stage.outcomes:
                self.assertIn(outcome, allowed,
                              f"stage {name} declares {outcome!r}, which pack-v1 refuses")

    def test_the_owners_are_the_ones_the_roles_call_for(self) -> None:
        """Producer is Claude, reviewer is Codex (D-2026-09-14-05)."""
        owners = {name: stage.owner for name, stage in self.profile.stages.items()
                  if not stage.terminal}
        self.assertEqual(owners["apply"], "claude")
        self.assertEqual(owners["repair"], "claude")
        self.assertEqual(owners["contract_review"], "codex")
        self.assertEqual(owners["review"], "codex")


class EnvelopePromptTest(unittest.TestCase):
    """A reviewer cannot guess the framing it is required to produce.

    Describing the markers instead of spelling them out fails at the only point
    where it is expensive: after the provider has run, when the envelope does
    not parse and the round is spent. These assert the prompt carries the exact
    strings the parser looks for, so renaming a constant breaks a test rather
    than a live review.
    """

    REVIEWING_STAGES = ("contract_review", "review")

    def setUp(self) -> None:
        self.profile = load_profile(PROFILE)

    def test_the_reviewing_stages_spell_the_markers_out(self) -> None:
        for name in self.REVIEWING_STAGES:
            prompt = self.profile.stages[name].prompt
            self.assertIn(REVIEW_BEGIN, prompt, f"{name} does not name the opening marker")
            self.assertIn(REVIEW_END, prompt, f"{name} does not name the closing marker")

    def test_every_stage_asks_for_the_outcome_line_the_parser_reads(self) -> None:
        for name, stage in self.profile.stages.items():
            if stage.terminal:
                continue
            self.assertIn(OUTCOME_PREFIX, stage.prompt,
                          f"{name} never asks for a typed outcome line")

    def test_each_stage_asks_only_for_outcomes_it_is_allowed(self) -> None:
        for name, stage in self.profile.stages.items():
            if stage.terminal:
                continue
            allowed = allowed_outcomes(name)
            asked = {
                token.split()[0].rstrip(".,")
                for token in stage.prompt.split(OUTCOME_PREFIX)[1:]
            }
            self.assertTrue(asked, f"{name} names no outcome after the prefix")
            self.assertLessEqual(
                asked, allowed,
                f"{name} asks for {sorted(asked - allowed)}, which pack-v1 refuses")
