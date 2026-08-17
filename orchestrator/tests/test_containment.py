"""Worktree + Git containment：把 stage 關在 worktree 裡，並讓結果無法經由 Git 離開。

這不是 process sandbox——agent 仍是同一個 UNIX user，可讀寫 worktree 以外的檔案、可連外網。

重點不是「prompt 有沒有叫 agent 不要 push」，而是「就算它想 push 也做不到」。
執行：python3 -m unittest orchestrator.tests.test_containment
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path

from orchestrator.controller import Controller
from orchestrator.runner import RunResult, prepare_containment

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
    prompt: "containment test stage"
    outcomes:
      pass: done
  done:
    terminal: done
edge_caps:
  work.pass: 2
"""


def _git(cwd: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=False, env=env, timeout=30
    )


class PrepareContainmentTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.workspace = self.root / "worktree"
        self.workspace.mkdir()
        self.log_path = self.root / "artifacts" / "stage.log"
        self.log_path.parent.mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def test_credentials_are_stripped_from_environment(self):
        os.environ["SSH_AUTH_SOCK"] = "/tmp/fake-agent.sock"
        os.environ["GITHUB_TOKEN"] = "ghp_fake"
        try:
            env = prepare_containment(self.workspace, self.log_path)
        finally:
            os.environ.pop("SSH_AUTH_SOCK", None)
            os.environ.pop("GITHUB_TOKEN", None)

        self.assertNotIn("SSH_AUTH_SOCK", env)
        self.assertNotIn("GITHUB_TOKEN", env)
        self.assertEqual(env["GIT_SSH_COMMAND"], "/usr/bin/false")
        self.assertEqual(env["GIT_ASKPASS"], "/usr/bin/false")
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(env["ORCH_CONTAINMENT"], "worktree+git")
        # CLI 仍然需要 HOME / PATH 才能跑，containment 不是把環境清空。
        self.assertIn("PATH", env)

    def test_containment_artifacts_live_next_to_the_stage_log(self):
        prepare_containment(self.workspace, self.log_path)
        artifacts = self.log_path.parent / "containment"
        self.assertTrue((artifacts / "gitconfig").is_file())
        hook = artifacts / "hooks" / "pre-push"
        self.assertTrue(hook.is_file())
        self.assertTrue(os.access(hook, os.X_OK))
        # 不寫進 worktree：避免 agent 把 containment 設定一起 commit 出去。
        self.assertFalse((self.workspace / "containment").exists())


