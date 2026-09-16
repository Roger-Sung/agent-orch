"""Provider adapters and the versioned policy branch (step 2)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from orchestrator.execution import (
    DEFAULT_POLICY,
    POLICIES,
    ExecutionConfigError,
    resolve_request,
    restore_plan,
)
from orchestrator.pack.provider_adapters import (
    CODEX_DISABLED_FEATURES,
    ClaudeAdapter,
    CodexAdapter,
    ProviderLaunchError,
    SessionUnconfirmed,
    adapter_for,
    contains_secret,
    redact,
)
from orchestrator.profile import load_profile

SESSION = "01a0a4dc-e503-71b1-8951-f641d274422d"
PROFILE_DIR = Path(__file__).resolve().parents[1] / "profiles"

# Real profiles: pack-v1 needs claude to produce and codex to review, which is
# the opposite pairing to the legacy one - exactly the reversal step 2 adds.
PROFILE = load_profile(PROFILE_DIR / "claude_apply_codex_review.yaml")
LEGACY_PROFILE = load_profile(PROFILE_DIR / "codex_implement_claude_review.yaml")


def _stages(profile, role_of, model_of) -> dict:
    return {
        name: {"role": role_of(stage.owner), "provider": stage.owner,
               "model": model_of(stage.owner), "effort": "high"}
        for name, stage in profile.stages.items() if not stage.terminal
    }


def legacy_request() -> dict:
    return {
        "schema_version": 1,
        "logical_work_id": "W1",
        "spec_series_id": "S1",
        "stages": _stages(
            LEGACY_PROFILE,
            lambda owner: "reviewer" if owner == "claude" else "executor",
            lambda owner: "claude-fable-5-1" if owner == "claude" else "gpt-5.6-sol",
        ),
    }


def pack_request() -> dict:
    return {
        "schema_version": 1,
        "policy_version": "pack-v1",
        "logical_work_id": "W1",
        "spec_series_id": "S1",
        "stages": _stages(
            PROFILE,
            lambda owner: "producer" if owner == "claude" else "reviewer",
            lambda owner: "claude-opus-5" if owner == "claude" else "gpt-6-astra",
        ),
    }


class PolicyBranchTest(unittest.TestCase):
    # P-1: a request without a policy must resolve exactly as it did before.
    # The literal below was captured by running the pre-change code against the
    # same profile and request, so this test fails if the branch ever leaks into
    # the default path - a self-comparison would not catch that.
    LEGACY_DIGEST = "34161442283716f3ccda90ccd25b1e073cc528f071e4df4dbfa8f1f2fbbc742e"

    def test_legacy_request_digest_is_unchanged(self) -> None:
        plan = resolve_request(legacy_request(), LEGACY_PROFILE)
        self.assertIsNone(plan.policy_version)
        self.assertEqual(
            sorted(plan.to_dict()),
            ["defaults_digest", "logical_work_id", "schema_version", "spec_series_id", "stages"],
        )
        self.assertEqual(plan.digest, self.LEGACY_DIGEST)

    def test_legacy_reviewer_model_lock_still_applies(self) -> None:
        request = legacy_request()
        request["stages"]["review"]["model"] = "claude-opus-5"
        with self.assertRaises(ExecutionConfigError) as ctx:
            resolve_request(request, LEGACY_PROFILE)
        self.assertIn("claude-fable-5-1", str(ctx.exception))

    # pack-v1 reverses the pairing, which execution-v1 would reject outright.
    def test_pack_v1_reverses_producer_and_reviewer(self) -> None:
        plan = resolve_request(pack_request(), PROFILE)
        self.assertEqual(plan.policy_version, "pack-v1")
        self.assertEqual(plan.stages["apply"].provider, "claude")
        self.assertEqual(plan.stages["review"].provider, "codex")

    def test_pack_v1_has_no_reviewer_model_lock(self) -> None:
        request = pack_request()
        request["stages"]["review"]["model"] = "gpt-6-other"
        resolve_request(request, PROFILE)  # must not raise

    def test_pack_roles_are_rejected_under_the_default_policy(self) -> None:
        request = pack_request()
        del request["policy_version"]
        with self.assertRaises(ExecutionConfigError):
            resolve_request(request, PROFILE)

    def test_unknown_policy_is_refused(self) -> None:
        request = pack_request()
        request["policy_version"] = "pack-v9"
        with self.assertRaises(ExecutionConfigError):
            resolve_request(request, PROFILE)

    def test_pack_plan_survives_a_snapshot_round_trip(self) -> None:
        plan = resolve_request(pack_request(), PROFILE)
        restored = restore_plan(plan.to_dict(), PROFILE, plan.digest)
        self.assertEqual(restored.policy_version, "pack-v1")

    def test_default_policy_table_matches_the_legacy_constant(self) -> None:
        self.assertEqual(DEFAULT_POLICY, "execution-v1")
        self.assertEqual(POLICIES[DEFAULT_POLICY]["roles"],
                         {"executor": "codex", "reviewer": "claude"})


class ClaudeAdapterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = ClaudeAdapter(binary="/usr/local/bin/claude", model="claude-opus-5")

    def result_json(self, **overrides) -> str:
        payload = {
            "type": "result",
            "session_id": SESSION,
            "result": "done\nORCHESTRATOR_OUTCOME: produced",
            "usage": {"input_tokens": 120, "output_tokens": 45},
            "modelUsage": {"claude-opus-5": {"inputTokens": 120}},
        }
        payload.update(overrides)
        return json.dumps(payload)

    def test_command_pins_the_model_and_asks_for_json(self) -> None:
        argv = self.adapter.command(cwd="/w")
        self.assertEqual(argv[:2], ["/usr/local/bin/claude", "-p"])
        self.assertIn("--model", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "claude-opus-5")
        self.assertIn("--output-format", argv)

    def test_resume_passes_the_session(self) -> None:
        argv = self.adapter.command(cwd="/w", resume_session=SESSION)
        self.assertEqual(argv[argv.index("--resume") + 1], SESSION)

    def test_session_id_and_text(self) -> None:
        out = self.result_json()
        self.assertEqual(self.adapter.session_id(stdout=out, stderr=""), SESSION)
        self.assertIn("ORCHESTRATOR_OUTCOME", self.adapter.authoritative_text(out))

    def test_empty_result_is_a_launch_class_failure(self) -> None:
        with self.assertRaises(ProviderLaunchError) as ctx:
            self.adapter.authoritative_text(self.result_json(result="  "))
        self.assertEqual(ctx.exception.reason, "provider_final_response_empty")

    def test_unreadable_output(self) -> None:
        with self.assertRaises(ProviderLaunchError) as ctx:
            self.adapter.authoritative_text("not json")
        self.assertEqual(ctx.exception.reason, "provider_final_response_unreadable")

    # Claude does report which model billed, so confirmation is real here.
    def test_attestation_confirms_the_model(self) -> None:
        attestation = self.adapter.attestation(stdout=self.result_json())
        self.assertEqual(attestation["requested"], "claude-opus-5")
        self.assertEqual(attestation["invoked"], "claude-opus-5")
        self.assertEqual(attestation["provider_confirmed"], "claude-opus-5")

    def test_usage_is_read_from_the_result(self) -> None:
        self.assertEqual(
            self.adapter.usage(self.result_json()),
            {"input_tokens": 120, "output_tokens": 45},
        )

    def test_resume_confirmation_compares_session_ids(self) -> None:
        self.assertTrue(self.adapter.resume_confirmed(
            stdout=self.result_json(), expected_session=SESSION))
        self.assertFalse(self.adapter.resume_confirmed(
            stdout=self.result_json(session_id="other"), expected_session=SESSION))


class CodexAdapterTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.adapter = CodexAdapter(
            binary="/Applications/ChatGPT.app/Contents/Resources/codex",
            model="gpt-6-astra", codex_home=self.home,
        )

    def write_rollout(self, *, lines: list[dict] | None = None) -> Path:
        path = self.home / "sessions" / "2026" / f"rollout-2026-09-15T20-00-00-{SESSION}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        # The real rollout shape, copied from an actual transcript: every line
        # wraps its content in `payload`, the model rides on a `turn_context`
        # line and usage on an `event_msg` of type `token_count`.  A simplified
        # shape here would have made the parser pass against a format that does
        # not exist.
        entries = lines if lines is not None else [
            {"type": "turn_context", "payload": {"cwd": "/w", "model": "gpt-6-astra",
                                                 "effort": "xhigh"}},
            {"type": "event_msg", "payload": {"type": "token_count", "info": {
                "total_token_usage": {"input_tokens": 200_000, "cached_input_tokens": 1_000,
                                      "output_tokens": 8_000, "total_tokens": 208_000}}}},
        ]
        path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
        return path

    STDERR = f"OpenAI Codex v0.154.0\n--------\nsandbox: read-only\nsession id: {SESSION}\n--------\n"

    def test_command_disables_every_bypass_feature(self) -> None:
        argv = self.adapter.command(cwd="/w")
        for feature in CODEX_DISABLED_FEATURES:
            self.assertIn(feature, argv)
        self.assertEqual(argv[argv.index("-s") + 1], "read-only")
        # The prompt goes in on stdin, which is what `-` selects.
        self.assertEqual(argv[-1], "-")

    def test_resume_command_shape(self) -> None:
        argv = self.adapter.command(cwd="/w", resume_session=SESSION)
        self.assertEqual(argv[1:4], ["exec", "resume", SESSION])

    def test_session_id_comes_from_stderr(self) -> None:
        self.assertEqual(self.adapter.session_id(stdout="", stderr=self.STDERR), SESSION)
        self.assertIsNone(self.adapter.session_id(stdout="", stderr="no id here"))

    # Two-phase binding: a printed id with no transcript is not a session.
    def test_confirm_requires_both_halves(self) -> None:
        with self.assertRaises(SessionUnconfirmed):
            self.adapter.confirm_session(stdout="", stderr=self.STDERR)
        self.write_rollout()
        self.assertEqual(self.adapter.confirm_session(stdout="", stderr=self.STDERR), SESSION)

    def test_missing_session_id_is_unconfirmed(self) -> None:
        self.write_rollout()
        with self.assertRaises(SessionUnconfirmed):
            self.adapter.confirm_session(stdout="", stderr="")

    # Codex never confirms a canonical model, so we must not claim it did.
    def test_attestation_keeps_provider_confirmation_null(self) -> None:
        self.write_rollout()
        attestation = self.adapter.attestation(
            stdout="", transcript=self.adapter.transcript(SESSION))
        self.assertEqual(attestation["requested"], "gpt-6-astra")
        self.assertEqual(attestation["invoked"], "gpt-6-astra")
        self.assertIsNone(attestation["provider_confirmed"])

    def test_attestation_detects_a_different_invoked_model(self) -> None:
        self.write_rollout(lines=[
            {"type": "turn_context", "payload": {"model": "gpt-6-other"}}])
        attestation = self.adapter.attestation(
            stdout="", transcript=self.adapter.transcript(SESSION))
        self.assertNotEqual(attestation["requested"], attestation["invoked"])

    # The transcript total is cumulative; the CLI's printed figure is too, which
    # is why summing printed values across calls double-counts.
    def test_usage_takes_the_last_cumulative_total(self) -> None:
        self.write_rollout(lines=[
            {"type": "event_msg", "payload": {"type": "token_count", "info": {
                "total_token_usage": {"total_tokens": 100}}}},
            {"type": "event_msg", "payload": {"type": "token_count", "info": {
                "total_token_usage": {"total_tokens": 250}}}},
        ])
        self.assertEqual(
            self.adapter.usage(self.adapter.transcript(SESSION))["total_tokens"], 250
        )

    def test_resume_must_append_to_the_transcript(self) -> None:
        one = {"type": "turn_context", "payload": {"model": "gpt-6-astra"}}
        self.write_rollout(lines=[one])
        self.assertFalse(self.adapter.resume_confirmed(SESSION, before_lines=1))
        self.write_rollout(lines=[one, one])
        self.assertTrue(self.adapter.resume_confirmed(SESSION, before_lines=1))


class EnvironmentAndRedactionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = ClaudeAdapter(binary="claude", model="claude-opus-5")
        self.policy_env = {
            "set": {"LANG": "C.UTF-8", "TZ": "UTC"},
            "secret_refs": {"ORCH_DB_PASSWORD": {"source": "keychain"}},
        }

    def test_environment_is_built_not_inherited(self) -> None:
        env = self.adapter.environment(self.policy_env, {"ORCH_DB_PASSWORD": "s3cr3t"})
        self.assertEqual(set(env), {"LANG", "TZ", "ORCH_DB_PASSWORD"})

    def test_missing_secret_is_a_launch_failure(self) -> None:
        with self.assertRaises(ProviderLaunchError) as ctx:
            self.adapter.environment(self.policy_env, {})
        self.assertEqual(ctx.exception.reason, "secret_unavailable")

    # R2-H2: masking replaces the literal and nothing else - the failure must
    # still read as a failure.
    def test_redaction_preserves_the_failure(self) -> None:
        secrets = self.adapter.secret_values(self.policy_env, {"ORCH_DB_PASSWORD": "s3cr3t"})
        masked = redact("connection failed: s3cr3t (exit 1)", secrets)
        self.assertNotIn("s3cr3t", masked)
        self.assertIn("connection failed", masked)
        self.assertIn("exit 1", masked)

    def test_backstop_detects_an_unmasked_secret(self) -> None:
        secrets = ["s3cr3t"]
        self.assertTrue(contains_secret("oops s3cr3t", secrets))
        self.assertFalse(contains_secret(redact("oops s3cr3t", secrets), secrets))


class AdapterSelectionTest(unittest.TestCase):
    def test_selection_by_provider(self) -> None:
        self.assertIsInstance(adapter_for("claude", binary="claude", model="m"), ClaudeAdapter)
        self.assertIsInstance(
            adapter_for("codex", binary="codex", model="m", codex_home=None), CodexAdapter
        )

    def test_unknown_provider(self) -> None:
        with self.assertRaises(Exception):
            adapter_for("gemini", binary="g", model="m")


if __name__ == "__main__":
    unittest.main()
