"""Acceptance tests for filesystem containment: L1 prevention, L2 detection.

Each test maps to one of the four conditions the containment work had to
satisfy, and the mapping is stated in the class docstrings so a reviewer can
check coverage without reading the implementation.

A note on temporary directories: the L1 allowlist deliberately includes /tmp
and $TMPDIR, because provider CLIs write there constantly. A test that put its
"protected" directory under /tmp would therefore prove nothing. These tests
create their sandbox fixtures under the repository instead, which is *not*
allowlisted, and clean them up afterwards.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
import unittest.mock
import uuid
from pathlib import Path

from orchestrator.containment import (
    DEFAULT_SENTINEL_EXCLUDES,
    SANDBOX_EXEC,
    ContainmentError,
    Sentinel,
    build_sandbox_profile,
    prepare_sandbox,
    protected_roots_from_env,
    sandbox_available,
)
from orchestrator.controller import Controller
from orchestrator.runner import (
    ALLOW_UNSANDBOXED_ENV,
    GIT_IDENTITY_ENV,
    RunResult,
    SubprocessRunner,
    _git_identity,
    allow_unsandboxed_requested,
    classify_result,
    prepare_containment,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

PROFILE_YAML = """\
version: 1
type: containment-test
initial_stage: work
max_transitions: 4
stages:
  work:
    owner: claude
    attempt_cap: 1
    timeout: 30
    prompt: "do the thing"
    outcomes:
      pass: done
  done:
    terminal: done
edge_caps:
  work.pass: 1
