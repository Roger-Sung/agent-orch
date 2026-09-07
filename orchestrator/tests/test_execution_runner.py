from __future__ import annotations

import json
import tempfile
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator.execution import ExecutionChoice, ExecutionConfigError, extract_plan, render_plan, resolve_request
from orchestrator.execution_runner import ConfiguredRunner, configured_command, provider_json
from orchestrator.runner import CLAUDE_JSON_PROTOCOL, RunResult, SubprocessRunner, classify_result
from orchestrator.tests import test_execution
from orchestrator.tests import test_containment_layers


class ConfiguredCommandTests(unittest.TestCase):
    def setUp(self):
        self.review = ExecutionChoice("reviewer", "claude", "claude-fable-5-1", "high", "claude-fable-5-1", "high")
        self.code = ExecutionChoice("executor", "codex", "gpt-5.6-sol", "medium", "gpt-5.6-sol", "medium")

    def test_reviewer_cannot_inherit_bypass_tools_hooks_or_mcp(self):
        cmd = configured_command(["claude", "-p", "--dangerously-skip-permissions", "--model", "claude-opus-5"], self.review)
        self.assertNotIn("--dangerously-skip-permissions", cmd)
        self.assertIn("--safe-mode", cmd)
        self.assertEqual(cmd[cmd.index("--tools") + 1], "")
        self.assertEqual(cmd.count("--model"), 1)
        self.assertEqual(cmd[cmd.index("--model") + 1], "claude-fable-5-1")

    def test_executor_effort_reaches_config_and_model_is_not_duplicated(self):
        cmd = configured_command(["codex", "exec", "--approve-for-me", "--model=old"], self.code)
        self.assertIn('model_reasoning_effort="medium"', cmd)
        self.assertEqual(cmd.count("--model"), 1)
        self.assertIn('approval_policy="never"', cmd)
        self.assertEqual(cmd[cmd.index("--sandbox") + 1], "danger-full-access")

    def test_executor_refuses_absent_or_disabled_outer_sandbox_before_spawn(self):
        runner = ConfiguredRunner(self.code, "a" * 64, ["codex", "exec"])
        with tempfile.TemporaryDirectory() as root:
            for workspace, value in ((None, "0"), (Path(root), "1")):
                with patch.dict("os.environ", {"ORCH_ALLOW_UNSANDBOXED": value}), patch("orchestrator.runner.subprocess.Popen") as spawn:
                    result = runner.run("codex", "prompt", 10, Path(root) / "run.log", workspace=workspace)
                self.assertEqual(result.containment_stop, "outer_sandbox_required")
                spawn.assert_not_called()

    def test_executor_refuses_unavailable_outer_sandbox(self):
        runner = ConfiguredRunner(self.code, "a" * 64, ["codex", "exec"])
        with tempfile.TemporaryDirectory() as root:
            with patch.dict("os.environ", {"ORCH_ALLOW_UNSANDBOXED": "0"}), patch("orchestrator.containment.sandbox_available", return_value=False), patch("orchestrator.runner.subprocess.Popen") as spawn:
                result = runner.run("codex", "prompt", 10, Path(root) / "run.log", workspace=Path(root))
            self.assertEqual(result.containment_stop, "sandbox_unavailable")
            spawn.assert_not_called()

    def test_unknown_duplicate_and_authority_flags_fail_closed(self):
        for tail in (["--model", "x", "-m", "y"], ["--resume", "old"], ["--tools", "Bash"],
                     ["--settings", "x"], ["--model"], ["--model="], ["--allowedTools", "Write"]):
            with self.subTest(tail=tail), self.assertRaises(ExecutionConfigError):
                configured_command(["claude", "-p"] + tail, self.review)

    def test_custom_wrappers_are_not_silently_rewritten(self):
        with self.assertRaises(ExecutionConfigError):
            configured_command(["wrapper", "claude", "-p"], self.review)

    def test_native_json_is_the_authority_not_a_result_like_string(self):
        payload = {"type": "result", "subtype": "success", "is_error": False,
                   "result": "ORCHESTRATOR_OUTCOME: ready", "session_id": "session-a",
                   "modelUsage": {"claude-fable-5-1": {"canonicalModel": "claude-fable-5-1"}}}
        runner = ConfiguredRunner(self.review, "a" * 64, ["claude", "-p"])
        raw = RunResult(0, json.dumps(payload), None, "raw", "raw", started_at_ms=1)
        with patch.object(SubprocessRunner, "run", return_value=raw):
            result = runner.run("claude", "prompt", 10, Path("unused"))
        self.assertEqual(result.final_response_source, CLAUDE_JSON_PROTOCOL)
        self.assertEqual(classify_result(0, result.output, {"ready"}, source=result).outcome, "ready")
        self.assertEqual(result.execution_receipt["provider_reported_model"], self.review.model)
        self.assertEqual(result.execution_receipt["provider_session_id"], "session-a")
        self.assertTrue(result.execution_receipt["provider_effort_unreported"])
        payload["modelUsage"] = {"claude-opus-5": {"canonicalModel": "claude-opus-5"}}
        raw = RunResult(0, json.dumps(payload), None, "raw", "raw")
        with patch.object(SubprocessRunner, "run", return_value=raw):
            result = runner.run("claude", "prompt", 10, Path("unused"))
        self.assertEqual(classify_result(0, result.output, {"ready"}, source=result).classification, "blocked")

    def test_preflight_and_run_share_frozen_argv(self):
        runner = ConfiguredRunner(self.review, "a" * 64, ["claude", "-p"])
        before = runner._command("claude")
        with patch.dict("os.environ", {"ORCH_CLAUDE_COMMAND": "danger --tools Bash"}):
            self.assertEqual(runner._command("claude"), before)
        before.append("--unsafe")
        self.assertNotIn("--unsafe", runner._command("claude"))

    def test_diagnostics_do_not_hide_or_select_between_result_objects(self):
        line = json.dumps({"type": "result", "result": "quoted model text"})
        self.assertEqual(provider_json("CLI warning\n" + line + "\n")["result"], "quoted model text")
        for raw in (line + "\n" + line, '{"type":"result","type":"result"}'):
            with self.assertRaises(ExecutionConfigError): provider_json(raw)


