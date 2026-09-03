from __future__ import annotations

import hashlib
import json
import os
import shlex
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch
import uuid

from orchestrator.cli import build_parser, _enqueue
from orchestrator.containment import Sentinel, sandbox_available, write_allowlist
from orchestrator.controller import Controller, ControllerError
from orchestrator.ipc import enqueue_request, wait_for_result
from orchestrator.runner import RunResult, SubprocessRunner, classify_result
from orchestrator.tests import test_containment_layers as fixtures
from orchestrator.tests.test_containment_layers import EscapingRunner


class DriftFixture(fixtures.SandboxFixture):
    def setUp(self):
        super().setUp()
        self.home = self.base / "home"
        self.home.mkdir()
        self.profile_path = self.base / "profile.yaml"
        self.profile_path.write_text(fixtures.PROFILE_YAML)
        self.input_path = self.base / "input.md"
        self.input_path.write_text("synthetic containment test")

    def _run_task(self, runner):
        return fixtures.ControllerEscapeTest._run_task(self, runner)


class RetainedOutputTest(DriftFixture):
    def blocked(self):
        return self._run_task(EscapingRunner(self.protected / "victim.txt"))

    def test_inspection_preserves_true_outcome_without_accepting_it(self):
        controller, task_id = self.blocked()
        try:
            before = controller.status(task_id)
            inspected = controller.containment_inspect(task_id)
            self.assertEqual(inspected["candidate_outcome"], "pass")
            self.assertEqual(inspected["integrity"], "verified")
            self.assertFalse(inspected["authorised_to_advance"])
            self.assertFalse(inspected["source_snapshot_verified"])
            self.assertEqual(inspected["drift_evidence"]["attribution"], "unknown")
            self.assertEqual(before, controller.status(task_id))
            self.assertIsNone(before["stage_runs"][0]["outcome"])
            self.assertEqual(before["task"]["transitions_count"], 0)
        finally:
            controller.close()

    def test_resume_without_explicit_rerun_has_no_state_change(self):
        controller, task_id = self.blocked()
        try:
            before = controller.status(task_id)
            for _ in range(2):
                with self.assertRaisesRegex(ControllerError, "containment_review_required"):
                    controller.resume(task_id)
            self.assertEqual(before, controller.status(task_id))
        finally:
            controller.close()

    def test_explicit_rerun_preserves_both_runs_and_original_evidence(self):
        controller, task_id = self.blocked()
        try:
            run = controller.status(task_id)["stage_runs"][0]
            original = Path(run["manifest_path"]).read_bytes()
            original_drift = Path(run["log_path"]).with_suffix(".containment-drift.json").read_bytes()
            controller.runner = EscapingRunner(self.protected / "second.txt")
            after = controller.resume(task_id, rerun_stage=True)
            self.assertEqual(after["task"]["status"], "blocked")
            self.assertEqual(len(after["stage_runs"]), 2)
            self.assertEqual(Path(run["manifest_path"]).read_bytes(), original)
            self.assertEqual(Path(run["log_path"]).with_suffix(".containment-drift.json").read_bytes(), original_drift)
            self.assertEqual(after["task"]["transitions_count"], 0)
            self.assertEqual(len(list(Path(run["log_path"]).parent.glob("*.containment-drift.json"))), 2)
        finally:
            controller.close()

    def test_restart_and_read_only_inspection_preserve_blocked_lease(self):
        controller, task_id = self.blocked()
        before = controller.status(task_id)
        controller.close()
        for read_only in (True, False):
            controller = Controller(self.home, read_only=read_only, protected_roots=(self.protected,))
            try:
                self.assertEqual(controller.containment_inspect(task_id)["candidate_outcome"], "pass")
                self.assertEqual(before, controller.status(task_id))
                self.assertIsNone(controller.status(task_id)["task"]["lease_token"])
            finally:
                controller.close()

    def test_tampered_artifacts_fail_closed(self):
        controller, task_id = self.blocked()
        try:
            run = controller.status(task_id)["stage_runs"][0]
            manifest = json.loads(Path(run["manifest_path"]).read_bytes())
            for path in (run["manifest_path"], run["log_path"], manifest["output_path"], manifest["containment_evidence_path"]):
                with self.subTest(path=path):
                    target = Path(path)
                    original = target.read_bytes()
                    target.write_bytes(original + b"tamper")
                    with self.assertRaises(ValueError):
                        controller.containment_inspect(task_id)
                    target.write_bytes(original)
            self.assertEqual(controller.status(task_id)["task"]["status"], "blocked")
        finally:
            controller.close()

    def test_legacy_output_is_inspectable_but_never_cleared(self):
        controller, task_id = self.blocked()
        try:
            run = controller.status(task_id)["stage_runs"][0]
            output = b"ORCHESTRATOR_OUTCOME: pass\n"
            log = Path(run["log_path"])
            log.write_bytes(b"owner=claude\n\n--- output ---\n" + output + b"containment_violation_count=1\ncontainment_evidence=/legacy/shared.json\n")
            path = Path(run["manifest_path"])
            manifest = json.loads(path.read_bytes())
            manifest.update(schema_version=1, reason="workspace_escape", log_hash=hashlib.sha256(log.read_bytes()).hexdigest())
            for key in ("output_path", "containment_evidence_hash", "containment_evidence_path", "candidate_outcome", "candidate_classification", "candidate_reason", "profile_hash", "input_hash"):
                manifest.pop(key)
            path.write_text(json.dumps(manifest))
            controller.conn.execute("UPDATE stage_runs SET manifest_hash=? WHERE run_token=?", (hashlib.sha256(path.read_bytes()).hexdigest(), run["run_token"]))
            controller.conn.execute("UPDATE tasks SET stop_reason='workspace_escape' WHERE id=?", (task_id,))
            result = controller.containment_inspect(task_id)
            self.assertEqual(result["candidate_outcome"], "pass")
            self.assertIsNone(result["drift_evidence"])
            self.assertFalse(result["authorised_to_advance"])
            with self.assertRaisesRegex(ControllerError, "containment_review_required"):
                controller.resume(task_id)
        finally:
            controller.close()


