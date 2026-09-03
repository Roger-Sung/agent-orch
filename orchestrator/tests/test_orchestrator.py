from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from orchestrator.controller import Controller
from orchestrator.cli import build_parser
from orchestrator.daemon import _reconcile_startup_requests
from orchestrator.ipc import wait_for_result
from orchestrator.profile import ProfileError, load_profile
from orchestrator.runner import RunResult, SubprocessRunner, classify_result
from orchestrator.start import (
    StartFlags,
    _read_yaml,
    _write_yaml,
    gate_status,
    run_gate_run,
    run_gate_decision,
    run_gate_sync,
    run_start,
    run_start_go,
    run_start_sync,
)


ROOT = Path(__file__).resolve().parents[2]
DEMO_PROFILE = ROOT / "orchestrator" / "examples" / "demo-loop.yaml"
DEMO_INPUT = ROOT / "orchestrator" / "examples" / "demo-input.md"
TRACKED_PROFILES = [
    ROOT / "orchestrator" / "profiles" / "propose.yaml",
    ROOT / "orchestrator" / "profiles" / "spec_review.yaml",
    ROOT / "orchestrator" / "profiles" / "codex_implement_claude_review.yaml",
    ROOT / "orchestrator" / "profiles" / "claude_apply_codex_review.yaml",
    ROOT / "orchestrator" / "profiles" / "stop_gate_claude.yaml",
    ROOT / "orchestrator" / "profiles" / "stop_gate_codex.yaml",
    ROOT / "orchestrator" / "profiles" / "provider_smoke.yaml",
    ROOT / "orchestrator" / "profiles" / "provider_smoke_gated.yaml",
    ROOT / "orchestrator" / "profiles" / "artifact_validation.yaml",
]


class SequenceRunner:
    def __init__(self, outcomes: list[str]):
        self.outcomes = iter(outcomes)
        self.owners: list[str] = []

    def run(self, owner: str, prompt: str, timeout: int, log_path: Path) -> RunResult:
        self.owners.append(owner)
        outcome = next(self.outcomes)
        output = f"ORCHESTRATOR_OUTCOME: {outcome}\n"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(output, encoding="utf-8")
        return RunResult(0, output, None, "raw", "raw")


class FixedRunner:
    def __init__(self, exit_code: int, output: str):
        self.exit_code = exit_code
        self.output = output

    def run(self, owner: str, prompt: str, timeout: int, log_path: Path) -> RunResult:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(self.output, encoding="utf-8")
        return RunResult(self.exit_code, self.output, None, "raw", "raw")


class ProfileValidationTests(unittest.TestCase):
    def test_demo_profile_passes_validation(self):
        profile = load_profile(DEMO_PROFILE)
        self.assertEqual(profile.initial_stage, "draft")
        self.assertEqual(profile.stages["draft"].owner, "claude")
        self.assertEqual(profile.stages["review"].owner, "codex")
        self.assertEqual(profile.edge_caps["review.block"], 1)

    def test_missing_edge_target_is_rejected(self):
        bad = DEMO_PROFILE.read_text().replace("submit: review", "submit: missing")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.yaml"
            path.write_text(bad, encoding="utf-8")
            with self.assertRaisesRegex(ProfileError, "targets missing stage"):
                load_profile(path)

    def test_duplicate_yaml_key_is_rejected(self):
        bad = DEMO_PROFILE.read_text() + "\nmax_transitions: 99\n"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.yaml"
            path.write_text(bad, encoding="utf-8")
            with self.assertRaisesRegex(ProfileError, "duplicate key"):
                load_profile(path)

    def test_tracked_orch_start_profiles_pass_validation(self):
        profiles = {path.name: load_profile(path) for path in TRACKED_PROFILES}
        self.assertEqual(profiles["propose.yaml"].type, "propose")
        self.assertEqual(profiles["spec_review.yaml"].type, "spec-review")
        self.assertEqual(profiles["codex_implement_claude_review.yaml"].type, "apply")
        self.assertEqual(profiles["claude_apply_codex_review.yaml"].type, "apply")
        self.assertEqual(profiles["stop_gate_claude.yaml"].type, "stop-gate")
        self.assertEqual(profiles["stop_gate_claude.yaml"].stages["review"].owner, "claude")
        self.assertEqual(
            profiles["stop_gate_claude.yaml"].stages["review"].outcomes,
            {"allow": "done", "block": "done"},
        )
        self.assertEqual(profiles["stop_gate_codex.yaml"].type, "stop-gate")
        self.assertEqual(profiles["stop_gate_codex.yaml"].stages["review"].owner, "codex")
        self.assertEqual(
            profiles["stop_gate_codex.yaml"].stages["review"].outcomes,
            {"allow": "done", "block": "done"},
        )
        self.assertEqual(profiles["provider_smoke.yaml"].type, "provider-smoke")
        self.assertEqual(profiles["provider_smoke.yaml"].stages["draft"].owner, "claude")
        self.assertEqual(profiles["provider_smoke.yaml"].stages["review"].owner, "codex")
        self.assertEqual(profiles["provider_smoke_gated.yaml"].type, "provider-smoke")
        self.assertEqual(profiles["artifact_validation.yaml"].type, "artifact-validation")
        self.assertEqual(profiles["artifact_validation.yaml"].stages["validate"].owner, "claude")
        self.assertEqual(profiles["artifact_validation.yaml"].stages["review"].owner, "codex")


