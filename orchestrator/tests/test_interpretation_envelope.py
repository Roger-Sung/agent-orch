"""Acceptance evidence A-1 .. A-24 for the universal Interpretation Envelope.

One test per acceptance case, named for it, observing the case's own
`Observable evidence` column: the rendered execution input block, the composed
prompt, the task row and its edge counts, the sealed run outputs, and the
`orch start` / `gate-status` output.

Run: python3 -m unittest orchestrator.tests.test_interpretation_envelope
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import orchestrator.start
from orchestrator.controller import Controller
from orchestrator.retained import inspect_retained
from orchestrator.runner import (
    CONVERGENCE_BEGIN,
    CONVERGENCE_END,
    ENVELOPE_AXES,
    ENVELOPE_BEGIN,
    ENVELOPE_END,
    ENVELOPE_ENUM_AXIS,
    ENVELOPE_SCHEMA_VERSION,
    ENVELOPE_SET_AXES,
    ENVELOPE_SOURCE_ENGINE,
    ENVELOPE_SOURCE_REQUIREMENT,
    HOLD_OUTCOME,
    HOLD_STOP_REASON,
    RunResult,
    allowed_outcomes,
    classify_result,
    convergence_verdict,
    envelope_block_text,
    extract_envelope,
)
from orchestrator.profile import load_profile
from orchestrator.start import (
    ENVELOPE_SCHEMA_VERSION as START_ENVELOPE_SCHEMA_VERSION,
    RESOLVER_BEGIN,
    RESOLVER_END,
    RESOLVER_ENV_ALLOW_NAMES,
    RESOLVER_ISOLATION_FLAGS,
    RESOLVER_VARIADIC_FLAGS,
    EnvelopeResolverError,
    StartFlags,
    _resolver_command,
    _resolver_environment,
    _read_yaml,
    _result,
    gate_status,
    run_gate_run,
    run_gate_sync,
    run_start,
    run_start_go,
    run_start_sync,
)
from orchestrator.tests.envelope_resolver_stub import scripted_reply


ROOT = Path(__file__).resolve().parents[2]
PROFILES = ROOT / "orchestrator" / "profiles"
APPLY_PROFILE = PROFILES / "claude_apply_codex_review.yaml"
ARTIFACT_PROFILE = PROFILES / "artifact_validation.yaml"
STOP_GATE_CODEX_PROFILE = PROFILES / "stop_gate_codex.yaml"
DEMO_PROFILE = ROOT / "orchestrator" / "examples" / "demo-loop.yaml"

#: Natural language, as an operator writes it. No heading, bullet, keyword,
#: marker, section or ordering is asked of the user, and none is parsed: intake
#: asks the routed executor one bounded question about this text, and the
#: engine judges the answer.
DECLARED_SCOPE = (
    "Change the greeting text emitted by the demo stage, and write only work/greeting.txt.\n"
    "Prove it with the existing unit test for the greeting.\n"
    "There is no adversary here beyond the trusted local operator, and add no instrumentation\n"
    "beyond the existing stage log. If the work exceeds this, stop and ask me.\n"
)

#: The same task in Chinese, silent on every defaulting axis but explicit
#: about the one path it may write.
SILENT_SCOPE = "修改 demo stage 的問候文字，只寫 work/greeting.txt 這個檔案。\n"

#: The same task again, laid out differently: a blank-line-separated list with
#: no punctuation. Layout is not a contract, so this must resolve too.
SILENT_SCOPE_ALT_LAYOUT = (
    "change the greeting text emitted by the demo stage\n"
    "\n"
    "    work/greeting.txt\n"
)

#: Names a path only to say it is off limits. A read-only mention is not
#: authorisation, so the write axis has nothing determinate to resolve to.
NEGATED_SCOPE = (
    "Change stop-gate behaviour so a held gate stays pending.\n"
    "Keep orchestrator/controller.py untouched; it is read-only background for this task.\n"
)

#: Names a behaviour only to forbid changing it. Reading the words without
#: their polarity turns a prohibition into a permission, which is the exact
#: failure the resolver is the single reader in order to prevent.
NEGATED_BEHAVIOUR_SCOPE = (
    "Do not rewrite the daemon lease protocol.\n"
    "Change the greeting text emitted by the demo stage, and write only work/greeting.txt.\n"
)


# --- the resolver seam -------------------------------------------------------
#
# Every intake test below replaces exactly one thing: `_invoke_resolver`, the
# subprocess call. The scopes stay in the operator's own natural language, and
# every engine-side validation of the untrusted reply still runs against them.


def _axis(state: str, value, evidence=(), detail: str = "") -> dict:
    return {"state": state, "value": value, "evidence": list(evidence), "detail": detail}


def _reply(*, candidates=None, **axes) -> str:
    """One well-formed proposal; unnamed axes are plainly silent."""
    payload = {
        "schema_version": START_ENVELOPE_SCHEMA_VERSION,
        "semantic_change_surface": _axis("semantically_silent", []),
        "task_owned_write_targets": _axis("declared", [], []),
        "assurance_ceiling": _axis("semantically_silent", []),
        "threat_model": _axis("semantically_silent", []),
        "evidence_ceiling": _axis("semantically_silent", []),
        "scope_expansion_policy": _axis("semantically_silent", "user_decision"),
    }
    payload.update(axes)
    if candidates is not None:
        payload["candidates"] = candidates
    return f"{RESOLVER_BEGIN}\n{json.dumps(payload, ensure_ascii=False)}\n{RESOLVER_END}\n"


def _raises(error: Exception):
    def boundary(_prompt: str) -> str:
        raise error

    return boundary


#: A prompt shaped like the engine's, used only to build a canned reply.
_ANY_PROMPT = "### SOURCE: task text\nchange the greeting text\n\nReport six axes:"


def _start_result(home: Path, task_id: str) -> dict:
    """The task's own record after a refused start-go, read back from disk."""
    task_path = home / "tasks" / f"{task_id}.yaml"
    routing_path = home / "tasks" / f"{task_id}-routing.yaml"
    return _result(task_id, task_path, routing_path, _read_yaml(task_path), _read_yaml(routing_path))


def _first_run_record(live: list[str]) -> str:
    return "\n".join(
        [CONVERGENCE_BEGIN, json.dumps({"live": live, "resolved": []}), CONVERGENCE_END]
    )


def _repeat_record(
    live: list[str], prior_live: list[str], historical_resolved: list[str], verdict: str | None = None
) -> str:
    current, prior = set(live), set(prior_live)
    payload = {
        "live": live,
        "resolved": sorted(prior - current),
        "new": sorted(current - prior),
        "repeated": sorted(prior & current),
        "verdict": verdict or convergence_verdict(prior, set(historical_resolved), current),
    }
    return "\n".join([CONVERGENCE_BEGIN, json.dumps(payload), CONVERGENCE_END])


def _output(outcome: str, *parts: str) -> str:
    body = "\n".join(part for part in parts if part)
    return f"{body}\n\nORCHESTRATOR_OUTCOME: {outcome}\n" if body else f"ORCHESTRATOR_OUTCOME: {outcome}\n"


class ScriptedRunner:
    """A runner that replays whole provider outputs and keeps every prompt."""

    def __init__(self, outputs: list[str]):
        self.outputs = list(outputs)
        self.prompts: list[tuple[str, str]] = []

    def run(self, owner: str, prompt: str, timeout: int, log_path: Path, **kwargs) -> RunResult:
        self.prompts.append((owner, prompt))
        output = self.outputs.pop(0)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(output, encoding="utf-8")
        return RunResult(0, output, None, "raw", "raw")

    def preflight(self, owner: str, timeout: int = 5):
        from orchestrator.runner import ProviderPreflightResult

        return ProviderPreflightResult("pass", "scripted", "", 0, [], None, 0, 0)

    def prompt_for(self, stage_owner: str, index: int) -> str:
        return [prompt for owner, prompt in self.prompts if owner == stage_owner][index]


