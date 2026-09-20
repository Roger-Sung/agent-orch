"""The whole pack-v1 pipeline on the fixture target, with stub providers.

Every stage had unit coverage and the pipeline still could not run: the pieces
were each correct and nothing joined them. Four separate defects only appeared
when a pack was driven end to end, and each of them presented the same way -
one stage dispatched over and over until the call budget stopped it - so this
asserts the *sequence*, not merely that the run terminated.

No provider is contacted: the runner is a stub. What is real is the intake, the
contract, the target's own verify CLI, the state machine and the budget.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from orchestrator.controller import Controller, ProviderPreflightResult
from orchestrator.pack.envelopes import REVIEW_BEGIN, REVIEW_END
from orchestrator.pack.launch import launch_packs
from orchestrator.runner import RunResult
from orchestrator.tests.pack_stub import REVIEW_HEADER, full_review_envelope

TARGET = Path(__file__).resolve().parents[2] / "targets" / "_fixture_min"
PROFILE = Path(__file__).resolve().parents[1] / "profiles" / "pack_v1.yaml"


def _framed(envelope: dict, outcome: str) -> str:
    return (f"{REVIEW_BEGIN}\n{json.dumps(envelope)}\n{REVIEW_END}\n"
            f"ORCHESTRATOR_OUTCOME: {outcome}\n")


@unittest.skipUnless((TARGET / "profile.yaml").is_file(), "the fixture target is absent")
class PackPipelineTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home = self.root / "home"
        previous = os.environ.get("ORCH_HOME")
        os.environ["ORCH_HOME"] = str(self.home)
        self.addCleanup(lambda: os.environ.__setitem__("ORCH_HOME", previous)
                        if previous is not None else os.environ.pop("ORCH_HOME", None))

        self.workspace = self.root / "ws"
        (self.workspace / "src").mkdir(parents=True)
        (self.workspace / "src" / "A.java").write_text("class A {}\n")
        for argv in (["git", "init", "-q"], ["git", "config", "user.email", "t@example.com"],
                     ["git", "config", "user.name", "t"], ["git", "add", "-A"],
                     ["git", "commit", "-qm", "base"]):
            subprocess.run(argv, cwd=self.workspace, check=True, capture_output=True)

        self.calls: list[tuple[str, str]] = []
        outer = self

        class Stub:
            session_binding = None

            def __init__(self, stage: str) -> None:
                self.stage = stage

            def run(self, owner, prompt, timeout, log_path, **kwargs):
                outer.calls.append((self.stage, owner))
                if len(outer.calls) > 20:
                    raise AssertionError(f"runaway dispatch: {outer.calls}")
                if self.stage == "contract_review":
                    envelope = dict(REVIEW_HEADER)
                    envelope.update({
                        "review_round": None, "candidate_fingerprint": None,
                        "verdict": "accepted", "blocked_reason": None, "obligations": {},
                        "findings": [], "contract_findings": [], "improvements": [],
                        "prior_round": None, "remaining": []})
                    text = _framed(envelope, "contract_pass")
                elif self.stage == "review":
                    # The same blocking finding every round: no progress, which
                    # is what the judge is supposed to call stalled.
                    text = _framed(full_review_envelope(1), "review")
                else:
                    text = "ORCHESTRATOR_OUTCOME: produced\n"
                log_path.parent.mkdir(parents=True, exist_ok=True)
                log_path.write_text(text, encoding="utf-8")
                return RunResult(0, text, None, "raw", "raw")

        self.controller = Controller(self.home, runner=None)
        self.addCleanup(self.controller.close)
        launched = launch_packs(
            self.controller, target_dir=TARGET, change_dir=TARGET / "change",
            workspace=self.workspace, profile_path=PROFILE, base_revision="abc",
            producer_model="claude-opus-5", reviewer_model="gpt-6-astra")
        self.controller.conn.commit()
        self.pack_id = launched[0]["pack"]
        self.controller._execution_runner_for = lambda task, stage: Stub(stage.name)
        self.controller._provider_preflight = lambda owner, runner=None: (
            ProviderPreflightResult("pass", "ok", None, None, [], None, 0, 0))

    def test_the_pack_walks_the_whole_pipeline_and_stops_for_a_reason(self) -> None:
        self.controller.run_until_stop(self.pack_id)

        self.assertEqual(
            self.calls,
            [("contract_review", "codex"), ("apply", "claude"), ("review", "codex"),
             ("repair", "claude"), ("review", "codex")],
            "the pipeline did not walk the stages in order",
        )
        pack = self.controller.pack_store.get_pack(self.pack_id)
        # The step-7 shape: a hold that was enumerated, never `accepted`.
        self.assertEqual(pack["state"], "hold(stalled)")
        self.assertEqual(pack["k_last"], 2, "each producer round freezes a candidate")
        task = self.controller.conn.execute(
            "SELECT status, stop_reason FROM tasks WHERE id=?", (self.pack_id,)).fetchone()
        self.assertEqual((task["status"], task["stop_reason"]), ("waiting_user", "stalled"))

    def test_prerun_runs_between_every_producer_and_review(self) -> None:
        self.controller.run_until_stop(self.pack_id)

        stages = [(op["stage"], op["type"]) for op
                  in self.controller.pack_store.operations(self.pack_id)]
        self.assertEqual(
            stages,
            [("contract_review", "provider"), ("apply", "provider"), ("prerun", "verify"),
             ("review", "provider"), ("repair", "provider"), ("prerun", "verify"),
             ("review", "provider")],
        )
        # Verification is not a provider call and must not be charged as one.
        self.assertEqual(self.controller.pack_store.get_pack(self.pack_id)["calls_reserved"],
                         len(self.calls))

    def test_a_repair_round_is_dispatched_as_repair_not_apply(self) -> None:
        """The prompts differ: apply implements the obligations, repair only fixes."""
        self.controller.run_until_stop(self.pack_id)
        producer_stages = [stage for stage, _owner in self.calls
                           if stage in {"apply", "repair"}]
        self.assertEqual(producer_stages, ["apply", "repair"])

    def test_a_producer_that_always_fails_stops_at_its_attempt_cap(self) -> None:
        """The live failure: an argv error the producer would hit every time.

        Without the settled counter this spent the whole call budget - 24 real
        dispatches of a call that could not succeed.
        """
        from orchestrator.pack import budgets

        outer = self

        class AlwaysFails:
            session_binding = None

            def __init__(self, stage: str) -> None:
                self.stage = stage

            def run(self, owner, prompt, timeout, log_path, **kwargs):
                outer.calls.append((self.stage, owner))
                if len(outer.calls) > 20:
                    raise AssertionError(f"runaway dispatch: {outer.calls}")
                log_path.parent.mkdir(parents=True, exist_ok=True)
                if self.stage == "contract_review":
                    envelope = dict(REVIEW_HEADER)
                    envelope.update({
                        "review_round": None, "candidate_fingerprint": None,
                        "verdict": "accepted", "blocked_reason": None, "obligations": {},
                        "findings": [], "contract_findings": [], "improvements": [],
                        "prior_round": None, "remaining": []})
                    text = _framed(envelope, "contract_pass")
                    log_path.write_text(text, encoding="utf-8")
                    return RunResult(0, text, None, "raw", "raw")
                text = "error: unknown option '--sandbox'\n"
                log_path.write_text(text, encoding="utf-8")
                return RunResult(1, text, None, "blocked", "runner_nonzero")

        self.controller._execution_runner_for = lambda task, stage: AlwaysFails(stage.name)

        self.controller.run_until_stop(self.pack_id)

        producer_calls = [c for c in self.calls if c[0] in {"apply", "repair"}]
        self.assertEqual(len(producer_calls), budgets.DEFAULTS["attempt_cap"],
                         f"the producer was dispatched {len(producer_calls)} times")
        pack = self.controller.pack_store.get_pack(self.pack_id)
        self.assertEqual(pack["state"], "hold(producer_failed)")
        self.assertLess(pack["calls_reserved"], budgets.DEFAULTS["call_budget"],
                        "a capped producer must not have spent the call budget")
