"""The minimal second target, intake, and ST26 (step 5).

The point of `_fixture_min` is negative: if the engine can drive a target that
shares nothing with a production target - no build tool, no database,
no JVM - then the engine
does not secretly know about acme.  So these tests run the *real* target CLIs
through the real bootstrap, not stubs.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

from orchestrator.pack.envelopes import validate_verify
from orchestrator.pack.intake import assemble_contract, build_environment, start_packs
from orchestrator.pack.manifest import validate_manifest
from orchestrator.pack.state_machine import PackMachine
from orchestrator.pack.store import PackStore
from orchestrator.pack.target import TargetError, load_target, parse_profile_yaml
from orchestrator.pack.verify_runner import (
    VerifyReceipt,
    build_plan,
    decide_usable,
    prepare_artifacts_root,
)
from orchestrator.tests.pack_stub import envelope, high_finding

TARGET_ROOT = Path(__file__).resolve().parents[2] / "targets" / "_fixture_min"
# Target packages are deliberately not tracked in this repository, so these
# tests describe what the engine does *with* a target rather than assuming one
# is present. Without it they skip; they do not fail.
HAS_FIXTURE = (TARGET_ROOT / "profile.yaml").is_file()


def new_store() -> PackStore:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return PackStore(conn)


class ProfileParserTest(unittest.TestCase):
    """The engine's own profile parser rejects sequences; target profiles need them."""

    def test_nested_sequences_and_mappings(self) -> None:
        parsed = parse_profile_yaml(
            "version: 1\n"
            "checks:\n"
            "  hello:\n"
            "    result_kind: test\n"
            "    tools:\n"
            "      - sh\n"
            "    argv_template:\n"
            "      - tests/check.sh\n"
            '      - "{word}"\n'
            "resources: []\n"
        )
        self.assertEqual(parsed["version"], 1)
        self.assertEqual(parsed["checks"]["hello"]["tools"], ["sh"])
        self.assertEqual(parsed["checks"]["hello"]["argv_template"],
                         ["tests/check.sh", "{word}"])
        self.assertEqual(parsed["resources"], [])

    def test_malformed_line_is_refused(self) -> None:
        with self.assertRaises(TargetError):
            parse_profile_yaml("version 1\n")


@unittest.skipUnless(HAS_FIXTURE, "the _fixture_min target package is not present")
class TargetLoadingTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.copy = Path(self._tmp.name) / "_fixture_min"
        shutil.copytree(TARGET_ROOT, self.copy)

    def test_loads_the_fixture_target(self) -> None:
        target = load_target(self.copy)
        self.assertEqual(target.target_id, "_fixture_min")
        self.assertEqual(target.version, "0.1.0")

    # Refused rather than defaulted: the version is half of `generated_by`.
    def test_missing_version_is_refused(self) -> None:
        (self.copy / "VERSION").unlink()
        with self.assertRaises(TargetError):
            load_target(self.copy)

    def test_missing_profile_is_refused(self) -> None:
        (self.copy / "profile.yaml").unlink()
        with self.assertRaises(TargetError):
            load_target(self.copy)

    def test_missing_cli_is_refused(self) -> None:
        (self.copy / "cli" / "verify.py").unlink()
        with self.assertRaises(TargetError):
            load_target(self.copy)

    def test_profile_missing_required_key_is_refused(self) -> None:
        (self.copy / "profile.yaml").write_text("version: 1\n")
        with self.assertRaises(TargetError):
            load_target(self.copy)


