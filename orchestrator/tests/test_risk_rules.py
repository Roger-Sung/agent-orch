"""Risk vocabulary loading: what happens when the file is fine, absent, or wrong.

The failure modes matter more than the happy path here. A risk file that
silently loads as empty is the worst outcome available: the operator believes
gating is in force while every task classifies as low risk.
"""

from __future__ import annotations

import io
import contextlib
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from orchestrator.risk_rules import (
    EMPTY_RULES,
    RISK_RULES_ENV,
    RiskRules,
    RiskRulesError,
    load_risk_rules,
    parse_risk_rules,
)
from orchestrator.start import _stop_gate

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = REPO_ROOT / "risk-rules.yaml"

MINIMAL = """\
version: 1
rules:
  publish:
    keyword: publish
    category: stop_gate
    action: require_stop_gate
    match: word
"""


class ShippedExampleTest(unittest.TestCase):
    def test_the_example_file_parses(self) -> None:
        rules = load_risk_rules(EXAMPLE)
        self.assertTrue(rules.rules)

    def test_the_example_carries_no_deployment_private_vocabulary(self) -> None:
        """The whole reason this file exists is that private names stay out of it."""
        text = EXAMPLE.read_text(encoding="utf-8").lower()
        for private in ("user.md", "soul.md", "memory.md", "dispatch.db"):
            self.assertNotIn(private, text, f"{private} must not ship in the example ruleset")


class DefaultsTest(unittest.TestCase):
    def test_no_configuration_means_an_empty_ruleset(self) -> None:
        self.assertIs(load_risk_rules(None, env={}), EMPTY_RULES)
        self.assertFalse(EMPTY_RULES)

    def test_an_empty_ruleset_classifies_everything_as_the_default(self) -> None:
        verdict = EMPTY_RULES.evaluate("publish and deploy and delete everything")
        self.assertEqual(verdict["risk"], "low")
        self.assertFalse(verdict["require_stop_gate"])

    def test_environment_variable_is_honoured(self) -> None:
        rules = load_risk_rules(None, env={RISK_RULES_ENV: str(EXAMPLE)})
        self.assertTrue(rules.rules)


