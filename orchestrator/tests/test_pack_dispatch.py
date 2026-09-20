"""Provider dispatch through the engine's runner and containment (step 7)."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

from orchestrator.pack.dispatch import (
    OperationPaths,
    PackRunner,
    build_env,
    dispatch,
    masked,
    write_roots,
)
from orchestrator.pack.errors import PackError
from orchestrator.pack.provider_adapters import ClaudeAdapter, CodexAdapter
from orchestrator.runner import _child_env


class OperationPathsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_directories_are_fresh_per_operation(self) -> None:
        first = OperationPaths(self.root, "OP-1").create()
        second = OperationPaths(self.root, "OP-2").create()
        self.assertNotEqual(first.home, second.home)
        for path in (first.tmp, first.home, first.artifacts):
            self.assertTrue(path.is_dir())

    def test_home_seeds_are_copied_from_the_package(self) -> None:
        package = self.root / "pkg"
        (package / "seeds").mkdir(parents=True)
        (package / "seeds" / "a.conf").write_text("value=1\n")
        paths = OperationPaths(self.root, "OP-3").create(
            home_seeds=[{"relpath": ".toolrc", "package_relpath": "seeds/a.conf"}],
            package_root=package,
        )
        self.assertEqual((paths.home / ".toolrc").read_text(), "value=1\n")

    def test_seeds_without_a_package_root_are_refused(self) -> None:
        with self.assertRaises(PackError):
            OperationPaths(self.root, "OP-4").create(
                home_seeds=[{"relpath": ".x", "package_relpath": "s"}])


class WriteRootsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.paths = OperationPaths(Path(self._tmp.name), "OP-1")

    # A reviewer that could write the tree could change what it is judging.
    def test_reviewer_roles_get_no_extra_root(self) -> None:
        self.assertEqual(write_roots("reviewer", self.paths), ())
        self.assertEqual(write_roots("contract_review", self.paths), ())

    def test_producer_gets_its_own_temp_and_home(self) -> None:
        self.assertEqual(write_roots("producer", self.paths),
                         (self.paths.tmp, self.paths.home))

    # Verify additionally owns the artifacts root, which is where its evidence
    # lands; the producer must not be able to write there.
    def test_verify_additionally_gets_the_artifacts_root(self) -> None:
        roots = write_roots("verify", self.paths)
        self.assertIn(self.paths.artifacts, roots)
        self.assertNotIn(self.paths.artifacts, write_roots("producer", self.paths))


class EnvironmentTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.paths = OperationPaths(Path(self._tmp.name), "OP-1").create()

    def policy(self) -> dict:
        return {
            "set": {"LANG": "C.UTF-8", "TMPDIR": "{op_tmp}", "HOME": "{op_home}"},
            "secret_refs": {"ORCH_DB_PASSWORD": {"source": "keychain"}},
        }

    def test_placeholders_resolve_to_this_operation(self) -> None:
        env = build_env(self.policy(), self.paths, {"ORCH_DB_PASSWORD": "s3cr3t"})
        self.assertEqual(env["TMPDIR"], str(self.paths.tmp))
        self.assertEqual(env["HOME"], str(self.paths.home))

    def test_a_declared_secret_must_be_supplied(self) -> None:
        with self.assertRaises(PackError):
            build_env(self.policy(), self.paths, {})

    # Zero inheritance: the ambient environment must not reach the child, or a
    # sealed contract would not determine what it runs.
    def test_the_ambient_environment_does_not_leak(self) -> None:
        env = build_env(self.policy(), self.paths, {"ORCH_DB_PASSWORD": "x"})
        self.assertEqual(set(env), {"LANG", "TMPDIR", "HOME", "ORCH_DB_PASSWORD"})

    # ...but the keys containment itself set are engine-supplied, not inherited,
    # and dropping them would remove the confinement this call relies on.
    def test_engine_set_containment_keys_survive_the_override(self) -> None:
        os.environ["ORCH_TEST_AMBIENT"] = "inherited"
        self.addCleanup(os.environ.pop, "ORCH_TEST_AMBIENT", None)
        containment = {
            "ORCH_TEST_AMBIENT": "inherited",
            "GIT_SSH_COMMAND": "/usr/bin/false",
            "ORCH_CONTAINMENT_SANDBOX": "sandboxed",
        }
        child = _child_env(containment, {"LANG": "C.UTF-8"})
        self.assertNotIn("ORCH_TEST_AMBIENT", child)
        self.assertEqual(child["GIT_SSH_COMMAND"], "/usr/bin/false")
        self.assertEqual(child["ORCH_CONTAINMENT_SANDBOX"], "sandboxed")
        self.assertEqual(child["LANG"], "C.UTF-8")

    def test_no_override_leaves_the_containment_env_alone(self) -> None:
        containment = {"A": "1"}
        self.assertIs(_child_env(containment, None), containment)

    def test_masking_covers_what_the_engine_persists(self) -> None:
        text = masked("failed: s3cr3t", self.policy(), {"ORCH_DB_PASSWORD": "s3cr3t"})
        self.assertNotIn("s3cr3t", text)
        self.assertIn("failed", text)


class PackRunnerTest(unittest.TestCase):
    # The runner's single decision about what to execute is `_command`;
    # overriding it keeps containment, the live stream and classification
    # exactly as the legacy path has them.
    def test_the_adapter_argv_is_what_runs(self) -> None:
        adapter = ClaudeAdapter(binary="claude", model="claude-opus-5")
        argv = adapter.command(cwd="/w")
        runner = PackRunner(argv)
        self.assertEqual(runner._command("claude"), argv)

    def test_a_codex_reviewer_keeps_its_read_only_sandbox(self) -> None:
        adapter = CodexAdapter(binary="codex", model="gpt-6-astra")
        argv = adapter.command(cwd="/w", sandbox="read-only")
        self.assertEqual(argv[argv.index("-s") + 1], "read-only")


class DispatchTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.paths = OperationPaths(self.root / "ops", "OP-1").create()

    def test_an_unknown_role_is_refused(self) -> None:
        adapter = ClaudeAdapter(binary="claude", model="m")
        with self.assertRaises(PackError):
            dispatch(adapter, role="auditor", prompt="x", workspace=self.workspace,
                     paths=self.paths, policy_env={})

    # The call goes through the runner, so a provider binary that does not
    # exist is a contained launch failure rather than an exception.
    def test_a_missing_binary_is_a_contained_failure(self) -> None:
        adapter = ClaudeAdapter(binary="/nonexistent/claude", model="m")
        result = dispatch(adapter, role="producer", prompt="hello",
                          workspace=self.workspace, paths=self.paths,
                          policy_env={"set": {"LANG": "C.UTF-8"}}, timeout=20)
        self.assertIsNotNone(result)
        self.assertNotEqual(result.exit_code, 0)
        self.assertTrue(self.paths.log.exists())


if __name__ == "__main__":
    unittest.main()


class StderrDoesNotDeadlockTest(unittest.TestCase):
    """A provider whose diagnostics exceed the pipe buffer must still finish.

    Only stdout is drained, so routing stderr to a second pipe stops the
    provider dead at 64 KiB - silently, with no output and no exit code, until
    the timeout fires.  Real Codex reviews emit far more than that on stderr,
    so this is the whole-run failure mode, not an edge case.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.paths = OperationPaths(Path(self._tmp.name), "OP-stderr").create()

    def test_large_stderr_completes_and_is_captured(self) -> None:
        noise = 200_000
        script = (
            "import sys;"
            f"sys.stderr.write('d' * {noise});"
            "sys.stderr.flush();"
            "sys.stdout.write('DONE')"
        )
        runner = PackRunner([sys.executable, "-c", script])
        result = runner.run(
            "reviewer",
            "",
            timeout=30,
            stderr_path=self.paths.stderr,
            log_path=self.paths.log,
        )
        self.assertFalse(result.timed_out, "provider blocked on an undrained stderr pipe")
        self.assertEqual(result.exit_code, 0)
        self.assertIn("DONE", result.output or "")
        self.assertEqual(self.paths.stderr.stat().st_size, noise)


class PromptGoesOnStdinTest(unittest.TestCase):
    """Both pack adapters read the prompt from stdin, so it must not be argv.

    A live run rejected every reviewer call before it started - `unexpected
    argument '<the whole prompt>'` - because the dispatch path appended the
    prompt the way the legacy providers take it.
    """

    def test_the_runner_passes_the_prompt_as_stdin_not_an_argument(self) -> None:
        captured: dict[str, object] = {}

        class Recording(PackRunner):
            def _spawn_and_capture(self, *args, **kwargs):  # pragma: no cover
                raise AssertionError("not reached")

        runner = Recording(["/bin/true", "-"])
        original = PackRunner.__mro__[1].run

        def fake(self, owner, prompt, *args, **kwargs):
            captured.update(kwargs)
            captured["argv"] = self._command(owner)
            return None

        PackRunner.__mro__[1].run = fake
        try:
            runner.run("codex", "THE PROMPT", timeout=1)
        finally:
            PackRunner.__mro__[1].run = original

        self.assertEqual(captured.get("stdin_payload"), "THE PROMPT")
        self.assertNotIn("THE PROMPT", captured["argv"],
                         "the prompt was passed as an argument as well")