class PlanFramingTests(unittest.TestCase):
    def test_framing_roundtrip_and_spoof_rejection(self):
        fixture = test_execution.ExecutionPlanTests(); fixture.setUp()
        plan = resolve_request(fixture.request, fixture.profile)
        frame = render_plan(plan)
        self.assertEqual(extract_plan(frame + "Task text", fixture.profile), plan)
        self.assertIsNone(extract_plan("legacy task", fixture.profile))
        for text in ("Task\n" + frame, frame + frame, frame.replace(plan.digest, "f" * 64)):
            with self.subTest(text=text[:40]), self.assertRaises(ExecutionConfigError):
                extract_plan(text, fixture.profile)


@unittest.skipUnless(test_containment_layers.sandbox_available(), "macOS sandbox-exec is required for L1 enforcement")
class ConfiguredOuterSandboxTests(test_containment_layers.SandboxFixture):
    def test_executor_can_write_workspace_but_not_protected_sibling(self):
        binary = self.workspace / "codex"
        binary.write_text(f'''#!{sys.executable}
import sys
from pathlib import Path
Path(sys.argv[-1]).write_text("mutated")
Path(sys.argv[sys.argv.index("--output-last-message")+1]).write_text("ORCHESTRATOR_OUTCOME: ready")
''')
        binary.chmod(0o755)
        choice = ExecutionChoice("executor", "codex", "gpt-5.6-sol", "medium", "gpt-5.6-sol", "medium")
        runner = ConfiguredRunner(choice, "a" * 64, [str(binary), "exec"])
        with patch.dict("os.environ", {"ORCH_ALLOW_UNSANDBOXED": "0"}):
            allowed = runner.run("codex", str(self.workspace / "allowed.txt"), 10,
                                 self.artifacts / "allowed.log", workspace=self.workspace)
            denied = runner.run("codex", str(self.protected_file), 10,
                                self.artifacts / "denied.log", workspace=self.workspace)
        self.assertEqual(allowed.exit_code, 0, allowed.output)
        self.assertEqual((self.workspace / "allowed.txt").read_text(), "mutated")
        self.assertNotEqual(denied.exit_code, 0)
        self.assertEqual(self.protected_file.read_text(), "original")
        self.assertEqual(allowed.execution_receipt["sandbox_policy"], "orch-l1-required-v1")
