"""Intake and operability updates: explicit approval markers, the --executor
flag, the dry-run execution plan, the config file, doctor, and the out-of-
workspace reports directory.

Run: python3 -m unittest orchestrator.tests.test_intake_updates
"""

from __future__ import annotations

import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from orchestrator.config import ConfigFileError, load_config_into_env
from orchestrator.controller import Controller
from orchestrator.doctor import run_doctor
from orchestrator.runner import RunResult
from orchestrator.start import StartFlags, _spec_is_approved, run_start


class SpecApprovalMarkerTests(unittest.TestCase):
    def _spec(self, directory: str, text: str) -> Path:
        path = Path(directory) / "spec.md"
        path.write_text(text, encoding="utf-8")
        return path

    def test_marker_word_in_prose_is_not_approval(self):
        # The old check matched `ready` anywhere; a spec merely *discussing*
        # readiness must not count as approved.
        with tempfile.TemporaryDirectory() as directory:
            spec = self._spec(directory, "We will decide when this is ready for apply.\n")
            self.assertFalse(_spec_is_approved(spec))

    def test_explicit_marker_line_is_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            for line in ("Status: ready\n", "approval: approved\n", "Decision = accepted\n", "  STATUS: FINAL\n"):
                self.assertTrue(_spec_is_approved(self._spec(directory, f"# Spec\n{line}body\n")), line)

    def test_bare_word_on_its_own_line_is_not_approval(self):
        # "approved" alone is still prose until someone writes the field.
        with tempfile.TemporaryDirectory() as directory:
            self.assertFalse(_spec_is_approved(self._spec(directory, "approved\n")))

    def test_qualified_marker_values_are_not_approval(self):
        # The value must be exactly the marker word: a qualified value states
        # the opposite of approval, and an end-anchorless pattern read it as one.
        with tempfile.TemporaryDirectory() as directory:
            for line in (
                "Status: approved pending review\n",
                "Status: final draft\n",
                "Decision: ready for discussion\n",
                "approval: not approved\n",
            ):
                self.assertFalse(_spec_is_approved(self._spec(directory, line)), line)

    def test_missing_file_is_not_approval(self):
        self.assertFalse(_spec_is_approved(Path("/nonexistent/spec.md")))


class ExecutorFlagTests(unittest.TestCase):
    def _apply_flags(self, directory: str, executor: str | None) -> tuple[Path, StartFlags]:
        worktree = Path(directory) / "worktree"
        worktree.mkdir()
        spec = worktree / "approved-spec.md"
        spec.write_text("Status: approved\n", encoding="utf-8")
        return (
            Path(directory) / "runtime",
            StartFlags("apply", "B17 worktree", worktree, spec, None, True, executor=executor),
        )

    def test_executor_flag_selects_codex_pairing_without_keywords(self):
        with tempfile.TemporaryDirectory() as directory:
            home, flags = self._apply_flags(directory, "codex")
            result = run_start(home, "apply implementation to B17 worktree", flags)
        routing = result["routing"]
        self.assertEqual(
            (routing["pattern"], routing["executor"], routing["reviewer"]),
            ("codex_implement_claude_review", "codex", "claude"),
        )

    def test_executor_flag_wins_over_a_contradicting_keyword(self):
        with tempfile.TemporaryDirectory() as directory:
            home, flags = self._apply_flags(directory, "claude")
            result = run_start(home, "apply implementation, executor=codex", flags)
        self.assertEqual(result["routing"]["pattern"], "claude_apply_codex_review")

    def test_executor_flag_on_a_non_apply_task_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            flags = StartFlags("propose", "orch intake", None, None, None, True, executor="codex")
            with self.assertRaises(ValueError):
                run_start(Path(directory) / "runtime", "propose a spec for orch intake", flags)


class DryRunPlanTests(unittest.TestCase):
    def test_dry_run_reports_the_execution_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            result = run_start(
                Path(directory) / "runtime",
                "propose a spec for orch start intake",
                StartFlags("propose", "orch start intake", None, None, None, True),
            )
        plan = result["plan"]
        self.assertEqual(plan["pattern"], "propose_spec")
        self.assertTrue(str(plan["profile"]).endswith("propose.yaml"))
        stage_names = {stage["stage"] for stage in plan["stages"]}
        self.assertIn("draft", stage_names)
        owners = {stage.get("owner") for stage in plan["stages"]}
        self.assertIn("claude", owners)
        for owner in ("claude", "codex"):
            self.assertIn("command", plan["provider_commands"][owner])
        self.assertIn("l2_detection", plan["containment"])
        self.assertIn("approved", plan["approved_spec"])

    def test_non_dry_run_result_carries_no_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict(os.environ, {}, clear=False):
                result = run_start(
                    Path(directory) / "runtime",
                    "propose a spec for orch start intake",
                    StartFlags("propose", "orch start intake", None, None, None, False),
                )
        self.assertNotIn("plan", result)


