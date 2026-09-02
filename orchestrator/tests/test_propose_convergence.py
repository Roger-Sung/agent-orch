"""Route-level checks for the propose convergence policy and its fixtures.

Everything here is deterministic. No provider is invoked and no model prose is
compared, so these tests cannot flake on wording. Whether a reviewer actually
judges a given case.md the way its expected.json says is human / dogfood
verification owned by the stop-gate, not by this file.

See docs/decisions/propose-convergence-policy.md and
orchestrator/fixtures/convergence/README.md.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from orchestrator.profile import load_profile


ROOT = Path(__file__).resolve().parents[2]
PROPOSE_PROFILE = ROOT / "orchestrator" / "profiles" / "propose.yaml"
FIXTURES = ROOT / "orchestrator" / "fixtures" / "convergence"
OLD_PROFILE = FIXTURES / "F-A8" / "old-profile.yaml"

# F-A9 records an apply-shaped risk for future observation only: no profile, no
# route, no assertion until runtime evidence shows the shape occurs in apply.
OBSERVATION_ONLY = {"F-A9"}
EXPECTED_FIXTURES = {
    "F-A1", "F-A2", "F-A3", "F-A4", "F-A4b",
    "F-A5", "F-A6", "F-A7", "F-A8", "F-A9", "F-A10",
}


def _fixture_dirs() -> dict[str, Path]:
    return {
        path.name: path
        for path in sorted(FIXTURES.iterdir())
        if path.is_dir() and path.name.startswith("F-A")
    }


class FixtureLayoutTests(unittest.TestCase):
    def test_every_declared_fixture_exists_and_nothing_extra_does(self):
        self.assertEqual(set(_fixture_dirs()), EXPECTED_FIXTURES)

    def test_each_fixture_has_a_case_and_the_right_expectation_file(self):
        for name, path in _fixture_dirs().items():
            with self.subTest(fixture=name):
                self.assertTrue((path / "case.md").is_file(), "case.md missing")
                self.assertGreater((path / "case.md").stat().st_size, 0)
                expected = path / "expected.json"
                if name in OBSERVATION_ONLY:
                    self.assertFalse(
                        expected.exists(),
                        "observation-only fixture must carry no expectation",
                    )
                else:
                    self.assertTrue(expected.is_file(), "expected.json missing")

    def test_expected_json_has_exactly_the_contract_keys(self):
        for name, path in _fixture_dirs().items():
            if name in OBSERVATION_ONLY:
                continue
            with self.subTest(fixture=name):
                data = json.loads((path / "expected.json").read_text(encoding="utf-8"))
                self.assertEqual(
                    set(data), {"outcome", "route_target", "must_preserve"}
                )
                self.assertIsInstance(data["outcome"], str)
                self.assertIsInstance(data["route_target"], str)
                self.assertIsInstance(data["must_preserve"], list)
                for item in data["must_preserve"]:
                    self.assertIsInstance(item, str)


class RouteTableTests(unittest.TestCase):
    def setUp(self):
        self.profile = load_profile(PROPOSE_PROFILE)
        self.review = self.profile.stages["review"]

    def test_review_declares_exactly_the_four_policy_outcomes(self):
        self.assertEqual(
            self.review.outcomes,
            {
                "ready": "done",
                "needs_correction": "draft",
                "needs_simplification": "simplify",
                "needs_user_decision": "draft",
            },
        )

    def test_needs_convergence_is_gone(self):
        self.assertNotIn("needs_convergence", self.review.outcomes)

    def test_simplify_stage_returns_to_review(self):
        simplify = self.profile.stages["simplify"]
        self.assertEqual(simplify.owner, "claude")
        self.assertEqual(simplify.outcomes, {"simplified": "review"})

    def test_simplify_prompt_requires_a_bounded_targeted_edit(self):
        prompt = self.profile.stages["simplify"].prompt
        self.assertIn("smallest coherent edit", prompt)
        self.assertIn("one row per current blocking finding", prompt)
        self.assertIn("Do not rewrite the whole document", prompt)
        self.assertNotIn("Rewrite spec-draft.md completely", prompt)
        self.assertNotIn("Every risk the previous version carried", prompt)

    def test_simplify_prompt_cannot_trade_away_a_sourced_requirement(self):
        prompt = self.profile.stages["simplify"].prompt
        self.assertIn(
            "must never remove, weaken, defer, reinterpret, or relabel a requirement",
            prompt,
        )
        self.assertIn("remove only a requirement invented by the current draft", prompt)
        self.assertIn("exactly one smallest necessary mechanism", prompt)
        self.assertIn("no delete, merge, or reuse design can preserve", prompt)
        self.assertNotIn("remove the requirement that created the contradiction", prompt)

    def test_every_edge_has_a_cap(self):
        edges = {
            f"{stage.name}.{outcome}"
            for stage in self.profile.stages.values()
            for outcome in stage.outcomes
        }
        self.assertEqual(edges, set(self.profile.edge_caps))
        for edge, cap in self.profile.edge_caps.items():
            with self.subTest(edge=edge):
                self.assertGreaterEqual(cap, 1)

    def test_cap_total_fits_under_max_transitions(self):
        # Divergence is bounded by the edge caps. max_transitions only has to be
        # large enough that a run never stops with the uninformative reason
        # transition_cap while caps are still unspent. transitions_count is
        # incremented in exactly one place in the engine, so this is exact.
        self.assertLessEqual(
            sum(self.profile.edge_caps.values()), self.profile.max_transitions
        )

    def test_existing_stage_execution_values_did_not_move(self):
        old = load_profile(OLD_PROFILE)
        for name in ("draft", "review"):
            with self.subTest(stage=name):
                self.assertEqual(
                    self.profile.stages[name].owner, old.stages[name].owner
                )
                self.assertEqual(
                    self.profile.stages[name].attempt_cap,
                    old.stages[name].attempt_cap,
                )
                self.assertEqual(
                    self.profile.stages[name].timeout, old.stages[name].timeout
                )

    def test_the_reserved_hold_outcome_is_declared_only_by_propose(self):
        declaring = []
        for path in sorted((ROOT / "orchestrator" / "profiles").glob("*.yaml")):
            profile = load_profile(path)
            for stage in profile.stages.values():
                if "needs_user_decision" in stage.outcomes:
                    declaring.append(path.name)
        self.assertEqual(declaring, ["propose.yaml"])


class FixtureRoutingTests(unittest.TestCase):
    def setUp(self):
        self.profile = load_profile(PROPOSE_PROFILE)
        self.review = self.profile.stages["review"]

    def test_each_expected_outcome_is_a_declared_review_outcome(self):
        for name, path in _fixture_dirs().items():
            if name in OBSERVATION_ONLY:
                continue
            with self.subTest(fixture=name):
                data = json.loads((path / "expected.json").read_text(encoding="utf-8"))
                self.assertIn(data["outcome"], self.review.outcomes)

    def test_each_expected_route_target_matches_the_profile(self):
        for name, path in _fixture_dirs().items():
            if name in OBSERVATION_ONLY:
                continue
            with self.subTest(fixture=name):
                data = json.loads((path / "expected.json").read_text(encoding="utf-8"))
                self.assertEqual(
                    self.review.outcomes[data["outcome"]], data["route_target"]
                )

    def test_each_referenced_edge_has_a_cap(self):
        for name, path in _fixture_dirs().items():
            if name in OBSERVATION_ONLY:
                continue
            with self.subTest(fixture=name):
                data = json.loads((path / "expected.json").read_text(encoding="utf-8"))
                self.assertIn(
                    f"review.{data['outcome']}", self.profile.edge_caps
                )

    def test_the_policy_is_exercised_by_at_least_one_fixture_per_outcome(self):
        seen = set()
        for name, path in _fixture_dirs().items():
            if name in OBSERVATION_ONLY:
                continue
            seen.add(
                json.loads((path / "expected.json").read_text(encoding="utf-8"))["outcome"]
            )
        self.assertEqual(seen, set(self.review.outcomes))


class OldProfileContrastTests(unittest.TestCase):
    """F-A8: what the frozen pre-change profile did with the same review round."""

    def setUp(self):
        self.old = load_profile(OLD_PROFILE)
        self.new = load_profile(PROPOSE_PROFILE)
        self.case = (FIXTURES / "F-A8" / "case.md").read_text(encoding="utf-8")
        self.expected = json.loads(
            (FIXTURES / "F-A8" / "expected.json").read_text(encoding="utf-8")
        )

    def test_the_old_profile_could_not_express_simplification_or_a_hold(self):
        outcomes = self.old.stages["review"].outcomes
        self.assertEqual(set(outcomes), {"needs_convergence", "ready"})
        self.assertNotIn("needs_simplification", outcomes)
        self.assertNotIn("needs_user_decision", outcomes)

    def test_under_the_old_profile_all_six_findings_share_one_route_target(self):
        # High findings remain, so `ready` is unavailable; the only edge left is
        # needs_convergence -> draft. Both origins collapse onto it. That collapse
        # is the structural cause of the additive feedback loop.
        outcomes = self.old.stages["review"].outcomes
        available = {
            name: target for name, target in outcomes.items() if target != "done"
        }
        self.assertEqual(available, {"needs_convergence": "draft"})

        origins = ["original_surface"] + ["newly_added_spec_mechanism"] * 5
        self.assertEqual(len(origins), 6)
        targets = {available["needs_convergence"] for _ in origins}
        self.assertEqual(targets, {"draft"})

    def test_the_case_classifies_all_six_findings_and_cites_its_sources(self):
        for finding in ("H1", "H2", "H3", "H4", "H5", "H6"):
            with self.subTest(finding=finding):
                self.assertIn(finding, self.case)
        rows = [line for line in self.case.splitlines() if line.startswith("| H")]
        self.assertEqual(len(rows), 6)
        self.assertEqual(
            sum("`newly_added_spec_mechanism`" in row for row in rows), 5
        )
        self.assertEqual(sum("`original_surface`" in row for row in rows), 1)
        self.assertIn(
            "<ORCH_HOME>/tasks/"
            "<TASK_ID>/reports/propose-scratch/"
            "review-notes.md",
            self.case,
        )
        self.assertIn(
            "<ORCH_HOME>/tasks/"
            "<TASK_ID>/evidence.json",
            self.case,
        )

    def test_under_the_new_profile_the_same_case_routes_to_simplify(self):
        self.assertEqual(self.expected["outcome"], "needs_simplification")
        self.assertEqual(
            self.new.stages["review"].outcomes[self.expected["outcome"]], "simplify"
        )
        self.assertEqual(self.expected["route_target"], "simplify")
        # The one original-surface finding must survive the simplification.
        self.assertEqual(self.expected["must_preserve"], ["H2"])


class BlueprintTests(unittest.TestCase):
    BLUEPRINT = FIXTURES / "blueprints" / "sqlite-state-relocation" / "blueprint.md"

    def test_the_single_blueprint_exists_and_is_the_only_one(self):
        blueprints = sorted(
            path for path in (FIXTURES / "blueprints").iterdir() if path.is_dir()
        )
        self.assertEqual([path.name for path in blueprints], ["sqlite-state-relocation"])
        self.assertTrue(self.BLUEPRINT.is_file())

    def test_the_blueprint_has_all_six_required_sections(self):
        text = self.BLUEPRINT.read_text(encoding="utf-8")
        for heading in (
            "## Original risk",
            "## Boundary",
            "## Goal",
            "## Steps",
            "## Failure handling",
            "## Acceptance",
        ):
            with self.subTest(heading=heading):
                self.assertIn(heading, text)

    def test_the_blueprint_stays_a_document_and_names_its_exclusions(self):
        text = self.BLUEPRINT.read_text(encoding="utf-8")
        self.assertIn("## Explicitly not part of this blueprint", text)
        # The excluded machinery may only be named in the exclusion section.
        body = text.split("## Explicitly not part of this blueprint")[0]
        for banned in ("attestation", "choreography", "evidence matri"):
            with self.subTest(term=banned):
                self.assertNotIn(banned, body.lower())


class PolicyDocumentTests(unittest.TestCase):
    POLICY = ROOT / "docs" / "decisions" / "propose-convergence-policy.md"

    def test_the_policy_document_exists(self):
        self.assertTrue(self.POLICY.is_file())

    def test_the_policy_documents_the_engine_reserved_outcome_names(self):
        text = self.POLICY.read_text(encoding="utf-8")
        self.assertIn("Engine reserved outcome names", text)
        self.assertIn("needs_user_decision", text)
        self.assertIn("user_decision_required", text)
        # The semantics come from the engine version, not from the snapshot.
        self.assertIn("not from the task's `profile.snapshot.json`", text)

    def test_the_policy_states_the_boundary_of_the_hold_rule(self):
        text = self.POLICY.read_text(encoding="utf-8")
        self.assertIn("The boundary of rule 3", text)
        self.assertIn("F-A4b", text)

    def test_the_policy_bounds_simplification_to_the_current_findings(self):
        text = self.POLICY.read_text(encoding="utf-8")
        self.assertIn("smallest", text)
        self.assertIn("coherent edit", text)
        self.assertIn("one row per current blocking", text)
        self.assertIn("does not rewrite the whole document", text)
        self.assertNotIn("A complete rewritten spec", text)


if __name__ == "__main__":
    unittest.main()