class EnvelopeFixture(unittest.TestCase):
    """Shared construction of envelope and legacy tasks."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def envelope_payload(
        self,
        *,
        write_targets: list[str] | None = None,
        surface: list[str] | None = None,
    ) -> dict:
        targets = write_targets or ["work/greeting.txt"]
        behaviours = surface or ["the greeting text"]
        payload = {"schema_version": ENVELOPE_SCHEMA_VERSION}
        for axis in ENVELOPE_SET_AXES:
            if axis == "task_owned_write_targets":
                payload[axis] = {
                    "state": "declared",
                    "value": targets,
                    "default_applied": False,
                    "source": {target: ENVELOPE_SOURCE_REQUIREMENT for target in targets},
                }
            elif axis == "semantic_change_surface":
                payload[axis] = {
                    "state": "declared",
                    "value": behaviours,
                    "default_applied": False,
                    "source": {item: ENVELOPE_SOURCE_REQUIREMENT for item in behaviours},
                }
            else:
                payload[axis] = {
                    "state": "semantically_silent",
                    "value": [],
                    "default_applied": True,
                    "source": {},
                }
        payload[ENVELOPE_ENUM_AXIS] = {
            "state": "semantically_silent",
            "value": "user_decision",
            "default_applied": True,
            "source": "safe_default",
        }
        return payload

    def envelope_input(self, name: str = "input.md", **kwargs) -> Path:
        payload = self.envelope_payload(**kwargs)
        path = self.root / name
        path.write_text(
            "# task\n\nchange the greeting text.\n\n"
            + ENVELOPE_BEGIN
            + "\n"
            + json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n"
            + ENVELOPE_END
            + "\n",
            encoding="utf-8",
        )
        return path

    def legacy_input(self, name: str = "legacy.md") -> Path:
        path = self.root / name
        path.write_text("# task\n\nchange the greeting text.\n", encoding="utf-8")
        return path

    def controller(self, outputs: list[str]) -> tuple[Controller, ScriptedRunner]:
        runner = ScriptedRunner(outputs)
        controller = Controller(self.root / f"runtime-{len(outputs)}-{id(outputs)}", runner=runner)
        self.addCleanup(controller.close)
        return controller, runner

    def run_task(
        self,
        outputs: list[str],
        *,
        profile: Path = APPLY_PROFILE,
        task_type: str = "apply",
        envelope: bool = True,
        input_path: Path | None = None,
    ):
        controller, runner = self.controller(outputs)
        source = input_path or (self.envelope_input() if envelope else self.legacy_input())
        task_id = controller.submit(task_type, profile, source)
        status = controller.run_until_stop(task_id)
        return controller, runner, task_id, status

    @staticmethod
    def edge(status: dict, edge: str) -> dict:
        return next(row for row in status["edge_counts"] if row["edge"] == edge)


# ---------------------------------------------------------------------------
# Slice 1 — intake resolution and envelope emission
# ---------------------------------------------------------------------------


class IntakeResolutionTests(EnvelopeFixture):
    def start(
        self,
        home: Path,
        description: str,
        scope: str,
        *,
        reply=None,
        go: bool = True,
        **kwargs,
    ):
        """One `orch start`, with the provider process boundary replaced.

        Only `_invoke_resolver` is stubbed. The scope stays exactly as an
        operator would type it, and every engine-side check of the reply —
        framing, axes, shapes, state consistency, bounds and grounding — still
        runs against these real sources.
        """
        spec = self.root / "approved-spec.md"
        spec.write_text("Status: approved\n", encoding="utf-8")
        worktree = self.root / "worktree"
        worktree.mkdir(exist_ok=True)
        (worktree / "orchestrator").mkdir(exist_ok=True)
        flags = StartFlags(
            kwargs.get("task_type", "apply"),
            scope,
            worktree,
            spec,
            None,
            kwargs.get("dry_run", False),
        )
        answer = reply if reply is not None else scripted_reply
        calls: list[str] = []

        def boundary(prompt: str) -> str:
            calls.append(prompt)
            return answer(prompt) if callable(answer) else answer

        self.calls = calls
        with patch("orchestrator.start._invoke_resolver", side_effect=boundary):
            result = run_start(home, description, flags)
            if go and result["status"] == "waiting_user" and result["routing"].get("pattern"):
                try:
                    result = run_start_go(home, result["task_id"])
                except ValueError:
                    result = _start_result(home, result["task_id"])
        return result

    def rendered_envelope(self, home: Path, task_id: str) -> dict:
        text = (home / "tasks" / f"{task_id}-execution-input.md").read_text(encoding="utf-8")
        return extract_envelope(text)

    def test_a1_declared_axis_records_no_default(self):
        home = self.root / "a1"
        result = self.start(
            home,
            "apply the greeting change",
            DECLARED_SCOPE,
            reply=_reply(
                semantic_change_surface=_axis(
                    "declared",
                    ["the greeting text emitted by the demo stage"],
                    ["Change the greeting text emitted by the demo stage"],
                ),
                task_owned_write_targets=_axis(
                    "declared", ["work/greeting.txt"], ["write only work/greeting.txt"]
                ),
                assurance_ceiling=_axis(
                    "declared",
                    ["the existing unit test for the greeting"],
                    ["the existing unit test for the greeting"],
                ),
                threat_model=_axis(
                    "declared",
                    ["the trusted local operator"],
                    ["no adversary here beyond the trusted local operator"],
                ),
                evidence_ceiling=_axis(
                    "declared", ["the existing stage log"], ["beyond the existing stage log"]
                ),
                scope_expansion_policy=_axis(
                    "declared", "user_decision", ["If the work exceeds this, stop and ask me"]
                ),
            ),
        )
        self.assertEqual(result["routing"]["preflight"]["status"], "pass")

        envelope = self.rendered_envelope(home, result["task_id"])

        for axis in ENVELOPE_AXES:
            with self.subTest(axis=axis):
                self.assertEqual(envelope[axis]["state"], "declared")
                self.assertFalse(envelope[axis]["default_applied"])
        self.assertTrue(
            any(
                path.endswith("work/greeting.txt")
                for path in envelope["task_owned_write_targets"]["value"]
            ),
            envelope["task_owned_write_targets"]["value"],
        )
        self.assertEqual(
            envelope["semantic_change_surface"]["value"],
            ["the greeting text emitted by the demo stage"],
        )
        # E-17: the task's own engine-owned outputs are inside the axis, marked
        # as engine-owned, and they leave `default_applied` false.
        targets = envelope["task_owned_write_targets"]
        engine_owned = [
            path for path, source in targets["source"].items() if source == ENVELOPE_SOURCE_ENGINE
        ]
        self.assertTrue(any(path.endswith("/reports") for path in engine_owned), engine_owned)
        routing = _read_yaml(home / "tasks" / f"{result['task_id']}-routing.yaml")
        if routing.get("stop_gate") is True:
            self.assertTrue(
                any(path.endswith("-gate-review-output.md") for path in engine_owned),
                engine_owned,
            )
        for path in engine_owned:
            self.assertIn(path, targets["value"])
        self.assertFalse(targets["default_applied"])

    def test_a2_silent_axis_records_the_safe_default(self):
        # Chinese prose, no axis vocabulary of any kind.
        home = self.root / "a2"
        result = self.start(home, "apply the greeting change", SILENT_SCOPE)

        envelope = self.rendered_envelope(home, result["task_id"])

        for axis in ("assurance_ceiling", "threat_model", "evidence_ceiling"):
            with self.subTest(axis=axis):
                self.assertEqual(envelope[axis]["state"], "semantically_silent")
                self.assertTrue(envelope[axis]["default_applied"])
                self.assertEqual(envelope[axis]["value"], [])
        policy = envelope[ENVELOPE_ENUM_AXIS]
        self.assertEqual(policy["state"], "semantically_silent")
        self.assertTrue(policy["default_applied"])
        self.assertEqual(policy["value"], "user_decision")
        self.assertTrue(
            any(
                path.endswith("work/greeting.txt")
                for path in envelope["task_owned_write_targets"]["value"]
            ),
            envelope["task_owned_write_targets"]["value"],
        )

    def test_a2_layout_variation_resolves_the_same_task(self):
        # Same requirement, different layout. Nothing in intake reads layout.
        home = self.root / "a2-layout"
        result = self.start(home, "apply the greeting change", SILENT_SCOPE_ALT_LAYOUT)

        envelope = self.rendered_envelope(home, result["task_id"])

        self.assertTrue(
            any(
                path.endswith("work/greeting.txt")
                for path in envelope["task_owned_write_targets"]["value"]
            ),
            envelope["task_owned_write_targets"]["value"],
        )

    def test_a2_a_silent_semantic_surface_defaults_to_the_behaviours_the_scope_names(self):
        # The adversarial shape for section 1.3: the scope plainly names a
        # behaviour in prose and names its path, but says nothing about a
        # "change surface". Defaulting the axis to `[]` would freeze an
        # envelope that excludes the task's own intended change.
        home = self.root / "a2-prose"
        scope = "Change stop-gate behaviour so a held gate stays pending, in work/greeting.txt.\n"
        result = self.start(home, "apply the approved stop-gate change", scope)
        self.assertEqual(result["routing"]["preflight"]["status"], "pass")

        surface = self.rendered_envelope(home, result["task_id"])["semantic_change_surface"]

        self.assertEqual(surface["state"], "semantically_silent")
        self.assertTrue(surface["default_applied"])
        self.assertNotEqual(surface["value"], [])
        self.assertEqual(
            surface["value"],
            ["Change stop-gate behaviour so a held gate stays pending, in work/greeting.txt"],
        )
        self.assertEqual(set(surface["source"].values()), {"safe_default"})

    def test_a2_a_silent_semantic_surface_may_not_be_empty_under_a_non_empty_scope(self):
        home = self.root / "a2-empty"
        result = self.start(
            home,
            "apply the greeting change",
            SILENT_SCOPE,
            reply=_reply(
                semantic_change_surface=_axis("semantically_silent", []),
                task_owned_write_targets=_axis(
                    "declared", ["work/greeting.txt"], ["只寫 work/greeting.txt 這個檔案"]
                ),
            ),
        )

        self.assertEqual(result["status"], "waiting_user")
        self.assertIn("semantic_change_surface", result["routing"]["preflight"]["reason"])
        self.assertFalse(list((home / "inbox").glob("*.json")))

    def test_a2_a_silent_semantic_surface_may_not_expand_past_the_scope(self):
        home = self.root / "a2-expanded"
        result = self.start(
            home,
            "apply the greeting change",
            SILENT_SCOPE,
            reply=_reply(
                semantic_change_surface=_axis(
                    "semantically_silent", ["and also rewrite the daemon lease"]
                ),
                task_owned_write_targets=_axis(
                    "declared", ["work/greeting.txt"], ["只寫 work/greeting.txt 這個檔案"]
                ),
            ),
        )

        self.assertEqual(result["status"], "waiting_user")
        self.assertIn("expands beyond the task's own scope", result["routing"]["preflight"]["reason"])
        self.assertFalse(list((home / "inbox").glob("*.json")))

    def test_a3_unrecognisable_or_conflicting_axis_stops_before_any_provider(self):
        conflicting = (
            DECLARED_SCOPE
            + "On reflection, assume a hostile local process, and also assume nothing but the "
            "trusted local operator.\n"
        )
        cases = {
            "conflict": (
                conflicting,
                _reply(
                    semantic_change_surface=_axis(
                        "declared",
                        ["the greeting text emitted by the demo stage"],
                        ["Change the greeting text emitted by the demo stage"],
                    ),
                    task_owned_write_targets=_axis(
                        "declared", ["work/greeting.txt"], ["write only work/greeting.txt"]
                    ),
                    threat_model=_axis(
                        "unresolved",
                        [],
                        detail="the sources ask for both a hostile local process and nothing "
                        "beyond the trusted local operator",
                    ),
                ),
                "threat_model",
            ),
            "policy-not-honoured": (
                DECLARED_SCOPE,
                _reply(
                    semantic_change_surface=_axis(
                        "declared",
                        ["the greeting text emitted by the demo stage"],
                        ["Change the greeting text emitted by the demo stage"],
                    ),
                    task_owned_write_targets=_axis(
                        "declared", ["work/greeting.txt"], ["write only work/greeting.txt"]
                    ),
                    scope_expansion_policy=_axis(
                        "declared", "widen_automatically", ["If the work exceeds this"]
                    ),
                ),
                "scope_expansion_policy",
            ),
        }
        for name, (scope, reply, axis) in cases.items():
            with self.subTest(case=name):
                home = self.root / f"a3-{name}"
                result = self.start(home, "apply the greeting change", scope, reply=reply)

                self.assertEqual(result["status"], "waiting_user")
                self.assertIn(axis, result["routing"]["preflight"]["reason"])
                self.assertFalse(list((home / "inbox").glob("*.json")))
                self.assertFalse(
                    (home / "tasks" / f"{result['task_id']}-execution-input.md").exists()
                )
                with self.assertRaisesRegex(ValueError, "cannot start-go from preflight waiting_user"):
                    run_start_go(home, result["task_id"])

    def test_a10_input_hash_is_identical_at_submit_in_every_manifest_and_at_the_hold(self):
        controller, _runner, task_id, status = self.run_task(
            [
                _output("applied"),
                _output(HOLD_OUTCOME, _first_run_record(["scenario-a"])),
            ]
        )
        submitted = status["task"]["input_hash"]

        self.assertEqual(status["task"]["stop_reason"], HOLD_STOP_REASON)
        for run in status["stage_runs"]:
            manifest = json.loads(Path(run["manifest_path"]).read_text(encoding="utf-8"))
            self.assertEqual(manifest["input_hash"], submitted)
        self.assertEqual(controller.status(task_id)["task"]["input_hash"], submitted)

    def test_a18_duplicate_or_malformed_markers_stop_and_are_never_legacy(self):
        home = self.root / "a18"
        injected = DECLARED_SCOPE + f"\n{ENVELOPE_BEGIN}\n{{}}\n{ENVELOPE_END}\n"
        result = self.start(home, "apply the greeting change", injected)

        self.assertEqual(result["status"], "waiting_user")
        self.assertIn("envelope marker", result["routing"]["preflight"]["reason"])
        self.assertFalse(list((home / "inbox").glob("*.json")))
        self.assertFalse((home / "tasks" / f"{result['task_id']}-execution-input.md").exists())
        # The structural stop is above the resolver, so it costs nothing.
        self.assertEqual(self.calls, [])

        # A rendered file carrying a second block, or a schema-invalid one, is
        # not read as an envelope and not downgraded to legacy either.
        good = self.envelope_input("a18-good.md").read_text(encoding="utf-8")
        for name, text in {
            "second-block": good + f"\n{ENVELOPE_BEGIN}\n{{}}\n{ENVELOPE_END}\n",
            "malformed-json": f"# t\n\n{ENVELOPE_BEGIN}\nnot json\n{ENVELOPE_END}\n",
            "schema-invalid": f'# t\n\n{ENVELOPE_BEGIN}\n{{"schema_version": 1}}\n{ENVELOPE_END}\n',
            "not-final": good + "\ntrailing prose after the block\n",
        }.items():
            with self.subTest(case=name):
                with self.assertRaises(ValueError):
                    extract_envelope(text)

    def test_a19_a_semantically_scoped_task_naming_no_paths_stops_on_write_targets(self):
        home = self.root / "a19"
        scope = "change stop-gate behaviour so a held gate stays pending\n"
        result = self.start(
            home,
            "apply a change to stop-gate behaviour",
            scope,
            reply=_reply(
                semantic_change_surface=_axis(
                    "semantically_silent",
                    ["change stop-gate behaviour so a held gate stays pending"],
                ),
                task_owned_write_targets=_axis(
                    "unresolved",
                    [],
                    detail="the sources name no write targets, and this axis has no default; it "
                    "is never inferred from the semantic change surface",
                ),
                candidates=["orchestrator/start.py", "orchestrator/controller.py"],
            ),
        )

        self.assertEqual(result["status"], "waiting_user")
        reason = result["routing"]["preflight"]["reason"]
        self.assertIn("axis task_owned_write_targets", reason)
        self.assertIn("never inferred", reason)
        record = _read_yaml(home / "tasks" / f"{result['task_id']}.yaml")
        # The axis is named in the stop, but no value for it exists anywhere:
        # no envelope was rendered, and none is carried in the task record.
        self.assertNotIn("envelope", record)
        self.assertFalse((home / "tasks" / f"{result['task_id']}-execution-input.md").exists())

    def test_a19_a_read_only_path_reference_is_not_write_authority(self):
        # The sources name a path only to forbid writing it. Treating a
        # mention as authorisation is exactly the silent widening the axis
        # exists to prevent, so the honest result is a stop.
        home = self.root / "a19-negated"
        result = self.start(
            home,
            "apply a change to stop-gate behaviour",
            NEGATED_SCOPE,
            reply=_reply(
                semantic_change_surface=_axis(
                    "semantically_silent",
                    ["Change stop-gate behaviour so a held gate stays pending"],
                ),
                task_owned_write_targets=_axis(
                    "unresolved",
                    [],
                    detail="orchestrator/controller.py is named read-only, so the sources "
                    "authorise no write target",
                ),
            ),
        )

        self.assertEqual(result["status"], "waiting_user")
        self.assertIn("axis task_owned_write_targets", result["routing"]["preflight"]["reason"])
        self.assertFalse(list((home / "inbox").glob("*.json")))

    def test_a19_a_write_target_the_quoted_source_does_not_carry_is_refused(self):
        # Incomplete write authority: the quote is verbatim, but it does not
        # carry the path the reply attributes to it.
        home = self.root / "a19-ungrounded-path"
        result = self.start(
            home,
            "apply the greeting change",
            SILENT_SCOPE,
            reply=_reply(
                semantic_change_surface=_axis("semantically_silent", ["修改 demo stage 的問候文字"]),
                task_owned_write_targets=_axis(
                    "declared", ["orchestrator/controller.py"], ["修改 demo stage 的問候文字"]
                ),
            ),
        )

        self.assertEqual(result["status"], "waiting_user")
        reason = result["routing"]["preflight"]["reason"]
        self.assertIn("axis task_owned_write_targets", reason)
        self.assertIn("not carried by the source text quoted for it", reason)
        self.assertFalse(list((home / "inbox").glob("*.json")))

    def test_a24_the_write_target_stop_carries_all_four_required_elements(self):
        home = self.root / "a24"
        result = self.start(
            home,
            "apply a change to stop-gate behaviour",
            "change stop-gate behaviour so a held gate stays pending\n",
            reply=_reply(
                semantic_change_surface=_axis(
                    "semantically_silent",
                    ["change stop-gate behaviour so a held gate stays pending"],
                ),
                task_owned_write_targets=_axis("unresolved", [], detail="no path is named"),
                candidates=["orchestrator/start.py", "orchestrator/controller.py"],
            ),
        )

        reason = result["routing"]["preflight"]["reason"]

        self.assertIn("impact scope of this change may reach files or modules beyond", reason)
        self.assertIn("candidates to think with", reason)
        self.assertIn("confer NO write authority", reason)
        self.assertIn("falls outside the frozen envelope", reason)
        candidates = reason.split("candidates to think with", 1)[1]
        self.assertIn("orchestrator/start.py", candidates)
        # The diagnostic is display-only: it may appear in this stop reason,
        # and it reaches no canonical carrier, because the stopped task has no
        # envelope at all.
        self.assertFalse((home / "tasks" / f"{result['task_id']}-execution-input.md").exists())
        record = _read_yaml(home / "tasks" / f"{result['task_id']}.yaml")
        self.assertNotIn("envelope", record)

    def test_a24_the_stop_names_a_candidate_even_when_the_resolver_offers_none(self):
        home = self.root / "a24-fallback"
        result = self.start(
            home,
            "apply a change to stop-gate behaviour",
            NEGATED_SCOPE,
            reply=_reply(
                semantic_change_surface=_axis(
                    "semantically_silent",
                    ["Change stop-gate behaviour so a held gate stays pending"],
                ),
                task_owned_write_targets=_axis("unresolved", [], detail="no path is authorised"),
            ),
        )

        reason = result["routing"]["preflight"]["reason"]
        candidates = reason.split("candidates to think with", 1)[1]

        self.assertTrue(
            any(marker in candidates for marker in ("/", ".")),
            f"no candidate file or module was named: {candidates}",
        )


class ResolverBoundaryTests(EnvelopeFixture):
    """The resolver is one bounded, untrusted, fail-closed call."""

    def start(self, home: Path, *, reply=None, dry_run: bool = False, scope: str = SILENT_SCOPE):
        spec = self.root / "approved-spec.md"
        spec.write_text("Status: approved\n", encoding="utf-8")
        worktree = self.root / "worktree"
        worktree.mkdir(exist_ok=True)
        flags = StartFlags("apply", scope, worktree, spec, None, dry_run)
        calls: list[str] = []

        def boundary(prompt: str) -> str:
            calls.append(prompt)
            answer = reply if reply is not None else scripted_reply
            return answer(prompt) if callable(answer) else answer

        with patch("orchestrator.start._invoke_resolver", side_effect=boundary):
            result = run_start(home, "apply the greeting change", flags)
            if not dry_run and result["status"] == "waiting_user" and result["routing"].get("pattern"):
                try:
                    result = run_start_go(home, result["task_id"])
                except ValueError:
                    result = _start_result(home, result["task_id"])
        return result, calls

    def test_a_dry_run_makes_no_resolver_call(self):
        _result, calls = self.start(self.root / "dry", dry_run=True)
        self.assertEqual(calls, [])

    def test_a_structural_preflight_stop_makes_no_resolver_call(self):
        home = self.root / "structural"
        worktree = self.root / "worktree"
        worktree.mkdir(exist_ok=True)
        calls: list[tuple[str, str]] = []
        with patch(
            "orchestrator.start._invoke_resolver",
            side_effect=lambda prompt: calls.append(prompt) or "",
        ):
            # apply with no approved spec: an existing structural stop.
            result = run_start(
                home,
                "apply the greeting change",
                StartFlags("apply", SILENT_SCOPE, worktree, None, None, False),
            )
        self.assertEqual(result["status"], "waiting_user")
        self.assertEqual(calls, [])

    def test_an_execution_attempt_makes_at_most_one_resolver_call(self):
        result, calls = self.start(self.root / "once")
        self.assertEqual(result["status"], "execute")
        self.assertEqual(len(calls), 1)

    def test_the_resolver_sees_only_the_immutable_sources(self):
        home = self.root / "sources"
        _result, calls = self.start(home)
        prompt = calls[0]

        self.assertIn("修改 demo stage 的問候文字", prompt)
        self.assertIn("Status: approved", prompt)
        # No workspace, no routing decision, no engine state, no prior report.
        self.assertNotIn(str(self.root / "worktree"), prompt)
        self.assertNotIn(str(home), prompt)
        self.assertNotIn("claude_apply_codex_review", prompt)
        self.assertNotIn(ENVELOPE_BEGIN, prompt)

    def test_d2_the_resolver_is_claude_whatever_routing_decided(self):
        # The resolver is not a stage and does not follow the executor: it is
        # one fixed, isolated question asked before any stage exists. Routing
        # itself is untouched, which is what the executor assertions check.
        self.assertEqual(orchestrator.start.RESOLVER_OWNER, "claude")

        claude_executor, _calls = self.start(self.root / "owner-claude")
        self.assertEqual(claude_executor["routing"]["executor"], "claude")

        # A pattern routed to Codex still resolves through the Claude command.
        home = self.root / "owner-codex"
        commands: list[list[str]] = []
        spec = self.root / "approved-spec.md"
        spec.write_text("Status: approved\n", encoding="utf-8")
        with patch.dict(
            os.environ,
            {"ORCH_CLAUDE_COMMAND": "claude -p", "ORCH_CODEX_COMMAND": "codex exec"},
            clear=False,
        ):
            with patch(
                "orchestrator.start.subprocess.run",
                side_effect=lambda command, **kwargs: commands.append(command)
                or subprocess.CompletedProcess(command, 0, scripted_reply(command[-1]), None),
            ):
                codex_executor = run_start(
                    home,
                    "review the orch start spec",
                    StartFlags("review", "orch start spec, write only work/greeting.txt", None, spec, None, False),
                )

        self.assertEqual(codex_executor["routing"]["executor"], "codex")
        self.assertEqual(codex_executor["status"], "execute")
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0][:2], ["claude", "-p"])
        self.assertNotIn("codex", commands[0])

    def test_d2_an_unusable_claude_command_fails_closed_with_no_fallback(self):
        # No retry, no alternate route, no second provider: the one existing
        # fail-closed intake path, even though this task's executor is Codex
        # and its command is perfectly usable.
        home = self.root / "owner-no-claude"
        spec = self.root / "approved-spec.md"
        spec.write_text("Status: approved\n", encoding="utf-8")
        with patch.dict(
            os.environ, {"ORCH_CLAUDE_COMMAND": "", "ORCH_CODEX_COMMAND": "codex exec"}, clear=False
        ):
            result = run_start(
                home,
                "review the orch start spec",
                StartFlags("review", "orch start spec, write only work/greeting.txt", None, spec, None, False),
            )

        self.assertEqual(result["status"], "waiting_user")
        reason = result["routing"]["preflight"]["reason"]
        self.assertIn("'claude' intake resolver", reason)
        self.assertIn("unusable", reason)
        self.assertFalse(list((home / "inbox").glob("*.json")))

    def test_a_failed_or_malformed_reply_fails_closed_before_enqueue(self):
        cases = {
            "cli-unavailable": _raises(EnvelopeResolverError("intake resolver CLI is unavailable")),
            "no-proposal": "I had a think about it and here are my views.",
            "two-proposals": scripted_reply(_ANY_PROMPT) + scripted_reply(_ANY_PROMPT),
            "not-json": f"{RESOLVER_BEGIN}\nnot json\n{RESOLVER_END}",
            "wrong-schema": f'{RESOLVER_BEGIN}\n{{"schema_version": 9}}\n{RESOLVER_END}',
            "missing-axis": f'{RESOLVER_BEGIN}\n{{"schema_version": 1}}\n{RESOLVER_END}',
            "oversized": f"{RESOLVER_BEGIN}\n" + ("x" * 70000) + f"\n{RESOLVER_END}",
        }
        for name, reply in cases.items():
            with self.subTest(case=name):
                home = self.root / f"closed-{name}"
                result, _calls = self.start(home, reply=reply)
                self.assertEqual(result["status"], "waiting_user")
                self.assertFalse(list((home / "inbox").glob("*.json")))
                self.assertFalse(
                    (home / "tasks" / f"{result['task_id']}-execution-input.md").exists()
                )

    def test_an_ungrounded_declared_member_is_refused(self):
        home = self.root / "ungrounded"
        result, _calls = self.start(
            home,
            reply=_reply(
                semantic_change_surface=_axis(
                    "declared",
                    ["rewrite the daemon lease protocol"],
                    ["the operator asked for a new lease protocol"],
                ),
                task_owned_write_targets=_axis(
                    "declared", ["work/greeting.txt"], ["只寫 work/greeting.txt 這個檔案"]
                ),
            ),
        )

        self.assertEqual(result["status"], "waiting_user")
        self.assertIn("not verbatim in the sources", result["routing"]["preflight"]["reason"])

    def test_a_silent_bounded_axis_may_not_carry_members(self):
        home = self.root / "silent-members"
        result, _calls = self.start(
            home,
            reply=_reply(
                semantic_change_surface=_axis("semantically_silent", ["修改 demo stage 的問候文字"]),
                task_owned_write_targets=_axis(
                    "declared", ["work/greeting.txt"], ["只寫 work/greeting.txt 這個檔案"]
                ),
                threat_model=_axis("semantically_silent", ["a hostile local process"]),
            ),
        )

        self.assertEqual(result["status"], "waiting_user")
        self.assertIn("its safe default is []", result["routing"]["preflight"]["reason"])

    def test_a_candidates_diagnostic_is_refused_where_it_grants_authority(self):
        home = self.root / "candidates-misplaced"
        result, _calls = self.start(
            home,
            reply=_reply(
                semantic_change_surface=_axis("semantically_silent", ["修改 demo stage 的問候文字"]),
                task_owned_write_targets=_axis(
                    "declared", ["work/greeting.txt"], ["只寫 work/greeting.txt 這個檔案"]
                ),
                candidates=["orchestrator/start.py"],
            ),
        )

        self.assertEqual(result["status"], "waiting_user")
        self.assertIn("not unresolved", result["routing"]["preflight"]["reason"])

    # --- what the sources do not authorise ---------------------------------
    #
    # The three cases below are the ones a resolver gets wrong by being
    # helpful rather than by being broken: it answers the question it thinks
    # was asked. Each presents a well-framed, schema-valid, individually
    # grounded reply, and each must still be refused.

    def test_a_declared_member_the_quote_does_not_carry_is_refused(self):
        # The quote is genuine and verbatim from the scope. It simply says
        # nothing about the requirement attributed to it. A quote is evidence
        # for what it carries, not for whatever is listed beside it.
        home = self.root / "unentailed-member"
        result, _calls = self.start(
            home,
            scope=DECLARED_SCOPE,
            reply=_reply(
                semantic_change_surface=_axis(
                    "declared",
                    ["rewrite the daemon lease protocol"],
                    ["Change the greeting text emitted by the demo stage"],
                ),
                task_owned_write_targets=_axis(
                    "declared", ["work/greeting.txt"], ["write only work/greeting.txt"]
                ),
            ),
        )

        self.assertEqual(result["status"], "waiting_user")
        reason = result["routing"]["preflight"]["reason"]
        self.assertIn("not carried by the source text quoted for it", reason)
        self.assertFalse(list((home / "inbox").glob("*.json")))
        self.assertFalse((home / "tasks" / f"{result['task_id']}-execution-input.md").exists())

    def test_d1_a_negated_or_read_only_reference_is_not_authority(self):
        # The three cases the delta review reported, driven through the
        # resolver seam rather than through a deterministic re-reading of the
        # prose. Polarity is the resolver's to read, and the contract it is
        # held to is: a sentence that forbids something is not a requirement to
        # do it, so the axis is unresolved and intake stops.
        cases = {
            # "Keep orchestrator/controller.py untouched."
            "negated write target": (
                NEGATED_SCOPE,
                _reply(
                    semantic_change_surface=_axis(
                        "semantically_silent",
                        ["Change stop-gate behaviour so a held gate stays pending"],
                    ),
                    task_owned_write_targets=_axis(
                        "unresolved",
                        [],
                        detail="orchestrator/controller.py is named read-only background and the "
                        "sources forbid writing it, so they authorise no write target",
                    ),
                ),
                "task_owned_write_targets",
            ),
            # "Do not rewrite the daemon lease protocol."
            "negated behaviour": (
                NEGATED_BEHAVIOUR_SCOPE,
                _reply(
                    semantic_change_surface=_axis(
                        "unresolved",
                        [],
                        detail="the sources forbid rewriting the daemon lease protocol and name "
                        "no behaviour this task may change instead",
                    ),
                    task_owned_write_targets=_axis(
                        "declared", ["work/greeting.txt"], ["and write only work/greeting.txt."]
                    ),
                ),
                "semantic_change_surface",
            ),
            # A true sentence about a different axis is not a declaration of
            # this one.
            "unrelated expansion policy": (
                DECLARED_SCOPE,
                _reply(
                    semantic_change_surface=_axis(
                        "declared",
                        ["Change the greeting text emitted by the demo stage"],
                        ["Change the greeting text emitted by the demo stage"],
                    ),
                    task_owned_write_targets=_axis(
                        "declared", ["work/greeting.txt"], ["write only work/greeting.txt"]
                    ),
                    scope_expansion_policy=_axis(
                        "unresolved",
                        [],
                        detail="no source sentence states what happens when the work exceeds the "
                        "surface in terms this engine can honour",
                    ),
                ),
                "scope_expansion_policy",
            ),
        }
        for name, (scope, reply, axis) in cases.items():
            with self.subTest(case=name):
                home = self.root / f"d1-{name.replace(' ', '-')}"
                result, _calls = self.start(home, scope=scope, reply=reply)

                self.assertEqual(result["status"], "waiting_user")
                self.assertIn(f"axis {axis}", result["routing"]["preflight"]["reason"])
                self.assertFalse(list((home / "inbox").glob("*.json")))
                self.assertFalse(
                    (home / "tasks" / f"{result['task_id']}-execution-input.md").exists()
                )

    def test_d1_the_resolver_prompt_owns_polarity_ambiguity_and_authorisation(self):
        # The semantic half of the contract is a rule the resolver is given,
        # not a hint. If it is not in the prompt it is nowhere: no other part
        # of intake reads the sources for meaning.
        prompt = orchestrator.start._resolver_prompt([("task text", "change the greeting text")])

        self.assertIn("You are the only reader of this text", prompt)
        self.assertIn("Read polarity before you read words", prompt)
        self.assertIn("Do not rewrite the daemon lease protocol", prompt)
        self.assertIn("Keep orchestrator/controller.py untouched", prompt)
        self.assertIn(
            "A negated or read-only reference is never returned as write authority or as "
            "positive semantic-change authority",
            prompt,
        )
        self.assertIn("authorisation, polarity or scope is ambiguous", prompt)
        self.assertIn("Do not resolve an ambiguity in the direction of more authority", prompt)
        self.assertIn("A quote is evidence only for what it actually asserts about the member", prompt)

    def test_d1_the_validator_is_not_a_second_natural_language_reader(self):
        # The deterministic side keeps framing, schema, bounds, value shapes,
        # exact source-span grounding, path normalisation and containment —
        # and gives up the denial-word list, which could only ever recognise
        # the phrasings someone had thought of.
        for removed in ("WRITE_DENIAL_MARKERS", "_denies_write", "_CLAUSE_SPLIT"):
            with self.subTest(symbol=removed):
                self.assertFalse(hasattr(orchestrator.start, removed))

        # And no replacement for it under another name: no collection this
        # module holds is a vocabulary of prohibition phrases waiting to be
        # matched against the operator's prose.
        phrases = ("read-only", "read only", "do not write", "must not write", "do not modify",
                   "leave unchanged", "off limits", "唯讀", "不可寫", "不修改", "禁止")
        for name, value in vars(orchestrator.start).items():
            if not isinstance(value, (tuple, list, set, frozenset)):
                continue
            matched = [
                phrase
                for phrase in phrases
                for item in value
                if isinstance(item, str) and phrase in item.casefold()
            ]
            with self.subTest(constant=name):
                self.assertEqual(matched, [], f"{name} is a denial-word list")

        # What remains is exact source-span grounding: the member has to occur
        # in the span quoted for it, whatever that span goes on to say.
        self.assertTrue(orchestrator.start._carried_by("work/greeting.txt", "write work/greeting.txt"))
        self.assertFalse(orchestrator.start._carried_by("work/greeting.txt", "write the other file"))

    def test_an_empty_declared_write_set_is_refused_for_a_task_that_changes_code(self):
        # A pathless apply. "This task writes nothing" is a claim about the
        # sources, and an apply task contradicts it: the paths are undetermined,
        # which is the one axis with no default.
        home = self.root / "empty-declared-apply"
        result, _calls = self.start(
            home,
            scope="change stop-gate behaviour so a held gate stays pending\n",
            reply=_reply(
                semantic_change_surface=_axis(
                    "semantically_silent",
                    ["change stop-gate behaviour so a held gate stays pending"],
                ),
                task_owned_write_targets=_axis("declared", [], []),
            ),
        )

        self.assertEqual(result["status"], "waiting_user")
        reason = result["routing"]["preflight"]["reason"]
        self.assertIn("axis task_owned_write_targets", reason)
        self.assertIn("never inferred", reason)
        self.assertFalse(list((home / "inbox").glob("*.json")))
        self.assertFalse((home / "tasks" / f"{result['task_id']}-execution-input.md").exists())

    def test_an_empty_declared_write_set_is_refused_while_the_sources_name_paths(self):
        # The sources do name a path; the reply just does not account for it.
        # Silence it is not, so the set is not determined.
        home = self.root / "empty-declared-with-paths"
        result, _calls = self.start(
            home,
            reply=_reply(
                semantic_change_surface=_axis("semantically_silent", ["修改 demo stage 的問候文字"]),
                task_owned_write_targets=_axis("declared", [], []),
            ),
        )

        self.assertEqual(result["status"], "waiting_user")
        self.assertIn("axis task_owned_write_targets", result["routing"]["preflight"]["reason"])
        self.assertFalse(list((home / "inbox").glob("*.json")))

    def test_an_unusable_provider_command_fails_closed_like_any_other_resolver_failure(self):
        # M1: a misconfigured provider command is a resolver failure, not a
        # crash, and it stops intake in the ordinary place.
        home = self.root / "bad-command"
        spec = self.root / "approved-spec.md"
        spec.write_text("Status: approved\n", encoding="utf-8")
        worktree = self.root / "worktree"
        worktree.mkdir(exist_ok=True)
        with patch.dict(os.environ, {"ORCH_CLAUDE_COMMAND": ""}, clear=False):
            result = run_start(
                home,
                "apply the greeting change",
                StartFlags("apply", SILENT_SCOPE, worktree, spec, None, False),
            )

        self.assertEqual(result["status"], "waiting_user")
        self.assertIn("unusable", result["routing"]["preflight"]["reason"])
        self.assertFalse(list((home / "inbox").glob("*.json")))
        self.assertFalse((home / "tasks" / f"{result['task_id']}-execution-input.md").exists())

    def test_the_request_id_and_input_write_timing_are_unchanged(self):
        home = self.root / "timing"
        result, _calls = self.start(home)

        execution = result["routing"]["execution"]
        request_id = execution["request_id"]
        self.assertEqual(execution["controller_task_id"], request_id)
        input_path = home / "tasks" / f"{result['task_id']}-execution-input.md"
        self.assertTrue(input_path.is_file())
        self.assertEqual(execution["input"], str(input_path.resolve()))
        # E-17 needs the request id inside the envelope, so the input is still
        # written after routing and after the id is drawn, and exactly once.
        envelope = extract_envelope(input_path.read_text(encoding="utf-8"))
        self.assertTrue(
            any(
                request_id in path
                for path in envelope["task_owned_write_targets"]["value"]
            ),
            envelope["task_owned_write_targets"]["value"],
        )
        request = json.loads(
            next((home / "inbox").glob("*.json")).read_text(encoding="utf-8")
        )
        self.assertEqual(request["request_id"], request_id)


class ResolverProcessBoundaryTests(unittest.TestCase):
    """The command and environment the resolver child is actually given.

    Every other resolver test replaces `_invoke_resolver`, so nothing above
    would notice if the boundary it stands for stopped isolating anything.
    These tests look at the boundary itself: the argv the engine builds and the
    environment it hands over.
    """

    def test_the_resolver_command_disables_tools_config_and_session_state(self):
        with patch.dict(os.environ, {"ORCH_CLAUDE_COMMAND": "claude -p"}, clear=False):
            command = _resolver_command()

        self.assertEqual(command[:2], ["claude", "-p"])
        # The tools are unavailable, not merely unapproved: `--tools ""` is the
        # CLI's own "disable all tools" control, so the child has no file read,
        # no shell and no browsing to be approved in the first place. A
        # permission allowlist would not establish that, so this asserts the
        # availability option and not `--allowedTools`.
        self.assertIn("--tools", command)
        self.assertEqual(command[command.index("--tools") + 1], "")
        self.assertNotIn("--allowedTools", command)
        self.assertNotIn("--allowed-tools", command)
        # And no other configuration may add a tool back.
        self.assertIn("--strict-mcp-config", command)
        # No user, project or local settings: no hooks, plugins or agents.
        self.assertIn("--setting-sources", command)
        self.assertEqual(command[command.index("--setting-sources") + 1], "")
        # Nothing about the call survives it.
        self.assertIn("--no-session-persistence", command)

    def test_the_resolver_command_is_built_only_from_the_fixed_owner(self):
        # No provider selection happens here at all: there is one command, and
        # the routed executor is not an input to it.
        self.assertEqual(orchestrator.start.RESOLVER_OWNER, "claude")
        with patch.dict(
            os.environ,
            {"ORCH_CLAUDE_COMMAND": "claude -p", "ORCH_CODEX_COMMAND": "codex exec"},
            clear=False,
        ):
            command = _resolver_command()
        self.assertNotIn("codex", command)
        self.assertNotIn("exec", command)

    def test_no_resolver_command_ends_with_an_option_that_would_eat_the_prompt(self):
        # `_invoke_resolver` appends the prompt as a positional argument. A
        # trailing variadic option would read it as one more value and leave
        # the resolver with no question, which no reply-level test can catch.
        self.assertNotIn(RESOLVER_ISOLATION_FLAGS[-1], RESOLVER_VARIADIC_FLAGS)
        with patch.dict(os.environ, {"ORCH_CLAUDE_COMMAND": "claude -p"}):
            self.assertNotIn(_resolver_command()[-1], RESOLVER_VARIADIC_FLAGS)

    def test_an_unusable_provider_command_raises_the_fail_closed_error(self):
        for value in ("", 'claude -p "unbalanced'):
            with self.subTest(command=value):
                with patch.dict(os.environ, {"ORCH_CLAUDE_COMMAND": value}, clear=False):
                    with self.assertRaises(EnvelopeResolverError):
                        _resolver_command()

    def test_the_resolver_environment_is_an_allowlist_not_a_blocklist(self):
        env = {
            # Provider authentication has to survive.
            "HOME": "/opt/example",
            "PATH": "/usr/bin",
            "ANTHROPIC_API_KEY": "secret",
            "CLAUDE_CONFIG_DIR": "/opt/example/.claude",
            "HTTPS_PROXY": "http://127.0.0.1:1080",
            "SSL_CERT_FILE": "/etc/ssl/cert.pem",
            # Engine state must not travel.
            "ORCH_HOME": "/opt/example/.local/state/aios-orchestrator",
            "ORCH_CLAUDE_COMMAND": "claude -p",
            # Nor another provider's namespace: the resolver owner is fixed,
            # so nothing else needs to authenticate through this child.
            "CODEX_HOME": "/opt/example/.codex",
            "OPENAI_API_KEY": "secret",
            # Nor must anything else this engine has never heard of: a
            # blocklist would have forwarded every one of these.
            "GITHUB_TOKEN": "secret",
            "AWS_SECRET_ACCESS_KEY": "secret",
            "PWD": "/opt/example/code/secret-project",
            "GIT_DIR": "/opt/example/code/secret-project/.git",
            "CI_JOB_URL": "https://example.invalid/job/1",
        }

        passed = _resolver_environment(env)

        self.assertEqual(passed["ANTHROPIC_API_KEY"], "secret")
        self.assertEqual(passed["CLAUDE_CONFIG_DIR"], "/opt/example/.claude")
        self.assertEqual(passed["HOME"], "/opt/example")
        self.assertEqual(passed["HTTPS_PROXY"], "http://127.0.0.1:1080")
        for blocked in ("ORCH_HOME", "ORCH_CLAUDE_COMMAND", "CODEX_HOME", "OPENAI_API_KEY",
                        "GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY", "PWD", "GIT_DIR", "CI_JOB_URL"):
            with self.subTest(variable=blocked):
                self.assertNotIn(blocked, passed)
        self.assertTrue(set(passed) <= set(env))
        self.assertNotIn("ORCH_HOME", RESOLVER_ENV_ALLOW_NAMES)

    def test_the_resolver_child_runs_in_an_empty_directory_with_the_bounded_environment(self):
        # The real subprocess call, against a command that only reports what it
        # was given. No provider and no network are involved.
        script = Path(tempfile.mkdtemp()) / "fake-provider.py"
        script.write_text(
            "import json, os, sys\n"
            "print(json.dumps({'cwd': os.getcwd(), 'env': sorted(os.environ),\n"
            "                  'argv': sys.argv[1:], 'listing': sorted(os.listdir('.'))}))\n",
            encoding="utf-8",
        )
        command = f"{sys.executable} {script}"
        with patch.dict(
            os.environ,
            {"ORCH_CLAUDE_COMMAND": command, "ORCH_HOME": "/tmp/orch-home", "GITHUB_TOKEN": "s"},
            clear=False,
        ):
            with patch.object(orchestrator.start, "RESOLVER_ISOLATION_FLAGS", ("--marker",)):
                output = orchestrator.start._invoke_resolver("the question")

        observed = json.loads(output)
        self.assertEqual(observed["argv"], ["--marker", "the question"])
        self.assertEqual(observed["listing"], [])
        self.assertNotIn("ORCH_HOME", observed["env"])
        self.assertNotIn("GITHUB_TOKEN", observed["env"])
        self.assertNotIn(str(ROOT), observed["cwd"])
        # The temporary working root does not outlive the call.
        self.assertFalse(Path(observed["cwd"]).exists())


# ---------------------------------------------------------------------------
# Slice 2 — chokepoint, shared outcomes, hold
# ---------------------------------------------------------------------------


class ChokepointTests(EnvelopeFixture):
    def test_a4_every_stage_prompt_carries_the_envelope_above_the_instructions(self):
        _controller, runner, _task_id, _status = self.run_task(
            [
                _output("applied"),
                _output("needs_repair", _first_run_record(["scenario-a"])),
                _output("repaired"),
                _output(
                    "ready",
                    _repeat_record(["scenario-a"], ["scenario-a"], [], verdict="stalled"),
                ),
            ]
        )

        self.assertEqual(len(runner.prompts), 4)
        for owner, prompt in runner.prompts:
            with self.subTest(owner=owner):
                self.assertIn(ENVELOPE_BEGIN, prompt)
                self.assertIn(ENVELOPE_END, prompt)
                self.assertIn("the envelope above outranks the stage instructions below", prompt)
                self.assertLess(prompt.index(ENVELOPE_END), prompt.index("Stage instructions:"))

    def test_a5_no_shipped_profile_stage_prompt_text_changes(self):
        diff = subprocess.run(
            ["git", "diff", "HEAD", "--", "orchestrator/profiles"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(diff.returncode, 0, diff.stderr)
        self.assertEqual(diff.stdout, "")

    def test_a6_a_legacy_prompt_is_byte_identical_to_the_pre_change_engine(self):
        profile = load_profile(APPLY_PROFILE)
        stage = profile.stage("review")
        input_text = self.legacy_input().read_text(encoding="utf-8")
        # The pre-change composition, reproduced literally rather than by
        # calling the function under test.
        expected = (
            f"You are executing agent-orch task legacy-1, stage {stage.name}.\n"
            f"Reports directory (write stage reports here): /tmp/reports\n"
            f"Stage instructions: {stage.prompt}\n\n"
            f"Task input:\n---\n{input_text}\n---\n\n"
            f"Allowed typed outcomes: {', '.join(stage.outcomes)}.\n"
            "Complete this stage. As the VERY LAST line of your output, print the outcome once:\n"
            "ORCHESTRATOR_OUTCOME: <typed outcome>\n"
            "Do not print this line more than once and do not write it into any file.\n"
        )

        composed = Controller._build_prompt("legacy-1", stage, input_text, "/tmp/reports")

        self.assertEqual(composed.encode("utf-8"), expected.encode("utf-8"))
        self.assertEqual(allowed_outcomes(stage.outcomes, False), list(stage.outcomes))

    def test_a7_the_hold_outcome_is_accepted_on_a_profile_that_never_declared_it(self):
        stage = load_profile(STOP_GATE_CODEX_PROFILE).stage("review")
        output = _output(HOLD_OUTCOME)

        legacy = classify_result(0, output, set(allowed_outcomes(stage.outcomes, False)))
        envelope = classify_result(0, output, set(allowed_outcomes(stage.outcomes, True)))

        self.assertEqual((legacy.classification, legacy.reason), ("blocked", "unknown_outcome"))
        self.assertEqual((envelope.classification, envelope.reason), ("success", "success"))
        self.assertEqual(envelope.outcome, HOLD_OUTCOME)

    def test_a8_retained_inspection_reports_the_same_candidate_outcome(self):
        controller, _runner = self.controller([])
        input_path = self.envelope_input("a8.md")
        task_id = controller.submit("stop-gate", STOP_GATE_CODEX_PROFILE, input_path)
        task = dict(controller._task(task_id))
        stage = load_profile(STOP_GATE_CODEX_PROFILE).stage("review")
        output = _output(HOLD_OUTCOME)
        artifact_dir = Path(task["artifact_dir"])
        log_path = artifact_dir / "runs" / "0001-review.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(output, encoding="utf-8")
        output_path = log_path.with_suffix(".output.txt")
        output_path.write_text(output, encoding="utf-8")
        drift_path = log_path.with_suffix(".containment-drift.json")
        drift = {
            "task_id": task_id,
            "log_path": str(log_path),
            "attribution": "unknown",
            "violations": [{"path": "work/greeting.txt", "kind": "modified"}],
        }
        drift_path.write_text(json.dumps(drift), encoding="utf-8")
        expected = classify_result(
            0, output, set(allowed_outcomes(stage.outcomes, True)), False
        )
        manifest = {
            "schema_version": 2,
            "task_id": task_id,
            "run_token": "run-1",
            "lease_token": "lease-1",
            "stage": "review",
            "owner": "codex",
            "classification": "blocked",
            "reason": "protected_root_drift",
            "outcome": None,
            "exit_code": 0,
            "timed_out": False,
            "log_path": str(log_path),
            "log_hash": hashlib.sha256(log_path.read_bytes()).hexdigest(),
            "output_path": str(output_path),
            "output_hash": hashlib.sha256(output_path.read_bytes()).hexdigest(),
            "profile_hash": task["profile_hash"],
            "input_hash": task["input_hash"],
            "candidate_outcome": expected.outcome,
            "candidate_classification": expected.classification,
            "candidate_reason": expected.reason,
            "containment_evidence_path": str(drift_path),
            "containment_evidence_hash": hashlib.sha256(drift_path.read_bytes()).hexdigest(),
        }
        manifest_path = log_path.with_suffix(log_path.suffix + ".manifest.json")
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        run = {
            "sealed": 1,
            "manifest_path": str(manifest_path),
            "manifest_hash": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            "run_token": "run-1",
            "stage": "review",
            "lease_token": "lease-1",
            "owner": "codex",
            "exit_code": 0,
            "outcome": None,
            "log_path": str(log_path),
        }

        retained = inspect_retained(task, run)

        self.assertEqual(retained["candidate_outcome"], expected.outcome)
        self.assertEqual(retained["candidate_outcome"], HOLD_OUTCOME)
        self.assertEqual(retained["candidate_classification"], expected.classification)
        self.assertEqual(retained["candidate_reason"], expected.reason)

    def test_a9_the_hold_preserves_stage_owner_edges_and_the_transition_count(self):
        controller, _runner = self.controller([_output("applied"), _output(HOLD_OUTCOME, _first_run_record([]))])
        task_id = controller.submit("apply", APPLY_PROFILE, self.envelope_input("a9.md"))
        controller.run_until_stop(task_id)
        before = controller.status(task_id)
        self.assertEqual(before["task"]["current_stage"], "review")

        controller.runner.outputs = [_output(HOLD_OUTCOME, _first_run_record([]))]
        after = controller.resume(task_id)

        self.assertEqual(after["task"]["status"], "waiting_user")
        self.assertEqual(after["task"]["stop_reason"], HOLD_STOP_REASON)
        # `current_stage` and `owner` are preserved: the held stage is the one
        # a resume re-runs, not the one after it.
        self.assertEqual(after["task"]["current_stage"], before["task"]["current_stage"])
        self.assertEqual(after["task"]["owner"], before["task"]["owner"])
        self.assertEqual(after["task"]["transitions_count"], before["task"]["transitions_count"])
        for edge in after["edge_counts"]:
            with self.subTest(edge=edge["edge"]):
                self.assertEqual(edge["count"], self.edge(before, edge["edge"])["count"])
        self.assertEqual(after["notifications"][-1]["reason"], HOLD_STOP_REASON)

    def test_a13_a_legacy_task_still_stops_for_edge_cap(self):
        controller, _runner = self.controller(
            [_output("submit"), _output("block"), _output("submit"), _output("block")]
        )
        task_id = controller.submit("demo-loop", DEMO_PROFILE, self.legacy_input("a13.md"))

        status = controller.run_until_stop(task_id)

        self.assertEqual(status["task"]["status"], "waiting_user")
        self.assertEqual(status["task"]["stop_reason"], "edge_cap")
        self.assertEqual(self.edge(status, "review.block")["count"], 1)

    def test_a20_an_in_envelope_correction_takes_the_ordinary_repair_outcome(self):
        _controller, _runner, _task_id, status = self.run_task(
            [
                _output("applied"),
                _output("needs_repair", _first_run_record(["scenario-a"])),
                _output("repaired"),
                _output(
                    "ready",
                    _repeat_record(["scenario-a"], ["scenario-a"], [], verdict="stalled"),
                ),
            ]
        )

        routed = [
            row["outcome"] for row in status["transitions"] if row["outcome"] == "needs_repair"
        ]
        self.assertEqual(routed, ["needs_repair"])
        repair = next(row for row in status["transitions"] if row["outcome"] == "needs_repair")
        self.assertEqual(repair["to_status"], "queued")
        self.assertNotEqual(repair["reason"], HOLD_STOP_REASON)
        self.assertIn("repair", [row["stage"] for row in status["stage_runs"]])

    def test_a21_each_prohibited_move_routes_to_needs_user_decision(self):
        prohibited = {
            "assurance_ceiling": "raising assurance_ceiling",
            "evidence_ceiling": "raising evidence_ceiling",
            "threat_model": "raising threat_model",
            "semantic_change_surface": "widening semantic_change_surface",
            "task_owned_write_targets": "widening task_owned_write_targets",
        }
        for axis, description in prohibited.items():
            with self.subTest(axis=axis):
                controller, runner = self.controller(
                    [
                        _output("applied"),
                        _output(
                            HOLD_OUTCOME,
                            _first_run_record([f"out-of-envelope: {description}"]),
                        ),
                    ]
                )
                task_id = controller.submit("apply", APPLY_PROFILE, self.envelope_input(f"a21-{axis}.md"))

                status = controller.run_until_stop(task_id)

                self.assertEqual(status["task"]["status"], "waiting_user")
                self.assertEqual(status["task"]["stop_reason"], HOLD_STOP_REASON)
                # The correction rule names this axis and this move explicitly,
                # so the prohibition is stated, not assumed.
                prompt = runner.prompts[-1][1]
                self.assertIn(axis, prompt)
                self.assertIn("never a self-authorised correction", prompt)
                for edge in status["edge_counts"]:
                    if edge["edge"].startswith("review."):
                        self.assertEqual(edge["count"], 0)


# ---------------------------------------------------------------------------
# Slice 3 — repeat-review convergence and stop-gate reuse
# ---------------------------------------------------------------------------


class ConvergenceTests(EnvelopeFixture):
    #: A profile with a branching stage that can route to itself, so a history
    #: with and without a correction run between two branching runs is
    #: expressible. Nothing in it is shipped; it exists to pin prior selection.
    PRIOR_SELECTION_PROFILE = """version: 1
