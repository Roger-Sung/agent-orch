"""Plan / package / contract / environment / bundle identities (step 0a)."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from orchestrator.pack.blobs import BlobStore, seal_observation, sha256_hex
from orchestrator.pack.errors import IdentityRefused, PackError
from orchestrator.pack.identities import (
    bundle_hash,
    bundle_view,
    contract_hash,
    contract_projection,
    environment_digest,
    package_digest,
    plan_digest,
    plan_fingerprint,
    project_secret_refs,
)

MANIFEST = {
    "packs": [
        {
            "id": "P1",
            "tasks": ["T1"],
            "files_writable": ["src/A.java"],
            "obligations": [{"id": "O1", "selector": ":app:test"}],
            "depends": [],
            "tier": "standard",
        }
    ],
    "deferred": [],
    "evidence_declared": [],
}


class PlanIdentityTest(unittest.TestCase):
    # ID5: the 12 hex form is a prefix of the digest, never a separate hash.
    def test_id5_fingerprint_is_digest_prefix(self) -> None:
        self.assertEqual(
            plan_fingerprint(MANIFEST), plan_digest(MANIFEST).removeprefix("sha256:")[:12]
        )

    # ID6: a field outside the subset moves manifest_sha256, not the plan digest.
    def test_id6_selector_change_leaves_plan_digest(self) -> None:
        changed = {
            "packs": [
                {**MANIFEST["packs"][0], "obligations": [{"id": "O1", "selector": ":app:other"}]}
            ],
            "deferred": [],
            "evidence_declared": [],
        }
        self.assertEqual(plan_digest(changed), plan_digest(MANIFEST))

    # ID7: a pack's files_writable is inside the subset.
    def test_id7_files_writable_change_moves_plan(self) -> None:
        changed = {
            "packs": [{**MANIFEST["packs"][0], "files_writable": ["src/B.java"]}],
            "deferred": [],
            "evidence_declared": [],
        }
        self.assertNotEqual(plan_digest(changed), plan_digest(MANIFEST))

    def test_pack_order_is_significant(self) -> None:
        two = {
            "packs": [MANIFEST["packs"][0], {**MANIFEST["packs"][0], "id": "P2"}],
            "deferred": [],
            "evidence_declared": [],
        }
        flipped = {**two, "packs": list(reversed(two["packs"]))}
        self.assertNotEqual(plan_digest(two), plan_digest(flipped))


class PackageDigestTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.pkg = self.root / "pkg"
        (self.pkg / "cli").mkdir(parents=True)
        (self.pkg / "VERSION").write_text("0.1.0\n")
        (self.pkg / "cli" / "verify.py").write_text("print('hi')\n")

    def test_same_bytes_same_digest(self) -> None:
        self.assertEqual(package_digest(self.pkg), package_digest(self.pkg))

    def test_content_change_moves_digest(self) -> None:
        before = package_digest(self.pkg)
        (self.pkg / "VERSION").write_text("0.2.0\n")
        self.assertNotEqual(package_digest(self.pkg), before)

    # Compiled bytecode would make the digest depend on who ran what.
    def test_pycache_is_refused(self) -> None:
        (self.pkg / "__pycache__").mkdir()
        (self.pkg / "__pycache__" / "x.cpython-313.pyc").write_bytes(b"\0")
        with self.assertRaises(IdentityRefused) as ctx:
            package_digest(self.pkg)
        self.assertEqual(ctx.exception.code, "unsupported_entry")

    def test_symlink_escaping_package_is_refused(self) -> None:
        outside = self.root / "outside.txt"
        outside.write_text("x\n")
        os.symlink(outside, self.pkg / "escape")
        with self.assertRaises(IdentityRefused):
            package_digest(self.pkg)

    def test_symlink_inside_package_is_hashed(self) -> None:
        os.symlink("VERSION", self.pkg / "alias")
        self.assertNotEqual(package_digest(self.pkg), None)


class ContractHashTest(unittest.TestCase):
    def contract(self, **overrides) -> dict:
        base = {
            "policy_version": "pack-v1",
            "target_id": "acme",
            "change": "c1",
            "pack": "P1",
            "contract_version": 1,
            "contract_revision": 1,
            "execution_policy": {
                "verify": {
                    "timeout_s": 900,
                    "env": {
                        "set": {"LANG": "C.UTF-8", "TZ": "UTC"},
                        "secret_refs": {
                            "ORCH_DB_PASSWORD": {
                                "source": "keychain",
                                "service": "orch-acme",
                                "account": "mysql-root",
                            }
                        },
                    },
                }
            },
        }
        base.update(overrides)
        return base

    # ID25g: where a secret is fetched from is not part of any identity, so
    # rotating it - or moving the Keychain entry - keeps acceptance valid.
    def test_secret_location_is_outside_the_hash(self) -> None:
        a = self.contract()
        b = self.contract()
        b["execution_policy"]["verify"]["env"]["secret_refs"]["ORCH_DB_PASSWORD"].update(
            {"service": "other-service", "account": "other-account"}
        )
        self.assertEqual(contract_hash(a), contract_hash(b))

    def test_secret_name_set_is_inside_the_hash(self) -> None:
        a = self.contract()
        b = self.contract()
        b["execution_policy"]["verify"]["env"]["secret_refs"]["EXTRA"] = {"source": "keychain"}
        self.assertNotEqual(contract_hash(a), contract_hash(b))

    def test_secret_source_kind_is_inside_the_hash(self) -> None:
        a = self.contract()
        b = self.contract()
        b["execution_policy"]["verify"]["env"]["secret_refs"]["ORCH_DB_PASSWORD"]["source"] = "env_file"
        self.assertNotEqual(contract_hash(a), contract_hash(b))

    def test_public_env_value_is_inside_the_hash(self) -> None:
        a = self.contract()
        b = self.contract()
        b["execution_policy"]["verify"]["env"]["set"]["TZ"] = "Asia/Taipei"
        self.assertNotEqual(contract_hash(a), contract_hash(b))

    def test_projection_drops_service_and_account(self) -> None:
        projected = contract_projection(self.contract())
        refs = projected["execution_policy"]["verify"]["env"]["secret_refs"]
        self.assertEqual(refs, {"ORCH_DB_PASSWORD": {"source": "keychain"}})

    def test_project_secret_refs_handles_absent_env(self) -> None:
        self.assertIsNone(project_secret_refs(None))


class BundleAndObservationTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.store = BlobStore(self.root / "blobs")
        self.bundle = self.root / "bundle"

    def read_acquisition(self) -> dict:
        return {
            "kind": "read",
            "candidate_fingerprint": "sha256:" + "a" * 64,
            "source_path": "src/A.java",
            "byte_range": None,
            "truncated": False,
            "operation_id": "OP-1",
            "produced_by": "engine",
        }

    def test_projection_exposes_only_declared_keys(self) -> None:
        obs = seal_observation(
            self.store, self.bundle, "OBS-1", self.read_acquisition(), "class A {}\n", usable=True
        )
        self.assertEqual(set(obs["projection"]), {"path", "candidate_fingerprint", "truncated"})
        self.assertEqual(obs["projection"]["path"], "src/A.java")

    def test_payload_hash_covers_the_whole_wrapper(self) -> None:
        obs = seal_observation(
            self.store, self.bundle, "OBS-1", self.read_acquisition(), "class A {}\n", usable=True
        )
        written = (self.bundle / obs["payload"]["locator"]).read_bytes()
        self.assertEqual(obs["payload"]["sha256"], sha256_hex(written))
        self.assertIn(b'"acquisition"', written)

    def test_acquisition_must_match_its_kind(self) -> None:
        bad = self.read_acquisition()
        del bad["truncated"]
        with self.assertRaises(PackError):
            seal_observation(self.store, self.bundle, "OBS-1", bad, "x", usable=True)

    def test_bundle_hash_is_order_independent_for_observations(self) -> None:
        one = seal_observation(
            self.store, self.bundle, "OBS-1", self.read_acquisition(), "a\n", usable=True
        )
        other = self.read_acquisition()
        other["source_path"] = "src/B.java"
        two = seal_observation(self.store, self.bundle, "OBS-2", other, "b\n", usable=True)
        kwargs = dict(
            contract_hash_value="sha256:" + "c" * 64,
            candidate_output="sha256:" + "d" * 64,
            review_round=1,
            prior_review_sha256=None,
            history_snapshot_sha256="e" * 64,
            checkpoint_sha256=None,
        )
        self.assertEqual(
            bundle_hash(bundle_view(observations=[one, two], **kwargs)),
            bundle_hash(bundle_view(observations=[two, one], **kwargs)),
        )

    def test_usable_flag_changes_bundle_hash(self) -> None:
        obs = seal_observation(
            self.store, self.bundle, "OBS-1", self.read_acquisition(), "a\n", usable=True
        )
        unusable = {**obs, "usable": False}
        kwargs = dict(
            contract_hash_value="sha256:" + "c" * 64,
            candidate_output="sha256:" + "d" * 64,
            review_round=1,
            prior_review_sha256=None,
            history_snapshot_sha256="e" * 64,
            checkpoint_sha256=None,
        )
        self.assertNotEqual(
            bundle_hash(bundle_view(observations=[obs], **kwargs)),
            bundle_hash(bundle_view(observations=[unusable], **kwargs)),
        )

    def test_blob_store_detects_tampering(self) -> None:
        digest = self.store.put(b"hello")
        path = self.store.root / digest[:2] / digest[2:]
        path.write_bytes(b"tampered")
        with self.assertRaises(PackError):
            self.store.get(digest)

    def test_environment_digest_is_stable(self) -> None:
        env = {"target_package_digest": "sha256:" + "a" * 64, "engine_version": "abc"}
        self.assertEqual(environment_digest(env), environment_digest(dict(reversed(list(env.items())))))


if __name__ == "__main__":
    unittest.main()