class ContainmentBlocksPushTest(unittest.TestCase):
    """用真的 git repo 驗證：containment 環境下 commit 可以、push 不行。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.remote = self.root / "origin.git"
        self.workspace = self.root / "worktree"
        self.log_path = self.root / "artifacts" / "stage.log"
        self.log_path.parent.mkdir(parents=True)

        subprocess.run(["git", "init", "--bare", "-b", "main", str(self.remote)], check=True, capture_output=True)
        subprocess.run(["git", "init", "-b", "main", str(self.workspace)], check=True, capture_output=True)
        _git(self.workspace, "config", "user.name", "test")
        _git(self.workspace, "config", "user.email", "test@example.com")
        _git(self.workspace, "remote", "add", "origin", str(self.remote))
        (self.workspace / "seed.txt").write_text("seed\n", encoding="utf-8")
        _git(self.workspace, "add", "seed.txt")
        _git(self.workspace, "commit", "-m", "seed")

    def tearDown(self):
        self._tmp.cleanup()

    def test_push_succeeds_without_containment(self):
        """對照組：沒有 containment 時 push 是會成功的——證明測試環境本身沒問題。"""
        result = _git(self.workspace, "push", "origin", "main")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_push_is_rejected_under_containment(self):
        env = prepare_containment(self.workspace, self.log_path)
        result = _git(self.workspace, "push", "origin", "main", env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("agent-orch containment", result.stderr)
        # remote 真的沒有拿到任何東西
        refs = subprocess.run(
            ["git", "-C", str(self.remote), "for-each-ref"], capture_output=True, text=True, check=False
        )
        self.assertEqual(refs.stdout.strip(), "")

    def test_commit_still_works_under_containment(self):
        env = prepare_containment(self.workspace, self.log_path)
        (self.workspace / "change.txt").write_text("work\n", encoding="utf-8")
        self.assertEqual(_git(self.workspace, "add", "change.txt", env=env).returncode, 0)
        commit = _git(self.workspace, "commit", "-m", "contained change", env=env)
        self.assertEqual(commit.returncode, 0, commit.stderr)


class RecordingRunner:
    """記錄 controller 傳了什麼給 runner。"""

    def __init__(self):
        self.calls: list[dict] = []

    def run(self, owner, prompt, timeout, log_path, *, workspace=None, protected_roots=None):
        self.calls.append({"owner": owner, "workspace": workspace})
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("ORCHESTRATOR_OUTCOME: pass\n", encoding="utf-8")
        return RunResult(0, "ORCHESTRATOR_OUTCOME: pass\n", "pass", "clean", "ok")


class ControllerWorkspaceTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name) / "home"
        self.home.mkdir(parents=True)
        self.profile_path = Path(self._tmp.name) / "profile.yaml"
        self.profile_path.write_text(PROFILE_YAML, encoding="utf-8")
        self.input_path = Path(self._tmp.name) / "input.md"
        self.input_path.write_text("do the thing\n", encoding="utf-8")
        self.workspace = Path(self._tmp.name) / "worktree"
        self.workspace.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_workspace_is_passed_through_to_the_runner(self):
        runner = RecordingRunner()
        controller = Controller(self.home, runner=runner)
        try:
            task_id = controller.submit(
                "containment-test",
                self.profile_path,
                self.input_path,
                task_id=str(uuid.uuid4()),
                workspace=self.workspace,
            )
            controller.run_until_stop(task_id)
        finally:
            controller.close()
        self.assertTrue(runner.calls)
        self.assertEqual(runner.calls[0]["workspace"], self.workspace.resolve())

    def test_tasks_without_workspace_keep_the_old_call_shape(self):
        """既有任務（沒有 worktree）不能被迫走新簽名——舊 runner 仍要能用。"""

        class LegacyRunner:
            def __init__(self):
                self.calls = 0

            def run(self, owner, prompt, timeout, log_path):
                del owner, prompt, timeout
                self.calls += 1
                log_path.parent.mkdir(parents=True, exist_ok=True)
                log_path.write_text("ORCHESTRATOR_OUTCOME: pass\n", encoding="utf-8")
                return RunResult(0, "ORCHESTRATOR_OUTCOME: pass\n", "pass", "clean", "ok")

        runner = LegacyRunner()
        controller = Controller(self.home, runner=runner)
        try:
            task_id = controller.submit(
                "containment-test", self.profile_path, self.input_path, task_id=str(uuid.uuid4())
            )
            controller.run_until_stop(task_id)
        finally:
            controller.close()
        self.assertEqual(runner.calls, 1)

    def test_missing_workspace_never_runs_uncontained(self):
        """worktree 不見時寧可讓 stage 失敗，也不能退回沒有 containment 的環境跑。"""
        runner = RecordingRunner()
        controller = Controller(self.home, runner=runner)
        try:
            task_id = controller.submit(
                "containment-test",
                self.profile_path,
                self.input_path,
                task_id=str(uuid.uuid4()),
                workspace=self.workspace,
            )
            self.workspace.rmdir()
            result = controller.run_until_stop(task_id)
        finally:
            controller.close()
        self.assertEqual(runner.calls, [])
        self.assertNotEqual(result["task"]["status"], "done")
        log = Path(result["stage_runs"][-1]["log_path"]).read_text(encoding="utf-8")
        self.assertIn("workspace no longer exists", log)


if __name__ == "__main__":
    unittest.main()