@unittest.skipUnless(HAS_FIXTURE, "the _fixture_min target package is not present")
class TargetCliTest(unittest.TestCase):
    """The four CLIs, run for real through the bootstrap."""

    def setUp(self) -> None:
        self.target = load_target(TARGET_ROOT)
        self.change = TARGET_ROOT / "change"
        self.addCleanup(self._assert_package_stayed_clean)

    def _assert_package_stayed_clean(self) -> None:
        # A `.pyc` inside the package would change its digest, so the bootstrap
        # keeping bytecode out is part of the contract, not a tidiness habit.
        stray = [p for p in TARGET_ROOT.rglob("*")
                 if p.name == "__pycache__" or p.suffix == ".pyc"]
        self.assertEqual(stray, [], f"bytecode leaked into the package: {stray}")

    def test_requirement_fingerprint_is_stable_and_scoped(self) -> None:
        first = self.target.requirement_fingerprint(self.change)
        second = self.target.requirement_fingerprint(self.change)
        self.assertEqual(first["digest"], second["digest"])
        # tasks.md is deliberately outside the requirement's domain.
        self.assertNotIn("tasks.md", [entry["path"] for entry in first["files"]])

    def test_validate_plan_produces_a_valid_manifest(self) -> None:
        payload = self.target.validate_plan(self.change)
        validate_manifest(payload["manifest"], validator_version=self.target.version)

    # joint-r2: without the previous text the field must be absent, so the
    # engine treats it as "cannot be minor" rather than "nothing changed".
    def test_text_changed_tasks_needs_the_previous_text(self) -> None:
        payload = self.target.validate_plan(self.change)
        self.assertNotIn("plan_change", payload)

    def test_changed_prose_is_reported_when_comparable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            previous = Path(directory) / "tasks.md"
            previous.write_text(
                (self.change / "tasks.md").read_text().replace(
                    "Make the greeting correct.", "Something else entirely."),
                encoding="utf-8",
            )
            payload = self.target.validate_plan(self.change, previous_tasks=previous)
        self.assertEqual(payload["plan_change"]["packs"]["P1"]["text_changed_tasks"], ["T1"])

    def test_env_probe_key_set_must_match(self) -> None:
        self.assertEqual(set(self.target.env_probe(["sh"])), {"sh"})
        with self.assertRaises(TargetError):
            self.target.env_probe(["no_such_tool"])


@unittest.skipUnless(HAS_FIXTURE, "the _fixture_min target package is not present")
class IntakeTest(unittest.TestCase):
    def test_intake_creates_a_contracting_pack(self) -> None:
        store = new_store()
        target = load_target(TARGET_ROOT)
        started = start_packs(store, target=target, change_dir=TARGET_ROOT / "change",
                              base_revision="abc")
        self.assertEqual(len(started), 1)
        record = started[0]
        self.assertTrue(record["contract_hash"].startswith("sha256:"))
        self.assertEqual(store.get_pack("P1")["state"], "contracting")

        contract = record["contract"]
        self.assertEqual(contract["active_obligations"], ["O1"])
        check = contract["approved_checks"][0]
        self.assertEqual(check["selector"], "hello")
        # Obligation-derived checks all share the target's one template; only
        # the selector varies between them.
        self.assertEqual(check["argv_template"], ["tests/check.sh", "{selector}"])

    # The same inputs must produce the same contract hash, or nothing binds.
    def test_contract_hash_is_stable(self) -> None:
        target = load_target(TARGET_ROOT)
        first = start_packs(new_store(), target=target,
                            change_dir=TARGET_ROOT / "change", base_revision="abc")[0]
        second = start_packs(new_store(), target=target,
                             change_dir=TARGET_ROOT / "change", base_revision="abc")[0]
        self.assertEqual(first["contract_hash"], second["contract_hash"])

    # Found by a real Codex contract review of this very fixture: a check whose
    # argv template has a placeholder nothing supplies is unrunnable, and the
    # contract is where that has to be caught.
    def test_unrenderable_check_is_refused_at_assembly(self) -> None:
        from orchestrator.pack.errors import ContractRejected
        from orchestrator.pack.intake import unsatisfied_placeholders

        self.assertEqual(unsatisfied_placeholders(["x", "{word}"], {}), {"word"})
        self.assertEqual(unsatisfied_placeholders(["x", "{word}"], {"word": "hello"}), set())

        with tempfile.TemporaryDirectory() as directory:
            copy = Path(directory) / "_fixture_min"
            shutil.copytree(TARGET_ROOT, copy)
            profile = (copy / "profile.yaml").read_text(encoding="utf-8")
            # A template that needs a value the contract never supplies.
            (copy / "profile.yaml").write_text(
                profile.replace('    - "{selector}"',
                                '    - "{selector}"\n    - "{nowhere}"'),
                encoding="utf-8")
            with self.assertRaises(ContractRejected) as ctx:
                start_packs(new_store(), target=load_target(copy),
                            change_dir=copy / "change", base_revision="abc")
        self.assertEqual(ctx.exception.code, "unrenderable_check")

    def test_the_selector_reaches_the_prerun_checks(self) -> None:
        record = start_packs(new_store(), target=load_target(TARGET_ROOT),
                             change_dir=TARGET_ROOT / "change", base_revision="abc")[0]
        self.assertEqual(record["contract"]["prerun_checks"][0]["params"],
                         {"selector": "hello"})

    def test_a_different_base_revision_changes_the_contract(self) -> None:
        target = load_target(TARGET_ROOT)
        first = start_packs(new_store(), target=target,
                            change_dir=TARGET_ROOT / "change", base_revision="abc")[0]
        second = start_packs(new_store(), target=target,
                             change_dir=TARGET_ROOT / "change", base_revision="def")[0]
        self.assertNotEqual(first["contract_hash"], second["contract_hash"])