class ClassificationTest(unittest.TestCase):
    def test_candidate_is_separate_and_never_a_transition_outcome(self):
        for text, code, timeout, reason in (
            ("ORCHESTRATOR_OUTCOME: needs_repair\n", 0, False, "success"),
            ("ORCHESTRATOR_OUTCOME: pass\n", 1, False, "runner_nonzero"),
            ("ORCHESTRATOR_OUTCOME: pass\n", 0, True, "timeout"),
            ("no marker", 0, False, "missing_outcome"),
            ("ORCHESTRATOR_OUTCOME: other\n", 0, False, "unknown_outcome"),
        ):
            with self.subTest(reason=reason):
                source = RunResult(code, text, None, "raw", "raw", containment_stop="protected_root_drift")
                result = classify_result(code, text, {"needs_repair", "pass"}, timeout, source=source)
                self.assertIsNone(result.outcome)
                self.assertEqual(result.classification, "blocked")
                self.assertEqual(result.candidate_reason, reason)

    def test_rerun_flag_is_explicit_in_ipc_only_for_resume(self):
        parser = build_parser()
        with patch("orchestrator.cli.enqueue_request", return_value=Path("/request")) as enqueue:
            _enqueue(Path("/home"), parser.parse_args(["enqueue", "--resume", "task", "--rerun-stage"]))
            self.assertIs(enqueue.call_args.args[1]["rerun_stage"], True)
            with self.assertRaises(ControllerError):
                _enqueue(Path("/home"), parser.parse_args(["enqueue", "--rerun-stage"]))


class LargeFileTest(DriftFixture):
    def test_large_unchanged_touch_is_proven_not_drift(self):
        self.protected_file.write_bytes(b"x" * 745472)
        sentinel = Sentinel((self.protected,), self.workspace)
        before = sentinel.snapshot()
        stat = self.protected_file.stat()
        os.utime(self.protected_file, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1000000))
        self.assertEqual(sentinel.compare(before), [])
        self.protected_file.write_bytes(b"y" * 745472)
        self.assertEqual(sentinel.compare(before)[0].kind, "modified")

    def test_unreadable_hash_is_unknown_even_without_mtime_change(self):
        sentinel = Sentinel((self.protected,), self.workspace)
        with patch.object(Sentinel, "_hash", return_value=None):
            before = sentinel.snapshot()
            self.assertEqual(sentinel.compare(before)[0].kind, "unverified")


