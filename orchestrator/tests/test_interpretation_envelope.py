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
from orchestrator.doctor import run_doctor
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
    resolver_isolation_option_names,
    resolver_isolation_support,
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
IMPLEMENT_PROFILE = PROFILES / "codex_implement_claude_review.yaml"
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

    # --- A1: a faithful semantic summary is a valid member ------------------
    #
    # Regression for the two production intake failures the follow-up brief
    # names: tasks `d73d31a3` and `6b84744d` both stopped at `waiting_user`
    # with a genuine, verbatim quote and a faithful summary of it, because the
    # engine additionally required the *member* to occur lexically inside its
    # own quote. Semantic mapping is the resolver's job; the deterministic
    # side grounds the quote, not the reading of it.

    def test_a1_a_faithful_summary_of_its_own_quote_resolves(self):
        # `6b84744d`'s shape: an assurance_ceiling member that summarises a
        # long sentence instead of repeating a span of it.
        home = self.root / "a1-summary"
        scope = (
            "Repair stages use focused tests; full-suite, live-provider, deployment, and "
            "repository hygiene evidence run once on the final ready candidate with explicit "
            "ownership.\n"
            "Change the greeting text emitted by the demo stage, and write only work/greeting.txt.\n"
        )
        result = self.start(
            home,
            "apply the greeting change",
            scope,
            reply=_reply(
                semantic_change_surface=_axis(
                    "semantically_silent", ["the greeting text emitted by the demo stage"]
                ),
                task_owned_write_targets=_axis(
                    "declared", ["work/greeting.txt"], ["write only work/greeting.txt"]
                ),
                assurance_ceiling=_axis(
                    "declared",
                    ["focused tests at repair stages"],
                    [
                        "Repair stages use focused tests; full-suite, live-provider, deployment, "
                        "and repository hygiene evidence run once on the final ready candidate "
                        "with explicit ownership."
                    ],
                ),
            ),
        )

        self.assertEqual(result["routing"]["preflight"]["status"], "pass")
        self.assertNotEqual(result["status"], "waiting_user")
        envelope = self.rendered_envelope(home, result["task_id"])
        self.assertEqual(envelope["assurance_ceiling"]["state"], "declared")
        self.assertEqual(
            envelope["assurance_ceiling"]["value"], ["focused tests at repair stages"]
        )
        self.assertFalse(envelope["assurance_ceiling"]["default_applied"])

    def test_a1_a_summary_in_another_language_than_its_quote_resolves(self):
        # `d73d31a3`'s shape: Chinese sources, an English member. Lexical
        # identity between the two is impossible in principle, not merely
        # inconvenient.
        home = self.root / "a1-cross-language"
        scope = (
            "讓 envelope task 的 blocking finding 受 Interpretation Envelope 約束。\n"
            "只修改 orchestrator/controller.py 這個檔案。\n"
        )
        result = self.start(
            home,
            "apply a change to envelope blocking findings",
            scope,
            reply=_reply(
                semantic_change_surface=_axis(
                    "declared",
                    ["envelope task blocking finding constrained by Interpretation Envelope"],
                    ["讓 envelope task 的 blocking finding 受 Interpretation Envelope 約束"],
                ),
                task_owned_write_targets=_axis(
                    "declared",
                    ["orchestrator/controller.py"],
                    ["只修改 orchestrator/controller.py 這個檔案"],
                ),
            ),
        )

        self.assertEqual(result["routing"]["preflight"]["status"], "pass")
        self.assertNotEqual(result["status"], "waiting_user")
        envelope = self.rendered_envelope(home, result["task_id"])
        self.assertEqual(
            envelope["semantic_change_surface"]["value"],
            ["envelope task blocking finding constrained by Interpretation Envelope"],
        )

    def test_a1_a_write_target_its_quote_does_not_spell_resolves(self):
        # The write axis is no longer exempt from the same rule. The sources
        # describe the change without spelling the file that carries it; the
        # resolver maps one to the other, and the path is still normalised
        # against the execution worktree and still bounded at execution time.
        home = self.root / "a1-write-mapping"
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
                    "declared",
                    ["orchestrator/controller.py"],
                    ["change stop-gate behaviour so a held gate stays pending"],
                ),
            ),
        )

        self.assertEqual(result["routing"]["preflight"]["status"], "pass")
        self.assertNotEqual(result["status"], "waiting_user")
        envelope = self.rendered_envelope(home, result["task_id"])
        targets = envelope["task_owned_write_targets"]
        self.assertEqual(targets["state"], "declared")
        self.assertTrue(
            any(path.endswith("orchestrator/controller.py") for path in targets["value"]),
            targets["value"],
        )
        # Normalised against the worktree, exactly as a spelled-out path is.
        self.assertTrue(
            all(path.startswith("/") for path in targets["value"]), targets["value"]
        )

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

    def test_a6_intake_spawns_nothing_for_capability_evidence(self):
        # The capability probe is an operator-invoked read, not a per-task
        # provider call: one full intake must reach the resolver exactly once
        # and never reach `resolver_isolation_support`. Patched at the module
        # attribute, so any intake-side caller would be counted.
        probes: list[int] = []
        with patch.object(
            orchestrator.start,
            "resolver_isolation_support",
            side_effect=lambda: probes.append(1),
        ):
            with patch.object(orchestrator.start.subprocess, "run") as spawn:
                result, calls = self.start(self.root / "no-probe")

        self.assertEqual(result["status"], "execute")
        self.assertEqual(len(calls), 1)
        self.assertEqual(probes, [])
        # `_invoke_resolver` is stubbed by `start`, so the only way a real
        # process could be spawned during intake is a second, unstubbed caller.
        self.assertEqual(spawn.call_args_list, [])

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
    # The cases below are the ones a resolver gets wrong by being helpful
    # rather than by being broken: it answers the question it thinks was
    # asked. Each presents a well-framed, schema-valid, individually grounded
    # reply, and each must still be refused. What is *not* refused any more is
    # a member that faithfully summarises its own quote instead of repeating
    # it — see `SemanticMemberTests`.

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

        # Nor a lexical-identity check on the member under any name: reading
        # the member back out of its own quote is the same deterministic
        # natural-language reader, and it is gone from every axis.
        self.assertFalse(hasattr(orchestrator.start, "_carried_by"))

        # What remains is exact grounding of the *quote* in the sources, plus
        # path normalisation for a declared write target. Neither reads prose.
        blob = orchestrator.start._normalise_for_grounding(
            "修改 demo stage 的問候文字，只寫 work/greeting.txt 這個檔案。"
        )
        self.assertTrue(orchestrator.start._grounded("只寫 work/greeting.txt 這個檔案", blob))
        self.assertFalse(orchestrator.start._grounded("rewrite the daemon lease protocol", blob))
        self.assertEqual(
            orchestrator.start._normalise_write_target("work/greeting.txt", "/tmp/wt"),
            "/tmp/wt/work/greeting.txt",
        )
        self.assertIsNone(orchestrator.start._normalise_write_target("the demo stage", None))

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


