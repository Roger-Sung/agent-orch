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
import sys
import tempfile
import unittest
import unittest.mock
import uuid
from pathlib import Path

from orchestrator.containment import (
    DEFAULT_SENTINEL_EXCLUDES,
    EXTRA_WRITE_ROOTS_ENV,
    SANDBOX_EXEC,
    SENTINEL_EXCLUDES_ENV,
    ContainmentConfigError,
    SandboxSetupError,
    _contains,
    extra_write_roots_from_env,
    validate_extra_write_roots,
    write_allowlist,
    ContainmentError,
    Sentinel,
    build_sandbox_profile,
    prepare_sandbox,
    protected_roots_from_env,
    sandbox_available,
    sentinel_excludes_from_env,
)
from orchestrator.controller import Controller, _protected_roots_support
from orchestrator.runner import (
    ALLOW_UNSANDBOXED_ENV,
    UnattendedConsentError,
    require_unattended_consent,
    GIT_IDENTITY_ENV,
    RunResult,
    SubprocessRunner,
    _git_identity,
    allow_unsandboxed_requested,
    classify_result,
    prepare_containment,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def uncontained_base() -> Path | None:
    """A directory the L1 allowlist does *not* cover, for fixtures to live in.

    The allowlist necessarily includes the temporary directories, so a fixture
    under /tmp proves nothing about whether writes are blocked — the test would
    pass while asserting nothing. Usually the repository itself is a fine host,
    but not when the repository has been cloned into /tmp, which is exactly
    what a clean-checkout check does. So pick the first candidate that is
    genuinely outside every allowlisted root, and skip loudly if there is none.
    """
    allowed = write_allowlist(Path("/nonexistent-workspace"), Path("/nonexistent-artifacts"))

    def covered(path: Path) -> bool:
        real = os.path.realpath(path)
        return any(real == root or real.startswith(root + os.sep) for root in allowed)

    for candidate in (REPO_ROOT, Path.home() / ".agent-orch-test-fixtures"):
        candidate.mkdir(parents=True, exist_ok=True)
        if not covered(candidate):
            return candidate
    return None

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
        host = uncontained_base()
        if host is None:
            self.skipTest("no directory outside the L1 allowlist is available to host fixtures")
        self.base = Path(tempfile.mkdtemp(prefix=".containment-test-", dir=host))
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

    def test_extra_excludes_are_split_trimmed_and_deduplicated(self) -> None:
        raw = os.pathsep.join((" /provider-state/ ", ".idea", "provider-state", ""))
        self.assertEqual(sentinel_excludes_from_env(raw), ("provider-state", ".idea"))

    def test_unset_extra_excludes_change_nothing(self) -> None:
        with unittest.mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(SENTINEL_EXCLUDES_ENV, None)
            self.assertEqual(sentinel_excludes_from_env(), ())

    def test_controller_adds_deployment_excludes_to_the_sentinel(self) -> None:
        home = self.base / "sentinel-home"
        home.mkdir()
        with unittest.mock.patch.dict(os.environ, {SENTINEL_EXCLUDES_ENV: "provider-state"}):
            controller = Controller(home, protected_roots=(self.base,))
            try:
                sentinel = controller._sentinel_for(self.workspace)
            finally:
                controller.close()
        self.assertIsNotNone(sentinel)
        self.assertEqual(sentinel.excludes, DEFAULT_SENTINEL_EXCLUDES + ("provider-state",))


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

    def run(self, owner, prompt, timeout, log_path, *, workspace=None, protected_roots=None):
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
            self.assertEqual(transitions[-1]["reason"], "protected_root_drift")
        finally:
            controller.close()

    def test_escape_is_quarantined_with_the_offending_paths_as_evidence(self) -> None:
        victim = self.protected / "victim.txt"
        controller, task_id = self._run_task(EscapingRunner(victim))
        try:
            rows = list(controller.conn.execute("SELECT * FROM quarantine WHERE task_id=?", (task_id,)))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["reason"], "protected_root_drift")
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
        return (log_path.with_suffix(".containment") / "gitconfig").read_text(encoding="utf-8")

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
            marker = (log_path.with_suffix(".containment") / "identity-source").read_text(encoding="utf-8")
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