@unittest.skipUnless(sandbox_available(), "real macOS sandbox required")
class ConcurrentChildTest(DriftFixture):
    def test_real_contained_child_and_external_create_modify_delete_are_not_attributed(self):
        for kind in ("added", "modified", "removed"):
            with self.subTest(kind=kind):
                victim = self.protected / f"{kind}.txt"
                if kind != "added":
                    victim.write_text("before")
                ready = self.workspace / f"{kind}.ready"
                release = self.workspace / f"{kind}.release"
                allowed = write_allowlist(self.workspace, self.artifacts)
                self.assertFalse(any(victim.resolve().is_relative_to(root) for root in allowed))
                errors = []

                def writer():
                    try:
                        deadline = time.monotonic() + 10
                        while not ready.exists():
                            if time.monotonic() > deadline:
                                raise AssertionError("contained child never reached barrier")
                            time.sleep(0.01)
                        if kind == "removed":
                            victim.unlink()
                        else:
                            victim.write_text("external writer")
                    except BaseException as exc:
                        errors.append(exc)
                    finally:
                        release.write_text("release")

                script = (
                    "from pathlib import Path\nimport time\n"
                    f"Path({str(ready)!r}).write_text('ready')\n"
                    "deadline=time.monotonic()+12\n"
                    f"while not Path({str(release)!r}).exists():\n"
                    " if time.monotonic()>deadline: raise RuntimeError('barrier timeout')\n"
                    " time.sleep(0.01)\n"
                    "print('ORCHESTRATOR_OUTCOME: pass')\n"
                )
                runner = RealChildRunner(script)
                thread = threading.Thread(target=writer)
                thread.start()
                controller = None
                try:
                    controller, task_id = self._run_task(runner)
                    thread.join(timeout=15)
                    self.assertFalse(thread.is_alive())
                    self.assertEqual(errors, [])
                    status = controller.status(task_id)
                    self.assertEqual(status["task"]["status"], "blocked")
                    self.assertEqual(status["task"]["stop_reason"], "protected_root_drift")
                    self.assertEqual(status["task"]["transitions_count"], 0)
                    inspected = controller.containment_inspect(task_id)
                    self.assertEqual(inspected["candidate_outcome"], "pass")
                    self.assertEqual(inspected["containment_attribution"], "unknown")
                    self.assertIn({"kind": kind, "path": str(victim)}, inspected["drift_evidence"]["violations"])
                    self.assertTrue(Path(status["stage_runs"][0]["log_path"]).with_suffix(".containment").joinpath("sandbox.sb").exists())
                finally:
                    release.write_text("release")
                    thread.join(timeout=15)
                    if controller:
                        controller.close()

    def test_real_forbidden_write_still_fails_closed(self):
        runner = RealChildRunner(
            f"from pathlib import Path; Path({str(self.protected_file)!r}).write_text('forbidden'); "
            "print('ORCHESTRATOR_OUTCOME: pass')"
        )
        controller, task_id = self._run_task(runner)
        try:
            self.assertEqual(self.protected_file.read_text(), "original")
            status = controller.status(task_id)
            self.assertEqual(status["task"]["status"], "blocked")
            self.assertEqual(status["task"]["transitions_count"], 0)
            self.assertNotEqual(status["stage_runs"][0]["exit_code"], 0)
        finally:
            controller.close()

    def test_transient_file_hardlink_cannot_release_a_real_escape(self):
        alias = self.workspace / "alias.txt"
        os.link(self.protected_file, alias)
        runner = RealChildRunner(
            f"from pathlib import Path; p=Path({str(alias)!r}); p.write_text('changed through alias'); p.unlink(); "
            "print('ORCHESTRATOR_OUTCOME: pass')"
        )
        controller, task_id = self._run_task(runner)
        try:
            status = controller.status(task_id)
            self.assertEqual(status["task"]["status"], "blocked")
            self.assertEqual(status["task"]["transitions_count"], 0)
            if self.protected_file.read_text() != "original":
                self.assertEqual(status["task"]["stop_reason"], "protected_root_drift")
                self.assertEqual(self.protected_file.stat().st_nlink, 1)
        finally:
            controller.close()


class RealChildRunner(SubprocessRunner):
    def __init__(self, script):
        self.script = script

    def _command(self, owner):
        return [sys.executable, "-c", self.script]

    def preflight(self, owner, timeout=5):
        return EscapingRunner(Path("/not-used")).preflight(owner, timeout)