class ConfigFileTests(unittest.TestCase):
    def _config(self, directory: str, body: str) -> dict[str, str]:
        path = Path(directory) / "orch.toml"
        path.write_text(body, encoding="utf-8")
        return {"ORCH_CONFIG": str(path)}

    def test_fills_unset_variables_and_joins_lists(self):
        with tempfile.TemporaryDirectory() as directory:
            env = self._config(
                directory,
                'ORCH_HOME = "~/state"\nORCH_PROTECTED_ROOTS = ["/a", "/b"]\nORCH_POLL_INTERVAL = 5\n',
            )
            applied = load_config_into_env(env)
        self.assertEqual(env["ORCH_HOME"], "~/state")
        self.assertEqual(env["ORCH_PROTECTED_ROOTS"], os.pathsep.join(["/a", "/b"]))
        self.assertEqual(env["ORCH_POLL_INTERVAL"], "5")
        self.assertEqual(set(applied), {"ORCH_HOME", "ORCH_PROTECTED_ROOTS", "ORCH_POLL_INTERVAL"})

    def test_environment_always_wins(self):
        with tempfile.TemporaryDirectory() as directory:
            env = self._config(directory, 'ORCH_HOME = "/from-file"\n')
            env["ORCH_HOME"] = "/from-env"
            applied = load_config_into_env(env)
        self.assertEqual(env["ORCH_HOME"], "/from-env")
        self.assertEqual(applied, {})

    def test_acknowledgement_gates_are_refused_in_the_file(self):
        with tempfile.TemporaryDirectory() as directory:
            env = self._config(directory, 'ORCH_ALLOW_UNATTENDED = "1"\n')
            with self.assertRaises(ConfigFileError):
                load_config_into_env(env)

    def test_non_orch_keys_and_booleans_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            env = self._config(directory, 'PATH = "/tmp"\n')
            with self.assertRaises(ConfigFileError):
                load_config_into_env(env)
        with tempfile.TemporaryDirectory() as directory:
            env = self._config(directory, "ORCH_SOMETHING = true\n")
            with self.assertRaises(ConfigFileError):
                load_config_into_env(env)

    def test_invalid_key_applies_nothing_at_all(self):
        # Validation happens for the whole table before any key is applied; a
        # later invalid key must not leave earlier keys half-applied.
        with tempfile.TemporaryDirectory() as directory:
            env = self._config(directory, 'ORCH_HOME = "/from-file"\nPATH = "/tmp"\n')
            with self.assertRaises(ConfigFileError):
                load_config_into_env(env)
            self.assertNotIn("ORCH_HOME", env)

    def test_explicit_config_pointing_at_a_missing_file_is_an_error(self):
        env = {"ORCH_CONFIG": "/nonexistent/orch.toml"}
        with self.assertRaises(ConfigFileError):
            load_config_into_env(env)

    def test_wait_timeout_from_config_reaches_the_parser_default(self):
        # Regression for load order: the parser reads ORCH_WAIT_TIMEOUT while
        # it is being built, so the config file must be loaded first.
        from orchestrator.cli import build_parser

        with tempfile.TemporaryDirectory() as directory:
            overrides = self._config(directory, "ORCH_WAIT_TIMEOUT = 37.5\n")
            with mock.patch.dict(os.environ, overrides, clear=False):
                os.environ.pop("ORCH_WAIT_TIMEOUT", None)
                load_config_into_env()
                args = build_parser().parse_args(
                    ["submit", "--type", "t", "--profile", "p", "--input", "i"]
                )
        self.assertEqual(args.wait_timeout, 37.5)

    def test_no_config_file_applies_nothing(self):
        env: dict[str, str] = {"HOME": "/nonexistent-home"}
        with mock.patch("orchestrator.config.DEFAULT_CONFIG_PATH", "/nonexistent/orch.toml"):
            self.assertEqual(load_config_into_env(env), {})


