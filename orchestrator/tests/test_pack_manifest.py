"""ENVELOPES §1 manifest fixtures M1-M8 plus the derivations (step 0a)."""
from __future__ import annotations

import copy
import unittest

from orchestrator.pack.errors import EnvelopeInvalid
from orchestrator.pack.identities import plan_fingerprint
from orchestrator.pack.manifest import (
    active_obligations,
    approved_checks,
    deferred_obligations,
    deferred_projection,
    validate_manifest,
)

FP = "sha256:" + "a" * 64


def base_manifest() -> dict:
    manifest = {
        "schema_version": 1,
        "change": "c1",
        "generated_by": "pack_readiness.py@1.1.0",
        "source_fingerprints": {"design_md": "sha256:" + "b" * 64},
        "requirement_fingerprint": FP,
        "plan_fingerprint": "",
        "packs": [
            {
                "id": "P1",
                "contract_version": 1,
                "title": "first",
                "tasks": ["T1", "T2"],
                "files_writable": ["src/A.java"],
                "obligations": [
                    {
                        "id": "O1",
                        "source": "test-spec#1.1",
                        "disposition": "active",
                        "claimed_by": [{"task": "T1", "disposition": "active"}],
                        "forbids": [],
                        "verify_by": ["unit"],
                        "selector": ":app:test --tests AT",
                    },
                    {
                        "id": "O2",
                        "source": "test-spec#1.2",
                        "disposition": "deferred",
                        "claimed_by": [{"task": "T2", "disposition": "deferred"}],
                        "forbids": [],
                        "verify_by": ["manual"],
                        "selector": None,
                    },
                ],
                "depends": {"packs": [], "evidence": []},
                "tier": "standard",
                "tier_basis_ref": "design#tier",
            }
        ],
        "deferred": [
            {
                "ref": "D1",
                "owner": "TICKET-1",
                "release_condition": "staging ready",
                "affected_packs": ["P1"],
                "affected_tasks": ["T2"],
            }
        ],
        "evidence_declared": [],
        "coverage": {
            "obligations_total": 2,
            "obligations_active": 1,
            "obligations_deferred": 1,
            "obligations_uncovered": [],
        },
    }
    manifest["plan_fingerprint"] = plan_fingerprint(manifest)
    return manifest


class ManifestValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = base_manifest()

    def expect(self, code: str, mutate) -> None:
        manifest = copy.deepcopy(self.manifest)
        mutate(manifest)
        with self.assertRaises(EnvelopeInvalid) as ctx:
            validate_manifest(manifest)
        self.assertEqual(ctx.exception.code, code)

    # M1: the happy path must pass, otherwise every negative case is vacuous.
    def test_valid_manifest_passes(self) -> None:
        validate_manifest(self.manifest, validator_version="1.1.0")

    def test_em001_unknown_top_level_key(self) -> None:
        self.expect("EM-001", lambda m: m.update(extra=1))

    def test_em001_schema_version(self) -> None:
        self.expect("EM-001", lambda m: m.update(schema_version=2))

    def test_em002_duplicate_pack_id(self) -> None:
        self.expect("EM-002", lambda m: m["packs"].append(copy.deepcopy(m["packs"][0])))

    def test_em002_empty_tasks(self) -> None:
        self.expect("EM-002", lambda m: m["packs"][0].update(tasks=[]))

    # EM-002's key set is exact *except* for `tier_basis_ref`, which the
    # underlying schema requires only where a low tier has to justify itself.
    # Listing it unconditionally rejects every manifest a real validator emits,
    # since it omits the key entirely above that tier.
    def test_em002_tier_basis_ref_may_be_absent_above_low(self) -> None:
        manifest = copy.deepcopy(self.manifest)
        manifest["packs"][0].pop("tier_basis_ref")
        validate_manifest(manifest)

    def test_em002_low_tier_must_justify_itself(self) -> None:
        def mutate(m):
            m["packs"][0].update(tier="low")
            m["packs"][0].pop("tier_basis_ref")
        self.expect("EM-002", mutate)

    def test_em002_low_tier_basis_must_be_non_empty(self) -> None:
        self.expect("EM-002", lambda m: m["packs"][0].update(tier="low", tier_basis_ref=""))

    def test_em002_other_extra_keys_are_still_refused(self) -> None:
        self.expect("EM-002", lambda m: m["packs"][0].update(notes="x"))

    def test_em003_unknown_disposition(self) -> None:
        self.expect("EM-003", lambda m: m["packs"][0]["obligations"][0].update(disposition="maybe"))

    def test_em003_empty_selector_string(self) -> None:
        self.expect("EM-003", lambda m: m["packs"][0]["obligations"][0].update(selector=""))

    # EM-004: the enum is checked here so 005/013 only ever see valid claims -
    # a typo like `defered` must not be read as "not active" downstream.
    def test_em004_typo_disposition_is_caught_before_aggregation(self) -> None:
        self.expect(
            "EM-004",
            lambda m: m["packs"][0]["obligations"][0]["claimed_by"][0].update(disposition="defered"),
        )

    def test_em004_claim_task_not_in_pack(self) -> None:
        self.expect(
            "EM-004",
            lambda m: m["packs"][0]["obligations"][0]["claimed_by"][0].update(task="T9"),
        )

    def test_em004_empty_claimed_by(self) -> None:
        self.expect("EM-004", lambda m: m["packs"][0]["obligations"][0].update(claimed_by=[]))

    def test_em005_disposition_disagrees_with_claims(self) -> None:
        self.expect("EM-005", lambda m: m["packs"][0]["obligations"][0].update(disposition="deferred"))

    def test_em006_unknown_dependency(self) -> None:
        self.expect(
            "EM-006", lambda m: m["packs"][0]["depends"].update(packs=[{"id": "P9"}])
        )

    def test_em006_cycle(self) -> None:
        def mutate(m):
            second = copy.deepcopy(m["packs"][0])
            second["id"] = "P2"
            second["files_writable"] = ["src/B.java"]
            second["depends"] = {"packs": [{"id": "P1"}], "evidence": []}
            m["packs"][0]["depends"] = {"packs": [{"id": "P2"}], "evidence": []}
            m["packs"].append(second)
            m["deferred"][0]["affected_packs"] = ["P1", "P2"]
            m["coverage"].update(obligations_total=4, obligations_active=2, obligations_deferred=2)
            m["plan_fingerprint"] = plan_fingerprint(m)

        self.expect("EM-006", mutate)

    def test_em007_unknown_affected_task(self) -> None:
        self.expect("EM-007", lambda m: m["deferred"][0].update(affected_tasks=["T9"]))

    def test_em007_empty_owner(self) -> None:
        self.expect("EM-007", lambda m: m["deferred"][0].update(owner=""))

    # EM-008: a fingerprint the producer reports proves nothing on its own.
    def test_em008_recomputes_plan_fingerprint(self) -> None:
        self.expect("EM-008", lambda m: m.update(plan_fingerprint="0" * 12))

    def test_em009_malformed_requirement_fingerprint(self) -> None:
        self.expect("EM-009", lambda m: m.update(requirement_fingerprint="sha256:zz"))

    def test_em010_coverage_disagrees(self) -> None:
        self.expect("EM-010", lambda m: m["coverage"].update(obligations_active=2))

    def test_em010_uncovered_must_be_empty(self) -> None:
        self.expect("EM-010", lambda m: m["coverage"].update(obligations_uncovered=["O3"]))

    def test_em011_duplicate_writable_path_across_packs(self) -> None:
        def mutate(m):
            second = copy.deepcopy(m["packs"][0])
            second["id"] = "P2"
            m["packs"].append(second)
            m["coverage"].update(obligations_total=4, obligations_active=2, obligations_deferred=2)
            m["deferred"][0]["affected_packs"] = ["P1", "P2"]
            m["plan_fingerprint"] = plan_fingerprint(m)

        self.expect("EM-011", mutate)

    def test_em011_escaping_path(self) -> None:
        def mutate(m):
            m["packs"][0]["files_writable"] = ["../etc/passwd"]
            # files_writable is inside the plan subset, so EM-008 would fire
            # first if the fingerprint were left stale.
            m["plan_fingerprint"] = plan_fingerprint(m)

        self.expect("EM-011", mutate)

    def test_em012_validator_version_mismatch(self) -> None:
        with self.assertRaises(EnvelopeInvalid) as ctx:
            validate_manifest(self.manifest, validator_version="9.9.9")
        self.assertEqual(ctx.exception.code, "EM-012")

    # EM-013: a deferred claim without a deferral record is an unowned gap.
    def test_em013_deferred_claim_without_record(self) -> None:
        self.expect("EM-013", lambda m: m["deferred"][0].update(affected_tasks=["T1"]))


class DerivationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = base_manifest()

    def test_active_and_deferred_sets(self) -> None:
        self.assertEqual(active_obligations(self.manifest, "P1"), ["O1"])
        self.assertEqual(deferred_obligations(self.manifest, "P1"), ["O2"])

    def test_deferral_metadata_is_outside_the_plan_identity(self) -> None:
        before = plan_fingerprint(self.manifest)
        self.manifest["deferred"][0]["owner"] = "TICKET-99"
        self.assertEqual(plan_fingerprint(self.manifest), before)

    def test_deferred_projection_lists_every_record(self) -> None:
        self.manifest["deferred"].append(
            {
                "ref": "D2",
                "owner": "TICKET-2",
                "release_condition": "vendor reply",
                "affected_packs": ["P1"],
                "affected_tasks": ["T2"],
            }
        )
        records = deferred_projection(self.manifest, "P1")["O2"]
        self.assertEqual([r["ref"] for r in records], ["D1", "D2"])

    def test_approved_checks_only_for_selectors(self) -> None:
        checks = approved_checks(self.manifest, "P1")
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0]["check_id"], "O1")
        self.assertEqual(checks[0]["result_kind"], "test")


if __name__ == "__main__":
    unittest.main()