@unittest.skipUnless(sandbox_available(), "real macOS sandbox required")
class ContainedDaemonTest(DriftFixture):
    def test_live_daemon_drift_inspection_restart_and_explicit_rerun(self):
        ready = self.workspace / "ready"
        release = self.workspace / "release"
        modified = self.protected / "modify.txt"
        removed = self.protected / "remove.txt"
        modified.write_text("before")
        removed.write_text("before")
        script = self.base / "synthetic_provider.py"
        script.write_text(
            "import sys,time\nfrom pathlib import Path\n"
            "if '--version' in sys.argv: print('synthetic provider'); sys.exit(0)\n"
            f"Path({str(ready)!r}).write_text('ready')\n"
            "deadline=time.monotonic()+10\n"
            f"while not Path({str(release)!r}).exists():\n"
            " if time.monotonic()>deadline: raise RuntimeError('barrier timeout')\n"
            " time.sleep(.01)\n"
            "print('ORCHESTRATOR_OUTCOME: pass')\n"
        )
        env = {k: v for k, v in os.environ.items() if not k.startswith("ORCH_") and k != "ANTHROPIC_BASE_URL"}
        env.update(ORCH_HOME=str(self.home), ORCH_POLL_INTERVAL="0.02", ORCH_PROTECTED_ROOTS=str(self.protected),
                   ORCH_CLAUDE_COMMAND=shlex.join([sys.executable, str(script)]), PYTHONDONTWRITEBYTECODE="1")
        root = Path(__file__).resolve().parents[2]

        def start():
            process = subprocess.Popen([sys.executable, "-B", "-m", "orchestrator", "daemon"], cwd=root, env=env,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            deadline = time.monotonic() + 5
            while not (self.home / "daemon.pid").exists():
                if process.poll() is not None:
                    self.fail(process.communicate()[0])
                if time.monotonic() > deadline:
                    process.terminate()
                    process.communicate(timeout=5)
                    self.fail("synthetic daemon startup timed out")
                time.sleep(.01)
            return process

        def stop(process):
            process.terminate()
            process.communicate(timeout=5)

        def inspect():
            result = subprocess.run([sys.executable, "-B", "-m", "orchestrator", "containment-inspect", task_id],
                                    cwd=root, env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)

        daemon = start()
        try:
            duplicate = subprocess.run([sys.executable, "-B", "-m", "orchestrator", "daemon"], cwd=root, env=env,
                                       capture_output=True, text=True, timeout=5)
            self.assertEqual(duplicate.returncode, 2)
            self.assertIn("already holds", duplicate.stderr)
            task_id = str(uuid.uuid4())
            request = enqueue_request(self.home, {"request_id": task_id, "action": "run", "type": "containment-test",
                                                 "profile": str(self.profile_path), "input": str(self.input_path),
                                                 "workspace": str(self.workspace)})
            deadline = time.monotonic() + 5
            while not ready.exists():
                if time.monotonic() > deadline:
                    self.fail("real contained child did not reach barrier")
                time.sleep(.01)
            (self.protected / "added.txt").write_text("independent")
            modified.write_text("independent")
            removed.unlink()
            release.write_text("continue")
            first = wait_for_result(self.home, request, timeout=10, poll_interval=.02)
            self.assertEqual(first["status"], "blocked")
            self.assertEqual(first["stop_reason"], "protected_root_drift")
            evidence = inspect()
            self.assertEqual({x["kind"] for x in evidence["drift_evidence"]["violations"]}, {"added", "modified", "removed"})
            first_manifest = Path(evidence["manifest_path"]).read_bytes()
            request = enqueue_request(self.home, {"request_id": str(uuid.uuid4()), "action": "resume", "task_id": task_id})
            refused = wait_for_result(self.home, request, timeout=10, poll_interval=.02)
            self.assertIn("containment_review_required", refused["error"])
            self.assertEqual(inspect(), evidence)
            stop(daemon)
            daemon = start()
            self.assertEqual(inspect(), evidence)
            request = enqueue_request(self.home, {"request_id": str(uuid.uuid4()), "action": "resume", "task_id": task_id,
                                                 "rerun_stage": True})
            last = wait_for_result(self.home, request, timeout=10, poll_interval=.02)
            self.assertEqual(last["status"], "done")
            self.assertEqual(len(last["stage_runs"]), 2)
            self.assertEqual(Path(evidence["manifest_path"]).read_bytes(), first_manifest)
            self.assertIsNone(last["task"]["lease_token"])
            self.assertEqual(last["task"]["transitions_count"], 1)
            self.assertIn("manual_rerun_stage", [x["reason"] for x in last["transitions"]])
        finally:
            release.write_text("continue")
            if daemon.poll() is None:
                stop(daemon)
