from __future__ import annotations

import copy
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator.execution import ExecutionConfigError, parse_request, resolve_request, restore_plan
from orchestrator.profile import load_profile

PROFILE = Path(__file__).resolve().parents[1] / "profiles" / "codex_implement_claude_review.yaml"


class ExecutionPlanTests(unittest.TestCase):
    def setUp(self):
        self.profile = load_profile(PROFILE)
        self.request = {
            "schema_version": 1, "logical_work_id": "work-a", "spec_series_id": "spec-a",
            "stages": {name: {
                "role": "reviewer" if stage.owner == "claude" else "executor",
                "provider": stage.owner,
                "model": "claude-fable-5-1" if stage.owner == "claude" else "gpt-5.6-sol",
                "effort": "high",
            } for name, stage in self.profile.stages.items() if not stage.terminal},
        }

    def test_explicit_and_snapshot_roundtrip(self):
        plan = resolve_request(parse_request(json.dumps(self.request)), self.profile)
        self.assertEqual(restore_plan(plan.to_dict(), self.profile, plan.digest), plan)
        self.assertEqual(len(plan.digest), 64)
        self.assertEqual(plan.digest, "e7c78cfa021d9c907894332d2e7cf8d16f3439ccb74e0496ddbfacca26781d61")
        with self.assertRaises(TypeError):
            plan.stages["other"] = None

    def test_role_defaults_are_frozen_and_explicit_wins(self):
        name = next(iter(self.request["stages"]))
        config = self.request["stages"][name]
        model = config.pop("model")
        defaults = {config["role"]: {"model": model, "effort": "low"}}
        plan = resolve_request(self.request, self.profile, defaults=defaults)
        defaults[config["role"]]["model"] = "changed"
        self.assertEqual(plan.stages[name].model, model)
        self.assertEqual(plan.stages[name].effort, "high")
        self.assertEqual(plan.stages[name].to_dict()["model_source"], "role_default")
        self.assertEqual(restore_plan(plan.to_dict(), self.profile, plan.digest), plan)

    def test_no_environment_leakage_between_requests(self):
        before = dict(os.environ)
        first = resolve_request(self.request, self.profile)
        second = copy.deepcopy(self.request)
        for choice in second["stages"].values():
            choice["effort"] = "medium"
        with patch.dict(os.environ, {"ORCH_CODEX_MODEL": "unrelated", "ORCH_CLAUDE_MODEL": "unrelated"}):
            other = resolve_request(second, self.profile)
        self.assertNotEqual(first.digest, other.digest)
        self.assertTrue(all(c.effort == "high" for c in first.stages.values()))
        self.assertTrue(all(c.model != "unrelated" for c in other.stages.values()))
        self.assertEqual(dict(os.environ), before)

    def test_json_duplicate_keys_and_unknown_fields_rejected(self):
        for text in ('{"schema_version":1,"schema_version":2}', '[]', 'null', '{bad'):
            with self.subTest(text=text), self.assertRaises(ExecutionConfigError):
                parse_request(text)
        self.request["session_id"] = "not-in-this-contract"
        with self.assertRaises(ExecutionConfigError):
            resolve_request(self.request, self.profile)

    def test_every_nonterminal_stage_must_be_covered(self):
        self.request["stages"].pop(next(iter(self.request["stages"])))
        with self.assertRaises(ExecutionConfigError):
            resolve_request(self.request, self.profile)

    def test_invalid_types_provider_role_and_effort(self):
        name = next(iter(self.request["stages"]))
        for field, values in {
            "model": [None, [], "", "--model=bad", "hello world", "model\n--tools", "$(touch x)"],
            "effort": [None, [], "ultra", ""], "role": [None, [], "arbiter"],
            "provider": [None, [], "other"],
        }.items():
            for value in values:
                candidate = copy.deepcopy(self.request)
                candidate["stages"][name][field] = value
                with self.subTest(field=field, value=value), self.assertRaises(ExecutionConfigError):
                    resolve_request(candidate, self.profile)

    def test_reviewer_cannot_silently_downgrade(self):
        for config in self.request["stages"].values():
            if config["role"] == "reviewer":
                config["model"] = "claude-opus-5"
        with self.assertRaises(ExecutionConfigError):
            resolve_request(self.request, self.profile)

    def test_snapshot_tamper_and_provenance_contradiction_rejected(self):
        plan = resolve_request(self.request, self.profile)
        for change in ("identity", "effort", "source"):
            data = plan.to_dict()
            name = next(iter(data["stages"]))
            if change == "identity":
                data["spec_series_id"] = "other"
            elif change == "effort":
                data["stages"][name]["effort"] = "low"
            else:
                data["stages"][name]["model_source"] = "role_default"
            with self.subTest(change=change), self.assertRaises(ExecutionConfigError):
                restore_plan(data, self.profile, plan.digest)

    def test_schema_bool_is_not_version_one(self):
        self.request["schema_version"] = True
        with self.assertRaises(ExecutionConfigError):
            resolve_request(self.request, self.profile)

    def test_path_traversal_identifiers_rejected(self):
        for key in ("logical_work_id", "spec_series_id"):
            for value in ("a/../../x", "..", "a..b", "a/b", "a.b"):
                request = copy.deepcopy(self.request)
                request[key] = value
                with self.subTest(key=key, value=value), self.assertRaises(ExecutionConfigError):
                    resolve_request(request, self.profile)


if __name__ == "__main__":
    unittest.main()