class MissingFileTest(unittest.TestCase):
    def test_a_configured_but_missing_file_warns_and_continues_empty(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            rules = load_risk_rules("/nonexistent/agent-orch/risk-rules.yaml")
        self.assertEqual(rules.rules, ())
        self.assertIn("not found", stderr.getvalue())

    def test_the_warning_names_the_path_it_looked_for(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            load_risk_rules("/nonexistent/agent-orch/risk-rules.yaml")
        self.assertIn("/nonexistent/agent-orch/risk-rules.yaml", stderr.getvalue())


class MalformedFileTest(unittest.TestCase):
    """Fail closed: a malformed risk file stops the run rather than loading empty."""

    def _reject(self, text: str) -> str:
        with self.assertRaises(RiskRulesError) as caught:
            parse_risk_rules(text)
        return str(caught.exception)

    def test_unknown_version_is_rejected(self) -> None:
        self.assertIn("version", self._reject("version: 2\nrules:\n"))

    def test_unknown_category_is_rejected(self) -> None:
        message = self._reject(
            "version: 1\nrules:\n  r:\n    keyword: x\n    category: nonsense\n    action: mark_high_risk\n"
        )
        self.assertIn("category", message)

    def test_unknown_action_is_rejected(self) -> None:
        message = self._reject(
            "version: 1\nrules:\n  r:\n    keyword: x\n    category: high_risk\n    action: launch_missiles\n"
        )
        self.assertIn("action", message)

    def test_unknown_key_is_rejected_rather_than_ignored(self) -> None:
        message = self._reject(
            "version: 1\nrules:\n  r:\n    keyword: x\n    category: high_risk\n"
            "    action: mark_high_risk\n    sevrity: high\n"
        )
        self.assertIn("unexpected", message)

    def test_invalid_regex_is_rejected_at_load_time(self) -> None:
        message = self._reject(
            "version: 1\nrules:\n  r:\n    keyword: '([unclosed'\n    category: high_risk\n"
            "    action: mark_high_risk\n    match: regex\n"
        )
        self.assertIn("regex", message)

    def test_broken_yaml_is_rejected(self) -> None:
        self._reject("version: 1\nrules:\n\t bad indentation\n")

    def test_a_malformed_file_never_degrades_to_an_empty_ruleset(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as handle:
            handle.write("version: 1\nrules:\n  r:\n    category: bogus\n    action: mark_high_risk\n")
            path = handle.name
        try:
            with self.assertRaises(RiskRulesError):
                load_risk_rules(path)
        finally:
            Path(path).unlink()


class MatchingTest(unittest.TestCase):
    def rules(self) -> RiskRules:
        return parse_risk_rules(MINIMAL)

    def test_word_match_does_not_fire_on_a_substring(self) -> None:
        self.assertEqual(self.rules().evaluate("republished the notes")["matched"], ())

    def test_word_match_fires_on_the_word(self) -> None:
        self.assertEqual(self.rules().evaluate("publish the notes")["matched"], ("publish",))

    def test_substring_is_the_default_match_mode(self) -> None:
        rules = parse_risk_rules(
            "version: 1\nrules:\n  store:\n    keyword: datastore\n    category: high_risk\n"
            "    action: mark_high_risk\n"
        )
        self.assertTrue(rules.evaluate("touching the datastores")["high"])

    def test_keyword_defaults_to_the_rule_id(self) -> None:
        rules = parse_risk_rules(
            "version: 1\nrules:\n  scheduler:\n    category: high_risk\n    action: mark_high_risk\n"
        )
        self.assertTrue(rules.evaluate("rewrite the scheduler")["high"])

    def test_highest_severity_wins_and_actions_union(self) -> None:
        rules = parse_risk_rules(
            "version: 1\nrules:\n"
            "  soft:\n    keyword: notes\n    category: medium_risk\n    action: mark_medium_risk\n"
            "  hard:\n    keyword: migration\n    category: high_risk\n    action: mark_high_risk\n"
            "  gate:\n    keyword: publish\n    category: stop_gate\n    action: require_stop_gate\n"
        )
        verdict = rules.evaluate("notes about the migration, then publish")
        self.assertEqual(verdict["risk"], "high")
        self.assertTrue(verdict["require_stop_gate"])
        self.assertEqual(sorted(verdict["matched"]), ["gate", "hard", "soft"])

    def test_target_hints_are_collected(self) -> None:
        rules = parse_risk_rules(
            "version: 1\nrules:\n  h:\n    keyword: intake\n    category: target_hint\n"
            "    action: hint_target\n    target: intake\n"
        )
        self.assertEqual(rules.evaluate("fix intake")["targets"], ("intake",))


class IntakeIntegrationTest(unittest.TestCase):
    """The loader is wired into intake, not just importable."""

    def test_external_rules_can_raise_a_task_to_a_stop_gate(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as handle:
            handle.write(
                "version: 1\nrules:\n  ledger:\n    keyword: quarterly-ledger\n"
                "    category: stop_gate\n    action: require_stop_gate\n"
            )
            path = handle.name
        text = "tidy up the quarterly-ledger notes"
        low_risk = {"implementation": "low"}
        try:
            self.assertFalse(_stop_gate(text, low_risk), "baseline should not gate this task")
            with unittest.mock.patch.dict("os.environ", {RISK_RULES_ENV: path}):
                self.assertTrue(_stop_gate(text, low_risk), "declared vocabulary did not reach intake")
        finally:
            Path(path).unlink()

    def test_external_rules_cannot_lower_a_built_in_risk(self) -> None:
        """A deployment file may raise the assessment; it must never lower one."""
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as handle:
            handle.write("version: 1\ndefaults:\n  risk: low\n  require_stop_gate: false\nrules:\n")
            path = handle.name
        try:
            with unittest.mock.patch.dict("os.environ", {RISK_RULES_ENV: path}):
                self.assertTrue(_stop_gate("publish the release", {"implementation": "low"}))
        finally:
            Path(path).unlink()


if __name__ == "__main__":
    unittest.main()