#: A stand-in CLI that records every argv it is given and answers `--version`
#: and `--help`. No provider and no network are involved; what is under test is
#: which process the engine asks about its options, and what it does with the
#: answer.
FAKE_CLI_SOURCE = """\
#!/usr/bin/env python3
import sys
from pathlib import Path

Path(__file__).with_name("argv.log").open("a", encoding="utf-8").write(
    "\\x00".join(sys.argv[1:]) + "\\n"
)

OPTIONS = {options!r}
argv = sys.argv[1:]
if "--version" in argv:
    print("9.9.9 (Fake Claude Code)")
    sys.exit(0)
if "--help" in argv:
    print("Usage: fake-claude [options]")
    for option in OPTIONS:
        print("  " + option + " <value>   an option")
    sys.exit(0)
sys.exit(0)
"""


class ResolverCapabilityEvidenceTests(unittest.TestCase):
    """A6 — the resolver isolation flags are checked, locally and once.

    `_resolver_command` asserts four Claude CLI options exist. Nothing else
    reads the CLI's option surface, so a rename would fail every intake with a
    provider exit code and no statement of which capability vanished. These
    tests observe the probe's argv, its verdict, and the `orch doctor` check
    that carries it.
    """

    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="orch-capability-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.directory, ignore_errors=True))
        self.log = self.directory / "argv.log"

    def write_cli(self, options) -> Path:
        script = self.directory / "fake-claude.py"
        script.write_text(FAKE_CLI_SOURCE.format(options=list(options)), encoding="utf-8")
        script.chmod(0o755)
        return script

    def recorded_argv(self) -> list[list[str]]:
        if not self.log.is_file():
            return []
        return [
            line.split("\x00")
            for line in self.log.read_text(encoding="utf-8").splitlines()
            if line
        ]

    def test_a6_the_whole_configured_command_is_asked_not_its_interpreter(self):
        # A wrapper invocation has more than one element. What must be asked
        # about its options is the wrapper, with the configured flags intact
        # and `--help` where the resolver appends its own flags — never
        # `command[0]` alone, which here is the Python interpreter.
        script = self.write_cli(resolver_isolation_option_names())
        command = f"{sys.executable} {script} --model fake"
        with patch.dict(os.environ, {"ORCH_CLAUDE_COMMAND": command}, clear=False):
            support = resolver_isolation_support()

        self.assertEqual(
            list(support.command), [sys.executable, str(script), "--model", "fake", "--help"]
        )
        # The wrapper received the flags and `--help`; the interpreter was only
        # the thing that ran it.
        self.assertEqual(self.recorded_argv(), [["--model", "fake", "--help"]])
        self.assertTrue(support.verified)
        self.assertEqual(support.missing, ())

    def test_a6_a_renamed_option_is_reported_by_name_and_fails_doctor(self):
        options = [
            option for option in resolver_isolation_option_names() if option != "--strict-mcp-config"
        ]
        script = self.write_cli(options)
        with patch.dict(os.environ, {"ORCH_CLAUDE_COMMAND": str(script)}, clear=False):
            support = resolver_isolation_support()
            report = run_doctor(self.directory / "home")

        self.assertTrue(support.verified)
        self.assertEqual(support.missing, ("--strict-mcp-config",))

        check = {item["check"]: item for item in report["checks"]}["resolver_isolation"]
        self.assertEqual(check["status"], "fail")
        self.assertIn("--strict-mcp-config", check["detail"])
        for present in ("--tools", "--setting-sources", "--no-session-persistence"):
            self.assertNotIn(present, check["detail"])
        # `orch doctor` exits 1 on any fail, so a provisioning script gates on
        # this with no new plumbing.
        self.assertGreaterEqual(report["summary"]["fail"], 1)
        self.assertEqual(1 if report["summary"]["fail"] else 0, 1)

    def test_a6_a_complete_cli_is_ok_and_costs_no_second_version_spawn(self):
        script = self.write_cli(resolver_isolation_option_names())
        with patch.dict(os.environ, {"ORCH_CLAUDE_COMMAND": str(script)}, clear=False):
            report = run_doctor(self.directory / "home")

        by_name = {item["check"]: item for item in report["checks"]}
        self.assertEqual(by_name["provider_claude"]["status"], "ok")
        check = by_name["resolver_isolation"]
        self.assertEqual(check["status"], "ok")
        # The version comes from the `provider_claude` preflight that already
        # ran, not from a second `--version`.
        self.assertIn("9.9.9 (Fake Claude Code)", check["detail"])
        argv = self.recorded_argv()
        self.assertEqual([call for call in argv if call == ["--version"]], [["--version"]])
        self.assertEqual([call for call in argv if call == ["--help"]], [["--help"]])
        self.assertEqual(len(argv), 2)

    def test_a6_every_probe_failure_is_unverified_rather_than_raised(self):
        cases = {
            "": "unusable",
            'claude -p "unbalanced': "unusable",
            str(self.directory / "not-installed"): "unavailable",
        }
        for command, expected in cases.items():
            with self.subTest(command=command):
                with patch.dict(os.environ, {"ORCH_CLAUDE_COMMAND": command}, clear=False):
                    support = resolver_isolation_support()  # must not raise
                self.assertFalse(support.verified)
                self.assertEqual(support.missing, resolver_isolation_option_names())
                self.assertIn(expected, support.detail)

        # A CLI that answers, but not successfully, is unverified too.
        failing = self.directory / "failing-claude.py"
        failing.write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(2)\n", encoding="utf-8")
        failing.chmod(0o755)
        with patch.dict(os.environ, {"ORCH_CLAUDE_COMMAND": str(failing)}, clear=False):
            support = resolver_isolation_support()
        self.assertFalse(support.verified)
        self.assertEqual(support.missing, resolver_isolation_option_names())
        self.assertIn("exited 2", support.detail)

    def test_a6_the_probe_reports_no_option_present_by_accident(self):
        # A help text mentioning a longer option that merely starts with one of
        # ours must not be read as that option being present.
        script = self.write_cli(["--toolsets", "--strict-mcp-configuration"])
        with patch.dict(os.environ, {"ORCH_CLAUDE_COMMAND": str(script)}, clear=False):
            support = resolver_isolation_support()

        self.assertTrue(support.verified)
        self.assertEqual(support.missing, resolver_isolation_option_names())


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

    def test_a5_the_hold_outcome_is_named_in_exactly_one_shipped_profile(self):
        # A5 used to assert that `git diff HEAD -- orchestrator/profiles` was
        # empty, which made it fail for *any* profile edit ever, for any
        # reason — a self-invalidating check, not an invariant. Its real
        # invariant, "the envelope feature needs no profile change", is
        # carried by A6 (a legacy prompt is byte-identical) and A7 (the hold
        # outcome is accepted on a profile that never declared it). What is
        # durable is its reserved-name half: the hold outcome is engine-owned,
        # so no shipped profile may declare it as an ordinary stage outcome.
        naming = sorted(
            path.name
            for path in PROFILES.glob("*.yaml")
            if HOLD_OUTCOME in path.read_text(encoding="utf-8")
        )

        self.assertEqual(naming, ["propose.yaml"])
        # And only there is it a routed stage outcome. Everywhere else the
        # engine supplies it for envelope tasks and short-circuits it before
        # the outcome-to-target lookup, which is what A7 pins.
        declaring = sorted(
            path.name
            for path in PROFILES.glob("*.yaml")
            for stage in load_profile(path).stages.values()
            if HOLD_OUTCOME in (stage.outcomes or {})
        )
        self.assertEqual(declaring, ["propose.yaml"])

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

    def test_a7_the_blocking_finding_rule_reaches_every_envelope_stage(self):
        # Engine-side, so it is profile-independent: it reaches a profile that
        # declares neither the hold outcome nor any finding vocabulary, and it
        # reaches no legacy task at all.
        stage = load_profile(STOP_GATE_CODEX_PROFILE).stage("review")
        input_text = self.envelope_input("a7.md").read_text(encoding="utf-8")
        envelope = extract_envelope(input_text)

        composed = Controller._build_prompt(
            "a7-1", stage, input_text, "/tmp/reports", envelope, None
        )
        legacy = Controller._build_prompt(
            "a7-2", stage, self.legacy_input("a7-legacy.md").read_text(encoding="utf-8"),
            "/tmp/reports",
        )

        self.assertIn("Blocking-finding rule:", composed)
        self.assertIn(HOLD_OUTCOME, composed.split("Allowed typed outcomes:", 1)[1])
        self.assertNotIn("Blocking-finding rule:", legacy)

        # The counter-example the rule exists for: a finding that is compliant
        # on the axis it was filed under but whose correction escapes another
        # axis is still not repairable on the executor's own authority. So the
        # rule has to bind all five set axes by name, not just the filed one.
        rule = " ".join(
            composed.split("Blocking-finding rule:", 1)[1]
            .split("\nStage instructions:", 1)[0]
            .split()
        )
        for axis in ENVELOPE_SET_AXES:
            with self.subTest(axis=axis):
                self.assertIn(axis, rule)
        self.assertIn("all five set axes", rule)
        self.assertIn("including one it was", rule)
        self.assertIn("not filed under", rule)
        self.assertIn("is not a blocking finding for automatic repair", rule)
        self.assertIn(HOLD_OUTCOME, rule)
        self.assertIn("A containment statement you cannot make for all five set axes", rule)

    def test_a13_a_legacy_task_still_stops_for_edge_cap(self):
        controller, _runner = self.controller(
            [_output("submit"), _output("block"), _output("submit"), _output("block")]
        )
        task_id = controller.submit("demo-loop", DEMO_PROFILE, self.legacy_input("a13.md"))

        status = controller.run_until_stop(task_id)

        self.assertEqual(status["task"]["status"], "waiting_user")
        self.assertEqual(status["task"]["stop_reason"], "edge_cap")
        self.assertEqual(self.edge(status, "review.block")["count"], 1)

    #: A gate-reachable loop with generous edge caps, so `max_transitions` is
    #: the only cap that can bind. Every cycle (fix -> gate -> fix) carries
    #: both a correction stage and a branching stage, which is exactly what
    #: `_gate_reachable` proves.
    GATED_LOOP_PROFILE = """version: 1
type: demo-loop
initial_stage: fix
max_transitions: 2
stages:
  fix:
    owner: claude
    attempt_cap: 9
    timeout: 30
    prompt: "Correction stage."
    outcomes:
      fixed: gate
  gate:
    owner: codex
    attempt_cap: 9
    timeout: 30
    prompt: "Branching stage."
    outcomes:
      back: fix
      settled: done
  done:
    terminal: done
edge_caps:
  fix.fixed: 9
  gate.back: 9
  gate.settled: 1
"""

    def gated_loop_profile(self, name: str = "gated-loop.yaml") -> Path:
        path = self.root / name
        path.write_text(self.GATED_LOOP_PROFILE, encoding="utf-8")
        return path

    def test_a3_an_improving_envelope_task_continues_past_both_legacy_caps(self):
        # claude_apply_codex_review: `delta_review.needs_repair` is capped at 1,
        # `repair.repaired` at 2 and `max_transitions` is 6. This run crosses
        # all three and still reaches `done`, because what gates it is the
        # strictly shrinking identity set, not a number.
        _controller, _runner, _task_id, status = self.run_task(
            [
                _output("applied"),
                _output("needs_repair", _first_run_record(["a", "b", "c", "d"])),
                _output("repaired"),
                _output("needs_repair", _repeat_record(["a", "b", "c"], ["a", "b", "c", "d"], [])),
                _output("repaired"),
                _output("needs_repair", _repeat_record(["a", "b"], ["a", "b", "c"], ["d"])),
                _output("repaired"),
                _output("needs_repair", _repeat_record(["a"], ["a", "b"], ["c", "d"])),
                _output("repaired"),
                _output("ready", _repeat_record([], ["a"], ["b", "c", "d"])),
            ]
        )

        self.assertEqual(status["task"]["status"], "done")
        stops = [row["reason"] for row in status["transitions"]]
        self.assertNotIn("edge_cap", stops)
        self.assertNotIn("transition_cap", stops)
        # The counters counted the whole way past their caps and stay readable.
        delta = self.edge(status, "delta_review.needs_repair")
        self.assertEqual((delta["count"], delta["cap"]), (3, 1))
        repaired = self.edge(status, "repair.repaired")
        self.assertEqual((repaired["count"], repaired["cap"]), (4, 2))
        self.assertGreater(status["task"]["transitions_count"], 6)

    def test_a3_a_large_first_identity_set_is_not_a_stop_condition(self):
        # No number is a ceiling: 33 findings in the first round is converging
        # work, and the only thing that has to happen is that the set shrinks.
        first = [f"scenario-{index:02d}" for index in range(33)]
        _controller, _runner, _task_id, status = self.run_task(
            [
                _output("applied"),
                _output("needs_repair", _first_run_record(first)),
                _output("repaired"),
                _output("ready", _repeat_record([], first, [])),
            ]
        )

        self.assertEqual(status["task"]["status"], "done")
        self.assertNotIn("edge_cap", [row["reason"] for row in status["transitions"]])

    def test_a4_a_non_improving_envelope_task_still_stops(self):
        _controller, _runner, _task_id, status = self.run_task(
            [
                _output("applied"),
                _output("needs_repair", _first_run_record(["a", "b", "c", "d"])),
                _output("repaired"),
                _output(
                    "needs_repair",
                    _repeat_record(
                        ["a", "b", "c", "d"], ["a", "b", "c", "d"], [], verdict="stalled"
                    ),
                ),
            ]
        )

        self.assertEqual(status["task"]["status"], "waiting_user")
        self.assertEqual(status["task"]["stop_reason"], HOLD_STOP_REASON)
        self.assertNotEqual(status["task"]["stop_reason"], "edge_cap")
        # The hold is the last thing that happened: no stage ran after it.
        self.assertEqual(len(status["stage_runs"]), 4)
        self.assertEqual(status["transitions"][-1]["reason"], HOLD_STOP_REASON)

    #: The three graph shapes convergence cannot score. Each is a permanent
    #: loop the moment a cap stops rejecting, so each must keep its caps even
    #: with an envelope present.
    UNSCORED_PROFILES = {
        # (i) a feedback cycle of single-outcome stages: no branching stage is
        # ever entered, so no convergence record is ever owed.
        "single_outcome_cycle": """version: 1
type: demo-loop
initial_stage: step
max_transitions: 2
stages:
  step:
    owner: claude
    attempt_cap: 9
    timeout: 30
    prompt: "Correction stage."
    outcomes:
      next: back
  back:
    owner: codex
    attempt_cap: 9
    timeout: 30
    prompt: "Correction stage."
    outcomes:
      again: step
  done:
    terminal: done
edge_caps:
  step.next: 9
  back.again: 9
""",
        # (ii) a branching self-loop: the gate is reached every time, and no
        # correction run ever lies between two visits, so no visit is a repeat.
        "branching_self_loop": """version: 1
type: demo-loop
initial_stage: gate
max_transitions: 2
stages:
  gate:
    owner: codex
    attempt_cap: 9
    timeout: 30
    prompt: "Branching stage."
    outcomes:
      again: gate
      settled: done
  done:
    terminal: done
edge_caps:
  gate.again: 9
  gate.settled: 1
""",
        # (iii) a branching-only multi-stage cycle: same defect, spread over
        # two stages so it cannot be recognised by looking at one of them.
        "branching_only_cycle": """version: 1
type: demo-loop
initial_stage: review
max_transitions: 2
stages:
  review:
    owner: codex
    attempt_cap: 9
    timeout: 30
    prompt: "Branching stage."
    outcomes:
      again: triage
      settled: done
  triage:
    owner: claude
    attempt_cap: 9
    timeout: 30
    prompt: "Branching stage."
    outcomes:
      again: review
      settled: done
  done:
    terminal: done
edge_caps:
  review.again: 9
  review.settled: 1
  triage.again: 9
  triage.settled: 1
""",
    }

    def unscored_profile(self, name: str) -> Path:
        path = self.root / f"unscored-{name}.yaml"
        path.write_text(self.UNSCORED_PROFILES[name], encoding="utf-8")
        return path

    def test_a4b_an_unscored_cycle_keeps_its_caps(self):
        cases = {
            "single_outcome_cycle": [_output("next"), _output("again")],
            "branching_self_loop": [
                _output("again", _first_run_record(["a"])),
                _output("again", _first_run_record(["a"])),
            ],
            "branching_only_cycle": [
                _output("again", _first_run_record(["a"])),
                _output("again", _first_run_record(["a"])),
            ],
        }
        for name, outputs in cases.items():
            with self.subTest(shape=name):
                profile_path = self.unscored_profile(name)
                self.assertFalse(
                    Controller._gate_reachable(
                        Controller.__new__(Controller), load_profile(profile_path)
                    )
                )
                controller, runner = self.controller(outputs)
                task_id = controller.submit(
                    "demo-loop", profile_path, self.envelope_input(f"a4b-{name}.md")
                )

                status = controller.run_until_stop(task_id)

                self.assertEqual(status["task"]["status"], "waiting_user")
                self.assertEqual(status["task"]["stop_reason"], "transition_cap")
                self.assertEqual(status["task"]["transitions_count"], 2)
                if name != "single_outcome_cycle":
                    # Why the caps must stay: every visit files a *first-run*
                    # record, so no strict-subset comparison is ever made and
                    # convergence never becomes the bound.
                    for _owner, prompt in runner.prompts:
                        self.assertIn("Convergence record obligation", prompt)
                        self.assertNotIn("REPEAT REVIEW", prompt)

    def test_a4b_a_resumed_in_flight_task_meets_the_same_predicate(self):
        # The predicate is read from the task's own frozen profile snapshot at
        # every claim, so there is no submit-time gate an in-flight task can
        # miss and no migration to run. A task started before this release and
        # resumed after it stops exactly where it stopped before.
        profile_path = self.unscored_profile("single_outcome_cycle")
        home = self.root / "resumed-in-flight"
        controller = Controller(home, runner=ScriptedRunner([]))
        task_id = controller.submit("demo-loop", profile_path, self.envelope_input("a4b-resume.md"))
        # One claimed and committed leg, so the row, the frozen snapshots and
        # the counters all exist before the predicate is ever consulted.
        run_token, _stage, profile, log_path = controller.claim_stage(task_id)
        output = _output("next")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(output, encoding="utf-8")
        controller.commit_run(
            task_id, run_token, RunResult(0, output, "next", "success", "success"), profile
        )
        self.assertEqual(controller.status(task_id)["task"]["transitions_count"], 1)
        controller.close()

        resumed = Controller(home, runner=ScriptedRunner([_output("again")]))
        self.addCleanup(resumed.close)
        status = resumed.run_until_stop(task_id)

        self.assertEqual(status["task"]["status"], "waiting_user")
        self.assertEqual(status["task"]["stop_reason"], "transition_cap")

    def test_a5_a_legacy_task_still_stops_for_transition_cap(self):
        # Same gate-reachable profile as the envelope task below, so the only
        # difference between stopping and continuing is the envelope block.
        profile_path = self.gated_loop_profile()
        controller, _runner = self.controller([_output("fixed"), _output("back")])
        task_id = controller.submit("demo-loop", profile_path, self.legacy_input("a5-legacy.md"))

        status = controller.run_until_stop(task_id)

        self.assertEqual(status["task"]["status"], "waiting_user")
        self.assertEqual(status["task"]["stop_reason"], "transition_cap")
        self.assertEqual(status["task"]["transitions_count"], 2)

    def test_a5_the_same_profile_lifts_only_for_the_envelope_task(self):
        profile_path = self.gated_loop_profile("gated-loop-envelope.yaml")
        controller, _runner = self.controller(
            [
                _output("fixed"),
                _output("back", _first_run_record(["a", "b"])),
                _output("fixed"),
                _output("back", _repeat_record(["a"], ["a", "b"], [])),
                _output("fixed"),
                _output("settled", _repeat_record([], ["a"], ["b"])),
            ]
        )
        task_id = controller.submit(
            "demo-loop", profile_path, self.envelope_input("a5-envelope.md")
        )

        status = controller.run_until_stop(task_id)

        self.assertEqual(status["task"]["status"], "done")
        self.assertEqual(status["task"]["transitions_count"], 6)
        self.assertNotIn(
            "transition_cap", [row["reason"] for row in status["transitions"]]
        )

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