class ClassificationTests(unittest.TestCase):
    def test_explicit_rate_limit_signature_pauses(self):
        result = classify_result(1, "Error: rate_limit_exceeded", {"submit"})
        self.assertEqual((result.classification, result.reason), ("paused", "rate_limited"))

    def test_generic_nonzero_blocks_without_guessing(self):
        result = classify_result(1, "connection closed unexpectedly", {"submit"})
        self.assertEqual((result.classification, result.reason), ("blocked", "runner_nonzero"))

    def test_socket_signature_blocks_with_specific_reason(self):
        result = classify_result(1, "FailedToOpenSocket: ConnectionRefused", {"submit"})
        self.assertEqual((result.classification, result.reason), ("blocked", "provider_socket_error"))

    def test_zero_exit_requires_exactly_one_typed_outcome(self):
        result = classify_result(0, "completed without marker", {"submit"})
        self.assertEqual((result.classification, result.reason), ("blocked", "missing_outcome"))

    def test_repeated_identical_outcome_is_accepted(self):
        result = classify_result(
            0,
            "ORCHESTRATOR_OUTCOME: submit\nORCHESTRATOR_OUTCOME: submit\n",
            {"submit"},
        )
        self.assertEqual((result.classification, result.outcome), ("success", "submit"))

    def test_final_outcome_marker_wins_over_transcript_references(self):
        result = classify_result(
            0,
            "tool output\nORCHESTRATOR_OUTCOME: drafted\n"
            "quoted prior log\nORCHESTRATOR_OUTCOME: ready\n"
            "final decision\nORCHESTRATOR_OUTCOME: allow\n",
            {"allow", "block"},
        )
        self.assertEqual((result.classification, result.outcome), ("success", "allow"))

    def test_non_final_ambiguous_outcomes_still_block(self):
        result = classify_result(
            0,
            "ORCHESTRATOR_OUTCOME: allow\ntrailing transcript\nORCHESTRATOR_OUTCOME: block\nextra text\n",
            {"allow", "block"},
        )
        self.assertEqual((result.classification, result.reason), ("blocked", "ambiguous_outcome"))

    def test_runner_log_records_model_provenance_from_command(self):
        cases = [
            (
                "codex",
                ["codex", "exec", "--approve-for-me", "--model", "gpt-5.5"],
                "gpt-5.5",
            ),
            ("codex", ["codex", "exec", "-m", "gpt-5.5"], "gpt-5.5"),
            ("claude", ["claude", "-p", "--model=claude-opus-5"], "claude-opus-5"),
        ]
        for owner, command, expected_model in cases:
            with self.subTest(command=command), tempfile.TemporaryDirectory() as directory:
                log_path = Path(directory) / "run.log"
                SubprocessRunner._write_log(
                    log_path,
                    owner,
                    command,
                    1.0,
                    2.5,
                    0,
                    False,
                    "ok\n",
                    None,
                    None,
                )
                text = log_path.read_text(encoding="utf-8")
            self.assertIn(f"owner={owner}\n", text)
            self.assertIn(f"model={expected_model}\n", text)

    def test_runner_log_marks_unspecified_model(self):
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "run.log"
            SubprocessRunner._write_log(
                log_path,
                "claude",
                ["claude", "-p"],
                1.0,
                2.0,
                0,
                False,
                "ok\n",
                None,
                None,
            )
            text = log_path.read_text(encoding="utf-8")
        self.assertIn("owner=claude\n", text)
        self.assertIn("model=unspecified\n", text)

    def test_daemon_launcher_requires_explicit_models_and_consent(self):
        """The launcher must not supply defaults for either.

        A default model keeps working after a provider changes what its own
        default resolves to, and the run records would then name one model while
        another did the work. Unattended operation disables the provider CLIs'
        approval prompts, which is a decision an operator states rather than
        inherits from a script nobody re-read.
        """
        script = (ROOT / "packaging" / "run-daemon.sh").read_text(encoding="utf-8")
        self.assertIn('${ORCH_CLAUDE_MODEL:?', script)
        self.assertIn('${ORCH_CODEX_MODEL:?', script)
        self.assertNotIn("ORCH_CLAUDE_MODEL:-", script)
        self.assertNotIn("ORCH_CODEX_MODEL:-", script)
        self.assertIn('"${ORCH_ALLOW_UNATTENDED:-}" != "1"', script)
        self.assertIn("--model $ORCH_CLAUDE_MODEL", script)
        self.assertIn("--model $ORCH_CODEX_MODEL", script)

    def test_daemon_launcher_refuses_to_start_without_consent(self):
        script = ROOT / "packaging" / "run-daemon.sh"
        result = subprocess.run(
            ["bash", str(script)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env={k: v for k, v in os.environ.items() if k != "ORCH_ALLOW_UNATTENDED"},
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 78, result.stdout + result.stderr)
        self.assertIn("refusing to start", result.stderr)

    def test_daemon_launcher_refuses_without_explicit_models(self):
        script = ROOT / "packaging" / "run-daemon.sh"
        env = {k: v for k, v in os.environ.items() if not k.startswith("ORCH_")}
        env["ORCH_ALLOW_UNATTENDED"] = "1"
        result = subprocess.run(
            ["bash", str(script)], cwd=ROOT, capture_output=True, text=True,
            env=env, timeout=30, check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ORCH_CLAUDE_MODEL", result.stderr)


class StartPhaseTests(unittest.TestCase):
    def test_cli_parser_accepts_start_shape_and_dry_run_flags(self):
        args = build_parser().parse_args(
            [
                "start",
                "implement orch start",
                "--task-type",
                "apply",
                "--scope",
                "orchestrator/start.py",
                "--worktree",
                str(ROOT),
                "--approved-spec",
                str(ROOT / "output/orchestrator/propose-scratch/spec-draft.md"),
                "--effort",
                "high",
                "--dry-run",
            ]
        )
        self.assertEqual(args.command, "start")
        self.assertEqual(args.task_type, "apply")
        self.assertTrue(args.dry_run)

        smoke_args = build_parser().parse_args(["start", "provider smoke", "--task-type", "provider-smoke"])
        self.assertEqual(smoke_args.command, "start")
        self.assertEqual(smoke_args.task_type, "provider-smoke")

        go_args = build_parser().parse_args(["start-go", "abc12345"])
        self.assertEqual(go_args.command, "start-go")
        self.assertEqual(go_args.task_id, "abc12345")

        sync_args = build_parser().parse_args(["start-sync", "abc12345"])
        self.assertEqual(sync_args.command, "start-sync")
        self.assertEqual(sync_args.task_id, "abc12345")

        gate_args = build_parser().parse_args(["gate-status", "abc12345"])
        self.assertEqual(gate_args.command, "gate-status")
        self.assertEqual(gate_args.task_id, "abc12345")

        gate_run_args = build_parser().parse_args(["gate-run", "abc12345"])
        self.assertEqual(gate_run_args.command, "gate-run")
        self.assertEqual(gate_run_args.task_id, "abc12345")

        gate_sync_args = build_parser().parse_args(["gate-sync", "abc12345"])
        self.assertEqual(gate_sync_args.command, "gate-sync")
        self.assertEqual(gate_sync_args.task_id, "abc12345")

        allow_args = build_parser().parse_args(["gate-allow", "abc12345", "--reason", "review passed"])
        self.assertEqual(allow_args.command, "gate-allow")
        self.assertEqual(allow_args.task_id, "abc12345")
        self.assertEqual(allow_args.reason, "review passed")

        block_args = build_parser().parse_args(["gate-block", "abc12345"])
        self.assertEqual(block_args.command, "gate-block")
        self.assertEqual(block_args.task_id, "abc12345")
        self.assertIsNone(block_args.reason)

    def _enqueued_propose_start(self, home: Path) -> dict:
        return run_start(
            home,
            "propose a spec for orch start intake",
            StartFlags("propose", "orch start intake", None, None, None, False),
        )

    def _enqueued_stop_gate_apply_start(self, home: Path, worktree: Path) -> dict:
        spec = worktree / "approved-spec.md"
        spec.write_text("Status: approved\n", encoding="utf-8")
        return run_start(
            home,
            "apply deploy metadata implementation to B17 worktree",
            StartFlags("apply", "B17 worktree", worktree, spec, "medium", False),
        )

    def _write_processed_result(self, home: Path, request_id: str, payload: dict) -> Path:
        processed = home / "processed"
        processed.mkdir(parents=True, exist_ok=True)
        path = processed / f"20260101-000000-{request_id}.fake.result.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def _write_gate_review_processed_result(self, home: Path, request_id: str, outcome: str) -> Path:
        return self._write_processed_result(
            home,
            request_id,
            {
                "request_id": request_id,
                "task_id": "controller-task-review",
                "status": "done",
                "stop_reason": "terminal_done",
                "stage_runs": [
                    {
                        "run_token": "run-review",
                        "stage": "review",
                        "owner": "claude",
                        "status": "committed",
                        "outcome": outcome,
                    }
                ],
                "transitions": [
                    {
                        "seq": 1,
                        "stage": "review",
                        "owner": "claude",
                        "edge": f"review.{outcome}",
                        "outcome": outcome,
                        "from_status": "running",
                        "to_status": "done",
                        "reason": "stage_completed",
                    }
                ],
            },
        )

    def _synced_gate_pending_task(self, home: Path, worktree: Path) -> dict:
        started = self._enqueued_stop_gate_apply_start(home, worktree)
        request_id = started["routing"]["execution"]["request_id"]
        self._write_processed_result(
            home,
            request_id,
            {
                "request_id": request_id,
                "task_id": "controller-task-gate",
                "status": "done",
                "stop_reason": "terminal_done",
            },
        )
        return run_start_sync(home, started["task_id"])

    def _synced_claude_gate_pending_task(self, home: Path, worktree: Path) -> dict:
        spec = worktree / "approved-spec.md"
        spec.write_text("Status: approved\n", encoding="utf-8")
        started = run_start(
            home,
            "apply changes to orchestrator/router/daemon/memory flow with executor=claude",
            StartFlags("apply", "orchestrator/router/daemon/memory executor=claude", worktree, spec, "high", False),
        )
        approved = run_start_go(home, started["task_id"])
        request_id = approved["routing"]["execution"]["request_id"]
        self._write_processed_result(
            home,
            request_id,
            {
                "request_id": request_id,
                "task_id": "controller-task-gate",
                "status": "done",
                "stop_reason": "terminal_done",
            },
        )
        return run_start_sync(home, started["task_id"])

    def test_scope_ambiguity_blocks_even_when_task_type_keyword_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            result = run_start(Path(directory), "apply", StartFlags("apply", None, None, None, None, True))
        routing = result["routing"]
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(routing["preflight"]["status"], "blocked")
        self.assertIsNone(routing["pattern"])
        self.assertIsNone(routing["executor"])
        self.assertIsNone(routing["reviewer"])

    def test_apply_without_approved_spec_waits_for_user(self):
        with tempfile.TemporaryDirectory() as directory:
            result = run_start(
                Path(directory),
                "apply implementation to B17 worktree",
                StartFlags("apply", "B17 worktree", Path(directory), None, None, True),
            )
        self.assertEqual(result["status"], "waiting_user")
        self.assertEqual(result["routing"]["preflight"]["status"], "waiting_user")
        self.assertIn("--approved-spec", result["routing"]["preflight"]["reason"])

    def test_apply_without_worktree_blocks_when_approved_spec_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            spec = Path(directory) / "approved-spec.md"
            spec.write_text("status: approved\n", encoding="utf-8")
            result = run_start(
                Path(directory) / "runtime",
                "apply implementation to B17 worktree",
                StartFlags("apply", "B17 worktree", None, spec, None, True),
            )
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["routing"]["preflight"]["status"], "blocked")
        self.assertIn("--worktree", result["routing"]["preflight"]["reason"])

    def test_missing_approved_spec_path_blocks(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.md"
            result = run_start(
                Path(directory) / "runtime",
                "apply implementation to B17 worktree",
                StartFlags("apply", "B17 worktree", Path(directory), missing, None, True),
            )
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["routing"]["preflight"]["status"], "blocked")
        self.assertIn("approved spec not found", result["routing"]["preflight"]["reason"])

    def test_route_for_propose_selects_claude_to_codex(self):
        with tempfile.TemporaryDirectory() as directory:
            result = run_start(
                Path(directory),
                "propose a spec for orch start intake",
                StartFlags("propose", "orch start intake", None, None, None, True),
            )
        routing = result["routing"]
        self.assertEqual((routing["pattern"], routing["executor"], routing["reviewer"]), ("propose_spec", "claude", "codex"))
        self.assertEqual(routing["route_source"], "rule")

    def test_route_for_apply_defaults_to_claude_apply_codex_review(self):
        with tempfile.TemporaryDirectory() as directory:
            spec = Path(directory) / "approved-spec.md"
            spec.write_text("Status: ready\n", encoding="utf-8")
            result = run_start(
                Path(directory) / "runtime",
                "apply implementation to B17 worktree",
                StartFlags("apply", "B17 worktree", Path(directory), spec, None, True),
            )
        routing = result["routing"]
        self.assertEqual(
            (routing["pattern"], routing["executor"], routing["reviewer"]),
            ("claude_apply_codex_review", "claude", "codex"),
        )

    def test_provider_smoke_routes_to_stateful_smoke_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime"
            result = run_start(
                home,
                "provider smoke under daemon",
                StartFlags("provider-smoke", "provider smoke", None, None, None, False),
            )
            routing = result["routing"]
            self.assertEqual(result["status"], "execute")
            self.assertFalse(routing["stop_gate"])
            self.assertEqual((routing["pattern"], routing["executor"], routing["reviewer"]), ("provider_smoke", "claude", "codex"))
            request = json.loads(Path(routing["execution"]["request_path"]).read_text(encoding="utf-8"))
            self.assertEqual(request["type"], "provider-smoke")
            self.assertTrue(request["profile"].endswith("orchestrator/profiles/provider_smoke.yaml"))

    def test_gated_provider_smoke_routes_to_stateful_gated_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime"
            result = run_start(
                home,
                "gated provider smoke under daemon",
                StartFlags("provider-smoke", "gated provider smoke", None, None, None, False),
            )
            routing = result["routing"]
            self.assertEqual(result["status"], "execute")
            self.assertTrue(routing["stop_gate"])
            self.assertEqual(
                (routing["pattern"], routing["executor"], routing["reviewer"]),
                ("provider_smoke_gated", "claude", "codex"),
            )
            request = json.loads(Path(routing["execution"]["request_path"]).read_text(encoding="utf-8"))
            self.assertEqual(request["type"], "provider-smoke")
            self.assertTrue(request["profile"].endswith("orchestrator/profiles/provider_smoke_gated.yaml"))

    def test_high_risk_paths_set_high_risk_stop_gate_and_no_auto_start(self):
        with tempfile.TemporaryDirectory() as directory:
            spec = Path(directory) / "approved-spec.md"
            spec.write_text("Status: approved\n", encoding="utf-8")
            result = run_start(
                Path(directory) / "runtime",
                "apply changes to orchestrator/router/daemon/memory flow",
                StartFlags(
                    "apply",
                    "orchestrator/router/daemon/memory",
                    Path(directory),
                    spec,
                    "high",
                    True,
                ),
            )
        routing = result["routing"]
        self.assertEqual(routing["risk"]["implementation"], "high")
        self.assertTrue(routing["stop_gate"])
        self.assertFalse(routing["auto_start"])
        self.assertEqual(
            (routing["pattern"], routing["executor"], routing["reviewer"]),
            ("claude_apply_codex_review", "claude", "codex"),
        )
        self.assertEqual(result["status"], "waiting_user")

    def test_mixed_or_model_adjudicated_route_forces_no_auto_start(self):
        with tempfile.TemporaryDirectory() as directory:
            mixed = run_start(
                Path(directory) / "mixed",
                "propose orch start mixed route",
                StartFlags("propose", "orch start", None, None, None, True),
            )
            model = run_start(
                Path(directory) / "model",
                "investigate orch start model adjudication",
                StartFlags(None, "orch start", None, None, None, True),
            )
        self.assertEqual(mixed["routing"]["route_source"], "mixed")
        self.assertFalse(mixed["routing"]["auto_start"])
        self.assertEqual(model["routing"]["route_source"], "model-adjudicated")
        self.assertFalse(model["routing"]["auto_start"])

    def test_dry_run_does_not_enqueue_or_execute_providers(self):
        with tempfile.TemporaryDirectory() as directory:
            env = os.environ.copy()
            env["ORCH_HOME"] = directory
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "orchestrator",
                    "start",
                    "propose a spec for orch start intake",
                    "--task-type",
                    "propose",
                    "--scope",
                    "orch start intake",
                    "--dry-run",
                ],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertTrue(Path(payload["routing_decision"]).is_file())
            self.assertFalse(list((Path(directory) / "inbox").glob("*.json")))
            self.assertFalse((Path(directory) / "orchestrator.db").exists())

    def test_cli_status_accepts_start_lifecycle_task_id(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            started = run_start(
                home,
                "propose a spec for orch start status",
                StartFlags("propose", "orch start status", None, None, None, True),
            )
            env = os.environ.copy()
            env["ORCH_HOME"] = str(home)
            result = subprocess.run(
                [sys.executable, "-m", "orchestrator", "status", started["task_id"]],
                cwd=Path(__file__).resolve().parents[2],
                env=env,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["task_id"], started["task_id"])
            self.assertEqual(payload["status"], started["status"])
            self.assertFalse((home / "orchestrator.db").exists())

    def test_non_dry_run_propose_auto_start_enqueues_daemon_request(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            result = run_start(
                home,
                "propose a spec for orch start intake",
                StartFlags("propose", "orch start intake", None, None, None, False),
            )
            routing = result["routing"]
            execution = routing["execution"]
            self.assertEqual(result["status"], "execute")
            self.assertEqual(execution["status"], "enqueued")
            self.assertEqual(execution["controller_task_id"], execution["request_id"])
            request_path = Path(execution["request_path"])
            request = json.loads(request_path.read_text(encoding="utf-8"))
            self.assertEqual(request["action"], "run")
            self.assertEqual(request["type"], "propose")
            self.assertTrue(request["profile"].endswith("orchestrator/profiles/propose.yaml"))
            input_path = Path(request["input"])
            self.assertTrue(input_path.is_file())
            self.assertIn("propose a spec for orch start intake", input_path.read_text(encoding="utf-8"))
            self.assertFalse((home / "orchestrator.db").exists())

    def test_non_dry_run_spec_review_auto_start_enqueues_daemon_request(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            result = run_start(
                home,
                "review the orch start spec",
                StartFlags("review", "orch start spec", None, None, None, False),
            )
            routing = result["routing"]
            execution = routing["execution"]
            self.assertEqual(result["status"], "execute")
            self.assertEqual(execution["status"], "enqueued")
            self.assertEqual((routing["pattern"], routing["executor"], routing["reviewer"]), ("spec_review", "codex", "claude"))
            request = json.loads(Path(execution["request_path"]).read_text(encoding="utf-8"))
            self.assertEqual(request["action"], "run")
            self.assertEqual(request["type"], "spec-review")
            self.assertTrue(request["profile"].endswith("orchestrator/profiles/spec_review.yaml"))
            input_text = Path(request["input"]).read_text(encoding="utf-8")
            self.assertIn("- Pattern: spec_review", input_text)
            self.assertIn("- Executor: codex", input_text)
            self.assertIn("- Reviewer: claude", input_text)
            self.assertIn("- Route source: rule", input_text)

    def test_non_dry_run_medium_risk_apply_auto_start_enqueues_claude_apply_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime"
            spec = Path(directory) / "approved-spec.md"
            spec.write_text("Status: approved\n", encoding="utf-8")
            result = run_start(
                home,
                "apply deploy metadata implementation to B17 worktree",
                StartFlags("apply", "B17 worktree", Path(directory), spec, "medium", False),
            )
            routing = result["routing"]
            execution = routing["execution"]
            self.assertEqual(result["status"], "execute")
            self.assertEqual(routing["risk"]["implementation"], "medium")
            self.assertTrue(routing["auto_start"])
            self.assertEqual(
                (routing["pattern"], routing["executor"], routing["reviewer"]),
                ("claude_apply_codex_review", "claude", "codex"),
            )
            request = json.loads(Path(execution["request_path"]).read_text(encoding="utf-8"))
            self.assertEqual(request["type"], "apply")
            self.assertTrue(request["profile"].endswith("orchestrator/profiles/claude_apply_codex_review.yaml"))

    def test_non_dry_run_high_risk_waits_without_enqueue_and_preserves_route(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime"
            spec = Path(directory) / "approved-spec.md"
            spec.write_text("Status: approved\n", encoding="utf-8")
            result = run_start(
                home,
                "apply changes to orchestrator/router/daemon/memory flow",
                StartFlags("apply", "orchestrator/router/daemon/memory", Path(directory), spec, "high", False),
            )
            routing = result["routing"]
            self.assertEqual(result["status"], "waiting_user")
            self.assertFalse(list((home / "inbox").glob("*.json")))
            self.assertEqual(
                (routing["pattern"], routing["executor"], routing["reviewer"]),
                ("claude_apply_codex_review", "claude", "codex"),
            )
            self.assertIn("start-go", routing["go"]["command"])

    def test_start_go_for_post_route_waiting_user_enqueues_without_rerouting(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            waiting = run_start(
                home,
                "propose orch start mixed route",
                StartFlags("propose", "orch start", None, None, None, False),
            )
            self.assertEqual(waiting["status"], "waiting_user")
            self.assertEqual(waiting["routing"]["route_source"], "mixed")
            self.assertFalse(list((home / "inbox").glob("*.json")))

            approved = run_start_go(home, waiting["task_id"])
            routing = approved["routing"]
            self.assertEqual(approved["status"], "execute")
            self.assertEqual(
                (routing["pattern"], routing["executor"], routing["reviewer"]),
                ("propose_spec", "claude", "codex"),
            )
            request = json.loads(Path(routing["execution"]["request_path"]).read_text(encoding="utf-8"))
            self.assertEqual(request["action"], "run")
            self.assertEqual(request["type"], "propose")

    def test_start_go_for_high_risk_apply_enqueues_claude_apply_profile_and_preserves_route(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime"
            spec = Path(directory) / "approved-spec.md"
            spec.write_text("Status: approved\n", encoding="utf-8")
            waiting = run_start(
                home,
                "apply changes to orchestrator/router/daemon/memory flow",
                StartFlags("apply", "orchestrator/router/daemon/memory", Path(directory), spec, "high", False),
            )
            self.assertEqual(waiting["status"], "waiting_user")
            self.assertFalse(list((home / "inbox").glob("*.json")))

            approved = run_start_go(home, waiting["task_id"])
            routing = approved["routing"]
            self.assertEqual(approved["status"], "execute")
            self.assertEqual(routing["risk"]["implementation"], "high")
            self.assertEqual(
                (routing["pattern"], routing["executor"], routing["reviewer"]),
                ("claude_apply_codex_review", "claude", "codex"),
            )
            request = json.loads(Path(routing["execution"]["request_path"]).read_text(encoding="utf-8"))
            self.assertEqual(request["type"], "apply")
            self.assertTrue(request["profile"].endswith("orchestrator/profiles/claude_apply_codex_review.yaml"))
            input_text = Path(request["input"]).read_text(encoding="utf-8")
            self.assertIn("- Pattern: claude_apply_codex_review", input_text)
            self.assertIn("- Executor: claude", input_text)
            self.assertIn("- Reviewer: codex", input_text)

    def test_start_go_for_codex_executor_hint_enqueues_codex_implement_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime"
            spec = Path(directory) / "approved-spec.md"
            spec.write_text("Status: approved\n", encoding="utf-8")
            waiting = run_start(
                home,
                "apply changes to orchestrator/router/daemon/memory flow with executor=codex",
                StartFlags("apply", "orchestrator/router/daemon/memory executor=codex", Path(directory), spec, "high", False),
            )
            self.assertEqual(waiting["status"], "waiting_user")
            self.assertEqual(
                (waiting["routing"]["pattern"], waiting["routing"]["executor"], waiting["routing"]["reviewer"]),
                ("codex_implement_claude_review", "codex", "claude"),
            )

            approved = run_start_go(home, waiting["task_id"])
            routing = approved["routing"]
            self.assertEqual(approved["status"], "execute")
            request = json.loads(Path(routing["execution"]["request_path"]).read_text(encoding="utf-8"))
            self.assertEqual(request["type"], "apply")
            self.assertTrue(request["profile"].endswith("orchestrator/profiles/codex_implement_claude_review.yaml"))
            input_text = Path(request["input"]).read_text(encoding="utf-8")
            self.assertIn("- Pattern: codex_implement_claude_review", input_text)
            self.assertIn("- Executor: codex", input_text)
            self.assertIn("- Reviewer: claude", input_text)

    def test_start_go_for_preflight_waiting_user_or_blocked_fails_clearly(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            waiting = run_start(
                home,
                "apply implementation to B17 worktree",
                StartFlags("apply", "B17 worktree", Path(directory), None, None, False),
            )
            blocked = run_start(home, "apply", StartFlags("apply", None, None, None, None, False))
            with self.assertRaisesRegex(ValueError, "preflight waiting_user"):
                run_start_go(home, waiting["task_id"])
            with self.assertRaisesRegex(ValueError, "blocked before execution"):
                run_start_go(home, blocked["task_id"])
            self.assertFalse(list((home / "inbox").glob("*.json")))

    def test_start_sync_done_updates_artifacts_and_appends_notification(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            started = self._enqueued_propose_start(home)
            request_id = started["routing"]["execution"]["request_id"]
            inbox_requests_before = sorted((home / "inbox").glob("*.json"))
            result_path = self._write_processed_result(
                home,
                request_id,
                {
                    "request_id": request_id,
                    "task_id": "controller-task-1",
                    "status": "done",
                    "stop_reason": "terminal_done",
                },
            )

            synced = run_start_sync(home, started["task_id"])
            self.assertEqual(synced["status"], "done")
            execution_result = synced["routing"]["execution_result"]
            self.assertEqual(execution_result["processed_result_path"], str(result_path))
            self.assertEqual(execution_result["controller_task_id"], "controller-task-1")
            self.assertEqual(execution_result["controller_status"], "done")
            self.assertEqual(execution_result["stop_reason"], "terminal_done")
            self.assertEqual(execution_result["controller_lifecycle_stage"], "done")
            self.assertFalse(execution_result["gate_required"])
            task_record = _read_yaml(home / "tasks" / f"{started['task_id']}.yaml")
            routing = _read_yaml(home / "tasks" / f"{started['task_id']}-routing.yaml")
            self.assertEqual(task_record["execution_result"]["lifecycle_stage"], "done")
            self.assertEqual(routing["execution_result"]["lifecycle_stage"], "done")
            self.assertNotIn("gate", task_record)
            self.assertNotIn("gate", routing)
            inbox_text = (home / "inbox.md").read_text(encoding="utf-8")
            self.assertIn(str(result_path), inbox_text)
            self.assertIn("controller_task_id=controller-task-1", inbox_text)
            self.assertIn("controller_status=done", inbox_text)
            self.assertIn("stop_reason=terminal_done", inbox_text)
            self.assertEqual(sorted((home / "inbox").glob("*.json")), inbox_requests_before)

            synced_again = run_start_sync(home, started["task_id"])
            self.assertEqual(synced_again["status"], "done")
            self.assertEqual(synced_again["routing"]["execution_result"]["processed_result_path"], str(result_path))

    def test_start_sync_uses_latest_resume_result_for_same_controller_task(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            started = self._enqueued_propose_start(home)
            original_request_id = started["routing"]["execution"]["request_id"]
            controller_task_id = "controller-task-resumed"
            self._write_processed_result(
                home,
                original_request_id,
                {
                    "request_id": original_request_id,
                    "task_id": controller_task_id,
                    "status": "blocked",
                    "stop_reason": "runner_nonzero",
                },
            )
            resume_request_id = "resume-request-1"
            resume_result_path = self._write_processed_result(
                home,
                resume_request_id,
                {
                    "request_id": resume_request_id,
                    "task_id": controller_task_id,
                    "status": "done",
                    "stop_reason": None,
                },
            )

            synced = run_start_sync(home, started["task_id"])

            execution_result = synced["routing"]["execution_result"]
            self.assertEqual(synced["status"], "done")
            self.assertEqual(execution_result["request_id"], resume_request_id)
            self.assertEqual(execution_result["processed_result_path"], str(resume_result_path))
            self.assertEqual(execution_result["controller_task_id"], controller_task_id)

    def test_start_sync_stop_gate_done_pauses_for_gate_and_records_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime"
            started = self._enqueued_stop_gate_apply_start(home, Path(directory))
            request_id = started["routing"]["execution"]["request_id"]
            inbox_requests_before = sorted((home / "inbox").glob("*.json"))
            result_path = self._write_processed_result(
                home,
                request_id,
                {
                    "request_id": request_id,
                    "task_id": "controller-task-gate",
                    "status": "done",
                    "stop_reason": "terminal_done",
                },
            )

            synced = run_start_sync(home, started["task_id"])
            self.assertEqual(synced["status"], "waiting_user")
            self.assertTrue(synced["routing"]["stop_gate"])
            execution_result = synced["routing"]["execution_result"]
            self.assertEqual(execution_result["processed_result_path"], str(result_path))
            self.assertEqual(execution_result["controller_task_id"], "controller-task-gate")
            self.assertEqual(execution_result["controller_status"], "done")
            self.assertEqual(execution_result["controller_lifecycle_stage"], "done")
            self.assertEqual(execution_result["lifecycle_stage"], "waiting_user")
            self.assertTrue(execution_result["gate_required"])

            task_record = _read_yaml(home / "tasks" / f"{started['task_id']}.yaml")
            routing = _read_yaml(home / "tasks" / f"{started['task_id']}-routing.yaml")
            self.assertEqual(task_record["stage"], "waiting_user")
            self.assertEqual(task_record["gate"], routing["gate"])
            gate = routing["gate"]
            self.assertEqual(gate["type"], "stop_gate")
            self.assertEqual(gate["status"], "pending")
            self.assertEqual(gate["stage"], "waiting_user")
            self.assertEqual(gate["processed_result_path"], str(result_path))
            self.assertEqual(gate["controller_task_id"], "controller-task-gate")
            self.assertEqual(gate["controller_status"], "done")
            self.assertEqual(
                (gate["pattern"], gate["executor"], gate["reviewer"]),
                ("claude_apply_codex_review", "claude", "codex"),
            )
            inbox_text = (home / "inbox.md").read_text(encoding="utf-8")
            self.assertIn("stop-gate approval required", inbox_text)
            self.assertEqual(sorted((home / "inbox").glob("*.json")), inbox_requests_before)

            status = gate_status(home, started["task_id"])
            self.assertEqual(status["status"], "waiting_user")
            self.assertTrue(status["routing_stop_gate"])
            self.assertEqual(status["gate"], gate)

    def test_start_sync_stop_gate_done_repeated_sync_keeps_gate_summary_stable(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime"
            started = self._enqueued_stop_gate_apply_start(home, Path(directory))
            request_id = started["routing"]["execution"]["request_id"]
            self._write_processed_result(
                home,
                request_id,
                {
                    "request_id": request_id,
                    "task_id": "controller-task-gate",
                    "status": "done",
                    "stop_reason": "terminal_done",
                },
            )

            first = run_start_sync(home, started["task_id"])
            inbox_after_first = (home / "inbox.md").read_text(encoding="utf-8")
            second = run_start_sync(home, started["task_id"])
            self.assertEqual(first["status"], "waiting_user")
            self.assertEqual(second["status"], "waiting_user")
            self.assertEqual(first["routing"]["gate"], second["routing"]["gate"])
            self.assertEqual(first["routing"]["execution_result"], second["routing"]["execution_result"])
            self.assertEqual(second["routing"]["execution_result"]["controller_status"], "done")
            self.assertTrue(second["routing"]["execution_result"]["gate_required"])
            self.assertEqual((home / "inbox.md").read_text(encoding="utf-8"), inbox_after_first)

    def test_gate_allow_records_decision_moves_done_and_notifies_without_enqueue(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime"
            synced = self._synced_gate_pending_task(home, Path(directory))
            inbox_requests_before = sorted((home / "inbox").glob("*.json"))

            allowed = run_gate_decision(home, synced["task_id"], "ALLOW", "manual approval")

            self.assertEqual(allowed["status"], "done")
            decision = allowed["routing"]["gate_decision"]
            self.assertEqual(decision["decision"], "ALLOW")
            self.assertEqual(decision["reason"], "manual approval")
            self.assertEqual(decision["final_stage"], "done")
            self.assertEqual(decision["controller_task_id"], "controller-task-gate")
            self.assertEqual(decision["controller_status"], "done")
            self.assertEqual(decision["pattern"], "claude_apply_codex_review")
            self.assertEqual((decision["executor"], decision["reviewer"]), ("claude", "codex"))
            self.assertTrue(str(decision["profile"]).endswith("orchestrator/profiles/claude_apply_codex_review.yaml"))
            self.assertTrue(Path(decision["decision_artifact_path"]).is_file())
            self.assertEqual(allowed["routing"]["gate"]["status"], "decided")
            self.assertEqual(allowed["routing"]["gate"]["decision"], "ALLOW")

            task_record = _read_yaml(home / "tasks" / f"{synced['task_id']}.yaml")
            routing = _read_yaml(home / "tasks" / f"{synced['task_id']}-routing.yaml")
            artifact = _read_yaml(Path(decision["decision_artifact_path"]))
            self.assertEqual(task_record["stage"], "done")
            self.assertEqual(task_record["gate_decision"], routing["gate_decision"])
            self.assertEqual(artifact["decision"], "ALLOW")
            self.assertEqual(task_record["execution_result"]["request_id"], synced["routing"]["execution_result"]["request_id"])
            self.assertEqual(routing["execution"]["request_id"], synced["routing"]["execution"]["request_id"])
            self.assertIn("stop-gate decision ALLOW", (home / "inbox.md").read_text(encoding="utf-8"))
            self.assertEqual(sorted((home / "inbox").glob("*.json")), inbox_requests_before)

            status = gate_status(home, synced["task_id"])
            self.assertEqual(status["status"], "done")
            self.assertEqual(status["decision"]["decision"], "ALLOW")

            synced_after_decision = run_start_sync(home, synced["task_id"])
            self.assertEqual(synced_after_decision["status"], "done")
            self.assertFalse(synced_after_decision["routing"]["execution_result"]["gate_required"])
            self.assertEqual(synced_after_decision["routing"]["execution_result"]["lifecycle_stage"], "done")
            self.assertEqual(synced_after_decision["routing"]["gate"]["status"], "decided")
            self.assertEqual(synced_after_decision["routing"]["gate"]["decision"], "ALLOW")

    def test_gate_block_records_decision_moves_blocked_and_notifies_without_enqueue(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime"
            synced = self._synced_gate_pending_task(home, Path(directory))
            inbox_requests_before = sorted((home / "inbox").glob("*.json"))

            blocked = run_gate_decision(home, synced["task_id"], "BLOCK", "missing release approval")

            self.assertEqual(blocked["status"], "blocked")
            decision = blocked["routing"]["gate_decision"]
            self.assertEqual(decision["decision"], "BLOCK")
            self.assertEqual(decision["reason"], "missing release approval")
            self.assertEqual(decision["final_stage"], "blocked")
            self.assertEqual(blocked["routing"]["gate"]["status"], "decided")
            self.assertEqual(blocked["routing"]["gate"]["decision"], "BLOCK")
            self.assertTrue(Path(decision["decision_artifact_path"]).is_file())
            self.assertIn("stop-gate decision BLOCK", (home / "inbox.md").read_text(encoding="utf-8"))
            self.assertEqual(sorted((home / "inbox").glob("*.json")), inbox_requests_before)

            status = gate_status(home, synced["task_id"])
            self.assertEqual(status["status"], "blocked")
            self.assertEqual(status["decision"]["decision"], "BLOCK")

    def test_gate_decision_rejects_non_gate_and_already_decided_tasks(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            non_gate = self._enqueued_propose_start(home)
            request_id = non_gate["routing"]["execution"]["request_id"]
            self._write_processed_result(
                home,
                request_id,
                {
                    "request_id": request_id,
                    "task_id": "controller-task-1",
                    "status": "done",
                    "stop_reason": "terminal_done",
                },
            )
            run_start_sync(home, non_gate["task_id"])
            with self.assertRaisesRegex(ValueError, "not waiting on a stop-gate"):
                run_gate_decision(home, non_gate["task_id"], "ALLOW")

            pre_route = run_start(
                home / "pre-route",
                "propose orch start mixed route",
                StartFlags("propose", "orch start", None, None, None, False),
            )
            with self.assertRaisesRegex(ValueError, "not a stop-gate task"):
                run_gate_decision(home / "pre-route", pre_route["task_id"], "ALLOW")

            pending = self._synced_gate_pending_task(home / "gate", Path(directory))
            run_gate_decision(home / "gate", pending["task_id"], "ALLOW")
            inbox_requests_after_first = sorted(((home / "gate") / "inbox").glob("*.json"))
            with self.assertRaisesRegex(ValueError, "already decided"):
                run_gate_decision(home / "gate", pending["task_id"], "BLOCK")
            self.assertEqual(sorted(((home / "gate") / "inbox").glob("*.json")), inbox_requests_after_first)

    def test_start_sync_maps_processed_status_to_lifecycle_stage(self):
        cases = [
            ({"status": "blocked", "stop_reason": "runner_nonzero"}, "blocked"),
            ({"status": "paused", "stop_reason": "rate_limited"}, "paused"),
            ({"status": "waiting_user", "stop_reason": "edge_cap"}, "waiting_user"),
            ({"status": "round_cap", "stop_reason": "round_cap"}, "waiting_user"),
            ({"status": "failed", "stop_reason": "terminal_failed"}, "failed"),
            ({"error": "ValueError: bad request"}, "failed"),
        ]
        for payload, expected_stage in cases:
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as directory:
                home = Path(directory)
                started = self._enqueued_propose_start(home)
                request_id = started["routing"]["execution"]["request_id"]
                payload = {"request_id": request_id, "task_id": request_id, **payload}
                self._write_processed_result(home, request_id, payload)

                synced = run_start_sync(home, started["task_id"])
                self.assertEqual(synced["status"], expected_stage)
                self.assertEqual(synced["routing"]["execution_result"]["lifecycle_stage"], expected_stage)

    def test_start_sync_stop_gate_ignored_for_non_done_statuses(self):
        cases = [
            ({"status": "blocked", "stop_reason": "runner_nonzero"}, "blocked"),
            ({"status": "paused", "stop_reason": "rate_limited"}, "paused"),
            ({"status": "waiting_user", "stop_reason": "edge_cap"}, "waiting_user"),
            ({"status": "failed", "stop_reason": "terminal_failed"}, "failed"),
            ({"error": "ValueError: bad request"}, "failed"),
        ]
        for payload, expected_stage in cases:
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as directory:
                home = Path(directory) / "runtime"
                started = self._enqueued_stop_gate_apply_start(home, Path(directory))
                request_id = started["routing"]["execution"]["request_id"]
                payload = {"request_id": request_id, "task_id": request_id, **payload}
                self._write_processed_result(home, request_id, payload)

                synced = run_start_sync(home, started["task_id"])
                self.assertEqual(synced["status"], expected_stage)
                self.assertEqual(synced["routing"]["execution_result"]["lifecycle_stage"], expected_stage)
                self.assertFalse(synced["routing"]["execution_result"]["gate_required"])
                self.assertNotIn("gate", synced["routing"])

    def test_start_sync_missing_result_fails_without_terminal_stage_or_new_request(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            started = self._enqueued_propose_start(home)
            task_path = home / "tasks" / f"{started['task_id']}.yaml"
            inbox_requests_before = sorted((home / "inbox").glob("*.json"))
            inbox_markdown_before = (home / "inbox.md").read_text(encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "processed daemon result not found"):
                run_start_sync(home, started["task_id"])

            task_record = _read_yaml(task_path)
            self.assertEqual(task_record["stage"], "execute")
            self.assertNotIn("execution_result", task_record)
            self.assertEqual(sorted((home / "inbox").glob("*.json")), inbox_requests_before)
            self.assertEqual((home / "inbox.md").read_text(encoding="utf-8"), inbox_markdown_before)

    def test_start_sync_processing_result_reports_running_context(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            started = self._enqueued_propose_start(home)
            task_path = home / "tasks" / f"{started['task_id']}.yaml"
            request_id = started["routing"]["execution"]["request_id"]
            request = next((home / "inbox").glob(f"*{request_id}.json"))
            processing = home / "processing"
            processing.mkdir()
            os.replace(request, processing / request.name)

            controller = Controller(home, runner=SequenceRunner([]))
            try:
                controller.submit(
                    started["routing"]["execution"]["type"],
                    Path(started["routing"]["execution"]["profile"]),
                    Path(started["routing"]["execution"]["input"]),
                    task_id=request_id,
                    operation_id=request_id,
                )
                claim = controller.claim_stage(request_id)
                self.assertIsNotNone(claim)
            finally:
                controller.close()

            with self.assertRaisesRegex(ValueError, "daemon request still processing"):
                run_start_sync(home, started["task_id"])

            task_record = _read_yaml(task_path)
            self.assertEqual(task_record["stage"], "execute")
            self.assertNotIn("execution_result", task_record)

    def test_gate_run_for_default_apply_route_enqueues_codex_stop_gate_profile(self):
        with tempfile.TemporaryDirectory() as directory, patch("orchestrator.start.daemon_is_running", return_value=True):
            home = Path(directory) / "runtime"
            synced = self._synced_gate_pending_task(home, Path(directory))
            inbox_requests_before = sorted((home / "inbox").glob("*.json"))

            result = run_gate_run(home, synced["task_id"])

            self.assertEqual(result["status"], "waiting_user")
            gate_review = result["routing"]["gate_review_execution"]
            self.assertEqual(gate_review["status"], "enqueued")
            self.assertEqual(gate_review["owner"], "codex")
            self.assertTrue(gate_review["profile"].endswith("orchestrator/profiles/stop_gate_codex.yaml"))
            self.assertEqual(gate_review["executor"], "claude")
            self.assertEqual(gate_review["reviewer"], "codex")
            self.assertEqual(gate_review["original_request_id"], synced["routing"]["execution_result"]["request_id"])
            self.assertTrue(Path(gate_review["input_path"]).is_file())
            self.assertTrue(Path(gate_review["request_path"]).is_file())
            self.assertEqual(len(sorted((home / "inbox").glob("*.json"))), len(inbox_requests_before) + 1)

            request = json.loads(Path(gate_review["request_path"]).read_text(encoding="utf-8"))
            self.assertEqual(request["action"], "run")
            self.assertEqual(request["type"], "stop-gate")
            self.assertEqual(request["request_id"], gate_review["request_id"])
            self.assertEqual(request["profile"], gate_review["profile"])
            self.assertEqual(request["input"], str(Path(gate_review["input_path"]).resolve()))
            input_text = Path(gate_review["input_path"]).read_text(encoding="utf-8")
            self.assertIn(f"- Task record: {result['task_record']}", input_text)
            self.assertIn(f"- Routing decision: {result['routing_decision']}", input_text)
            self.assertIn(f"- Processed execution result: {synced['routing']['execution_result']['processed_result_path']}", input_text)
            self.assertIn("- Original pattern: claude_apply_codex_review", input_text)
            self.assertIn("- Original executor: claude", input_text)
            self.assertIn("- Original reviewer: codex", input_text)
            self.assertIn(f"- Expected review output path: {gate_review['expected_output_path']}", input_text)
            self.assertIn("# Stop-Gate Review", input_text)
            self.assertIn("## Recommendation", input_text)
            self.assertIn("## Findings", input_text)
            self.assertIn("## Evidence", input_text)
            self.assertIn("## Residual Risk", input_text)
            self.assertIn("## Manual Next Step", input_text)
            self.assertIn("The typed outcome printed by the reviewer process is the machine-readable source of truth.", input_text)

            task_record = _read_yaml(home / "tasks" / f"{synced['task_id']}.yaml")
            routing = _read_yaml(home / "tasks" / f"{synced['task_id']}-routing.yaml")
            self.assertEqual(task_record["stage"], "waiting_user")
            self.assertEqual(task_record["gate_review_execution"], routing["gate_review_execution"])
            self.assertEqual(routing["gate"]["review_execution"], gate_review)
            self.assertNotIn("gate_decision", routing)

    def test_gate_run_pending_claude_gate_enqueues_codex_stop_gate_profile(self):
        with tempfile.TemporaryDirectory() as directory, patch("orchestrator.start.daemon_is_running", return_value=True):
            home = Path(directory) / "runtime"
            synced = self._synced_claude_gate_pending_task(home, Path(directory))

            result = run_gate_run(home, synced["task_id"])

            gate_review = result["routing"]["gate_review_execution"]
            self.assertEqual(gate_review["owner"], "codex")
            self.assertTrue(gate_review["profile"].endswith("orchestrator/profiles/stop_gate_codex.yaml"))
            self.assertEqual(gate_review["executor"], "claude")
            request = json.loads(Path(gate_review["request_path"]).read_text(encoding="utf-8"))
            self.assertEqual(request["type"], "stop-gate")
            self.assertTrue(request["profile"].endswith("orchestrator/profiles/stop_gate_codex.yaml"))

    def test_gate_status_includes_gate_review_execution(self):
        with tempfile.TemporaryDirectory() as directory, patch("orchestrator.start.daemon_is_running", return_value=True):
            home = Path(directory) / "runtime"
            synced = self._synced_gate_pending_task(home, Path(directory))
            result = run_gate_run(home, synced["task_id"])

            status = gate_status(home, synced["task_id"])

            self.assertEqual(status["status"], "waiting_user")
            self.assertEqual(status["gate_review_execution"], result["routing"]["gate_review_execution"])
            self.assertEqual(status["gate"]["review_execution"], result["routing"]["gate_review_execution"])
            self.assertIsNone(status["decision"])

            synced_again = run_start_sync(home, synced["task_id"])
            self.assertEqual(synced_again["routing"]["gate_review_execution"], result["routing"]["gate_review_execution"])
            self.assertEqual(synced_again["routing"]["gate"]["review_execution"], result["routing"]["gate_review_execution"])

    def test_gate_sync_allow_records_recommendation_keeps_waiting_and_notifies_without_enqueue(self):
        with tempfile.TemporaryDirectory() as directory, patch("orchestrator.start.daemon_is_running", return_value=True):
            home = Path(directory) / "runtime"
            synced = self._synced_gate_pending_task(home, Path(directory))
            enqueued_review = run_gate_run(home, synced["task_id"])
            review_request_id = enqueued_review["routing"]["gate_review_execution"]["request_id"]
            result_path = self._write_gate_review_processed_result(home, review_request_id, "allow")
            inbox_requests_before = sorted((home / "inbox").glob("*.json"))

            result = run_gate_sync(home, synced["task_id"])

            self.assertEqual(result["status"], "waiting_user")
            review_result = result["routing"]["gate_review_result"]
            self.assertEqual(review_result["recommendation"], "ALLOW")
            self.assertEqual(review_result["outcome"], "allow")
            self.assertEqual(review_result["processed_result_path"], str(result_path))
            self.assertEqual(review_result["request_id"], review_request_id)
            self.assertEqual(review_result["controller_task_id"], "controller-task-review")
            self.assertEqual(review_result["original_request_id"], synced["routing"]["execution_result"]["request_id"])
            self.assertEqual(result["routing"]["gate"]["status"], "pending")
            self.assertEqual(result["routing"]["gate"]["review_result"], review_result)
            self.assertNotIn("gate_decision", result["routing"])

            task_record = _read_yaml(home / "tasks" / f"{synced['task_id']}.yaml")
            routing = _read_yaml(home / "tasks" / f"{synced['task_id']}-routing.yaml")
            self.assertEqual(task_record["stage"], "waiting_user")
            self.assertEqual(task_record["gate_review_result"], routing["gate_review_result"])
            self.assertEqual(task_record["gate"]["review_result"], review_result)
            inbox_text = (home / "inbox.md").read_text(encoding="utf-8")
            self.assertIn("stop-gate reviewer recommendation ALLOW", inbox_text)
            self.assertIn(str(result_path), inbox_text)
            self.assertEqual(sorted((home / "inbox").glob("*.json")), inbox_requests_before)

            status = gate_status(home, synced["task_id"])
            self.assertEqual(status["status"], "waiting_user")
            self.assertEqual(status["gate_review_result"], review_result)
            self.assertEqual(status["gate"]["review_result"], review_result)

    def test_gate_sync_block_records_recommendation(self):
        with tempfile.TemporaryDirectory() as directory, patch("orchestrator.start.daemon_is_running", return_value=True):
            home = Path(directory) / "runtime"
            synced = self._synced_gate_pending_task(home, Path(directory))
            enqueued_review = run_gate_run(home, synced["task_id"])
            review_request_id = enqueued_review["routing"]["gate_review_execution"]["request_id"]
            self._write_gate_review_processed_result(home, review_request_id, "block")

            result = run_gate_sync(home, synced["task_id"])

            self.assertEqual(result["status"], "waiting_user")
            self.assertEqual(result["routing"]["gate_review_result"]["recommendation"], "BLOCK")
            self.assertEqual(result["routing"]["gate_review_result"]["outcome"], "block")
            self.assertEqual(result["routing"]["gate"]["status"], "pending")
            self.assertNotIn("gate_decision", result["routing"])

    def test_gate_sync_uses_latest_resume_result_for_same_reviewer_task(self):
        controller_task_id = "controller-task-review"
        cases = [
            ("stalled-then-resumed-allow", {"status": "blocked", "stop_reason": "runner_nonzero"}, "allow", "ALLOW"),
            ("stale-allow-then-resumed-block", None, "block", "BLOCK"),
        ]
        for name, stalled_payload, resumed_outcome, expected in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory, patch(
                "orchestrator.start.daemon_is_running", return_value=True
            ):
                home = Path(directory) / "runtime"
                synced = self._synced_gate_pending_task(home, Path(directory))
                enqueued_review = run_gate_run(home, synced["task_id"])
                review_request_id = enqueued_review["routing"]["gate_review_execution"]["request_id"]

                if stalled_payload is None:
                    stale_path = self._write_gate_review_processed_result(home, review_request_id, "allow")
                else:
                    stale_path = self._write_processed_result(
                        home,
                        review_request_id,
                        {"request_id": review_request_id, "task_id": controller_task_id, **stalled_payload},
                    )

                resume_request_id = f"resume-request-{name}"
                resume_path = self._write_gate_review_processed_result(home, resume_request_id, resumed_outcome)
                # The resume result must win on mtime regardless of filesystem timestamp granularity.
                stale_stat = stale_path.stat()
                os.utime(resume_path, ns=(stale_stat.st_atime_ns + 1_000_000, stale_stat.st_mtime_ns + 1_000_000))

                result = run_gate_sync(home, synced["task_id"])

                review_result = result["routing"]["gate_review_result"]
                self.assertEqual(result["status"], "waiting_user")
                self.assertEqual(review_result["recommendation"], expected)
                self.assertEqual(review_result["outcome"], resumed_outcome)
                self.assertEqual(review_result["processed_result_path"], str(resume_path))
                self.assertEqual(review_result["request_id"], resume_request_id)
                self.assertEqual(review_result["controller_task_id"], controller_task_id)
                self.assertEqual(result["routing"]["gate"]["status"], "pending")
                self.assertEqual(
                    result["routing"]["gate"]["review_execution"]["request_id"], review_request_id
                )
                inbox_text = (home / "inbox.md").read_text(encoding="utf-8")
                self.assertIn(f"stop-gate reviewer recommendation {expected}", inbox_text)
                self.assertIn(str(resume_path), inbox_text)

    def test_gate_sync_rejects_missing_result_already_decided_already_synced_and_non_enqueued_gate(self):
        with tempfile.TemporaryDirectory() as directory, patch("orchestrator.start.daemon_is_running", return_value=True):
            home = Path(directory)

            missing = self._synced_gate_pending_task(home / "missing", Path(directory))
            run_gate_run(home / "missing", missing["task_id"])
            inbox_requests_before = sorted(((home / "missing") / "inbox").glob("*.json"))
            with self.assertRaisesRegex(ValueError, "processed stop-gate reviewer result not found"):
                run_gate_sync(home / "missing", missing["task_id"])
            self.assertEqual(sorted(((home / "missing") / "inbox").glob("*.json")), inbox_requests_before)

            non_enqueued = self._synced_gate_pending_task(home / "non-enqueued", Path(directory))
            with self.assertRaisesRegex(ValueError, "has not been enqueued"):
                run_gate_sync(home / "non-enqueued", non_enqueued["task_id"])

            decided = self._synced_gate_pending_task(home / "decided", Path(directory))
            run_gate_run(home / "decided", decided["task_id"])
            run_gate_decision(home / "decided", decided["task_id"], "ALLOW")
            with self.assertRaisesRegex(ValueError, "already decided"):
                run_gate_sync(home / "decided", decided["task_id"])

            already_synced = self._synced_gate_pending_task(home / "already-synced", Path(directory))
            enqueued_review = run_gate_run(home / "already-synced", already_synced["task_id"])
            request_id = enqueued_review["routing"]["gate_review_execution"]["request_id"]
            self._write_gate_review_processed_result(home / "already-synced", request_id, "allow")
            run_gate_sync(home / "already-synced", already_synced["task_id"])
            with self.assertRaisesRegex(ValueError, "already synced"):
                run_gate_sync(home / "already-synced", already_synced["task_id"])

    def test_gate_sync_rejects_malformed_and_unsupported_reviewer_outcomes(self):
        cases = [
            (
                "malformed-empty",
                {
                    "status": "done",
                    "stop_reason": "terminal_done",
                    "stage_runs": [],
                    "transitions": [],
                },
                "missing reviewer outcome",
            ),
            (
                "malformed-missing-lists",
                {"status": "done", "stop_reason": "terminal_done"},
                "missing stage_runs/transitions",
            ),
            (
                "unsupported",
                {
                    "status": "done",
                    "stop_reason": "terminal_done",
                    "stage_runs": [{"stage": "review", "status": "committed", "outcome": "reviewed"}],
                    "transitions": [{"stage": "review", "outcome": "reviewed"}],
                },
                "unsupported stop-gate reviewer outcome",
            ),
            (
                "ambiguous",
                {
                    "status": "done",
                    "stop_reason": "terminal_done",
                    "stage_runs": [{"stage": "review", "status": "committed", "outcome": "allow"}],
                    "transitions": [{"stage": "review", "outcome": "block"}],
                },
                "ambiguous reviewer outcomes",
            ),
        ]
        for name, payload, expected_error in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory, patch(
                "orchestrator.start.daemon_is_running", return_value=True
            ):
                home = Path(directory) / "runtime"
                synced = self._synced_gate_pending_task(home, Path(directory))
                enqueued_review = run_gate_run(home, synced["task_id"])
                request_id = enqueued_review["routing"]["gate_review_execution"]["request_id"]
                self._write_processed_result(home, request_id, {"request_id": request_id, "task_id": request_id, **payload})

                with self.assertRaisesRegex(ValueError, expected_error):
                    run_gate_sync(home, synced["task_id"])

    def test_gate_run_rejects_missing_daemon(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime"
            synced = self._synced_gate_pending_task(home, Path(directory))
            inbox_requests_before = sorted((home / "inbox").glob("*.json"))

            with self.assertRaisesRegex(ValueError, "daemon is not running"):
                run_gate_run(home, synced["task_id"])

            self.assertEqual(sorted((home / "inbox").glob("*.json")), inbox_requests_before)

    def test_gate_run_rejects_non_gate_already_decided_already_enqueued_and_unsupported_executor(self):
        with tempfile.TemporaryDirectory() as directory, patch("orchestrator.start.daemon_is_running", return_value=True):
            home = Path(directory)
            non_gate = self._enqueued_propose_start(home)
            request_id = non_gate["routing"]["execution"]["request_id"]
            self._write_processed_result(
                home,
                request_id,
                {
                    "request_id": request_id,
                    "task_id": "controller-task-1",
                    "status": "done",
                    "stop_reason": "terminal_done",
                },
            )
            run_start_sync(home, non_gate["task_id"])
            with self.assertRaisesRegex(ValueError, "not waiting on a stop-gate"):
                run_gate_run(home, non_gate["task_id"])

            pre_route = run_start(
                home / "pre-route",
                "propose orch start mixed route",
                StartFlags("propose", "orch start", None, None, None, False),
            )
            with self.assertRaisesRegex(ValueError, "not a stop-gate task"):
                run_gate_run(home / "pre-route", pre_route["task_id"])

            decided = self._synced_gate_pending_task(home / "decided", Path(directory))
            run_gate_decision(home / "decided", decided["task_id"], "ALLOW")
            with self.assertRaisesRegex(ValueError, "already decided"):
                run_gate_run(home / "decided", decided["task_id"])

            enqueued = self._synced_gate_pending_task(home / "enqueued", Path(directory))
            run_gate_run(home / "enqueued", enqueued["task_id"])
            inbox_requests_after_first = sorted(((home / "enqueued") / "inbox").glob("*.json"))
            with self.assertRaisesRegex(ValueError, "already enqueued"):
                run_gate_run(home / "enqueued", enqueued["task_id"])
            self.assertEqual(sorted(((home / "enqueued") / "inbox").glob("*.json")), inbox_requests_after_first)

            unsupported = self._synced_gate_pending_task(home / "unsupported", Path(directory))
            task_path = home / "unsupported" / "tasks" / f"{unsupported['task_id']}.yaml"
            routing_path = home / "unsupported" / "tasks" / f"{unsupported['task_id']}-routing.yaml"
            task_record = _read_yaml(task_path)
            routing = _read_yaml(routing_path)
            routing["executor"] = "ollama"
            routing["gate"]["executor"] = "ollama"
            _write_yaml(task_path, task_record)
            _write_yaml(routing_path, routing)
            with self.assertRaisesRegex(ValueError, "unsupported stop-gate executor"):
                run_gate_run(home / "unsupported", unsupported["task_id"])

class CapIntegrationTests(unittest.TestCase):
    def test_request_id_makes_submit_idempotent(self):
        request_id = str(uuid.uuid4())
        with tempfile.TemporaryDirectory() as directory:
            controller = Controller(Path(directory), runner=SequenceRunner([]))
            try:
                first = controller.submit(
                    "demo-loop",
                    DEMO_PROFILE,
                    DEMO_INPUT,
                    task_id=request_id,
                    operation_id=request_id,
                )
                second = controller.submit(
                    "demo-loop",
                    DEMO_PROFILE,
                    DEMO_INPUT,
                    task_id=request_id,
                    operation_id=request_id,
                )
                task_count = controller.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            finally:
                controller.close()
        self.assertEqual((first, second), (request_id, request_id))
        self.assertEqual(task_count, 1)

    def test_loop_hands_off_and_forces_edge_cap(self):
        runner = SequenceRunner(["submit", "block", "submit", "block"])
        with tempfile.TemporaryDirectory() as directory:
            controller = Controller(Path(directory), runner=runner)
            try:
                task_id = controller.submit("demo-loop", DEMO_PROFILE, DEMO_INPUT)
                result = controller.run_until_stop(task_id)
            finally:
                controller.close()
        self.assertEqual(result["task"]["status"], "waiting_user")
        self.assertEqual(result["task"]["stop_reason"], "edge_cap")
        self.assertEqual(result["task"]["transitions_count"], 4)
        self.assertEqual(runner.owners, ["claude", "codex", "claude", "codex"])
        block_edge = next(edge for edge in result["edge_counts"] if edge["edge"] == "review.block")
        self.assertEqual((block_edge["count"], block_edge["cap"]), (1, 1))
        self.assertEqual(result["notifications"][-1]["reason"], "edge_cap")

    def test_transition_cap_stops_before_spawning(self):
        runner = SequenceRunner(["submit", "block", "submit"])
        with tempfile.TemporaryDirectory() as directory:
            profile_path = Path(directory) / "profile.yaml"
            profile_path.write_text(DEMO_PROFILE.read_text().replace("max_transitions: 10", "max_transitions: 2"), encoding="utf-8")
            controller = Controller(Path(directory) / "runtime", runner=runner)
            try:
                task_id = controller.submit("demo-loop", profile_path, DEMO_INPUT)
                result = controller.run_until_stop(task_id)
            finally:
                controller.close()
        self.assertEqual(result["task"]["status"], "waiting_user")
        self.assertEqual(result["task"]["stop_reason"], "transition_cap")
        self.assertEqual(runner.owners, ["claude", "codex"])

    def test_done_task_writes_notification_inbox_and_evidence_index(self):
        runner = SequenceRunner(["submit", "allow"])
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime"
            controller = Controller(home, runner=runner)
            try:
                task_id = controller.submit("demo-loop", DEMO_PROFILE, DEMO_INPUT)
                result = controller.run_until_stop(task_id)
            finally:
                controller.close()

            artifact_dir = Path(result["task"]["artifact_dir"])
            evidence_path = artifact_dir / "evidence.json"
            notifications_path = artifact_dir / "notifications.jsonl"
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            notifications_text = notifications_path.read_text(encoding="utf-8")
            inbox_text = (home / "inbox.md").read_text(encoding="utf-8")
            first_manifest_exists = Path(evidence["stages"][0]["manifest_path"]).is_file()

        self.assertEqual(result["task"]["status"], "done")
        self.assertEqual(result["notifications"][-1]["reason"], "done")
        self.assertIn('"reason": "done"', notifications_text)
        self.assertIn(f"task {task_id} done", inbox_text)
        self.assertEqual(evidence["task_id"], task_id)
        self.assertEqual(evidence["status"], "done")
        self.assertEqual(evidence["profile_hash"], result["task"]["profile_hash"])
        self.assertEqual(len(evidence["stages"]), 2)
        self.assertEqual(evidence["stages"][0]["stage"], "draft")
        self.assertEqual(evidence["stages"][0]["preflight_status"], "pass")
        self.assertTrue(evidence["stages"][0]["sealed"])
        self.assertTrue(first_manifest_exists)
        self.assertRegex(evidence["stages"][0]["manifest_hash"], r"^[0-9a-f]{64}$")
        self.assertEqual(evidence["stages"][1]["outcome"], "allow")
        self.assertEqual(evidence["notifications"][-1]["reason"], "done")

    def test_successful_stage_writes_sealed_manifest_and_fenced_lease(self):
        runner = SequenceRunner(["submit", "allow"])
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime"
            controller = Controller(home, runner=runner)
            try:
                task_id = controller.submit("demo-loop", DEMO_PROFILE, DEMO_INPUT)
                result = controller.run_until_stop(task_id)
                first_run = result["stage_runs"][0]
                manifest_path = Path(first_run["manifest_path"])
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            finally:
                controller.close()
        self.assertEqual(result["task"]["status"], "done")
        self.assertIsNone(result["task"]["lease_token"])
        self.assertTrue(first_run["sealed"])
        self.assertIsNotNone(first_run["lease_token"])
        self.assertEqual(manifest["run_token"], first_run["run_token"])
        self.assertEqual(manifest["lease_token"], first_run["lease_token"])
        self.assertEqual(manifest["classification"], "success")
        self.assertEqual(manifest["outcome"], "submit")
        self.assertRegex(first_run["manifest_hash"], r"^[0-9a-f]{64}$")


class FailureIntegrationTests(unittest.TestCase):
    def test_blocked_task_can_resume_after_environment_is_fixed(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = Controller(
                Path(directory),
                runner=FixedRunner(127, "env: node: No such file or directory\n"),
            )
            try:
                task_id = controller.submit("demo-loop", DEMO_PROFILE, DEMO_INPUT)
                blocked = controller.run_until_stop(task_id)
                self.assertEqual(blocked["task"]["status"], "blocked")
                controller.runner = SequenceRunner(["submit", "allow"])
                resumed = controller.resume(task_id)
            finally:
                controller.close()
        self.assertEqual(resumed["task"]["status"], "done")
        self.assertEqual(
            [transition["reason"] for transition in resumed["transitions"]],
            ["submitted", "runner_nonzero", "manual_resume", "stage_completed", "stage_completed"],
        )

    def test_nonzero_exit_blocks_and_preserves_log(self):
        runner = FixedRunner(7, "provider process crashed\n")
        with tempfile.TemporaryDirectory() as directory:
            controller = Controller(Path(directory), runner=runner)
            try:
                task_id = controller.submit("demo-loop", DEMO_PROFILE, DEMO_INPUT)
                result = controller.run_until_stop(task_id)
                log_path = Path(result["stage_runs"][0]["log_path"])
                self.assertTrue(log_path.is_file())
                self.assertIn("provider process crashed", log_path.read_text(encoding="utf-8"))
            finally:
                controller.close()
        self.assertEqual(result["task"]["status"], "blocked")
        self.assertEqual(result["task"]["stop_reason"], "runner_nonzero")
        self.assertEqual(result["task"]["transitions_count"], 0)
        self.assertEqual(result["stage_runs"][0]["usage_unavailable_reason"], "provider_cli_usage_not_reported")

    def test_rate_limit_pauses_instead_of_blocking(self):
        runner = FixedRunner(1, "usage limit has been reached; reset later\n")
        with tempfile.TemporaryDirectory() as directory:
            controller = Controller(Path(directory), runner=runner)
            try:
                task_id = controller.submit("demo-loop", DEMO_PROFILE, DEMO_INPUT)
                result = controller.run_until_stop(task_id)
            finally:
                controller.close()
        self.assertEqual(result["task"]["status"], "paused")
        self.assertEqual(result["task"]["stop_reason"], "rate_limited")

    def test_provider_preflight_missing_cli_blocks_before_running_stage(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"ORCH_CLAUDE_COMMAND": str(Path(directory) / "missing-claude")},
        ):
            os.environ.pop("ANTHROPIC_BASE_URL", None)
            controller = Controller(Path(directory) / "runtime")
            try:
                task_id = controller.submit("demo-loop", DEMO_PROFILE, DEMO_INPUT)
                result = controller.run_until_stop(task_id)
                log_text = Path(result["stage_runs"][0]["log_path"]).read_text(encoding="utf-8")
            finally:
                controller.close()
        self.assertEqual(result["task"]["status"], "blocked")
        self.assertEqual(result["task"]["stop_reason"], "provider_cli_unavailable")
        self.assertEqual(result["task"]["transitions_count"], 0)
        run = result["stage_runs"][0]
        self.assertEqual(run["status"], "blocked")
        self.assertEqual(run["provider_preflight_status"], "blocked")
        self.assertEqual(run["provider_preflight_reason"], "provider_cli_unavailable")
        self.assertEqual(run["usage_unavailable_reason"], "not_applicable_provider_preflight_failed")
        self.assertIn("provider CLI not found", log_text)

    def test_provider_preflight_blocks_localhost_socket_misconfiguration(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "ANTHROPIC_BASE_URL": "http://127.0.0.1:9999",
                "ORCH_CLAUDE_COMMAND": sys.executable,
            },
        ):
            controller = Controller(Path(directory) / "runtime")
            try:
                task_id = controller.submit("demo-loop", DEMO_PROFILE, DEMO_INPUT)
                result = controller.run_until_stop(task_id)
            finally:
                controller.close()
        self.assertEqual(result["task"]["status"], "blocked")
        self.assertEqual(result["task"]["stop_reason"], "provider_socket_misconfigured")
        self.assertEqual(result["stage_runs"][0]["provider_preflight_reason"], "provider_socket_misconfigured")

    def test_provider_preflight_blocks_invalid_codex_config_before_running_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex_home = root / "codex-home"
            codex_home.mkdir()
            (codex_home / "config.toml").write_text('service_tier = "default"\n', encoding="utf-8")
            profile = root / "single.yaml"
            profile.write_text(
                "\n".join(
                    [
                        "version: 1",
                        "type: single",
                        "initial_stage: run",
                        "max_transitions: 1",
                        "stages:",
                        "  run:",
                        "    owner: codex",
                        "    attempt_cap: 1",
                        "    timeout: 30",
                        "    prompt: \"run once\"",
                        "    outcomes:",
                        "      ok: done",
                        "  done:",
                        "    terminal: done",
                        "edge_caps:",
                        "  run.ok: 1",
                    ]
                ),
                encoding="utf-8",
            )
            input_path = root / "input.md"
            input_path.write_text("input\n", encoding="utf-8")
            controller = Controller(root / "runtime")
            try:
                with patch.dict(
                    os.environ,
                    {
                        "CODEX_HOME": str(codex_home),
                        "ORCH_CODEX_COMMAND": sys.executable,
                    },
                    clear=False,
                ):
                    task_id = controller.submit("single", profile, input_path)
                    result = controller.run_until_stop(task_id)
            finally:
                controller.close()
        self.assertEqual(result["task"]["status"], "blocked")
        self.assertEqual(result["task"]["stop_reason"], "provider_config_invalid")
        self.assertEqual(result["stage_runs"][0]["provider_preflight_reason"], "provider_config_invalid")
        self.assertEqual(result["stage_runs"][0]["usage_unavailable_reason"], "not_applicable_provider_preflight_failed")

    def test_provider_preflight_accepts_current_codex_service_tier_value(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex_home = root / "codex-home"
            codex_home.mkdir()
            (codex_home / "config.toml").write_text('service_tier = "fast"\n', encoding="utf-8")
            script = root / "codex"
            script.write_text("#!/bin/sh\nprintf 'codex-cli 0.125.0\\n'\n", encoding="utf-8")
            script.chmod(0o755)

            with patch.dict(
                os.environ,
                {"CODEX_HOME": str(codex_home), "ORCH_CODEX_COMMAND": str(script)},
                clear=False,
            ):
                result = SubprocessRunner().preflight("codex")

        self.assertEqual((result.status, result.reason), ("pass", "provider_preflight_pass"))

    def test_provider_preflight_accepts_priority_service_tier(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex_home = root / "codex-home"
            codex_home.mkdir()
            (codex_home / "config.toml").write_text('service_tier = "priority"\n', encoding="utf-8")
            script = root / "codex"
            script.write_text("#!/bin/sh\nprintf 'codex-cli 0.125.0\\n'\n", encoding="utf-8")
            script.chmod(0o755)

            with patch.dict(
                os.environ,
                {"CODEX_HOME": str(codex_home), "ORCH_CODEX_COMMAND": str(script)},
                clear=False,
            ):
                result = SubprocessRunner().preflight("codex")

        self.assertEqual((result.status, result.reason), ("pass", "provider_preflight_pass"))

    def test_provider_preflight_service_tier_allowlist_is_env_configurable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex_home = root / "codex-home"
            codex_home.mkdir()
            (codex_home / "config.toml").write_text('service_tier = "priority"\n', encoding="utf-8")
            script = root / "codex"
            script.write_text("#!/bin/sh\nprintf 'codex-cli 0.125.0\\n'\n", encoding="utf-8")
            script.chmod(0o755)

            # Narrow the allowlist to exclude "priority" -> preflight must block.
            with patch.dict(
                os.environ,
                {
                    "CODEX_HOME": str(codex_home),
                    "ORCH_CODEX_COMMAND": str(script),
                    "ORCH_CODEX_SERVICE_TIERS": "fast",
                },
                clear=False,
            ):
                result = SubprocessRunner().preflight("codex")

        self.assertEqual((result.status, result.reason), ("blocked", "provider_config_invalid"))

    def test_usage_summary_is_recorded_when_runner_output_exposes_tokens(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = root / "single.yaml"
            profile.write_text(
                "\n".join(
                    [
                        "version: 1",
                        "type: single",
                        "initial_stage: run",
                        "max_transitions: 1",
                        "stages:",
                        "  run:",
                        "    owner: claude",
                        "    attempt_cap: 1",
                        "    timeout: 30",
                        "    prompt: \"run once\"",
                        "    outcomes:",
                        "      ok: done",
                        "  done:",
                        "    terminal: done",
                        "edge_caps:",
                        "  run.ok: 1",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            input_path = root / "input.md"
            input_path.write_text("input\n", encoding="utf-8")
            runner = FixedRunner(
                0,
                '{"usage":{"input_tokens":123,"output_tokens":45,"total_tokens":168}}\n'
                "ORCHESTRATOR_OUTCOME: ok\n",
            )
            controller = Controller(root / "runtime", runner=runner)
            try:
                task_id = controller.submit("single", profile, input_path)
                result = controller.run_until_stop(task_id)
            finally:
                controller.close()
        run = result["stage_runs"][0]
        self.assertEqual(result["task"]["status"], "done")
        self.assertEqual(run["usage_input_tokens"], 123)
        self.assertEqual(run["usage_output_tokens"], 45)
        self.assertEqual(run["usage_total_tokens"], 168)
        self.assertIsNone(run["usage_unavailable_reason"])
        self.assertGreaterEqual(run["duration_ms"], 0)

    def test_fresh_controller_blocks_unknown_running_state_and_adds_log(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            controller = Controller(home, runner=SequenceRunner([]))
            task_id = controller.submit("demo-loop", DEMO_PROFILE, DEMO_INPUT)
            claim = controller.claim_stage(task_id)
            self.assertIsNotNone(claim)
            log_path = claim[3]
            claimed_text = log_path.read_text(encoding="utf-8")
            self.assertIn("stage_status=claimed", claimed_text)
            self.assertIn("timeout_seconds=", claimed_text)
            running = controller.status(task_id)
            run = running["stage_runs"][0]
            self.assertIn("running_elapsed_ms", run)
            self.assertIn("timeout_seconds", run)
            controller.close()

            recovered = Controller(home, runner=SequenceRunner([]))
            try:
                result = recovered.status(task_id)
            finally:
                recovered.close()
            self.assertEqual(result["task"]["status"], "blocked")
            self.assertEqual(result["task"]["stop_reason"], "orphaned_running")
            self.assertIn("unknown interruption", log_path.read_text(encoding="utf-8"))

    def test_stale_lease_cannot_commit_run(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            controller = Controller(home, runner=SequenceRunner([]))
            try:
                task_id = controller.submit("demo-loop", DEMO_PROFILE, DEMO_INPUT)
                claim = controller.claim_stage(task_id)
                self.assertIsNotNone(claim)
                run_token, _stage, profile, log_path = claim
                log_path.write_text("ORCHESTRATOR_OUTCOME: submit\n", encoding="utf-8")
                controller.conn.execute("UPDATE tasks SET lease_token=? WHERE id=?", ("stale-fence", task_id))
                with self.assertRaisesRegex(Exception, "lease conflict"):
                    controller.commit_run(
                        task_id,
                        run_token,
                        RunResult(0, "ORCHESTRATOR_OUTCOME: submit\n", "submit", "success", "success"),
                        profile,
                    )
                result = controller.status(task_id)
            finally:
                controller.close()
        self.assertEqual(result["task"]["status"], "running")
        self.assertEqual(result["stage_runs"][0]["status"], "running")

    def test_startup_reconciliation_quarantines_missing_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            controller = Controller(home, runner=SequenceRunner([]))
            try:
                task_id = controller.submit("demo-loop", DEMO_PROFILE, DEMO_INPUT)
                artifact_dir = Path(controller.status(task_id)["task"]["artifact_dir"])
                for path in sorted(artifact_dir.rglob("*"), reverse=True):
                    if path.is_file():
                        path.unlink()
                    elif path.is_dir():
                        path.rmdir()
                artifact_dir.rmdir()
                summary = controller.reconcile_startup()
                rows = list(controller.conn.execute("SELECT reason,artifact_path FROM quarantine"))
            finally:
                controller.close()
        self.assertGreaterEqual(summary["artifact_quarantined"], 1)
        self.assertTrue(any(row["reason"] == "missing_artifact_dir" for row in rows))

    def test_daemon_startup_reconciliation_quarantines_corrupt_requests_and_keeps_processing(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            inbox = home / "inbox"
            processing = home / "processing"
            processed = home / "processed"
            inbox.mkdir(parents=True)
            processing.mkdir()
            processed.mkdir()
            corrupt = inbox / "bad.json"
            corrupt.write_text("{not json", encoding="utf-8")
            valid_request_id = str(uuid.uuid4())
            valid = processing / f"20260101-000000-{valid_request_id}.json"
            valid.write_text(json.dumps({"request_id": valid_request_id, "action": "resume", "task_id": "missing"}), encoding="utf-8")

            controller = Controller(home, runner=SequenceRunner([]))
            try:
                summary = _reconcile_startup_requests(controller, inbox, processing, processed)
                quarantined = list((home / "quarantine").glob("*bad.json"))
                rows = list(controller.conn.execute("SELECT reason,request_path FROM quarantine"))
                valid_still_exists = valid.is_file()
            finally:
                controller.close()
        self.assertEqual(summary["quarantined"], 1)
        self.assertEqual(summary["processing_replayable"], 1)
        self.assertTrue(valid_still_exists)
        self.assertTrue(quarantined)
        self.assertTrue(any(row["reason"].startswith("inbox_corrupt_request") for row in rows))


class BrokerIntegrationTests(unittest.TestCase):
    def _start_fake_daemon(self, home: Path, *, route_success: bool = False):
        env = os.environ.copy()
        env.update(
            {
                "ORCH_HOME": str(home),
                "ORCH_POLL_INTERVAL": "0.02",
                "ORCH_WAIT_TIMEOUT": "10",
                "ORCH_CLAUDE_COMMAND": (
                    f"{sys.executable} {ROOT / 'orchestrator/examples/fake_agent.py'} claude"
                ),
                "ORCH_CODEX_COMMAND": (
                    f"{sys.executable} {ROOT / 'orchestrator/examples/fake_agent.py'} codex"
                ),
                # L1 is a macOS mechanism, and a contained stage refuses to run
                # where it is unavailable. This test is about the broker loop,
                # not about containment: the fake agent writes nothing outside
                # the temporary workspace, and L1 has its own dedicated tests.
                # Set unconditionally so the behaviour is identical on every
                # host rather than depending on where the suite happens to run.
                "ORCH_ALLOW_UNSANDBOXED": "1",
            }
        )
        if route_success:
            env["ORCH_FAKE_AGENT_ROUTE_SUCCESS"] = "1"
        daemon = subprocess.Popen(
            [sys.executable, "-m", "orchestrator", "daemon"],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        deadline = time.monotonic() + 5
        while not (home / "daemon.pid").is_file():
            if daemon.poll() is not None:
                self.fail(f"daemon exited early: {daemon.stdout.read() if daemon.stdout else ''}")
            if time.monotonic() >= deadline:
                self.fail("daemon did not create its pidfile")
            time.sleep(0.02)
        return daemon, env

    def _stop_daemon(self, daemon: subprocess.Popen[str]) -> None:
        daemon.terminate()
        daemon.wait(timeout=5)
        if daemon.stdout:
            daemon.stdout.close()

    def _wait_request_result(self, home: Path, request_path: str | Path) -> dict:
        return wait_for_result(home, Path(request_path), timeout=10, poll_interval=0.02)

    def test_submit_routes_through_single_daemon_and_returns_result(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            daemon, env = self._start_fake_daemon(home)
            try:
                duplicate = subprocess.run(
                    [sys.executable, "-m", "orchestrator", "daemon"],
                    cwd=ROOT,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                self.assertEqual(duplicate.returncode, 2)
                self.assertIn("already holds", duplicate.stderr)

                submitted = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "orchestrator",
                        "submit",
                        "--type",
                        "demo-loop",
                        "--profile",
                        str(DEMO_PROFILE),
                        "--input",
                        str(DEMO_INPUT),
                    ],
                    cwd=ROOT,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )
                self.assertEqual(submitted.returncode, 0, submitted.stderr)
                result = json.loads(submitted.stdout)
                self.assertEqual(result["status"], "waiting_user")
                self.assertEqual(result["stop_reason"], "edge_cap")
                self.assertEqual(result["request_id"], result["task_id"])
                self.assertEqual(result["task"]["status"], "waiting_user")
                self.assertFalse(list((home / "inbox").glob("*.json")))
                self.assertFalse(list((home / "processing").glob("*.json")))
                self.assertEqual(len(list((home / "processed").glob("*.result.json"))), 1)

                status = subprocess.run(
                    [sys.executable, "-m", "orchestrator", "status", result["task_id"]],
                    cwd=ROOT,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                self.assertEqual(status.returncode, 0, status.stderr)
                self.assertEqual(json.loads(status.stdout)["task"]["status"], "waiting_user")

                refused = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "orchestrator",
                        "submit",
                        "--in-process",
                        "--type",
                        "demo-loop",
                        "--profile",
                        str(DEMO_PROFILE),
                        "--input",
                        str(DEMO_INPUT),
                    ],
                    cwd=ROOT,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                self.assertEqual(refused.returncode, 2)
                self.assertIn("service lock", refused.stderr)
            finally:
                self._stop_daemon(daemon)

            self.assertFalse((home / "daemon.pid").exists())

    def test_start_to_sync_gate_smoke_uses_fake_provider_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "runtime"
            daemon, _env = self._start_fake_daemon(home, route_success=True)
            try:
                spec = Path(directory) / "approved-spec.md"
                spec.write_text("Status: approved\n", encoding="utf-8")
                waiting = run_start(
                    home,
                    "apply changes to orchestrator/router/daemon/memory flow",
                    StartFlags("apply", "orchestrator/router/daemon/memory", Path(directory), spec, "high", False),
                )
                self.assertEqual(waiting["status"], "waiting_user")

                approved = run_start_go(home, waiting["task_id"])
                execution = approved["routing"]["execution"]
                self._wait_request_result(home, execution["request_path"])
                synced = run_start_sync(home, waiting["task_id"])

                self.assertEqual(synced["status"], "waiting_user")
                self.assertTrue(synced["routing"]["execution_result"]["gate_required"])
                processed = json.loads(Path(synced["routing"]["execution_result"]["processed_result_path"]).read_text())
                self.assertEqual(processed["status"], "done")
                self.assertTrue(Path(processed["evidence_path"]).is_file())
                self.assertEqual(processed["stage_runs"][0]["usage_total_tokens"], 18)
                self.assertEqual(processed["stage_runs"][0]["provider_preflight_status"], "pass")

                gate_review = run_gate_run(home, waiting["task_id"])
                review_execution = gate_review["routing"]["gate_review_execution"]
                self._wait_request_result(home, review_execution["request_path"])
                gate_synced = run_gate_sync(home, waiting["task_id"])

                self.assertEqual(gate_synced["routing"]["gate_review_result"]["recommendation"], "ALLOW")
                allowed = run_gate_decision(home, waiting["task_id"], "ALLOW", "fake smoke passed")
                self.assertEqual(allowed["status"], "done")
            finally:
                self._stop_daemon(daemon)

    def test_submit_without_daemon_fails_without_queuing(self):
        with tempfile.TemporaryDirectory() as directory:
            env = os.environ.copy()
            env["ORCH_HOME"] = directory
            submitted = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "orchestrator",
                    "submit",
                    "--type",
                    "demo-loop",
                    "--profile",
                    str(DEMO_PROFILE),
                    "--input",
                    str(DEMO_INPUT),
                ],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            self.assertEqual(submitted.returncode, 2)
            self.assertIn("daemon is not running", submitted.stderr)
            self.assertFalse(list((Path(directory) / "inbox").glob("*.json")))


class ConvergenceHoldTests(unittest.TestCase):
    """The propose convergence routing and the engine-reserved hold outcome.

    Every runner here is a fake, so no provider is contacted and nothing in this
    class can flake on model wording. See
    docs/decisions/propose-convergence-policy.md.
    """

    PROPOSE_PROFILE = ROOT / "orchestrator" / "profiles" / "propose.yaml"
    SPEC_REVIEW_PROFILE = ROOT / "orchestrator" / "profiles" / "spec_review.yaml"

    def _submit(self, outcomes, profile=None, task_type="propose"):
        """Submit a task with a fake runner and return (controller, task_id).

        The controller stays open so a test can resume and inspect further; cleanup
        is registered here. Note that Controller.resume ends by driving
        run_until_stop, so a test that resumes must supply outcomes for the stages
        the resumed run will execute.
        """
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        controller = Controller(Path(directory.name) / "runtime", runner=SequenceRunner(outcomes))
        self.addCleanup(controller.close)
        task_id = controller.submit(task_type, profile or self.PROPOSE_PROFILE, DEMO_INPUT)
        return controller, task_id

    def _run(self, outcomes, profile=None, task_type="propose"):
        controller, task_id = self._submit(outcomes, profile, task_type)
        return controller, task_id, controller.run_until_stop(task_id)

    @staticmethod
    def _edge(status, edge):
        return next(row for row in status["edge_counts"] if row["edge"] == edge)

    @staticmethod
    def _stage_sequence(status):
        return [row["stage"] for row in status["transitions"] if row["outcome"]]

    def test_hold_outcome_stops_at_waiting_user_with_stage_already_advanced(self):
        controller, task_id, status = self._run(["drafted", "needs_user_decision"])
        task = status["task"]

        self.assertEqual(task["status"], "waiting_user")
        self.assertEqual(task["stop_reason"], "user_decision_required")
        # The CAS in the success branch is untouched by the hold, so the task is
        # already pointed at the stage a resume will run.
        self.assertEqual(task["current_stage"], "draft")
        self.assertEqual(task["owner"], "claude")
        self.assertEqual(task["transitions_count"], 2)
        # The hold did not hit the cap, so the edge counter still advanced.
        self.assertEqual(self._edge(status, "review.needs_user_decision")["count"], 1)
        self.assertEqual(status["notifications"][-1]["reason"], "user_decision_required")
        self.assertIn("user decision required", status["notifications"][-1]["message"])

    def test_hold_resumes_straight_into_draft_without_rerun_stage(self):
        controller, task_id = self._submit(
            ["drafted", "needs_user_decision", "drafted", "ready"]
        )
        controller.run_until_stop(task_id)
        held = controller.status(task_id)
        self.assertEqual(held["task"]["stop_reason"], "user_decision_required")

        # user_decision_required is not a containment stop, so plain resume works.
        resumed = controller.resume(task_id)

        self.assertEqual(resumed["task"]["status"], "done")
        self.assertEqual(resumed["task"]["resume_allowance"], 0)
        self.assertEqual(
            self._stage_sequence(resumed), ["draft", "review", "draft", "review"]
        )
        # The hold does not compensate the edge counter the way edge_cap does.
        self.assertEqual(self._edge(resumed, "review.needs_user_decision")["count"], 1)
        # The immutable input never moves: before the hold, during it, after resume.
        submitted_hash = held["task"]["input_hash"]
        self.assertEqual(resumed["task"]["input_hash"], submitted_hash)
        self.assertEqual(
            controller.status(task_id)["task"]["input_hash"], submitted_hash
        )

    def test_edge_cap_takes_precedence_over_the_hold_on_the_second_crossing(self):
        controller, task_id = self._submit(
            ["drafted", "needs_user_decision", "drafted", "needs_user_decision"]
        )
        first = controller.run_until_stop(task_id)
        self.assertEqual(first["task"]["stop_reason"], "user_decision_required")

        second = controller.resume(task_id)

        # The cap is 1, so the second crossing is capped; the elif ordering means
        # the cap reason wins over the hold reason.
        self.assertEqual(second["task"]["status"], "waiting_user")
        self.assertEqual(second["task"]["stop_reason"], "edge_cap")
        self.assertEqual(second["notifications"][-1]["reason"], "edge_cap")
        self.assertEqual(self._edge(second, "review.needs_user_decision")["count"], 1)

    def test_correction_routes_to_draft_until_its_cap_is_reached(self):
        controller, task_id, status = self._run(
            [
                "drafted",
                "needs_correction",
                "drafted",
                "needs_correction",
                "drafted",
                "needs_correction",
            ]
        )
        self.assertEqual(status["task"]["status"], "waiting_user")
        self.assertEqual(status["task"]["stop_reason"], "edge_cap")
        self.assertEqual(status["task"]["current_stage"], "draft")
        self.assertEqual(self._edge(status, "review.needs_correction")["count"], 2)
        routed = [
            row["to_status"]
            for row in status["transitions"]
            if row["outcome"] == "needs_correction"
        ]
        self.assertEqual(routed, ["queued", "queued", "waiting_user"])

    def test_simplification_routes_to_the_simplify_stage_and_back_to_review(self):
        controller, task_id, status = self._run(
            ["drafted", "needs_simplification", "simplified", "ready"]
        )
        self.assertEqual(status["task"]["status"], "done")
        self.assertEqual(
            self._stage_sequence(status), ["draft", "review", "simplify", "review"]
        )
        routing = next(
            row for row in status["transitions"] if row["outcome"] == "needs_simplification"
        )
        self.assertEqual(routing["edge"], "review.needs_simplification")
        self.assertEqual(routing["to_status"], "queued")
        simplify_run = next(
            row for row in status["stage_runs"] if row["stage"] == "simplify"
        )
        self.assertEqual(simplify_run["owner"], "claude")

    def test_a_profile_that_does_not_declare_the_hold_outcome_is_unaffected(self):
        # spec-review has no needs_user_decision outcome; the reserved name must
        # not leak into its behaviour.
        controller, task_id, status = self._run(
            ["reviewed", "ready"],
            profile=self.SPEC_REVIEW_PROFILE,
            task_type="spec-review",
        )
        self.assertEqual(status["task"]["status"], "done")
        self.assertIsNone(status["task"]["stop_reason"])
        self.assertNotIn(
            "user_decision_required", [row["reason"] for row in status["transitions"]]
        )
        self.assertNotIn(
            "user_decision_required", [row["reason"] for row in status["notifications"]]
        )


if __name__ == "__main__":
    unittest.main()