class DoctorTests(unittest.TestCase):
    def test_doctor_reports_structure_and_flags_missing_providers(self):
        with tempfile.TemporaryDirectory() as directory:
            overrides = {
                "ORCH_CLAUDE_COMMAND": str(Path(directory) / "missing-claude"),
                "ORCH_CODEX_COMMAND": str(Path(directory) / "missing-codex"),
                "ORCH_PROTECTED_ROOTS": "",
                "ORCH_EXTRA_WRITE_ROOTS": "",
                "ORCH_CONFIG": "",
            }
            with mock.patch.dict(os.environ, overrides, clear=False):
                report = run_doctor(Path(directory) / "home")
        names = {item["check"] for item in report["checks"]}
        self.assertLessEqual(
            {"orch_home", "provider_claude", "provider_codex", "l1_sandbox", "l2_protected_roots"}, names
        )
        by_name = {item["check"]: item for item in report["checks"]}
        self.assertEqual(by_name["provider_claude"]["status"], "fail")
        self.assertEqual(by_name["l2_protected_roots"]["status"], "warn")
        self.assertGreaterEqual(report["summary"]["fail"], 2)

    def test_doctor_is_read_only_and_survives_a_malformed_command(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"  # deliberately never created
            overrides = {
                "ORCH_CLAUDE_COMMAND": 'claude -p "unterminated',
                "ORCH_CODEX_COMMAND": str(Path(directory) / "missing-codex"),
                "ORCH_PROTECTED_ROOTS": "",
                "ORCH_EXTRA_WRITE_ROOTS": "",
                "ORCH_CONFIG": "",
            }
            with mock.patch.dict(os.environ, overrides, clear=False):
                report = run_doctor(home)
        # Read-only: probing must not create the home directory or a lock file.
        self.assertFalse(home.exists())
        by_name = {item["check"]: item for item in report["checks"]}
        self.assertEqual(by_name["provider_claude"]["status"], "fail")
        self.assertIn("malformed", by_name["provider_claude"]["detail"])
        self.assertEqual(by_name["daemon"]["status"], "warn")


REPORTS_PROFILE = """\
version: 1
type: reports-test
initial_stage: work
max_transitions: 4
stages:
  work:
    owner: claude
    attempt_cap: 1
    timeout: 30
    prompt: "reports test stage"
    outcomes:
      pass: done
  done:
    terminal: done
edge_caps:
  work.pass: 1
"""


class _ReportsAwareRunner:
    def __init__(self):
        self.calls: list[dict] = []

    def run(self, owner, prompt, timeout, log_path, *, workspace=None, protected_roots=None, reports_dir=None):
        del timeout, protected_roots
        self.calls.append({"owner": owner, "prompt": prompt, "workspace": workspace, "reports_dir": reports_dir})
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("ORCHESTRATOR_OUTCOME: pass\n", encoding="utf-8")
        return RunResult(0, "ORCHESTRATOR_OUTCOME: pass\n", "pass", "success", "success")


class _LegacyWorkspaceRunner:
    def __init__(self):
        self.calls: list[dict] = []

    def run(self, owner, prompt, timeout, log_path, *, workspace=None, protected_roots=None):
        del timeout, protected_roots
        self.calls.append({"owner": owner, "prompt": prompt, "workspace": workspace})
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("ORCHESTRATOR_OUTCOME: pass\n", encoding="utf-8")
        return RunResult(0, "ORCHESTRATOR_OUTCOME: pass\n", "pass", "success", "success")


class ReportsDirectoryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.home = root / "home"
        self.home.mkdir()
        self.profile_path = root / "profile.yaml"
        self.profile_path.write_text(REPORTS_PROFILE, encoding="utf-8")
        self.input_path = root / "input.md"
        self.input_path.write_text("do the thing\n", encoding="utf-8")
        self.workspace = root / "worktree"
        self.workspace.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, runner, workspace):
        controller = Controller(self.home, runner=runner, protected_roots=())
        try:
            task_id = controller.submit(
                "reports-test",
                self.profile_path,
                self.input_path,
                task_id=str(uuid.uuid4()),
                workspace=workspace,
            )
            controller.run_until_stop(task_id)
            return task_id
        finally:
            controller.close()

    def test_reports_dir_reaches_a_capable_runner_and_the_prompt(self):
        runner = _ReportsAwareRunner()
        task_id = self._run(runner, self.workspace)
        call = runner.calls[0]
        expected = (self.home / "tasks" / task_id / "reports").resolve()
        self.assertEqual(Path(call["reports_dir"]).resolve(), expected)
        self.assertIn("Reports directory", call["prompt"])
        self.assertIn(str(call["reports_dir"]), call["prompt"])
        self.assertTrue(expected.is_dir())

    def test_workspace_task_with_a_legacy_runner_falls_back_to_in_workspace_reports(self):
        # If the runner cannot allowlist the external directory, the prompt
        # must not promise a path L1 would deny — but the shipped profiles
        # refer to "the reports directory named at the top of this prompt",
        # so the line still has to exist and point somewhere writable.
        runner = _LegacyWorkspaceRunner()
        self._run(runner, self.workspace)
        prompt = runner.calls[0]["prompt"]
        self.assertIn("Reports directory", prompt)
        self.assertIn("reports/ relative to your working directory", prompt)
        self.assertNotIn(str(self.home), prompt.split("Task input:")[0].split("Stage instructions:")[0])

    def test_workspaceless_task_still_gets_the_reports_line(self):
        runner = _LegacyWorkspaceRunner()
        self._run(runner, None)
        self.assertIn("Reports directory", runner.calls[0]["prompt"])


if __name__ == "__main__":
    unittest.main()