#: The two phrases the C5 gate paragraph is delimited by inside a review
#: prompt. Tests read the gate out of the shipped profile rather than
#: restating it, so a silent edit to either copy is a failure.
GATE_BEGIN = "ready additionally requires that the full test suite"
GATE_END = "If Product/Spec alignment and Platform constraints PASS,"
FINGERPRINT_BEGIN = "The candidate fingerprint is the output of exactly this command, run in the workspace: "
FINGERPRINT_END = " — it is path- and content-sensitive"


def _gate_paragraph(prompt: str) -> str:
    return GATE_BEGIN + prompt.split(GATE_BEGIN, 1)[1].split(GATE_END, 1)[0]


def _fingerprint_command(prompt: str) -> str:
    return prompt.split(FINGERPRINT_BEGIN, 1)[1].split(FINGERPRINT_END, 1)[0].strip()


class ReceiptRunner(ScriptedRunner):
    """A scripted runner that also writes what each stage records.

    The engine neither writes nor parses the receipt, so the artifact has to
    come from the stages themselves — which is exactly the property under
    test: the gate is a prompt contract on an artifact the review stages
    already own.
    """

    def __init__(self, outputs: list[str], receipts: list[str | None], review_file: str):
        super().__init__(outputs)
        self.receipts = list(receipts)
        self.review_file = review_file

    def run(self, owner: str, prompt: str, timeout: int, log_path: Path, **kwargs) -> RunResult:
        result = super().run(owner, prompt, timeout, log_path, **kwargs)
        note = self.receipts.pop(0) if self.receipts else None
        if note:
            reports = log_path.parent.parent / "reports"
            reports.mkdir(parents=True, exist_ok=True)
            with (reports / self.review_file).open("a", encoding="utf-8") as handle:
                handle.write(note)
        return result


