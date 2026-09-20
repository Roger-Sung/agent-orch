"""Which argv each role is launched with, and the boundary between the policies.

`configured_command` branched on the *role name*, which encodes execution-v1's
mapping of executor=codex and reviewer=claude. pack-v1 inverts it - producer is
Claude, both reviewers are Codex - so a pack-v1 producer was handed Codex's argv
and died on `--sandbox`, and a pack-v1 reviewer would have been handed Codex's
*executor* argv, which is `danger-full-access`: the opposite of the read-only
sandbox §3.1 requires of it.

The execution-v1 cases here are a golden record. They existed before this split
and must not move by a single argument.
"""
from __future__ import annotations

import unittest

from orchestrator.execution import ExecutionChoice
from orchestrator.execution_runner import configured_command
from orchestrator.pack.provider_adapters import ClaudeAdapter, CodexAdapter


class ExecutionV1ArgvIsUnchangedTest(unittest.TestCase):
    def test_the_executor_launches_codex_exactly_as_before(self) -> None:
        choice = ExecutionChoice("executor", "codex", "gpt-5.6-sol", "high",
                                 "gpt-5.6-sol", "high")
        self.assertEqual(
            configured_command(["/bin/codex", "exec"], choice),
            ["/bin/codex", "exec", "--model", "gpt-5.6-sol",
             "-c", 'model_reasoning_effort="high"',
             "--sandbox", "danger-full-access", "-c", 'approval_policy="never"'])

    def test_the_reviewer_launches_claude_exactly_as_before(self) -> None:
        choice = ExecutionChoice("reviewer", "claude", "claude-fable-5-1", "high",
                                 "claude-fable-5-1", "high")
        self.assertEqual(
            configured_command(["/bin/claude", "-p"], choice),
            ["/bin/claude", "-p", "--model", "claude-fable-5-1", "--effort", "high",
             "--safe-mode", "--tools", "", "--output-format", "json"])


class PackV1ArgvFollowsTheRoleMatrixTest(unittest.TestCase):
    """IMPLEMENTATION-PLAN §3.1: what each pack role may do to the tree."""

    def test_the_producer_gets_claude_and_may_write(self) -> None:
        argv = ClaudeAdapter(binary="/bin/claude", model="claude-opus-5").command(cwd="/ws")
        self.assertEqual(argv[0], "/bin/claude")
        self.assertIn("-p", argv)
        self.assertNotIn("--sandbox", argv, "the flag the Claude CLI rejects")
        self.assertNotIn("--safe-mode", argv, "the producer has to be able to write")

    def test_both_reviewers_get_codex_read_only(self) -> None:
        argv = CodexAdapter(binary="/bin/codex", model="gpt-6-astra").command(
            cwd="/ws", last_message="/out.txt")
        self.assertEqual(argv[:2], ["/bin/codex", "exec"])
        # The whole point of the role: it must not be able to change the tree.
        self.assertIn("-s", argv)
        self.assertEqual(argv[argv.index("-s") + 1], "read-only")
        self.assertNotIn("danger-full-access", argv)
        for feature in ("browser_use", "computer_use", "apps"):
            self.assertIn(feature, argv, f"{feature} was not disabled")


class PackStagesLaunchThroughTheirAdaptersTest(unittest.TestCase):
    """The controller has to pick the launcher, not just own the right one.

    Both adapters already encoded §3.1; nothing consulted them, so every pack
    stage went out with execution-v1's argv.
    """

    def setUp(self) -> None:
        import tempfile
        from pathlib import Path

        from orchestrator.controller import Controller
        from orchestrator.pack.launch import launch_packs

        target = Path(__file__).resolve().parents[2] / "targets" / "_fixture_min"
        if not (target / "profile.yaml").is_file():
            self.skipTest("the fixture target is not present")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.workspace = Path(tmp.name) / "ws"
        self.workspace.mkdir(parents=True)
        self.controller = Controller(Path(tmp.name) / "home", runner=None)
        self.addCleanup(self.controller.close)
        import orchestrator.runner as _runner
        self.controller.runner = _runner.SubprocessRunner()
        launch_packs(
            self.controller, target_dir=target, change_dir=target / "change",
            workspace=self.workspace,
            profile_path=Path(__file__).resolve().parents[1] / "profiles" / "pack_v1.yaml",
            base_revision="abc", producer_model="claude-opus-5",
            reviewer_model="gpt-6-astra")
        self.controller.conn.commit()
        self.task = self.controller.conn.execute(
            "SELECT * FROM tasks WHERE id='P1'").fetchone()
        self.profile = self.controller._profile_for(self.task)

    def _argv(self, stage_name: str) -> list[str]:
        runner = self.controller._execution_runner_for(
            self.task, self.profile.stage(stage_name))
        return runner._command(self.profile.stage(stage_name).owner)

    def test_the_producer_is_not_handed_the_flag_that_killed_it(self) -> None:
        argv = self._argv("apply")
        self.assertNotIn("--sandbox", argv)
        self.assertIn("-p", argv)

    def test_the_reviewer_is_read_only_not_danger_full_access(self) -> None:
        for stage in ("contract_review", "review"):
            argv = self._argv(stage)
            with self.subTest(stage=stage):
                self.assertNotIn("danger-full-access", argv)
                self.assertEqual(argv[argv.index("-s") + 1], "read-only")
