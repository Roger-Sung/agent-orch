"""IDENTITIES §3 classification fixtures (ID25*, ID27*) - step 0a."""
from __future__ import annotations

import copy
import unittest

from orchestrator.pack.transitions import classify, transition_record


def contract(**overrides) -> dict:
    base = {
        "policy_version": "pack-v1",
        "target_id": "acme",
        "change": "c1",
        "pack": "P1",
        "requirement_fingerprint": "sha256:" + "a" * 64,
        "approved_checks": [{"check_id": "O1", "selector": ":app:test"}],
        "prerun_checks": [],
        "prerun_reads": [],
        "readable_roots": ["src"],
        "hotspot_paths": ["src/core/**"],
        "declared_config_refs": [],
        "excludes": {"engine": ["pack-state.json"], "target": ["build/"]},
        "protected_source_globs": ["**/*.java"],
        "home_seeds": [{"relpath": ".toolrc", "package_relpath": "seeds/a.conf"}],
        "budget_policy": {"round_cap": 5},
        "execution_policy": {
            "verify": {
                "timeout_s": 900,
                "env": {
                    "set": {"TZ": "UTC"},
                    "secret_refs": {
                        "ORCH_DB_PASSWORD": {
                            "source": "keychain", "service": "svc", "account": "acct"
                        }
                    },
                },
            }
        },
        "prompt_templates": [{"name": "apply", "package_relpath": "templates/apply.md", "sha256": "x"}],
        "base_revision": "abc",
        "candidate_fingerprint_input": "sha256:" + "b" * 64,
        "dependency_revisions": {},
        "evidence_receipts": {},
        "environment_digest": "sha256:" + "c" * 64,
    }
    base.update(overrides)
    return base


def manifest(**overrides) -> dict:
    base = {
        "packs": [
            {
                "id": "P1",
                "contract_version": 1,
                "title": "first",
                "tasks": ["T1"],
                "files_writable": ["src/A.java"],
                "obligations": [{"id": "O1", "disposition": "active"}],
                "depends": {"packs": [], "evidence": []},
                "tier": "standard",
                "tier_basis_ref": "design#tier",
            },
            {
                "id": "P2",
                "contract_version": 1,
                "title": "second",
                "tasks": ["T9"],
                "files_writable": ["src/Z.java"],
                "obligations": [{"id": "O9", "disposition": "active"}],
                "depends": {"packs": [], "evidence": []},
                "tier": "standard",
                "tier_basis_ref": "design#tier",
            },
        ],
        "deferred": [],
        "evidence_declared": [],
        "source_fingerprints": {"design_md": "sha256:" + "d" * 64, "test_spec": {}},
    }
    base.update(overrides)
    return base


def grow_writable(new_manifest: dict, path: str = "src/B.java") -> None:
    pack = new_manifest["packs"][0]
    pack["files_writable"] = sorted(set(pack["files_writable"]) | {path})
    pack["contract_version"] += 1


class ClassificationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.old_c, self.new_c = contract(), contract()
        self.old_m, self.new_m = manifest(), manifest()

    def run_classify(self, **kwargs):
        params = dict(
            old_manifest=self.old_m,
            new_manifest=self.new_m,
            pack_id="P1",
            text_changed_tasks=[],
        )
        params.update(kwargs)
        return classify(self.old_c, self.new_c, **params)

    def test_no_change_is_none(self) -> None:
        result = self.run_classify()
        self.assertEqual(result.author_class, "none")
        self.assertEqual(result.refresh, set())

    # The canonical minor: one new writable path, nothing else moved.
    def test_added_writable_path_is_minor(self) -> None:
        grow_writable(self.new_m)
        result = self.run_classify()
        self.assertEqual(result.author_class, "minor")
        self.assertFalse(result.requires_major_approval)
        self.assertEqual(result.version_delta(pack_subset_changed=True), (1, 1))

    # ID27c: an environment refresh alongside a legal minor stays minor.
    def test_minor_with_environment_refresh(self) -> None:
        grow_writable(self.new_m)
        self.new_c["environment_digest"] = "sha256:" + "e" * 64
        result = self.run_classify()
        self.assertEqual(result.author_class, "minor")
        self.assertEqual(result.refresh, {"environment"})
        self.assertTrue(result.requires_environment_ack)
        # Composition takes the strictest observation policy.
        self.assertEqual(result.observation_policy, "all_stale")

    # A pure minor is the case where observations may actually be reused.
    def test_pure_minor_reuses_observations(self) -> None:
        grow_writable(self.new_m)
        self.assertEqual(self.run_classify().observation_policy, "reuse")

    # ID27f: prose drift inside a task is an author-side change.
    def test_changed_task_prose_forces_major(self) -> None:
        grow_writable(self.new_m)
        result = self.run_classify(text_changed_tasks=["T1"])
        self.assertEqual(result.author_class, "major")
        self.assertIn("tasks with changed prose: ['T1']", result.minor_blockers)

    # Without the validator field the engine cannot know, so it must not guess.
    def test_missing_text_changed_tasks_blocks_minor(self) -> None:
        grow_writable(self.new_m)
        result = self.run_classify(text_changed_tasks=None)
        self.assertEqual(result.author_class, "major")
        self.assertIn("validator did not report text_changed_tasks", result.minor_blockers)

    def test_removing_a_writable_path_is_major(self) -> None:
        self.new_m["packs"][0]["files_writable"] = []
        self.new_m["packs"][0]["contract_version"] += 1
        self.assertEqual(self.run_classify().author_class, "major")

    def test_added_path_matching_hotspot_is_major(self) -> None:
        grow_writable(self.new_m, "src/core/Engine.java")
        result = self.run_classify()
        self.assertEqual(result.author_class, "major")
        self.assertTrue(any("hotspot" in b for b in result.minor_blockers))

    def test_added_path_claimed_by_another_pack_is_major(self) -> None:
        grow_writable(self.new_m)
        result = self.run_classify(claimed_paths={"src/B.java"})
        self.assertEqual(result.author_class, "major")

    def test_touching_another_pack_is_major(self) -> None:
        grow_writable(self.new_m)
        self.new_m["packs"][1]["files_writable"] = ["src/Y.java"]
        self.assertEqual(self.run_classify().author_class, "major")

    # ID25e: remapping a home seed changes the contract even with equal bytes.
    def test_home_seed_remap_is_major(self) -> None:
        self.new_c["home_seeds"] = [{"relpath": ".toolrc", "package_relpath": "seeds/b.conf"}]
        self.assertEqual(self.run_classify().author_class, "major")

    # Rotating where a secret lives is not an author-side change at all.
    def test_secret_location_change_is_not_a_transition(self) -> None:
        refs = self.new_c["execution_policy"]["verify"]["env"]["secret_refs"]
        refs["ORCH_DB_PASSWORD"] = {"source": "keychain", "service": "new", "account": "new"}
        self.assertEqual(self.run_classify().author_class, "none")

    def test_secret_name_set_change_is_major(self) -> None:
        refs = self.new_c["execution_policy"]["verify"]["env"]["secret_refs"]
        refs["EXTRA"] = {"source": "keychain", "service": "s", "account": "a"}
        self.assertEqual(self.run_classify().author_class, "major")

    def test_source_fingerprint_change_is_major(self) -> None:
        self.new_m["source_fingerprints"]["design_md"] = "sha256:" + "f" * 64
        self.assertEqual(self.run_classify().author_class, "major")

    def test_base_refresh_creates_new_attempt(self) -> None:
        self.new_c["base_revision"] = "def"
        result = self.run_classify()
        self.assertEqual(result.author_class, "none")
        self.assertEqual(result.refresh, {"base"})
        self.assertTrue(result.new_work_attempt)
        self.assertEqual(result.observation_policy, "all_stale")

    # Dependency refresh keeps read observations but invalidates verify ones.
    def test_dependency_refresh_policy(self) -> None:
        self.new_c["dependency_revisions"] = {"P0": {"candidate_output": "x"}}
        result = self.run_classify()
        self.assertEqual(result.refresh, {"dependency"})
        self.assertEqual(result.observation_policy, "verify_stale")
        self.assertFalse(result.new_work_attempt)

    # superseded_by_contract is forbidden under a bare dependency refresh, but a
    # major carries its own permission.
    def test_superseded_permission(self) -> None:
        self.new_c["dependency_revisions"] = {"P0": {"candidate_output": "x"}}
        self.assertFalse(self.run_classify().superseded_allowed)

        self.new_m["source_fingerprints"]["design_md"] = "sha256:" + "f" * 64
        self.assertTrue(self.run_classify().superseded_allowed)

    def test_refresh_alone_does_not_move_version(self) -> None:
        self.new_c["environment_digest"] = "sha256:" + "e" * 64
        result = self.run_classify()
        self.assertEqual(result.version_delta(pack_subset_changed=False), (0, 0))


class TransitionRecordTest(unittest.TestCase):
    def test_record_carries_the_two_derived_sets(self) -> None:
        old_m, new_m = manifest(), manifest()
        grow_writable(new_m)
        result = classify(
            contract(), contract(),
            old_manifest=old_m, new_manifest=new_m, pack_id="P1", text_changed_tasks=[],
        )
        record = transition_record(
            transition_id="TR-1",
            old={"contract_hash": "sha256:" + "1" * 64},
            new={"contract_hash": "sha256:" + "2" * 64},
            classification=result,
            from_version=1,
            from_revision=1,
            pack_subset_changed=True,
            reverify=["O2", "O1"],
            changed_or_removed=[],
        )
        self.assertEqual(record["author_class"], "minor")
        self.assertEqual(record["to_version"], 2)
        self.assertEqual(record["to_revision"], 2)
        self.assertEqual(record["reverify"], ["O1", "O2"])
        self.assertEqual(record["changed_or_removed"], [])


if __name__ == "__main__":
    unittest.main()