class FinalCandidateEvidenceTests(EnvelopeFixture):
    """A8 / A8b: one final-candidate gate, on both `ready` paths."""

    def receipt_path(self, controller: Controller, task_id: str, review_file: str) -> Path:
        artifact_dir = Path(controller.status(task_id)["task"]["artifact_dir"])
        return artifact_dir / "reports" / review_file

    def test_a8_both_ready_paths_carry_the_same_gate(self):
        for profile_path, review_file in (
            (APPLY_PROFILE, "apply-review.md"),
            (IMPLEMENT_PROFILE, "implement-review.md"),
        ):
            with self.subTest(profile=profile_path.name):
                profile = load_profile(profile_path)
                repair = profile.stage("repair").prompt
                review = profile.stage("review").prompt
                delta = profile.stage("delta_review").prompt

                # Repair stages: focused tests, and the four one-shot classes
                # explicitly deferred to the final candidate.
                self.assertIn(
                    "Run the focused tests covering the symbols you changed and their direct "
                    "consumers.",
                    repair,
                )
                self.assertIn("Do not run the full test suite, a live-provider check, a "
                              "deployment check or a repository-hygiene check at this stage", repair)
                self.assertIn("those belong once to the final ready candidate", repair)
                self.assertNotIn(GATE_BEGIN, repair)
                # And the gate is not appended after the outcome sentinel: the
                # last thing either prompt says is still which token to print.
                self.assertTrue(repair.endswith("ORCHESTRATOR_OUTCOME: repaired"), repair[-60:])
                self.assertTrue(review.endswith("ORCHESTRATOR_OUTCOME: needs_repair"), review[-60:])
                self.assertTrue(delta.endswith("ORCHESTRATOR_OUTCOME: needs_repair"), delta[-60:])

                # A zero-repair run reaches `done` through `review.ready`
                # without ever entering `delta_review`, so gating only the
                # delta path would gate nothing on the commonest path.
                gate = _gate_paragraph(review)
                self.assertEqual(
                    gate.encode("utf-8"), _gate_paragraph(delta).encode("utf-8")
                )
                self.assertGreater(len(gate), 500)

                for phrase in (
                    "any live-provider evidence, any deployment evidence and any "
                    "repository-hygiene evidence have been executed once against this final "
                    "candidate",
                    "A mock, a stub or a fixture may not stand in for live-provider or "
                    "deployment evidence",
                    "Settle Product/Spec alignment and Platform constraints first",
                    "never to inspect one you are about to send back",
                    f"Record each in {review_file} under a '## Final-candidate evidence' heading",
                    "one line per evidence class carrying the candidate fingerprint, then the "
                    "command, its result and its owner",
                    "a line under this candidate's fingerprint is already spent and must not be "
                    "re-run",
                    "a line under any other fingerprint is evidence for a different candidate "
                    "and counts as absent",
                    "Run only the classes with no line under this fingerprint",
                    "within your authority: run them here, and never record them DEFERRED",
                    "ready is not available to you",
                    "include an outcome reserved for a user decision",
                    "record Verification evidence DEFERRED with the outstanding command, owner "
                    "and gate, as today",
                ):
                    with self.subTest(phrase=phrase[:48]):
                        self.assertIn(phrase, gate)

                # The gate names no engine mechanism: no profile outcome is
                # added, and the reserved hold outcome is never spelled, so
                # A5's narrowing still holds and the route stays engine-owned.
                self.assertNotIn(HOLD_OUTCOME, gate)
                self.assertEqual(
                    set(profile.stage("review").outcomes), {"ready", "needs_repair"}
                )

    def test_a8b_the_receipt_survives_the_hold_and_the_resume(self):
        # Envelope task, no repair round: the commonest path to `done`.
        fingerprint = "f1a2b3c4d5e6"
        controller_home = self.root / "a8b-hold"
        runner = ReceiptRunner(
            [
                _output("applied"),
                _output(HOLD_OUTCOME, _first_run_record([])),
                _output("ready", _first_run_record([])),
            ],
            [
                None,
                "## Final-candidate evidence\n"
                f"- full-suite | {fingerprint} | python3 -m unittest discover -s "
                "orchestrator/tests -t . | 0 | reviewer\n"
                f"- hygiene | {fingerprint} | git status --porcelain | clean | reviewer\n"
                "Outstanding, not mine to run: live-provider `orch doctor` (owner: operator); "
                "deployment `orch start --task-type propose` round-trip (owner: operator)\n",
                f"- live-provider | {fingerprint} | orch doctor | exit 0 | operator (recorded)\n",
            ],
            "apply-review.md",
        )
        controller = Controller(controller_home, runner=runner)
        self.addCleanup(controller.close)
        task_id = controller.submit("apply", APPLY_PROFILE, self.envelope_input("a8b.md"))

        held = controller.run_until_stop(task_id)

        self.assertEqual(held["task"]["status"], "waiting_user")
        self.assertEqual(held["task"]["stop_reason"], HOLD_STOP_REASON)
        receipt = self.receipt_path(controller, task_id, "apply-review.md")
        text = receipt.read_text(encoding="utf-8")
        self.assertIn("## Final-candidate evidence", text)
        self.assertIn("orch doctor", text)
        self.assertIn("owner: operator", text)

        # The operator runs the outstanding class and appends its line under
        # this same fingerprint, then resumes onto this same candidate.
        with receipt.open("a", encoding="utf-8") as handle:
            handle.write(
                f"- deployment | {fingerprint} | orch start --task-type propose | done | operator\n"
            )

        status = controller.resume(task_id)

        self.assertEqual(status["task"]["status"], "done")
        final = receipt.read_text(encoding="utf-8")
        for kind in ("full-suite", "hygiene", "live-provider", "deployment"):
            with self.subTest(kind=kind):
                lines = [
                    line
                    for line in final.splitlines()
                    if line.startswith(f"- {kind} ") and fingerprint in line
                ]
                self.assertEqual(len(lines), 1, final)
        # `delta_review` never ran: the gate had to be on `review.ready` too.
        self.assertNotIn("delta_review", [row["stage"] for row in status["stage_runs"]])

    def test_a8b_a_repair_moves_the_fingerprint_so_earlier_lines_read_as_absent(self):
        # Pre-repair lines are stale by construction: the repair changes the
        # worktree, so the candidate fingerprint changes, so the final review
        # must spend each class again under its own fingerprint. That the
        # fingerprint really moves is executed in
        # `FinalCandidateFingerprintTests`; what this pins is the receipt's
        # own reading rule across the repair round.
        before, after = "aaaaaaaaaaaa", "bbbbbbbbbbbb"
        runner = ReceiptRunner(
            [
                _output("applied"),
                _output("needs_repair", _first_run_record(["scenario-a"])),
                _output("repaired"),
                _output("ready", _repeat_record([], ["scenario-a"], [])),
            ],
            [
                None,
                "## Final-candidate evidence\n"
                f"- full-suite | {before} | python3 -m unittest discover | 0 | reviewer\n"
                f"- hygiene | {before} | git status --porcelain | clean | reviewer\n",
                None,
                f"- full-suite | {after} | python3 -m unittest discover | 0 | reviewer\n"
                f"- hygiene | {after} | git status --porcelain | clean | reviewer\n",
            ],
            "apply-review.md",
        )
        controller = Controller(self.root / "a8b-repair", runner=runner)
        self.addCleanup(controller.close)
        task_id = controller.submit("apply", APPLY_PROFILE, self.envelope_input("a8b-repair.md"))

        status = controller.run_until_stop(task_id)

        self.assertEqual(status["task"]["status"], "done")
        final = self.receipt_path(controller, task_id, "apply-review.md").read_text(
            encoding="utf-8"
        )
        for kind in ("full-suite", "hygiene"):
            with self.subTest(kind=kind):
                # Exactly one line per class under the final candidate, and
                # the pre-repair lines are still on record as another
                # candidate's evidence rather than deleted or reused.
                self.assertEqual(
                    len([l for l in final.splitlines() if l.startswith(f"- {kind} ") and after in l]),
                    1,
                    final,
                )
                self.assertEqual(
                    len([l for l in final.splitlines() if l.startswith(f"- {kind} ") and before in l]),
                    1,
                    final,
                )
        self.assertIn("delta_review", [row["stage"] for row in status["stage_runs"]])

    def test_a8b_a_legacy_task_keeps_its_deferred_to_ready_route(self):
        # The user's ruling, adopted verbatim: the final-candidate evidence
        # hold applies only to tasks carrying an envelope. A legacy task is
        # never offered the hold outcome, so the gate's own fallback is
        # today's `PASS or owned DEFERRED -> ready`, with no hold injected
        # and no receipt required of it.
        controller, runner = self.controller([_output("applied"), _output("ready")])
        task_id = controller.submit("apply", APPLY_PROFILE, self.legacy_input("a8b-legacy.md"))

        status = controller.run_until_stop(task_id)

        self.assertEqual(status["task"]["status"], "done")
        for _owner, prompt in runner.prompts:
            self.assertNotIn(HOLD_OUTCOME, prompt)
        self.assertFalse(
            (Path(status["task"]["artifact_dir"]) / "reports" / "apply-review.md").exists()
        )