@unittest.skipUnless(HAS_FIXTURE, "the _fixture_min target package is not present")
class RealVerifyTest(unittest.TestCase):
    """ST26: a real verify invocation against the fixture target."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.target = load_target(TARGET_ROOT)
        self.workspace = self.tmp / "workspace"
        (self.workspace / "src").mkdir(parents=True)
        (self.workspace / "src" / "hello.txt").write_text("hello\n")
        shutil.copytree(TARGET_ROOT / "tests", self.workspace / "tests")
        self.artifacts = self.tmp / "artifacts"

    def run_check(self, word: str):
        prepare_artifacts_root(self.artifacts)
        plan = build_plan(
            pack_id="P1", change="change", target_id=self.target.target_id,
            contract_version=1, operation_id="OP-1", attempt_id="WA-1",
            candidate_fingerprint="sha256:" + "a" * 64,
            check={"check_id": "O1", "result_kind": "test", "selector": "hello",
                   "argv_template": ["tests/check.sh", "{word}"]},
            params={"word": word},
            subject={"kind": "obligation", "id": "O1"},
            workspace=self.workspace, artifacts_root=self.artifacts,
            target_package_digest="sha256:pkg", target_package_version=self.target.version,
            tool_versions={"sh": "sh-1"}, assigned_resources=[],
        )
        plan_path = self.tmp / "plan.json"
        out_path = self.tmp / "out.json"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        self.target.verify(plan_path, out_path)
        return plan, json.loads(out_path.read_text(encoding="utf-8"))

    def test_passing_check_produces_a_usable_pass(self) -> None:
        plan, envelope_out = self.run_check("hello")
        validate_verify(envelope_out, plan=plan, contract_tool_versions={"sh": "sh-1"},
                        contract_package_digest="sha256:pkg",
                        contract_package_version=self.target.version)
        self.assertEqual(envelope_out["result"]["status"], "PASS")

        decision = decide_usable(
            envelope=envelope_out, plan=plan,
            receipt=VerifyReceipt(exit_code=0, timed_out=False, signal=None,
                                  artifacts_root_was_empty=True, out_file_sha256="h"),
            contract_tool_versions={"sh": "sh-1"},
            contract_package_digest="sha256:pkg",
            contract_package_version=self.target.version,
            artifacts_root=self.artifacts, verified_release={}, engine_cleanup={},
        )
        self.assertTrue(decision["usable"], decision)

    # An honest failure is still a usable observation - it just is not a PASS.
    def test_failing_check_is_a_usable_fail(self) -> None:
        plan, envelope_out = self.run_check("goodbye")
        self.assertEqual(envelope_out["result"]["status"], "FAIL")
        validate_verify(envelope_out, plan=plan, contract_tool_versions={"sh": "sh-1"},
                        contract_package_digest="sha256:pkg",
                        contract_package_version=self.target.version)

    def test_every_junit_xml_is_reported(self) -> None:
        _, envelope_out = self.run_check("hello")
        listed = {a["path"] for a in envelope_out["artifacts"] if a["kind"] == "junit_xml"}
        on_disk = {str(p.relative_to(self.artifacts))
                   for p in self.artifacts.rglob("*.xml")}
        self.assertEqual(listed, on_disk)


@unittest.skipUnless(HAS_FIXTURE, "the _fixture_min target package is not present")
class FixtureTargetFlowTest(unittest.TestCase):
    """§3.7 acceptance: the ST1a sequence on the fixture target, engine unchanged."""

    def test_st1a_on_the_fixture_target_reaches_repair_pending(self) -> None:
        store = new_store()
        machine = PackMachine(store)
        target = load_target(TARGET_ROOT)
        record = start_packs(store, target=target, change_dir=TARGET_ROOT / "change",
                             base_revision="abc")[0]
        pack_id = record["pack"]

        # contract review passes
        store.update_pack(pack_id, state="claimed")
        store.bump(pack_id, "review_seq")
        store.create_attempt("WA-1", pack_id, base_revision="abc",
                             candidate_input=None, next_output_id=1)

        # apply -> submitted(1)
        op = "OP-apply-1"
        store.create_operation(op, pack_id, type="producer", stage="apply", attempt_id="WA-1")
        machine.commit_call_result(
            op, result="completed",
            call_binding={"stage": "apply", "attempt_id": "WA-1", "output_id": None,
                          "review_round": None, "contract_hash": record["contract_hash"]},
        )
        store.update_pack(pack_id, state="producing(1)")
        machine.consume_result(pack_id, op)
        store.freeze_output(pack_id, 1, "WA-1", "sha256:" + "b" * 64)
        store.update_pack(pack_id, state="submitted(1)")

        # prerun -> reviewing(1) -> review with one blocking finding
        store.update_pack(pack_id, state="reviewing(1)")
        state = machine.judge_round(
            pack_id,
            envelope(1, verdict="needs_repair", obligations={"O1": "FAIL"},
                     findings=[high_finding("F1-1", "L-1", "O1")]),
            history=[],
        )
        self.assertEqual(state, "repair_pending(1)")
        self.assertEqual(store.get_pack(pack_id)["review_round"], 1)


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(
    (Path(__file__).resolve().parents[2] / "targets" / "_fixture_min" / "profile.yaml").is_file(),
    "the _fixture_min target package is not present")
class SelectorAuthorityTest(unittest.TestCase):
    """The engine must not re-translate selectors the target reported."""

    def test_a_check_the_target_did_not_report_is_refused(self) -> None:
        from orchestrator.pack.errors import ContractRejected
        from orchestrator.pack.intake import assemble_contract
        from orchestrator.pack.target import load_target

        target = load_target(
            Path(__file__).resolve().parents[2] / "targets" / "_fixture_min")
        manifest = {
            "packs": [{
                "id": "P1", "contract_version": 1, "title": "t", "tasks": ["T1"],
                "files_writable": [], "depends": {"packs": [], "evidence": []},
                "tier": "high", "tier_basis_ref": None,
                "obligations": [{"id": "O1", "disposition": "active", "selector": "hello",
                                 "claimed_by": [], "source": "", "forbids": [],
                                 "verify_by": []}],
            }],
            "deferred": [], "evidence_declared": [],
        }
        with self.assertRaises(ContractRejected) as ctx:
            assemble_contract(
                target=target, change="c", pack_id="P1", manifest=manifest,
                requirement="sha256:" + "a" * 64, manifest_sha256_value="sha256:b",
                base_revision="abc", candidate_input=None, environment={},
                target_checks=[],  # the target reported nothing
            )
        self.assertEqual(ctx.exception.code, "untranslated_selector")