class ExtraWriteRootsTest(SandboxFixture):
    """ORCH_EXTRA_WRITE_ROOTS: a narrow escape instead of turning L1 off.

    Real work writes outside the workspace — a JVM build wants its dependency
    cache — and the only previous escape was --allow-unsandboxed, which drops
    the layer for every path at once.
    """

    def setUp(self) -> None:
        super().setUp()
        self.tool_cache = self.base / "tool-cache"
        self.tool_cache.mkdir()
        (self.tool_cache / "cached.txt").write_text("original", encoding="utf-8")

    def test_unset_changes_nothing(self) -> None:
        with unittest.mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(EXTRA_WRITE_ROOTS_ENV, None)
            self.assertEqual(extra_write_roots_from_env(), ())
            profile = build_sandbox_profile(self.workspace, self.artifacts)
        self.assertNotIn(str(self.tool_cache), profile)

    @unittest.skipUnless(sandbox_available(), "sandbox-exec is unavailable on this host")
    def test_a_declared_root_becomes_writable(self) -> None:
        with unittest.mock.patch.dict(os.environ, {EXTRA_WRITE_ROOTS_ENV: str(self.tool_cache)}):
            decision = prepare_sandbox(self.workspace, self.artifacts)
            target = self.tool_cache / "cached.txt"
            result = subprocess.run(
                decision.wrap(["/bin/sh", "-c", f'echo built > "{target}"']),
                capture_output=True, text=True, check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(target.read_text(encoding="utf-8").strip(), "built")

    @unittest.skipUnless(sandbox_available(), "sandbox-exec is unavailable on this host")
    def test_everything_else_is_still_denied(self) -> None:
        """Declaring one root must not widen the rest of the allowlist."""
        with unittest.mock.patch.dict(os.environ, {EXTRA_WRITE_ROOTS_ENV: str(self.tool_cache)}):
            decision = prepare_sandbox(self.workspace, self.artifacts)
            result = subprocess.run(
                decision.wrap(["/bin/sh", "-c", f'echo mutated > "{self.protected_file}"']),
                capture_output=True, text=True, check=False,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.protected_file.read_text(encoding="utf-8"), "original")

    def test_multiple_roots_are_split_on_the_path_separator(self) -> None:
        second = self.base / "second-cache"
        second.mkdir()
        raw = f"{self.tool_cache}{os.pathsep}{second}"
        with unittest.mock.patch.dict(os.environ, {EXTRA_WRITE_ROOTS_ENV: raw}):
            self.assertEqual(extra_write_roots_from_env(), (self.tool_cache, second))

    def test_a_symlinked_root_is_resolved_into_the_profile(self) -> None:
        """The realpath trap: a rule built from an unresolved path never matches."""
        link = self.base / "cache-link"
        link.symlink_to(self.tool_cache)
        with unittest.mock.patch.dict(os.environ, {EXTRA_WRITE_ROOTS_ENV: str(link)}):
            profile = build_sandbox_profile(self.workspace, self.artifacts, extra_write_roots_from_env())
        self.assertIn(os.path.realpath(self.tool_cache), profile)
        self.assertNotIn(f'"{link}"', profile)

    def test_a_root_inside_a_protected_root_is_refused(self) -> None:
        inside = self.protected / "cache"
        inside.mkdir()
        with self.assertRaises(ContainmentError) as caught:
            validate_extra_write_roots([inside], [self.protected])
        self.assertIn("inside protected root", str(caught.exception))

    def test_a_root_containing_a_protected_root_is_refused(self) -> None:
        with self.assertRaises(ContainmentError) as caught:
            validate_extra_write_roots([self.base], [self.protected])
        self.assertIn("contains protected root", str(caught.exception))

    def test_an_adjacent_name_is_not_treated_as_an_overlap(self) -> None:
        """`/base/protected-notes` is not inside `/base/protected`."""
        neighbour = self.base / f"{self.protected.name}-notes"
        neighbour.mkdir()
        validate_extra_write_roots([neighbour], [self.protected])

    def test_a_symlink_cannot_smuggle_a_protected_root_past_the_check(self) -> None:
        link = self.base / "innocent-looking"
        link.symlink_to(self.protected)
        with self.assertRaises(ContainmentError):
            validate_extra_write_roots([link], [self.protected])

    def test_prepare_sandbox_refuses_a_conflicting_configuration(self) -> None:
        inside = self.protected / "cache"
        inside.mkdir()
        with unittest.mock.patch.dict(
            os.environ,
            {EXTRA_WRITE_ROOTS_ENV: str(inside), "ORCH_PROTECTED_ROOTS": str(self.protected)},
        ):
            with self.assertRaises(ContainmentError):
                prepare_sandbox(self.workspace, self.artifacts)

    def test_a_conflicting_configuration_stops_the_stage_with_its_own_reason(self) -> None:
        inside = self.protected / "cache"
        inside.mkdir()
        log_path = self.artifacts / "stage.log"
        with unittest.mock.patch.dict(
            os.environ, {EXTRA_WRITE_ROOTS_ENV: str(inside)}
        ):
            result = SubprocessRunner().run(
                "claude", "prompt", 5, log_path,
                workspace=self.workspace, protected_roots=(self.protected,),
            )
        self.assertEqual(result.containment_stop, "containment_config_conflict")
        classified = classify_result(result.exit_code, result.output, {"pass"}, source=result)
        self.assertEqual(classified.classification, "blocked")
        self.assertEqual(classified.reason, "containment_config_conflict")
        self.assertNotEqual(classified.reason, "runner_nonzero")


class WriteRootGuardEdgeCaseTest(SandboxFixture):
    """Ways a naive overlap check gets it wrong. Each of these was a real hole.

    All five were found by review rather than by the original tests, which is
    the argument for keeping them named after the mistake they prevent.
    """

    def test_the_filesystem_root_cannot_be_declared_as_a_write_root(self) -> None:
        """`outer + os.sep` for "/" builds "//", which matches nothing.

        Left unhandled, declaring "/" passes the guard and allowlists the entire
        filesystem — the widest possible hole, through the narrowest-looking
        setting.
        """
        with self.assertRaises(ContainmentConfigError):
            validate_extra_write_roots(["/"], [self.protected])
        self.assertTrue(_contains("/", str(self.protected)))

    def test_a_trailing_separator_does_not_hide_an_overlap(self) -> None:
        inside = self.protected / "cache"
        inside.mkdir()
        with self.assertRaises(ContainmentConfigError):
            validate_extra_write_roots([f"{inside}{os.sep}"], [f"{self.protected}{os.sep}"])

    def test_a_case_variant_is_treated_as_the_same_tree(self) -> None:
        """macOS is case-insensitive by default and realpath does not fold case."""
        variant = Path(str(self.protected).upper() if str(self.protected).islower()
                       else str(self.protected).lower())
        with self.assertRaises(ContainmentConfigError):
            validate_extra_write_roots([variant], [self.protected])

    def test_a_protected_root_symlinked_inside_a_write_root_is_refused(self) -> None:
        """Resolving alone hides this: the target is outside, the link is not.

        With the link inside writable space, a stage can re-point the sentinel
        anchor at a decoy tree of identical content and L2 would compare the
        wrong thing.
        """
        outside = self.base / "real-store"
        outside.mkdir()
        tools = self.base / "tools"
        tools.mkdir()
        anchor = tools / "store"
        anchor.symlink_to(outside)
        with self.assertRaises(ContainmentConfigError) as caught:
            validate_extra_write_roots([tools], [anchor])
        self.assertIn("contains protected root", str(caught.exception))

    def test_a_write_root_symlinked_to_a_protected_root_is_refused(self) -> None:
        link = self.base / "looks-harmless"
        link.symlink_to(self.protected)
        with self.assertRaises(ContainmentConfigError):
            validate_extra_write_roots([link], [self.protected])

    def test_the_same_directory_reached_two_ways_is_refused(self) -> None:
        with self.assertRaises(ContainmentConfigError) as caught:
            validate_extra_write_roots([self.protected / "." ], [self.protected])
        self.assertIn("protected root", str(caught.exception))

    def test_genuinely_separate_trees_are_still_allowed(self) -> None:
        """The guard must not become a blanket refusal."""
        sibling = self.base / f"{self.protected.name}-notes"
        sibling.mkdir()
        validate_extra_write_roots([sibling, self.base / "tool-cache"], [self.protected])


class SandboxSetupFailureTest(SandboxFixture):
    """A broken environment is not a broken configuration (finding 5)."""

    @unittest.skipUnless(sandbox_available(), "profile writing is only reached where L1 exists")
    def test_an_unwritable_artifact_directory_reports_a_setup_failure(self) -> None:
        blocked = self.base / "read-only"
        blocked.mkdir()
        blocked.chmod(0o500)
        try:
            with self.assertRaises(SandboxSetupError):
                prepare_sandbox(self.workspace, blocked / "artifacts")
        finally:
            blocked.chmod(0o700)

    def test_a_setup_failure_is_not_reported_as_a_config_conflict(self) -> None:
        blocked = self.base / "read-only-2"
        blocked.mkdir()
        blocked.chmod(0o500)
        log_path = blocked / "artifacts" / "stage.log"
        try:
            result = SubprocessRunner().run(
                "claude", "prompt", 5, log_path, workspace=self.workspace, protected_roots=()
            )
        finally:
            blocked.chmod(0o700)
        self.assertEqual(result.containment_stop, "sandbox_setup_failed")
        classified = classify_result(result.exit_code, result.output, {"pass"}, source=result)
        self.assertEqual(classified.reason, "sandbox_setup_failed")
        self.assertNotEqual(classified.reason, "containment_config_conflict")


class LegacyRunnerInterfaceTest(SandboxFixture):
    """A runner predating protected_roots must keep working (finding 4)."""

    def setUp(self) -> None:
        super().setUp()
        self.home = self.base / "home"
        self.home.mkdir()
        self.profile_path = self.base / "profile.yaml"
        self.profile_path.write_text(PROFILE_YAML, encoding="utf-8")
        self.input_path = self.base / "input.md"
        self.input_path.write_text("do the thing\n", encoding="utf-8")

    def _run(self, runner, protected):
        controller = Controller(self.home, runner=runner, protected_roots=protected)
        try:
            task_id = controller.submit(
                "containment-test", self.profile_path, self.input_path,
                task_id=str(uuid.uuid4()), workspace=self.workspace,
            )
            controller.run_until_stop(task_id)
            return controller.status(task_id)["task"]["status"]
        finally:
            controller.close()

    def test_a_legacy_runner_still_completes_the_task(self) -> None:
        class LegacyRunner:
            def preflight(self, owner, timeout=5):
                from orchestrator.runner import ProviderPreflightResult

                return ProviderPreflightResult("pass", "provider_preflight_pass", "", 0, ["fake"], None, 0, 0)

            def run(self, owner, prompt, timeout, log_path, *, workspace=None):
                log_path.parent.mkdir(parents=True, exist_ok=True)
                log_path.write_text("ORCHESTRATOR_OUTCOME: pass\n", encoding="utf-8")
                return RunResult(0, "ORCHESTRATOR_OUTCOME: pass\n", None, "raw", "raw")

        # Nothing is lost when the environment already declares the same roots:
        # the runner reads them there, so the guard still applies.
        with unittest.mock.patch.dict(os.environ, {"ORCH_PROTECTED_ROOTS": str(self.protected)}):
            self.assertEqual(self._run(LegacyRunner(), (self.protected,)), "done")

    def test_a_legacy_runner_is_refused_when_roots_would_be_dropped(self) -> None:
        """Fail-closed: a guard that silently stops applying is worse than a stop."""

        class LegacyRunner:
            def preflight(self, owner, timeout=5):
                from orchestrator.runner import ProviderPreflightResult

                return ProviderPreflightResult("pass", "provider_preflight_pass", "", 0, ["fake"], None, 0, 0)

            def run(self, owner, prompt, timeout, log_path, *, workspace=None):
                log_path.parent.mkdir(parents=True, exist_ok=True)
                log_path.write_text("ORCHESTRATOR_OUTCOME: pass\n", encoding="utf-8")
                return RunResult(0, "ORCHESTRATOR_OUTCOME: pass\n", None, "raw", "raw")

        with unittest.mock.patch.dict(os.environ, {"ORCH_PROTECTED_ROOTS": ""}):
            controller = Controller(self.home, runner=LegacyRunner(), protected_roots=(self.protected,))
            try:
                task_id = controller.submit(
                    "containment-test", self.profile_path, self.input_path,
                    task_id=str(uuid.uuid4()), workspace=self.workspace,
                )
                controller.run_until_stop(task_id)
                status = controller.status(task_id)
                self.assertEqual(status["task"]["status"], "blocked")
                self.assertEqual(status["transitions"][-1]["reason"], "runner_cannot_enforce_guard")
            finally:
                controller.close()

    def test_a_runner_without_protected_roots_still_works_when_none_are_configured(self) -> None:
        class LegacyRunner:
            def preflight(self, owner, timeout=5):
                from orchestrator.runner import ProviderPreflightResult

                return ProviderPreflightResult("pass", "provider_preflight_pass", "", 0, ["fake"], None, 0, 0)

            def run(self, owner, prompt, timeout, log_path, *, workspace=None):
                log_path.parent.mkdir(parents=True, exist_ok=True)
                log_path.write_text("ORCHESTRATOR_OUTCOME: pass\n", encoding="utf-8")
                return RunResult(0, "ORCHESTRATOR_OUTCOME: pass\n", None, "raw", "raw")

        self.assertEqual(self._run(LegacyRunner(), ()), "done")

    def test_the_capability_check_reads_the_signature(self) -> None:
        def legacy(owner, prompt, timeout, log_path, *, workspace=None):
            ...

        def current(owner, prompt, timeout, log_path, *, workspace=None, protected_roots=None):
            ...

        def wildcard(owner, prompt, timeout, log_path, **kwargs):
            ...

        self.assertEqual(_protected_roots_support(legacy), "none")
        self.assertEqual(_protected_roots_support(current), "explicit")
        self.assertEqual(_protected_roots_support(wildcard), "var_keyword")

    def test_a_positional_only_parameter_does_not_count_as_support(self) -> None:
        """The name is there but it cannot be passed by keyword.

        Counting it as support would raise TypeError at the call site — exactly
        the failure the check exists to prevent.
        """
        namespace: dict = {}
        exec(
            "def positional_only(owner, prompt, timeout, log_path, protected_roots, /):\n    ...",
            namespace,
        )
        self.assertEqual(_protected_roots_support(namespace["positional_only"]), "none")

    def test_a_typeerror_from_inside_the_runner_is_not_swallowed(self) -> None:
        """The reason the check is by signature and not by catching TypeError."""

        class BuggyRunner:
            def preflight(self, owner, timeout=5):
                from orchestrator.runner import ProviderPreflightResult

                return ProviderPreflightResult("pass", "provider_preflight_pass", "", 0, ["fake"], None, 0, 0)

            def run(self, owner, prompt, timeout, log_path, *, workspace=None, protected_roots=None):
                raise TypeError("a real bug inside the runner")

        status = self._run(BuggyRunner(), (self.protected,))
        self.assertEqual(status, "blocked")


class AliasedRootTest(SandboxFixture):
    """Path aliasing is not hypothetical on macOS (N2).

    Firmlinks make /Users/<name> and /System/Volumes/Data/Users/<name> the same
    directory, same device and inode, while realpath reports each spelling
    unchanged. Declaring the aliased spelling of an ancestor of a protected root
    therefore looked like an unrelated tree while granting write access straight
    through it. The guard walks ancestors and compares identity, so it does not
    need to know which mechanism produced the alias.
    """

    def test_identity_beats_spelling_for_an_ancestor(self) -> None:
        alias = Path("/System/Volumes/Data") / str(self.protected).lstrip(os.sep)
        if not alias.exists():
            self.skipTest("no firmlinked data volume on this host")
        self.assertNotEqual(os.path.realpath(alias), os.path.realpath(self.protected))
        with self.assertRaises(ContainmentConfigError):
            validate_extra_write_roots([alias.parent], [self.protected])

    def test_the_reverse_direction_is_also_caught(self) -> None:
        alias = Path("/System/Volumes/Data") / str(self.protected).lstrip(os.sep)
        if not alias.exists():
            self.skipTest("no firmlinked data volume on this host")
        with self.assertRaises(ContainmentConfigError):
            validate_extra_write_roots([self.protected / "cache"], [alias])

    def test_a_hardlinked_alias_of_a_protected_root_is_caught(self) -> None:
        """Mechanism-agnostic: same (dev, ino) reached by any means."""
        alias = self.base / "alias"
        try:
            os.link(self.protected, alias, follow_symlinks=False)
        except (OSError, NotImplementedError):
            self.skipTest("this filesystem does not allow directory hard links")
        with self.assertRaises(ContainmentConfigError):
            validate_extra_write_roots([alias], [self.protected])

    def test_unrelated_trees_on_the_same_volume_are_still_allowed(self) -> None:
        """The ancestor walk must not degrade into refusing everything."""
        cache = self.base / "unrelated-cache"
        cache.mkdir()
        validate_extra_write_roots([cache], [self.protected])


class ContainmentArtifactFailureTest(SandboxFixture):
    """Every containment artifact write, not just the hook (N5)."""

    def test_a_failure_writing_the_gitconfig_is_a_setup_failure(self) -> None:
        log_path = self.artifacts / "stage.log"
        with unittest.mock.patch(
            "pathlib.Path.write_text", side_effect=OSError(28, "No space left on device")
        ):
            with self.assertRaises(SandboxSetupError):
                prepare_containment(self.workspace, log_path)

    def test_that_failure_reaches_the_stage_as_a_setup_failure(self) -> None:
        log_path = self.artifacts / "stage.log"
        with unittest.mock.patch(
            "orchestrator.runner.prepare_containment",
            side_effect=SandboxSetupError("cannot write containment gitconfig: disk full"),
        ):
            result = SubprocessRunner().run(
                "claude", "prompt", 5, log_path, workspace=self.workspace, protected_roots=()
            )
        self.assertEqual(result.containment_stop, "sandbox_setup_failed")
        classified = classify_result(result.exit_code, result.output, {"pass"}, source=result)
        self.assertEqual(classified.reason, "sandbox_setup_failed")
        self.assertNotEqual(classified.reason, "containment_identity_invalid")


class UnattendedConsentTest(unittest.TestCase):
    """Unattended execution must be stated, not inherited.

    The launcher checks this before exec; the engine repeats it so a deployment
    with its own launcher cannot skip the acknowledgement by accident. Both are
    best-effort detection of *known* flags — a wrapper script or a renamed flag
    is invisible to them, which is why the launcher gate stays the first line.
    """

    def test_a_known_flag_without_consent_refuses(self) -> None:
        for command in (
            {"ORCH_CLAUDE_COMMAND": "claude -p --dangerously-skip-permissions"},
            {"ORCH_CODEX_COMMAND": "codex exec --approve-for-me"},
        ):
            with self.subTest(command=command):
                with self.assertRaises(UnattendedConsentError) as caught:
                    require_unattended_consent(command)
                self.assertIn("refusing to start", str(caught.exception))
                self.assertIn("ORCH_ALLOW_UNATTENDED=1", str(caught.exception))

    def test_consent_permits_it(self) -> None:
        require_unattended_consent(
            {
                "ORCH_CLAUDE_COMMAND": "claude -p --dangerously-skip-permissions",
                "ORCH_ALLOW_UNATTENDED": "1",
            }
        )

    def test_commands_without_a_known_flag_are_unaffected(self) -> None:
        require_unattended_consent(
            {"ORCH_CLAUDE_COMMAND": "claude -p --model m", "ORCH_CODEX_COMMAND": "codex exec"}
        )
        require_unattended_consent({})

    def test_the_error_names_which_command_carried_which_flag(self) -> None:
        with self.assertRaises(UnattendedConsentError) as caught:
            require_unattended_consent({"ORCH_CODEX_COMMAND": "codex exec --approve-for-me"})
        self.assertIn("ORCH_CODEX_COMMAND contains --approve-for-me", str(caught.exception))

    def test_consent_must_be_exactly_one(self) -> None:
        for value in ("", "0", "true", "yes", " "):
            with self.subTest(value=value):
                with self.assertRaises(UnattendedConsentError):
                    require_unattended_consent(
                        {
                            "ORCH_CODEX_COMMAND": "codex exec --approve-for-me",
                            "ORCH_ALLOW_UNATTENDED": value,
                        }
                    )

    def test_the_daemon_refuses_at_startup_with_the_launcher_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env = {k: v for k, v in os.environ.items() if not k.startswith("ORCH_")}
            env["ORCH_HOME"] = directory
            env["ORCH_CODEX_COMMAND"] = "codex exec --approve-for-me"
            result = subprocess.run(
                [sys.executable, "-m", "orchestrator", "daemon"],
                cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=60, check=False,
            )
        self.assertEqual(result.returncode, 78, result.stdout + result.stderr)
        self.assertIn("refusing to start", result.stderr)