class FinalCandidateFingerprintTests(unittest.TestCase):
    """The mandated correction to C5, executed rather than asserted.

    `git status --porcelain` is not content-sensitive: changing the contents of
    an already-modified file leaves its text identical, so a fingerprint built
    from it would let one candidate silently inherit another's final evidence.
    These tests run the command the shipped gate actually names.
    """

    GIT = ("git", "-c", "user.email=t@example.invalid", "-c", "user.name=orch-test")

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.repo = Path(directory.name)
        self.env = {
            **os.environ,
            "GIT_CONFIG_GLOBAL": str(self.repo / "no-global-gitconfig"),
            "GIT_CONFIG_SYSTEM": str(self.repo / "no-system-gitconfig"),
            "GIT_TERMINAL_PROMPT": "0",
        }
        self.git("init", "-q", ".")
        (self.repo / "tracked.txt").write_text("original\n", encoding="utf-8")
        (self.repo / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-qm", "seed")
        self.command = _fingerprint_command(load_profile(APPLY_PROFILE).stage("review").prompt)

    def git(self, *args: str) -> str:
        done = subprocess.run(
            [*self.GIT, *args], cwd=self.repo, capture_output=True, text=True, env=self.env
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout

    def fingerprint(self) -> str:
        done = subprocess.run(
            ["sh", "-c", self.command],
            cwd=self.repo,
            capture_output=True,
            text=True,
            env=self.env,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        value = done.stdout.strip()
        self.assertRegex(value, r"^[0-9a-f]{12}$")
        return value

    def fingerprint_run(self) -> subprocess.CompletedProcess:
        """Run the command without demanding success, for the failure cases."""
        return subprocess.run(
            ["sh", "-c", self.command],
            cwd=self.repo,
            capture_output=True,
            text=True,
            env=self.env,
        )

    def porcelain(self) -> str:
        return self.git("status", "--porcelain")

    def test_the_gate_names_a_content_sensitive_command(self):
        self.assertIn("git rev-parse HEAD", self.command)
        self.assertIn("git diff --no-ext-diff --binary HEAD --", self.command)
        self.assertIn("git ls-files --others --exclude-standard -z", self.command)
        # And it says, in the prompt, not to fall back to the defective one.
        prompt = load_profile(APPLY_PROFILE).stage("review").prompt
        self.assertIn("Never substitute git status --porcelain for it", prompt)
        # The command is read-only: no index write, no worktree write.
        self.assertNotIn("hash-object -w", self.command)
        self.assertNotIn("git add", self.command)
        self.assertNotIn("git stash", self.command)
        # Each path reaches a reader only as an operand after an option
        # terminator, so an option-like name cannot be read as a flag.
        self.assertIn("readlink -- ", self.command)
        self.assertIn("cat -- ", self.command)
        self.assertNotIn("xargs", self.command)
        # A failing read or hash has to fail the command, not the byte stream
        # alone: without pipefail the trailing cut would still exit 0.
        self.assertIn("pipefail", self.command)

    def test_a_modified_file_s_contents_change_the_fingerprint(self):
        (self.repo / "tracked.txt").write_text("candidate one\n", encoding="utf-8")
        first_status, first = self.porcelain(), self.fingerprint()
        (self.repo / "tracked.txt").write_text("candidate two\n", encoding="utf-8")
        second_status, second = self.porcelain(), self.fingerprint()

        # This is the defect the correction exists for: identical status text.
        self.assertEqual(first_status, second_status)
        self.assertNotEqual(first, second)

    def test_an_untracked_file_s_contents_change_the_fingerprint(self):
        (self.repo / "new.txt").write_text("added one\n", encoding="utf-8")
        first_status, first = self.porcelain(), self.fingerprint()
        (self.repo / "new.txt").write_text("added two\n", encoding="utf-8")
        second_status, second = self.porcelain(), self.fingerprint()

        self.assertEqual(first_status, second_status)
        self.assertNotEqual(first, second)

    def test_an_option_like_untracked_name_is_hashed_not_parsed(self):
        """`--preserve` is a valid Git path and must not become a flag.

        The first version of this command piped each path into
        `shasum -a 256 {}`, which read this name as an option, printed
        `Unknown option: preserve`, and still let the pipeline exit 0 — so
        the fingerprint never moved when the file's contents did.
        """
        hostile = self.repo / "--preserve"
        hostile.write_text("one\n", encoding="utf-8")
        first_status, first = self.porcelain(), self.fingerprint()
        hostile.write_text("two\n", encoding="utf-8")
        second_status, second = self.porcelain(), self.fingerprint()

        self.assertEqual(first_status, second_status)
        self.assertNotEqual(first, second)
        # And the reader really did see the file, rather than skipping it.
        self.assertNotIn("Unknown option", self.fingerprint_run().stderr)

    def test_an_untracked_symlink_s_link_text_is_its_content(self):
        """A symlink's candidate bytes are the link text, not the target's.

        Both targets hold identical bytes here, so a command that followed
        the link would report the same fingerprint for two different
        candidates.
        """
        (self.repo / "target-a").write_text("same\n", encoding="utf-8")
        (self.repo / "target-b").write_text("same\n", encoding="utf-8")
        link = self.repo / "link"
        link.symlink_to("target-a")
        first = self.fingerprint()
        link.unlink()
        link.symlink_to("target-b")
        second = self.fingerprint()

        self.assertNotEqual(first, second)

    def test_an_unreadable_untracked_path_fails_the_command(self):
        """A partial candidate must not yield a fingerprint at all.

        A silently truncated byte stream would still hash to something, and
        that something would be recorded as this candidate's identity.
        """
        if os.geteuid() == 0:
            self.skipTest("root can read a mode-000 file")
        blocked = self.repo / "blocked.txt"
        blocked.write_text("x\n", encoding="utf-8")
        blocked.chmod(0o000)
        self.addCleanup(blocked.chmod, 0o600)

        done = self.fingerprint_run()

        self.assertNotEqual(done.returncode, 0, done.stdout)

    def test_staging_renaming_deleting_and_ignoring(self):
        clean = self.fingerprint()
        (self.repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
        unstaged = self.fingerprint()
        self.git("add", "tracked.txt")
        # Staging is not a different candidate: the same bytes are the same
        # candidate whether or not the index knows about them.
        self.assertEqual(unstaged, self.fingerprint())
        self.assertNotEqual(clean, unstaged)

        self.git("mv", "tracked.txt", "renamed.txt")
        renamed = self.fingerprint()
        self.assertNotEqual(unstaged, renamed)

        self.git("rm", "-q", "-f", "renamed.txt")
        self.assertNotEqual(renamed, self.fingerprint())

        # An ignored file is not part of the candidate.
        deleted = self.fingerprint()
        (self.repo / "ignored.txt").write_text("noise\n", encoding="utf-8")
        self.assertEqual(deleted, self.fingerprint())

    def test_the_command_does_not_mutate_the_index_or_the_worktree(self):
        (self.repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
        (self.repo / "new.txt").write_text("added\n", encoding="utf-8")
        before_index = self.git("ls-files", "-s")
        before_status = self.porcelain()
        before_bytes = {
            path.name: path.read_bytes()
            for path in sorted(self.repo.iterdir())
            if path.is_file()
        }

        self.fingerprint()

        self.assertEqual(self.git("ls-files", "-s"), before_index)
        self.assertEqual(self.porcelain(), before_status)
        self.assertEqual(
            {
                path.name: path.read_bytes()
                for path in sorted(self.repo.iterdir())
                if path.is_file()
            },
            before_bytes,
        )


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