"""


class SandboxFixture(unittest.TestCase):
    """Base fixture: a workspace and a sibling protected tree, both outside /tmp."""

    def setUp(self) -> None:
        self.base = Path(tempfile.mkdtemp(prefix=".containment-test-", dir=REPO_ROOT))
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        self.artifacts = self.base / "artifacts"
        self.artifacts.mkdir()
        self.protected = self.base / "protected"
        self.protected.mkdir()
        self.protected_file = self.protected / "keep.txt"
        self.protected_file.write_text("original", encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.base, ignore_errors=True)

    def run_sandboxed(self, script: str) -> subprocess.CompletedProcess:
        decision = prepare_sandbox(self.workspace, self.artifacts)
        self.assertEqual(decision.mode, "sandboxed")
        return subprocess.run(
            decision.wrap(["/bin/sh", "-c", script]), capture_output=True, text=True, check=False
        )


@unittest.skipUnless(sandbox_available(), "sandbox-exec is unavailable on this host")
class L1PreventionTest(SandboxFixture):
    """Acceptance 1: a stage that tries to write outside its workspace fails."""

    def test_l1_blocks_write_outside_workspace(self) -> None:
        result = self.run_sandboxed(f'echo mutated > "{self.protected_file}"')
        self.assertNotEqual(result.returncode, 0, "write outside the workspace was permitted")
        self.assertEqual(self.protected_file.read_text(encoding="utf-8"), "original")

    def test_l1_blocks_creation_outside_workspace(self) -> None:
        target = self.protected / "new.txt"
        result = self.run_sandboxed(f'echo created > "{target}"')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(target.exists())

    def test_l1_blocks_a_sibling_whose_name_extends_the_workspace_name(self) -> None:
        """An over-broad prefix rule would let `/base/workspace-extra` through."""
        sibling = self.base / "workspace-extra"
        sibling.mkdir()
        target = sibling / "keep.txt"
        target.write_text("original", encoding="utf-8")
        result = self.run_sandboxed(f'echo mutated > "{target}"')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(target.read_text(encoding="utf-8"), "original")


@unittest.skipUnless(sandbox_available(), "sandbox-exec is unavailable on this host")
class L1LegitimateWorkTest(SandboxFixture):
    """Acceptance 3: legitimate work is unaffected."""

    def test_l1_allows_writes_inside_workspace(self) -> None:
        target = self.workspace / "output.txt"
        result = self.run_sandboxed(f'echo written > "{target}"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(target.read_text(encoding="utf-8").strip(), "written")

    def test_l1_allows_writes_to_the_stage_artifact_directory(self) -> None:
        target = self.artifacts / "stage.log"
        result = self.run_sandboxed(f'echo logged > "{target}"')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_l1_allows_writes_to_temporary_directories(self) -> None:
        result = self.run_sandboxed('t=$(mktemp) && echo scratch > "$t" && rm -f "$t"')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_profile_allows_every_provider_state_directory_that_exists(self) -> None:
        profile = build_sandbox_profile(self.workspace, self.artifacts)
        for candidate in ("~/.claude", "~/.codex", "~/Library/Caches/claude-cli-nodejs"):
            expanded = os.path.expanduser(candidate)
            if os.path.exists(expanded):
                self.assertIn(os.path.realpath(expanded), profile, f"{candidate} missing from the allowlist")


class SandboxFailClosedTest(SandboxFixture):
    """Acceptance 4: no sandbox and no explicit opt-out means the stage does not run."""

    def test_sandbox_unavailable_refuses_to_run(self) -> None:
        with unittest.mock.patch("orchestrator.containment.sandbox_available", return_value=False):
            decision = prepare_sandbox(self.workspace, self.artifacts)
        self.assertTrue(decision.blocks_run)
        self.assertEqual(decision.reason, "sandbox_unavailable")

    def test_explicit_opt_out_downgrades_instead_of_blocking(self) -> None:
        with unittest.mock.patch("orchestrator.containment.sandbox_available", return_value=False):
            decision = prepare_sandbox(self.workspace, self.artifacts, allow_unsandboxed=True)
        self.assertFalse(decision.blocks_run)
        self.assertEqual(decision.mode, "unsandboxed")

    def test_stop_reason_is_specific_and_not_a_generic_nonzero_exit(self) -> None:
        stopped = RunResult(None, "", None, "raw", "raw", containment_stop="sandbox_unavailable")
        classified = classify_result(None, "", {"pass"}, source=stopped)
        self.assertEqual(classified.classification, "blocked")
        self.assertEqual(classified.reason, "sandbox_unavailable")
        self.assertNotEqual(classified.reason, "runner_nonzero")

    def test_opt_out_requires_an_explicit_truthy_value(self) -> None:
        self.assertFalse(allow_unsandboxed_requested({}))
        self.assertFalse(allow_unsandboxed_requested({ALLOW_UNSANDBOXED_ENV: "0"}))
        self.assertFalse(allow_unsandboxed_requested({ALLOW_UNSANDBOXED_ENV: "maybe"}))
        self.assertTrue(allow_unsandboxed_requested({ALLOW_UNSANDBOXED_ENV: "1"}))


class SentinelTest(SandboxFixture):
    """Acceptance 2 and 3: detection catches what prevention did not."""

    def sentinel(self) -> Sentinel:
        return Sentinel(roots=(self.base,), workspace=self.workspace)

    def test_l2_detects_modification_outside_the_workspace(self) -> None:
        sentinel = self.sentinel()
        before = sentinel.snapshot()
        self.protected_file.write_text("mutated", encoding="utf-8")
        violations = sentinel.compare(before)
        self.assertEqual([v.kind for v in violations], ["modified"])
        self.assertEqual(violations[0].path, str(self.protected_file))

    def test_l2_detects_creation_and_deletion(self) -> None:
        sentinel = self.sentinel()
        before = sentinel.snapshot()
        (self.protected / "new.txt").write_text("new", encoding="utf-8")
        self.protected_file.unlink()
        kinds = sorted(v.kind for v in sentinel.compare(before))
        self.assertEqual(kinds, ["added", "removed"])

    def test_l2_ignores_work_inside_the_workspace(self) -> None:
        sentinel = self.sentinel()
        before = sentinel.snapshot()
        (self.workspace / "result.txt").write_text("legitimate", encoding="utf-8")
        self.assertEqual(sentinel.compare(before), [])

    def test_l2_ignores_a_touch_that_did_not_change_content(self) -> None:
        """mtime moves constantly; only a content change is a violation."""
        sentinel = self.sentinel()
        before = sentinel.snapshot()
        stat = self.protected_file.stat()
        os.utime(self.protected_file, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
        self.assertEqual(sentinel.compare(before), [])

    def test_l2_ignores_excluded_subtrees(self) -> None:
        cache = self.protected / "__pycache__"
        cache.mkdir()
        sentinel = self.sentinel()
        before = sentinel.snapshot()
        (cache / "junk.pyc").write_bytes(b"noise")
        self.assertEqual(sentinel.compare(before), [])

    def test_l2_still_flags_a_path_adjacent_to_an_exclusion(self) -> None:
        """An exclusion must not swallow its neighbours by prefix.

        `__pycache__` is excluded; `__pycache__-notes` is not, and a rule
        written as a string prefix would wrongly cover both. An exclusion that
        is too broad silently reopens the hole this layer closes.
        """
        neighbour = self.protected / "__pycache__-notes"
        neighbour.mkdir()
        watched = neighbour / "note.txt"
        watched.write_text("original", encoding="utf-8")
        sentinel = self.sentinel()
        before = sentinel.snapshot()
        watched.write_text("mutated", encoding="utf-8")
        violations = sentinel.compare(before)
        self.assertEqual([v.path for v in violations], [str(watched)])

    def test_l2_does_not_flag_provider_state_directory_writes(self) -> None:
        """Provider CLIs write to their own state directory during a stage.

        Those writes are legitimate. They are only ever seen by L2 if a
        deployment declares a root containing them, so the check here is that
        a declared exclusion works, not that the CLI is special-cased.
        """
        state_dir = self.base / "provider-state"
        state_dir.mkdir()
        (state_dir / "session.json").write_text("{}", encoding="utf-8")
        sentinel = Sentinel(
            roots=(self.base,), workspace=self.workspace, excludes=DEFAULT_SENTINEL_EXCLUDES + ("provider-state",)
        )
        before = sentinel.snapshot()
        (state_dir / "session.json").write_text('{"turn": 2}', encoding="utf-8")
        self.assertEqual(sentinel.compare(before), [])

    def test_large_files_cannot_be_proven_unchanged_so_they_fail_closed(self) -> None:
        big = self.protected / "big.bin"
        big.write_bytes(b"a" * 2048)
        sentinel = Sentinel(roots=(self.base,), workspace=self.workspace, hash_below_bytes=1024)
        before = sentinel.snapshot()
        stat = big.stat()
        os.utime(big, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
        violations = sentinel.compare(before)
        self.assertEqual([v.kind for v in violations], ["modified"])

    def test_protected_roots_come_from_the_environment_and_default_to_none(self) -> None:
        self.assertEqual(protected_roots_from_env(""), ())
        roots = protected_roots_from_env(f"{self.base}{os.pathsep}{self.protected}")
        self.assertEqual(roots, (self.base, self.protected))


class EscapingRunner:
    """A stage that ignores its workspace and writes somewhere else entirely.

    This is the failure that motivated the whole layer, reproduced as a test
    double so the controller's response can be asserted without a real agent.
    """

    def __init__(self, target: Path) -> None:
        self.target = target

    def preflight(self, owner, timeout=5):
        from orchestrator.runner import ProviderPreflightResult

        return ProviderPreflightResult("pass", "provider_preflight_pass", "", 0, ["fake"], None, 0, 0)

    def run(self, owner, prompt, timeout, log_path, *, workspace=None):
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("ORCHESTRATOR_OUTCOME: pass\n", encoding="utf-8")
        self.target.write_text("written by an escaped stage", encoding="utf-8")
        # Note the stage reports success. That is the dangerous case.
        return RunResult(0, "ORCHESTRATOR_OUTCOME: pass\n", None, "raw", "raw")


class ControllerEscapeTest(SandboxFixture):
    """Acceptance 2: with L1 bypassed, L2 blocks and quarantines the task."""

    def setUp(self) -> None:
        super().setUp()
        self.home = self.base / "home"
        self.home.mkdir()
        self.profile_path = self.base / "profile.yaml"
        self.profile_path.write_text(PROFILE_YAML, encoding="utf-8")
        self.input_path = self.base / "input.md"
        self.input_path.write_text("do the thing\n", encoding="utf-8")

    def _run_task(self, runner) -> tuple[Controller, str]:
        controller = Controller(self.home, runner=runner, protected_roots=(self.protected,))
        task_id = controller.submit(
            "containment-test",
            self.profile_path,
            self.input_path,
            task_id=str(uuid.uuid4()),
            workspace=self.workspace,
        )
        controller.run_until_stop(task_id)
        return controller, task_id

    def test_escape_blocks_the_task_with_a_specific_reason(self) -> None:
        controller, task_id = self._run_task(EscapingRunner(self.protected / "victim.txt"))
        try:
            status = controller.status(task_id)
            self.assertEqual(status["task"]["status"], "blocked")
            transitions = status["transitions"]
            self.assertEqual(transitions[-1]["reason"], "workspace_escape")
        finally:
            controller.close()

    def test_escape_is_quarantined_with_the_offending_paths_as_evidence(self) -> None:
        victim = self.protected / "victim.txt"
        controller, task_id = self._run_task(EscapingRunner(victim))
        try:
            rows = list(controller.conn.execute("SELECT * FROM quarantine WHERE task_id=?", (task_id,)))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["reason"], "workspace_escape")
            evidence = json.loads(Path(rows[0]["artifact_path"]).read_text(encoding="utf-8"))
            self.assertEqual([item["path"] for item in evidence["violations"]], [str(victim)])
            self.assertEqual(evidence["violations"][0]["kind"], "added")
        finally:
            controller.close()

    def test_a_stage_that_stays_inside_its_workspace_is_not_penalised(self) -> None:
        controller, task_id = self._run_task(EscapingRunner(self.workspace / "legit.txt"))
        try:
            status = controller.status(task_id)
            self.assertEqual(status["task"]["status"], "done")
            self.assertEqual(list(controller.conn.execute("SELECT * FROM quarantine")), [])
        finally:
            controller.close()


class GitIdentityTest(SandboxFixture):
    """docs/decisions/0001: synthetic by default, real identity only on request."""

    def _write_containment(self) -> str:
        log_path = self.artifacts / "stage.log"
        prepare_containment(self.workspace, log_path)
        return (log_path.parent / "containment" / "gitconfig").read_text(encoding="utf-8")

    def test_default_identity_is_synthetic(self) -> None:
        with unittest.mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(GIT_IDENTITY_ENV, None)
            config = self._write_containment()
        self.assertIn("name = agent-orch", config)
        self.assertIn("email = orchestrator@orch.invalid", config)

    def test_explicit_literal_is_used_verbatim(self) -> None:
        with unittest.mock.patch.dict(os.environ, {GIT_IDENTITY_ENV: "Test Person <t@x.invalid>"}):
            config = self._write_containment()
        self.assertIn("name = Test Person", config)
        self.assertIn("email = t@x.invalid", config)

    def test_global_opt_in_resolves_from_git_config(self) -> None:
        identity = _git_identity({GIT_IDENTITY_ENV: "global"})
        self.assertEqual(identity["source"], "global")

    def test_malformed_value_fails_closed_instead_of_falling_back(self) -> None:
        with self.assertRaises(ContainmentError):
            _git_identity({GIT_IDENTITY_ENV: "not-an-identity"})

    def test_only_the_identity_source_is_recorded_not_the_resolved_value(self) -> None:
        with unittest.mock.patch.dict(os.environ, {GIT_IDENTITY_ENV: "global"}):
            log_path = self.artifacts / "stage.log"
            prepare_containment(self.workspace, log_path)
            marker = (log_path.parent / "containment" / "identity-source").read_text(encoding="utf-8")
        self.assertEqual(marker.strip(), "global")

    def test_commit_still_works_with_the_synthetic_default(self) -> None:
        subprocess.run(["git", "init", "-q"], cwd=self.workspace, check=True)
        log_path = self.artifacts / "stage.log"
        env = prepare_containment(self.workspace, log_path)
        (self.workspace / "file.txt").write_text("content", encoding="utf-8")
        subprocess.run(["git", "add", "file.txt"], cwd=self.workspace, env=env, check=True)
        done = subprocess.run(
            ["git", "commit", "-m", "test"], cwd=self.workspace, env=env, capture_output=True, text=True
        )
        self.assertEqual(done.returncode, 0, done.stderr)


if __name__ == "__main__":
    unittest.main()