type: demo-loop
initial_stage: fix
max_transitions: 20
stages:
  fix:
    owner: claude
    attempt_cap: 5
    timeout: 30
    prompt: "Correction stage."
    outcomes:
      fixed: gate
  gate:
    owner: codex
    attempt_cap: 5
    timeout: 30
    prompt: "Branching stage."
    outcomes:
      again: gate
      back: fix
      settled: done
  done:
    terminal: done
edge_caps:
  fix.fixed: 5
  gate.again: 5
  gate.back: 5
  gate.settled: 1
"""

    def prior_selection_profile(self) -> Path:
        path = self.root / "prior-selection.yaml"
        path.write_text(self.PRIOR_SELECTION_PROFILE, encoding="utf-8")
        return path

    def sealed_outputs(self, status: dict, stage: str) -> list[str]:
        outputs = []
        for run in status["stage_runs"]:
            if run["stage"] != stage or not run["manifest_path"]:
                continue
            manifest = json.loads(Path(run["manifest_path"]).read_text(encoding="utf-8"))
            data = Path(manifest["output_path"]).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), manifest["output_hash"])
            outputs.append(data.decode("utf-8"))
        return outputs

    def test_a11_delta_review_after_review_repair_is_a_repeat_review(self):
        _controller, runner, _task_id, _status = self.run_task(
            [
                _output("applied"),
                _output("needs_repair", _first_run_record(["scenario-a", "scenario-b"])),
                _output("repaired"),
                _output("ready", _repeat_record(["scenario-a"], ["scenario-a", "scenario-b"], [])),
            ]
        )

        review_prompt = runner.prompt_for("codex", 0)
        delta_prompt = runner.prompt_for("codex", 1)

        self.assertNotIn("REPEAT REVIEW", review_prompt)
        self.assertIn("REPEAT REVIEW", delta_prompt)
        # Its prior branching run is `review`, not itself.
        self.assertIn("stage 'review'", delta_prompt)
        self.assertIn('["scenario-a", "scenario-b"]', delta_prompt)
        self.assertIn("Correction runs since then: repair", delta_prompt)

    def test_a12_a_first_branching_run_gets_no_directive_and_seals_an_empty_resolved_set(self):
        _controller, runner, _task_id, status = self.run_task(
            [_output("applied"), _output("needs_repair", _first_run_record(["scenario-a"]))]
        )

        review_prompt = runner.prompt_for("codex", 0)
        self.assertIn("Convergence record obligation", review_prompt)
        self.assertNotIn("REPEAT REVIEW", review_prompt)
        self.assertIn("first branching run of this task", review_prompt)

        sealed = self.sealed_outputs(status, "review")
        self.assertEqual(len(sealed), 1)
        record = json.loads(sealed[0].split(CONVERGENCE_BEGIN)[1].split(CONVERGENCE_END)[0])
        self.assertEqual(record, {"live": ["scenario-a"], "resolved": []})
        self.assertNotIn("verdict", record)

    def test_a12_a_direct_hold_still_owes_its_branching_convergence_record(self):
        # E-13 is unconditional: a branching run that prints the reserved hold
        # outcome itself is still a committed branching run, so accepting one
        # without a record would leave a later repeat with no readable
        # live/resolved baseline to be scored against.
        for name, held in {
            "first-missing": _output(HOLD_OUTCOME),
            "first-malformed": _output(
                HOLD_OUTCOME, f"{CONVERGENCE_BEGIN}\nnot json\n{CONVERGENCE_END}"
            ),
        }.items():
            with self.subTest(case=name):
                controller, _runner = self.controller([_output("applied"), held])
                task_id = controller.submit(
                    "apply", APPLY_PROFILE, self.envelope_input(f"a12-{name}.md")
                )

                status = controller.run_until_stop(task_id)

                self.assertEqual(status["task"]["status"], "waiting_user")
                self.assertEqual(status["task"]["stop_reason"], HOLD_STOP_REASON)
                self.assertIn(
                    "convergence_record_invalid", status["notifications"][-1]["message"]
                )
                # The no-edge hold semantics survive the added obligation.
                self.assertEqual(status["task"]["current_stage"], "review")
                self.assertEqual(self.edge(status, "review.needs_repair")["count"], 0)

        # A repeat review may not print the hold outcome to escape the record
        # either: its prior is readable, so only the current block is missing.
        controller, _runner = self.controller(
            [
                _output("applied"),
                _output("needs_repair", _first_run_record(["scenario-a"])),
                _output("repaired"),
                _output(HOLD_OUTCOME),
            ]
        )
        task_id = controller.submit("apply", APPLY_PROFILE, self.envelope_input("a12-repeat.md"))

        status = controller.run_until_stop(task_id)

        self.assertEqual(status["task"]["stop_reason"], HOLD_STOP_REASON)
        self.assertIn("convergence_record_invalid", status["notifications"][-1]["message"])

        # A well-formed direct hold is accepted, keeps its own hold reason, and
        # seals the same first-run baseline any other branching run would.
        controller, _runner = self.controller(
            [_output("applied"), _output(HOLD_OUTCOME, _first_run_record(["scenario-a"]))]
        )
        task_id = controller.submit("apply", APPLY_PROFILE, self.envelope_input("a12-valid.md"))

        status = controller.run_until_stop(task_id)

        self.assertEqual(status["task"]["stop_reason"], HOLD_STOP_REASON)
        self.assertNotIn("convergence_", status["notifications"][-1]["message"])
        sealed = self.sealed_outputs(status, "review")
        record = json.loads(sealed[0].split(CONVERGENCE_BEGIN)[1].split(CONVERGENCE_END)[0])
        self.assertEqual(record, {"live": ["scenario-a"], "resolved": []})

    def test_a14_an_absent_or_unreadable_prior_record_ends_needs_user_decision(self):
        for name, first_output in {
            "absent": _output("needs_repair"),
            "prose": _output("needs_repair", "the findings are roughly the same as before"),
        }.items():
            with self.subTest(case=name):
                controller, _runner = self.controller(
                    [_output("applied"), first_output, _output("repaired"), _output("ready")]
                )
                task_id = controller.submit("apply", APPLY_PROFILE, self.envelope_input(f"a14-{name}.md"))

                status = controller.run_until_stop(task_id)

                # The first review already fails closed: a branching run with no
                # readable record never becomes a prior for anything.
                self.assertEqual(status["task"]["status"], "waiting_user")
                self.assertEqual(status["task"]["stop_reason"], HOLD_STOP_REASON)
                self.assertEqual(self.edge(status, "review.needs_repair")["count"], 0)

        # And a repeat review whose prior sealed output no longer matches the
        # hash it was sealed under: unreadable, so the repeat does not silently
        # proceed as a fresh first round.
        controller, _runner = self.controller(
            [
                _output("applied"),
                _output("needs_repair", _first_run_record(["scenario-a", "scenario-b"])),
                _output(HOLD_OUTCOME),
            ]
        )
        task_id = controller.submit("apply", APPLY_PROFILE, self.envelope_input("a14-tampered.md"))
        controller.run_until_stop(task_id)
        first = controller.status(task_id)
        review_run = next(row for row in first["stage_runs"] if row["stage"] == "review")
        manifest = json.loads(Path(review_run["manifest_path"]).read_text(encoding="utf-8"))
        output_path = Path(manifest["output_path"])
        output_path.write_text(
            output_path.read_text(encoding="utf-8").replace("scenario-b", "scenario-z"),
            encoding="utf-8",
        )

        controller.runner.outputs = [
            _output("repaired"),
            _output("ready", _repeat_record(["scenario-a"], ["scenario-a", "scenario-b"], [])),
        ]
        resumed = controller.resume(task_id)

        self.assertEqual(resumed["task"]["stop_reason"], HOLD_STOP_REASON)
        delta = [row for row in resumed["stage_runs"] if row["stage"] == "delta_review"][-1]
        delta_manifest = json.loads(Path(delta["manifest_path"]).read_text(encoding="utf-8"))
        self.assertIn("convergence_unverifiable", delta_manifest["reason"])

    def test_a15_stalled_oscillating_and_bad_records_hold_before_any_cap_binds(self):
        prior_live = ["scenario-a", "scenario-b"]
        cases = {
            "stalled": _repeat_record(["scenario-a", "scenario-c"], prior_live, []),
            "oscillating": _repeat_record(["scenario-a"], prior_live, ["scenario-a"]),
            "missing": "",
            "malformed": f"{CONVERGENCE_BEGIN}\nnot json\n{CONVERGENCE_END}",
            "contradictory-partition": "\n".join(
                [
                    CONVERGENCE_BEGIN,
                    json.dumps(
                        {
                            "live": ["scenario-a"],
                            "resolved": [],
                            "new": [],
                            "repeated": ["scenario-a"],
                            "verdict": "improved",
                        }
                    ),
                    CONVERGENCE_END,
                ]
            ),
            "contradictory-verdict": _repeat_record(
                ["scenario-a"], prior_live, [], verdict="stalled"
            ),
        }
        for name, record in cases.items():
            with self.subTest(case=name):
                # The oscillating fixture needs scenario-a in historical_resolved,
                # which a three-run history produces naturally.
                outputs = [
                    _output("applied"),
                    _output("needs_repair", _first_run_record(prior_live)),
                    _output("repaired"),
                    _output("ready", record),
                ]
                if name == "oscillating":
                    outputs = [
                        _output("applied"),
                        _output("needs_repair", _first_run_record(prior_live)),
                        _output("repaired"),
                        _output(
                            "needs_repair",
                            _repeat_record(["scenario-b"], prior_live, []),
                        ),
                        _output("repaired"),
                        _output("ready", record),
                    ]
                controller, _runner = self.controller(outputs)
                task_id = controller.submit(
                    "apply", APPLY_PROFILE, self.envelope_input(f"a15-{name}.md")
                )

                status = controller.run_until_stop(task_id)

                self.assertEqual(status["task"]["status"], "waiting_user")
                self.assertEqual(status["task"]["stop_reason"], HOLD_STOP_REASON)
                self.assertNotEqual(status["task"]["stop_reason"], "edge_cap")
                # The held run consumed no edge and counted no transition: the
                # hold fired before the edge-cap test, not after it.
                held = status["transitions"][-1]
                self.assertEqual(held["reason"], HOLD_STOP_REASON)
                self.assertIsNone(held["edge"])
                self.assertEqual(self.edge(status, "delta_review.ready")["count"], 0)

    def test_a22_the_first_run_seals_its_record_and_the_repeat_scores_against_it(self):
        cases = {
            "claude_apply_codex_review": (
                APPLY_PROFILE,
                "apply",
                "review",
                "delta_review",
                [
                    _output("applied"),
                    _output("needs_repair", _first_run_record(["scenario-a", "scenario-b"])),
                    _output("repaired"),
                    _output(
                        "ready", _repeat_record(["scenario-a"], ["scenario-a", "scenario-b"], [])
                    ),
                ],
            ),
            "artifact_validation": (
                ARTIFACT_PROFILE,
                "artifact-validation",
                "review",
                "review",
                [
                    _output("validated"),
                    _output("needs_revision", _first_run_record(["scenario-a", "scenario-b"])),
                    _output("revised"),
                    _output(
                        "ready", _repeat_record(["scenario-a"], ["scenario-a", "scenario-b"], [])
                    ),
                ],
            ),
        }
        for name, (profile, task_type, first_stage, _repeat_stage, outputs) in cases.items():
            with self.subTest(profile=name):
                controller, runner = self.controller(outputs)
                task_id = controller.submit(
                    task_type, profile, self.envelope_input(f"a22-{name}.md")
                )

                status = controller.run_until_stop(task_id)

                self.assertEqual(status["task"]["status"], "done")
                sealed = self.sealed_outputs(status, first_stage)
                first = json.loads(sealed[0].split(CONVERGENCE_BEGIN)[1].split(CONVERGENCE_END)[0])
                self.assertEqual(first["resolved"], [])
                self.assertEqual(set(first["live"]), {"scenario-a", "scenario-b"})
                repeat_prompt = [
                    prompt for _owner, prompt in runner.prompts if "REPEAT REVIEW" in prompt
                ]
                self.assertEqual(len(repeat_prompt), 1)
                # The repeat scores against the first run's sealed record.
                self.assertIn(f"stage {first_stage!r}", repeat_prompt[0])
                self.assertIn('["scenario-a", "scenario-b"]', repeat_prompt[0])

    def test_a23_the_verdict_rule_and_prior_selection_are_functions_of_the_history(self):
        # Four fixtures over a single-prior sealed history.
        self.assertEqual(
            convergence_verdict({"A", "B"}, set(), {"A", "C"}), "stalled"
        )
        self.assertEqual(convergence_verdict({"A", "B"}, set(), {"A"}), "improved")
        self.assertEqual(convergence_verdict({"A", "B"}, {"A"}, {"A"}), "oscillating")

        controller, _runner = self.controller(
            [
                _output("applied"),
                _output("needs_repair", _first_run_record(["A", "B"])),
                _output("repaired"),
                # Declares `improved` for the first fixture's history: rejected.
                _output("ready", _repeat_record(["A", "C"], ["A", "B"], [], verdict="improved")),
            ]
        )
        task_id = controller.submit("apply", APPLY_PROFILE, self.envelope_input("a23-contra.md"))
        status = controller.run_until_stop(task_id)
        self.assertEqual(status["task"]["stop_reason"], HOLD_STOP_REASON)

        # Two more over three branching runs, differing only in whether a
        # correction run follows R2.
        profile = self.prior_selection_profile()
        for name, follows_correction in (("with-correction", True), ("without-correction", False)):
            with self.subTest(case=name):
                r3_prior = ["A"] if follows_correction else ["A", "B"]
                r3_verdict = "stalled" if follows_correction else "improved"
                outputs = [
                    _output("fixed"),
                    _output("back", _first_run_record(["A", "B"])),          # R1
                    _output("fixed"),
                    _output(
                        "back" if follows_correction else "again",
                        _repeat_record(["A"], ["A", "B"], []),               # R2, prior R1
                    ),
                ]
                if follows_correction:
                    outputs.append(_output("fixed"))
                outputs.append(
                    _output("settled", _repeat_record(["A"], r3_prior, ["B"], verdict=r3_verdict))
                )
                controller, _runner = self.controller(outputs)
                task_id = controller.submit(
                    "demo-loop", profile, self.envelope_input(f"a23-{name}.md")
                )

                status = controller.run_until_stop(task_id)
                gate_runs = [row for row in status["stage_runs"] if row["stage"] == "gate"]
                r3_manifest = json.loads(
                    Path(gate_runs[-1]["manifest_path"]).read_text(encoding="utf-8")
                )

                if follows_correction:
                    # Prior is R2, so the single accepted verdict is `stalled`,
                    # which holds rather than continuing.
                    self.assertEqual(status["task"]["stop_reason"], HOLD_STOP_REASON)
                    self.assertIn("convergence_stalled", r3_manifest["reason"])
                else:
                    # Prior is R1, the verdict is `improved`, and the run takes
                    # its ordinary profile path.
                    self.assertEqual(status["task"]["status"], "done", status["task"]["stop_reason"])
                    self.assertEqual(r3_manifest["outcome"], "settled")

                # The opposite prior would have produced the opposite verdict,
                # so this history discriminates the selection rule.
                other_prior = {"A", "B"} if follows_correction else {"A"}
                self.assertNotEqual(
                    convergence_verdict(other_prior, {"B"}, {"A"}),
                    convergence_verdict(set(r3_prior), {"B"}, {"A"}),
                )

                rejected = [
                    _output("fixed"),
                    _output("back", _first_run_record(["A", "B"])),
                    _output("fixed"),
                    _output(
                        "back" if follows_correction else "again",
                        _repeat_record(["A"], ["A", "B"], []),
                    ),
                ]
                if follows_correction:
                    rejected.append(_output("fixed"))
                wrong = "improved" if follows_correction else "stalled"
                rejected.append(
                    _output(
                        "settled",
                        _repeat_record(["A"], list(other_prior), ["B"], verdict=wrong),
                    )
                )
                controller, _runner = self.controller(rejected)
                task_id = controller.submit(
                    "demo-loop", profile, self.envelope_input(f"a23-{name}-wrong.md")
                )
                held = controller.run_until_stop(task_id)
                self.assertEqual(held["task"]["stop_reason"], HOLD_STOP_REASON)
                wrong_manifest = json.loads(
                    Path(
                        [row for row in held["stage_runs"] if row["stage"] == "gate"][-1][
                            "manifest_path"
                        ]
                    ).read_text(encoding="utf-8")
                )
                self.assertIn("convergence_contradictory", wrong_manifest["reason"])


class StopGateReuseTests(EnvelopeFixture):
    def synced_gate_pending(self, home: Path):
        spec = self.root / "approved-spec.md"
        spec.write_text("Status: approved\n", encoding="utf-8")
        worktree = self.root / "gate-worktree"
        worktree.mkdir(exist_ok=True)
        scope = DECLARED_SCOPE + "This deploys the orchestrator/daemon change.\n"
        with patch("orchestrator.start._invoke_resolver", side_effect=lambda prompt: scripted_reply(prompt)):
            started = run_start(
                home,
                "apply and deploy changes to orchestrator/daemon flow",
                StartFlags("apply", scope, worktree, spec, "high", False),
            )
            approved = run_start_go(home, started["task_id"])
        request_id = approved["routing"]["execution"]["request_id"]
        artifact_dir = home / "tasks" / "controller-task-gate"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        data = (home / "tasks" / f"{started['task_id']}-execution-input.md").read_bytes()
        snapshot = artifact_dir / "input.snapshot.md"
        snapshot.write_bytes(data)
        evidence_path = artifact_dir / "evidence.json"
        evidence_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "task_id": "controller-task-gate",
                    "input_snapshot_path": str(snapshot),
                    "input_hash": hashlib.sha256(data).hexdigest(),
                }
            ),
            encoding="utf-8",
        )
        processed = home / "processed"
        processed.mkdir(parents=True, exist_ok=True)
        (processed / f"20260101-000000-{request_id}.fake.result.json").write_text(
            json.dumps(
                {
                    "request_id": request_id,
                    "task_id": "controller-task-gate",
                    "status": "done",
                    "stop_reason": "terminal_done",
                    "evidence_path": str(evidence_path),
                }
            ),
            encoding="utf-8",
        )
        synced = run_start_sync(home, started["task_id"])
        self.assertTrue(synced["routing"]["execution_result"]["gate_required"])
        return synced, snapshot

    def test_a16_routing_has_no_envelope_and_the_gate_block_is_byte_identical(self):
        home = self.root / "a16"
        with patch("orchestrator.start.daemon_is_running", return_value=True):
            synced, snapshot = self.synced_gate_pending(home)
            routing = _read_yaml(home / "tasks" / f"{synced['task_id']}-routing.yaml")

            self.assertEqual([key for key in routing if "envelope" in key], [])
            rendered = json.dumps(routing)
            self.assertNotIn(ENVELOPE_BEGIN, rendered)
            for axis in ENVELOPE_AXES:
                self.assertNotIn(axis, rendered)

            run_gate_run(home, synced["task_id"])

        gate_input = (home / "tasks" / f"{synced['task_id']}-gate-review-input.md").read_text(
            encoding="utf-8"
        )
        verified = envelope_block_text(snapshot.read_text(encoding="utf-8"))
        copied = envelope_block_text(gate_input)

        self.assertIsNotNone(copied)
        self.assertEqual(copied.encode("utf-8"), verified.encode("utf-8"))

    def test_a16_a_tampered_controller_snapshot_stops_the_gate(self):
        home = self.root / "a16-tampered"
        with patch("orchestrator.start.daemon_is_running", return_value=True):
            synced, snapshot = self.synced_gate_pending(home)
            snapshot.write_text(snapshot.read_text(encoding="utf-8") + "\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                run_gate_run(home, synced["task_id"])

    def test_a17_a_held_gate_reviewer_leaves_the_gate_pending_and_decidable(self):
        home = self.root / "a17"
        with patch("orchestrator.start.daemon_is_running", return_value=True):
            synced, _snapshot = self.synced_gate_pending(home)
            enqueued = run_gate_run(home, synced["task_id"])
            review_request_id = enqueued["routing"]["gate_review_execution"]["request_id"]
            (home / "processed" / f"20260101-000001-{review_request_id}.fake.result.json").write_text(
                json.dumps(
                    {
                        "request_id": review_request_id,
                        "task_id": "controller-task-review",
                        "status": "waiting_user",
                        "stop_reason": HOLD_STOP_REASON,
                        "stage_runs": [],
                        "transitions": [],
                    }
                ),
                encoding="utf-8",
            )

            result = run_gate_sync(home, synced["task_id"])

        self.assertEqual(result["status"], "waiting_user")
        status = gate_status(home, synced["task_id"])
        self.assertEqual(status["gate"]["status"], "pending")
        self.assertIsNone(status["gate_review_result"])
        self.assertIsNone(status["decision"])
        self.assertNotIn("review_result", status["gate"])
        self.assertEqual(
            status["gate"]["review_pending"]["stop_reason"], HOLD_STOP_REASON
        )
        self.assertNotIn("ALLOW", json.dumps(status["gate"]["review_pending"]))
        self.assertNotIn("BLOCK", json.dumps(status["gate"]["review_pending"]))
        # Still decidable: gate-allow / gate-block remain available.
        from orchestrator.start import run_gate_decision

        decided = run_gate_decision(home, synced["task_id"], "BLOCK", "held for a user decision")
        self.assertEqual(decided["routing"]["gate_decision"]["decision"], "BLOCK")


if __name__ == "__main__":
    unittest.main()
