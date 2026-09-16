"""Verify runner, resources and bootstrap (step 4)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from orchestrator.pack.bootstrap import launch_argv, self_check
from orchestrator.pack.resources import (
    RELEASED,
    STILL_PRESENT,
    UNKNOWN,
    DirectoryAdapter,
    MySQLSchemaAdapter,
    RedisPrefixAdapter,
    ResourceManager,
    cleanup_gate,
)
from orchestrator.pack.verify_runner import (
    VerifyReceipt,
    build_plan,
    check_artifact_completeness,
    decide_usable,
    enumerate_artifacts,
    expand_argv,
    invocation_id,
    prepare_artifacts_root,
    run_verify,
)

BOOTSTRAP = Path(__file__).resolve().parents[1] / "pack" / "bootstrap.py"


class FakeMySQL:
    def __init__(self) -> None:
        self.schemas: set[str] = set()
        self.reachable = True

    def execute(self, sql: str) -> None:
        if not self.reachable:
            raise ConnectionError("mysql unreachable")
        if sql.startswith("CREATE SCHEMA"):
            self.schemas.add(sql.split("`")[1])
        elif sql.startswith("DROP SCHEMA"):
            self.schemas.discard(sql.split("`")[1])

    def query(self, sql: str, params):
        if not self.reachable:
            raise ConnectionError("mysql unreachable")
        return [(name,) for name in self.schemas if name == params[0]]


class FakeRedis:
    def __init__(self) -> None:
        self.keys: set[str] = set()
        self.reachable = True

    def scan_iter(self, match: str):
        if not self.reachable:
            raise ConnectionError("redis unreachable")
        prefix = match.rstrip("*")
        return [key for key in sorted(self.keys) if key.startswith(prefix)]

    def delete(self, key: str) -> None:
        self.keys.discard(key)


class ResourceLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.mysql = FakeMySQL()
        self.redis = FakeRedis()
        self.manager = ResourceManager({
            "mysql_schema": MySQLSchemaAdapter(self.mysql),
            "redis_prefix": RedisPrefixAdapter(self.redis),
            "dir": DirectoryAdapter(Path(self._tmp.name)),
        })
        self.declarations = [
            {"resource_id": "db", "kind": "mysql_schema"},
            {"resource_id": "cache", "kind": "redis_prefix"},
            {"resource_id": "tool_home", "kind": "dir", "engine_owned": True},
        ]

    # The full loop the exit criteria name: create -> release -> observe absent.
    def test_create_release_verify(self) -> None:
        assigned = self.manager.assign("P2", "OP-1", self.declarations)
        db = next(r for r in assigned if r["resource_id"] == "db")
        self.assertIn(db["name"], self.mysql.schemas)

        self.mysql.execute(f"DROP SCHEMA IF EXISTS `{db['name']}`")
        self.assertEqual(self.manager.verify_released(assigned)["db"], RELEASED)

    def test_a_leaked_schema_is_seen(self) -> None:
        assigned = self.manager.assign("P2", "OP-1", self.declarations)
        self.assertEqual(self.manager.verify_released(assigned)["db"], STILL_PRESENT)

    # An unreachable probe must not be read as "gone".
    def test_unreachable_database_reports_unknown(self) -> None:
        assigned = self.manager.assign("P2", "OP-1", self.declarations)
        self.mysql.reachable = False
        self.assertEqual(self.manager.verify_released(assigned)["db"], UNKNOWN)

    def test_redis_prefix_release(self) -> None:
        assigned = self.manager.assign("P2", "OP-1", self.declarations)
        prefix = next(r for r in assigned if r["resource_id"] == "cache")["name"]
        self.redis.keys.update({f"{prefix}a", f"{prefix}b", "other:key"})
        self.assertEqual(self.manager.verify_released(assigned)["cache"], STILL_PRESENT)
        RedisPrefixAdapter(self.redis).release(prefix)
        self.assertEqual(self.manager.verify_released(assigned)["cache"], RELEASED)
        self.assertIn("other:key", self.redis.keys)

    def test_engine_owned_resources_are_released_by_the_engine(self) -> None:
        assigned = self.manager.assign("P2", "OP-1", self.declarations)
        results = self.manager.release_engine_owned(assigned)
        self.assertEqual(results, {"tool_home": RELEASED})
        # Target-owned resources are not touched by the engine's own cleanup.
        self.assertNotIn("db", results)

    def test_names_are_unique_per_operation(self) -> None:
        first = self.manager.assign("P2", "OP-1", self.declarations[:1])[0]["name"]
        second = self.manager.assign("P2", "OP-2", self.declarations[:1])[0]["name"]
        self.assertNotEqual(first, second)


class CleanupGateTest(unittest.TestCase):
    # A CLI that calls its own cleanup failed is not rescued by a clean check.
    def test_cli_reported_failure_blocks_even_when_released(self) -> None:
        ok, reasons = cleanup_gate(
            [{"resource_id": "db", "status": "failed"}], {"db": RELEASED}, {}
        )
        self.assertFalse(ok)
        self.assertIn("cli_cleanup_failed", reasons)

    # V19-V23: a false `released` is both a leak and a broken claim.
    def test_false_released_claim_is_named_separately(self) -> None:
        ok, reasons = cleanup_gate(
            [{"resource_id": "db", "status": RELEASED}], {"db": STILL_PRESENT}, {}
        )
        self.assertFalse(ok)
        self.assertIn("cleanup_claim_mismatch:db", reasons)
        self.assertIn("cleanup_not_released:db:still_present", reasons)

    def test_unknown_is_not_released(self) -> None:
        ok, reasons = cleanup_gate([{"resource_id": "db", "status": RELEASED}],
                                   {"db": UNKNOWN}, {})
        self.assertFalse(ok)

    def test_engine_cleanup_failure_blocks(self) -> None:
        ok, reasons = cleanup_gate([], {}, {"tool_home": STILL_PRESENT})
        self.assertFalse(ok)
        self.assertIn("engine_cleanup_failed:tool_home:still_present", reasons)

    def test_clean_run_passes(self) -> None:
        ok, reasons = cleanup_gate(
            [{"resource_id": "db", "status": RELEASED}], {"db": RELEASED},
            {"tool_home": RELEASED},
        )
        self.assertTrue(ok)
        self.assertEqual(reasons, [])


class PlanAndArtifactsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "artifacts"

    # Empty params hash to sha256("{}"), not to the empty string.
    def test_invocation_id_uses_canonical_params(self) -> None:
        self.assertEqual(invocation_id("O1", {}), "O1@44136fa355b3678a")
        self.assertNotEqual(invocation_id("O1", {"a": 1}), invocation_id("O1", {"a": 2}))

    def test_argv_expansion(self) -> None:
        self.assertEqual(
            expand_argv(["./build.sh", ":app:test", "--tests", "{cls}"], {"cls": "AT"}),
            ["./build.sh", ":app:test", "--tests", "AT"],
        )

    # A selector is a command *fragment*: passing ":m:test --tests X" as one
    # argv element hands the build tool a single nonsense argument.  Found by a
    # real reviewer pass over a production contract.
    def test_a_lone_placeholder_holding_a_fragment_expands_to_tokens(self) -> None:
        self.assertEqual(
            expand_argv(["./build.sh", "--no-daemon", "{selector}"],
                        {"selector": ":acme-payment:test --tests SomeTest"}),
            ["./build.sh", "--no-daemon", ":acme-payment:test", "--tests", "SomeTest"],
        )

    def test_an_embedded_placeholder_still_substitutes_in_place(self) -> None:
        self.assertEqual(
            expand_argv(["--tests", "pkg.{cls}"], {"cls": "AT"}), ["--tests", "pkg.AT"]
        )

    # An unresolved placeholder would run a command nobody wrote.
    def test_unresolved_placeholder_is_refused(self) -> None:
        with self.assertRaises(Exception):
            expand_argv(["./build.sh", "{missing}"], {"cls": "AT"})

    # V8: a non-empty artifacts root means last run's files could be reused.
    def test_artifacts_root_emptiness_is_recorded(self) -> None:
        self.assertTrue(prepare_artifacts_root(self.root))
        (self.root / "stale.xml").write_text("<x/>")
        self.assertFalse(prepare_artifacts_root(self.root))

    # EV-V-015: an unreported XML is how a failing test disappears.
    def test_unreported_junit_xml_is_detected(self) -> None:
        prepare_artifacts_root(self.root)
        (self.root / "test-results").mkdir()
        (self.root / "test-results" / "a.xml").write_text("<x/>")
        (self.root / "test-results" / "b.xml").write_text("<y/>")
        missing = check_artifact_completeness(
            self.root, [{"path": "test-results/a.xml", "sha256": "x"}])
        self.assertEqual(missing, ["test-results/b.xml"])

    def test_enumerate_artifacts_hashes_each_file(self) -> None:
        prepare_artifacts_root(self.root)
        (self.root / "log.txt").write_text("hello")
        found = enumerate_artifacts(self.root)
        self.assertEqual(found[0]["path"], "log.txt")
        self.assertEqual(len(found[0]["sha256"]), 64)


class UsabilityTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "artifacts"
        prepare_artifacts_root(self.root)
        (self.root / "test-results").mkdir()
        (self.root / "test-results" / "a.xml").write_text("<x/>")

    def plan(self) -> dict:
        return {
            "verify_plan_id": "VP-OP-1", "operation_id": "OP-1",
            "invocation_id": "O1@44136fa355b3678a",
            "candidate_fingerprint": "sha256:" + "a" * 64,
            "result_kind": "test", "subject": {"kind": "obligation", "id": "O1"},
            "workspace": "/w", "expected_argv": ["./build.sh", ":app:test"],
            "selector": ":app:test",
            "assigned_resources": [{"resource_id": "db"}],
        }

    def envelope(self, **overrides) -> dict:
        base = {
            "verify_plan_id": "VP-OP-1", "operation_id": "OP-1",
            "invocation_id": "O1@44136fa355b3678a",
            "candidate_fingerprint": "sha256:" + "a" * 64,
            "result_kind": "test", "subject": {"kind": "obligation", "id": "O1"},
            "target_package_digest": "sha256:p", "target_package_version": "0.1.0",
            "tool_versions": {"jdk": "21"},
            "execution": {"cwd": "/w", "argv": ["./build.sh", ":app:test"], "timed_out": False,
                          "signal": None, "exit_code": 0, "outcome": "completed"},
            "result": {"status": "PASS", "selector": ":app:test",
                       "tests": {"total": 1, "passed": 1, "failed": 0, "skipped": 0, "errors": 0}},
            "artifacts": [{"path": "test-results/a.xml", "kind": "junit_xml"}],
            "cleanup": [{"resource_id": "db", "status": RELEASED, "evidence": "SHOW SCHEMAS empty"}],
        }
        base.update(overrides)
        return base

    def decide(self, *, receipt=None, verified=None, engine=None, envelope=None):
        return decide_usable(
            envelope=envelope or self.envelope(), plan=self.plan(),
            receipt=receipt or VerifyReceipt(exit_code=0, timed_out=False, signal=None,
                                             artifacts_root_was_empty=True, out_file_sha256="h"),
            contract_tool_versions={"jdk": "21"},
            contract_package_digest="sha256:p", contract_package_version="0.1.0",
            artifacts_root=self.root,
            verified_release=verified if verified is not None else {"db": RELEASED},
            engine_cleanup=engine or {},
        )

    def test_clean_run_is_usable(self) -> None:
        self.assertTrue(self.decide()["usable"])

    # A killed CLI cannot vouch for anything it printed - transport first.
    def test_killed_cli_fails_at_transport(self) -> None:
        receipt = VerifyReceipt(exit_code=None, timed_out=False, signal=9,
                                artifacts_root_was_empty=True, out_file_sha256="h")
        result = self.decide(receipt=receipt)
        self.assertEqual(result["stage"], "transport")
        self.assertIn("cli_signalled_9", result["reasons"])

    def test_timeout_fails_at_transport(self) -> None:
        receipt = VerifyReceipt(exit_code=None, timed_out=True, signal=None,
                                artifacts_root_was_empty=True, out_file_sha256="h")
        self.assertEqual(self.decide(receipt=receipt)["stage"], "transport")

    # V8: a dirty root disqualifies before any content is trusted.
    def test_non_empty_artifacts_root_fails_at_transport(self) -> None:
        receipt = VerifyReceipt(exit_code=0, timed_out=False, signal=None,
                                artifacts_root_was_empty=False, out_file_sha256="h")
        result = self.decide(receipt=receipt)
        self.assertEqual(result["stage"], "transport")
        self.assertIn("artifacts_root_not_empty", result["reasons"])

    def test_schema_violation_is_reported_with_its_code(self) -> None:
        envelope = self.envelope()
        envelope["execution"]["argv"] = ["./build.sh", "somethingelse"]
        result = self.decide(envelope=envelope)
        self.assertEqual(result["stage"], "schema")
        self.assertEqual(result["reasons"], ["EV-V-004"])

    def test_unreported_artifact_blocks(self) -> None:
        (self.root / "test-results" / "b.xml").write_text("<y/>")
        result = self.decide()
        self.assertEqual(result["stage"], "schema")
        self.assertTrue(result["reasons"][0].startswith("EV-V-015"))

    # A leaked resource must not back a PASS, however green the result reads.
    def test_leaked_resource_blocks_a_passing_run(self) -> None:
        result = self.decide(verified={"db": STILL_PRESENT})
        self.assertEqual(result["stage"], "cleanup")
        self.assertFalse(result["usable"])

    def test_engine_cleanup_failure_blocks(self) -> None:
        result = self.decide(engine={"tool_home": STILL_PRESENT})
        self.assertEqual(result["stage"], "cleanup")

    # An honest non-PASS is still a usable observation.
    def test_a_failing_test_run_is_usable(self) -> None:
        envelope = self.envelope()
        envelope["result"] = {"status": "FAIL", "selector": ":app:test",
                              "tests": {"total": 1, "passed": 0, "failed": 1,
                                        "skipped": 0, "errors": 0}}
        self.assertTrue(self.decide(envelope=envelope)["usable"])


class RunVerifyTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "artifacts"
        self.out = Path(self._tmp.name) / "out.json"

    def test_missing_out_file_leaves_no_envelope(self) -> None:
        envelope, receipt = run_verify(
            plan={"x": 1}, artifacts_root=self.root, out_path=self.out,
            spawn=lambda plan: {"exit_code": 1},
        )
        self.assertIsNone(envelope)
        self.assertFalse(receipt.transport_ok()[0])

    def test_envelope_and_hash_are_read_back(self) -> None:
        def spawn(plan):
            self.out.write_text(json.dumps({"result_kind": "test"}))
            return {"exit_code": 0}

        envelope, receipt = run_verify(
            plan={"x": 1}, artifacts_root=self.root, out_path=self.out, spawn=spawn)
        self.assertEqual(envelope["result_kind"], "test")
        self.assertEqual(len(receipt.out_file_sha256), 64)
        self.assertTrue(receipt.transport_ok()[0])


class BootstrapTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.pycache = self.root / "pycache"

    def launch(self, entry: str, args: tuple[str, ...] = ()) -> subprocess.CompletedProcess:
        argv = launch_argv(sys.executable, str(BOOTSTRAP), str(self.root), entry,
                           str(self.pycache), args)
        env = dict(os.environ)
        env["ORCH_EXPECTED_PYCACHE_PREFIX"] = str(self.pycache)
        return subprocess.run(argv, capture_output=True, text=True, env=env)

    def test_argv_uses_the_X_option_not_the_env_var(self) -> None:
        argv = launch_argv("python3", "/b.py", "/pkg", "e.py", "/pc")
        # -I ignores PYTHONPYCACHEPREFIX but honours -X, so the option is the
        # only form that actually takes effect.
        self.assertIn("-X", argv)
        self.assertIn("pycache_prefix=/pc", argv)
        self.assertEqual(argv[1:4], ["-I", "-S", "-B"])

    # ID32c: the entry runs with the package root on sys.path.
    def test_entry_runs_with_the_package_root(self) -> None:
        (self.root / "helper.py").write_text("VALUE = 7\n")
        (self.root / "entry.py").write_text(
            "import helper, sys\nprint(helper.VALUE)\n")
        result = self.launch("entry.py")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "7")

    # ID32d: the four interpreter guarantees actually hold in the child.
    def test_child_reports_the_expected_flags(self) -> None:
        (self.root / "entry.py").write_text(
            "import sys\n"
            "print(sys.flags.isolated, sys.flags.no_site, sys.flags.dont_write_bytecode,"
            " sys.pycache_prefix)\n"
        )
        result = self.launch("entry.py")
        self.assertEqual(result.returncode, 0, result.stderr)
        isolated, no_site, no_bytecode, prefix = result.stdout.split()
        self.assertEqual((isolated, no_site, no_bytecode), ("1", "1", "1"))
        self.assertEqual(prefix, str(self.pycache))

    # ID32e: a module outside the root is refused rather than imported.
    def test_module_outside_the_root_is_refused(self) -> None:
        outside = Path(self._tmp.name).parent / "orch_outside_pkg"
        outside.mkdir(exist_ok=True)
        (outside / "sneaky.py").write_text("VALUE = 1\n")
        self.addCleanup(lambda: (outside / "sneaky.py").unlink(missing_ok=True))
        (self.root / "entry.py").write_text(
            f"import sys\nsys.path.append({str(outside)!r})\n"
            "try:\n    import sneaky\n    print('IMPORTED')\n"
            "except ImportError as exc:\n    print('REFUSED')\n"
        )
        result = self.launch("entry.py")
        self.assertEqual(result.stdout.strip(), "REFUSED", result.stderr)

    # The self-check reports rather than assumes; the child cannot fix its flags.
    def test_self_check_reports_missing_guarantees(self) -> None:
        problems = self_check("/expected/prefix")
        self.assertTrue(any("pycache_prefix" in p for p in problems))

    def test_bad_usage_exits_two(self) -> None:
        result = subprocess.run(
            [sys.executable, str(BOOTSTRAP)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()


class DependencyCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.source = self.root / "modules-2"
        (self.source / "files-2.1").mkdir(parents=True)
        (self.source / "files-2.1" / "a.jar").write_bytes(b"jar")

    def test_warm_copies_and_makes_read_only(self) -> None:
        from orchestrator.pack.resources import warm_dependency_cache

        target = self.root / "ro-dep-cache"
        result = warm_dependency_cache(self.source, target)
        self.addCleanup(lambda: subprocess.run(["chmod", "-R", "u+w", str(target)]))
        self.assertEqual(result["files"], 1)
        copied = target / "files-2.1" / "a.jar"
        self.assertEqual(copied.read_bytes(), b"jar")
        # Read-only is the point: a verify run must not be able to mutate the
        # cache whose digest is part of the environment identity.
        self.assertFalse(os.access(copied, os.W_OK))

    def test_missing_source_is_an_error(self) -> None:
        from orchestrator.pack.resources import warm_dependency_cache

        with self.assertRaises(FileNotFoundError):
            warm_dependency_cache(self.root / "nope", self.root / "out")

    def test_digest_is_recorded_when_a_function_is_given(self) -> None:
        from orchestrator.pack.resources import warm_dependency_cache

        target = self.root / "cache2"
        result = warm_dependency_cache(self.source, target, digest_fn=lambda p: "sha256:fake")
        self.addCleanup(lambda: subprocess.run(["chmod", "-R", "u+w", str(target)]))
        self.assertEqual(result["digest"], "sha256:fake")


def _mysql_available() -> bool:
    try:
        result = subprocess.run(
            ["mysql", "-h", "127.0.0.1", "-P", "3306", "-uroot", "-proot", "-e", "SELECT 1"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


class MySQLCliConnector:
    """Minimal connector over the mysql client, so no driver dependency is added."""

    def __init__(self) -> None:
        self.base = ["mysql", "-h", "127.0.0.1", "-P", "3306", "-uroot", "-proot",
                     "--batch", "--skip-column-names"]

    def _run(self, sql: str) -> subprocess.CompletedProcess:
        return subprocess.run(self.base + ["-e", sql], capture_output=True, text=True, timeout=20)

    def execute(self, sql: str) -> None:
        result = self._run(sql)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip())

    def query(self, sql: str, params):
        rendered = sql.replace("%s", "'" + str(params[0]).replace("'", "''") + "'")
        result = self._run(rendered)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip())
        return [line for line in result.stdout.splitlines() if line.strip()]


@unittest.skipUnless(_mysql_available(), "local MySQL on 127.0.0.1:3306 is not reachable")
class RealMySQLReleaseTest(unittest.TestCase):
    """Step 4's exit criterion, against a real server rather than a fake.

    The fakes prove the branch logic; only a real server proves the SQL, the
    quoting and the probe actually do what the branch assumes - which is the
    half that decides whether a leaked schema is ever noticed.
    """

    def setUp(self) -> None:
        self.connector = MySQLCliConnector()
        self.manager = ResourceManager({"mysql_schema": MySQLSchemaAdapter(self.connector)})
        self.declarations = [{"resource_id": "db", "kind": "mysql_schema"}]
        self.assigned: list = []
        self.addCleanup(self._drop_all)

    def _drop_all(self) -> None:
        for resource in self.assigned:
            try:
                self.connector.execute(f"DROP SCHEMA IF EXISTS `{resource['name']}`")
            except RuntimeError:
                pass

    def test_create_release_then_observe_absent(self) -> None:
        self.assigned = self.manager.assign("P2", "OP-REAL-1", self.declarations)
        name = self.assigned[0]["name"]

        # Created: the probe must see it, or a leak would never be detectable.
        self.assertEqual(self.manager.verify_released(self.assigned)["db"], STILL_PRESENT)
        self.assertIn(name, self.connector.query("SHOW SCHEMAS LIKE %s", (name,)))

        MySQLSchemaAdapter(self.connector).release(name)
        self.assertEqual(self.manager.verify_released(self.assigned)["db"], RELEASED)
        self.assertEqual(self.connector.query("SHOW SCHEMAS LIKE %s", (name,)), [])

    def test_an_unreleased_schema_fails_the_cleanup_gate(self) -> None:
        self.assigned = self.manager.assign("P2", "OP-REAL-2", self.declarations)
        verified = self.manager.verify_released(self.assigned)
        ok, reasons = cleanup_gate(
            [{"resource_id": "db", "status": RELEASED, "evidence": "claimed"}], verified, {}
        )
        self.assertFalse(ok)
        self.assertIn("cleanup_claim_mismatch:db", reasons)

    def test_probe_failure_reports_unknown_not_released(self) -> None:
        class Unreachable:
            def execute(self, sql): raise RuntimeError("down")
            def query(self, sql, params): raise RuntimeError("down")

        manager = ResourceManager({"mysql_schema": MySQLSchemaAdapter(Unreachable())})
        assigned = [{"resource_id": "db", "kind": "mysql_schema",
                     "name": "orch_probe_unreachable", "engine_owned": False}]
        self.assertEqual(manager.verify_released(assigned)["db"], UNKNOWN)
