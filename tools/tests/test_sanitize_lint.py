"""Bidirectional tests for tools/sanitize-lint.py (stdlib unittest only).

A forbidden-pattern scanner fails in one direction that is easy to miss: a
broken regex makes it exit zero forever. So every test here comes in a pair —
the golden fixture must fail, a clean sample must pass — and the golden test
additionally asserts that *every* regex-backed rule fires at least once, so a
rule added without a golden example is a test failure rather than dead code.

Run: python3 -m unittest discover -s tools/tests
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
LINT = REPO / "tools" / "sanitize-lint.py"
RULES = REPO / "risk-patterns.yaml"
FIXTURES = REPO / "tools" / "tests" / "fixtures"
GOLDEN = FIXTURES / "forbidden-golden.txt"
GOLDEN_SECRETS = FIXTURES / "forbidden-golden-secrets.txt"
CLEAN = FIXTURES / "clean-sample.txt"
SECRETS = FIXTURES / "secrets-example.secrets"


def run_lint(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(LINT), "--rules", str(RULES), *args],
        capture_output=True,
        text=True,
        cwd=str(REPO),
        check=False,
    )


def load_rule_ids() -> tuple[list[str], list[str]]:
    """Return (regex_rule_ids, secret_backed_rule_ids) parsed by the tool itself."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("sanitize_lint", LINT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["sanitize_lint"] = module  # dataclasses resolves types via sys.modules
    spec.loader.exec_module(module)
    rules, _config = module.load_rules(RULES)
    return (
        [r.rule_id for r in rules if r.regex is not None],
        [r.rule_id for r in rules if r.requires_secrets],
    )


class RulesFileTest(unittest.TestCase):
    def test_rules_parse_and_declare_both_kinds(self) -> None:
        regex_ids, secret_ids = load_rule_ids()
        self.assertTrue(regex_ids, "no regex-backed rules parsed")
        self.assertTrue(secret_ids, "no requires_secrets rules declared")


class GoldenDirectionTest(unittest.TestCase):
    def test_golden_fixture_exits_nonzero(self) -> None:
        result = run_lint(str(GOLDEN), "--secrets-file", str(SECRETS), "--json")
        self.assertNotEqual(result.returncode, 0, "golden fixture scanned clean — regexes are broken")
        self.assertEqual(result.returncode, 1, result.stderr)

    def test_every_regex_rule_has_a_golden_example(self) -> None:
        result = run_lint(str(GOLDEN), "--secrets-file", str(SECRETS), "--json")
        payload = json.loads(result.stdout)
        hit = set(payload["counts"])
        regex_ids, _ = load_rule_ids()
        missing = sorted(set(regex_ids) - hit)
        self.assertEqual(missing, [], f"rules with no golden example: {missing}")

    def test_secret_backed_rules_fire_only_with_secrets_file(self) -> None:
        # The downgrade flag is required here precisely because the run is
        # knowingly partial — that is the only situation it exists for.
        without = run_lint(str(GOLDEN_SECRETS), "--no-strict-secrets", "--json")
        self.assertEqual(without.returncode, 0, "secret-backed golden matched without a secrets file")

        with_secrets = run_lint(str(GOLDEN_SECRETS), "--secrets-file", str(SECRETS), "--json")
        self.assertEqual(with_secrets.returncode, 1, with_secrets.stderr)
        payload = json.loads(with_secrets.stdout)
        _, secret_ids = load_rule_ids()
        missing = sorted(set(secret_ids) - set(payload["counts"]))
        self.assertEqual(missing, [], f"secret-backed rules with no golden example: {missing}")

    def test_reports_never_echo_the_secret_value(self) -> None:
        # Assembled at runtime so this test file itself stays clean under its
        # own repo-wide scan (see test_repo_scan_is_clean_and_skips_golden_fixtures).
        name = "Casey" + " Doe"
        domain = "acme" + "-internal"
        result = run_lint(str(GOLDEN_SECRETS), "--secrets-file", str(SECRETS))
        self.assertNotIn(name, result.stdout)
        self.assertNotIn(domain, result.stdout)
        self.assertIn("***", result.stdout)


class CleanDirectionTest(unittest.TestCase):
    def test_clean_sample_exits_zero(self) -> None:
        result = run_lint(str(CLEAN), "--no-strict-secrets")
        self.assertEqual(result.returncode, 0, f"false positives:\n{result.stdout}")

    def test_clean_sample_exits_zero_with_secrets_file(self) -> None:
        result = run_lint(str(CLEAN), "--secrets-file", str(SECRETS))
        self.assertEqual(result.returncode, 0, f"false positives:\n{result.stdout}")

    def test_repo_scan_is_clean_and_skips_golden_fixtures(self) -> None:
        result = run_lint(str(REPO), "--secrets-file", str(SECRETS), "--json")
        payload = json.loads(result.stdout)
        self.assertEqual(
            result.returncode,
            0,
            "repo scan is dirty (golden fixtures must be excluded from walks):\n"
            + json.dumps(payload["violations"], indent=2, ensure_ascii=False),
        )


class SecretCoverageTest(unittest.TestCase):
    """Fail-closed contract: a forgotten --secrets-file must never look clean."""

    def test_missing_secrets_file_fails_by_default(self) -> None:
        # No flags at all — the shape a release gate would accidentally use.
        result = run_lint(str(CLEAN))
        self.assertNotEqual(result.returncode, 0, "a partial scan reported success — fail-open regression")
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("uncovered secret-backed rules", result.stderr)
        self.assertNotIn("clean", result.stdout)

    def test_missing_secrets_file_fails_on_a_dirty_tree_too(self) -> None:
        # Exit 2 must win over exit 1: "I did not check everything" is a
        # different answer from "I checked and found things".
        result = run_lint(str(GOLDEN))
        self.assertEqual(result.returncode, 2, result.stdout)

    def test_explicit_downgrade_warns_and_passes(self) -> None:
        result = run_lint(str(CLEAN), "--no-strict-secrets")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("partial scan", result.stderr)

    def test_strict_secrets_passes_with_coverage(self) -> None:
        result = run_lint(str(CLEAN), "--secrets-file", str(SECRETS), "--strict-secrets")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class ConfigErrorTest(unittest.TestCase):
    def test_broken_rules_file_exits_two(self) -> None:
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as handle:
            handle.write("version: 1\npatterns:\n  - id: broken\n    regex: '([unclosed'\n")
            broken = handle.name
        result = subprocess.run(
            [sys.executable, str(LINT), "--rules", broken, str(CLEAN)],
            capture_output=True,
            text=True,
            check=False,
        )
        Path(broken).unlink()
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("config error", result.stderr)


if __name__ == "__main__":
    unittest.main()
