"""ENVELOPES §2/§3 fixtures (R/V series) - step 0a."""
from __future__ import annotations

import copy
import json
import unittest

from orchestrator.pack.envelopes import (
    REVIEW_BEGIN,
    REVIEW_END,
    check_selector_backed,
    derive_command_status,
    derive_execution_outcome,
    derive_test_status,
    parse_framing,
    validate_review,
    validate_verify,
    verify_usable,
)
from orchestrator.pack.errors import EnvelopeInvalid

HEADER = {
    "envelope": "pack-review",
    "policy_version": "pack-v1",
    "kind": "final_review",
    "target_id": "acme",
    "change": "c1",
    "pack": "P1",
    "contract_version": 1,
    "role": "reviewer",
    "review_round": 2,
    "candidate_fingerprint": "sha256:" + "a" * 64,
    "contract_hash": "sha256:" + "b" * 64,
    "bundle_hash": "sha256:" + "c" * 64,
    "plan_fingerprint": "0123456789ab",
    "requirement_fingerprint": "sha256:" + "d" * 64,
    "ext": {},
}

ECHO = {k: HEADER[k] for k in (
    "target_id", "change", "pack", "candidate_fingerprint", "contract_hash",
    "bundle_hash", "plan_fingerprint", "requirement_fingerprint",
)}

OBSERVATIONS = {
    "OBS-3": {"kind": "verify", "usable": True,
              "projection": {"result_kind": "test", "status": "PASS",
                             "subject": {"kind": "obligation", "id": "O1"}}},
    "OBS-5": {"kind": "read", "usable": True, "projection": {"path": "src/A.java"}},
    "OBS-7": {"kind": "verify", "usable": True,
              "projection": {"result_kind": "test", "status": "FAIL",
                             "subject": {"kind": "obligation", "id": "O2"}}},
    "OBS-8": {"kind": "verify", "usable": True,
              "projection": {"result_kind": "test", "status": "SKIP",
                             "subject": {"kind": "obligation", "id": "O1"}}},
}

PRIOR_FINDINGS = {"F1-1": ["L-1"], "F1-2": ["L-2"], "F1-3": ["L-1", "L-2"]}


def b_review() -> dict:
    """The §7 `B-review` base: round 2, O1 PASS, O2 FAIL, O3 UNKNOWN."""
    return {
        **HEADER,
        "verdict": "needs_repair",
        "blocked_reason": None,
        "obligations": {
            "O1": {"status": "PASS", "basis": ["OBS-3"], "note": None},
            "O2": {"status": "FAIL", "basis": [], "note": None},
            "O3": {"status": "UNKNOWN", "basis": [], "note": None},
        },
        "findings": [
            {
                "id": "F2-1",
                "severity": "High",
                "blocking": True,
                "obligation_refs": ["O2"],
                "title": "wallet not created",
                "evidence": "OBS-7 shows the FAIL",
                "requested_change": "create the sub wallet",
                "observations": ["OBS-7"],
                "locators": [],
                "lineage_ids": ["L-2"],
                "prior_refs": [
                    {"finding_id": "F1-2", "lineage_id": "L-2", "relation": "residual",
                     "obligation_ref": "O2", "counterexample": "same input still fails",
                     "causal_note": "fix did not cover the early return"},
                    {"finding_id": "F1-3", "lineage_id": "L-2", "relation": "residual",
                     "obligation_ref": "O2", "counterexample": "same input still fails",
                     "causal_note": "same root cause"},
                ],
                "recurrence_of": None,
            }
        ],
        "contract_findings": [],
        "improvements": [],
        "prior_round": {
            "round": 1,
            "contract_hash": "sha256:" + "b" * 64,
            "review_sha256": "e" * 64,
            "dispositions": {
                "F1-1": {"L-1": {"disposition": "resolved", "successors": [],
                                 "basis": ["OBS-3"], "reason": None}},
                "F1-2": {"L-2": {"disposition": "residual", "successors": ["F2-1"],
                                 "basis": [], "reason": None}},
                "F1-3": {"L-1": {"disposition": "resolved", "successors": [],
                                 "basis": ["OBS-3"], "reason": None},
                         "L-2": {"disposition": "residual", "successors": ["F2-1"],
                                 "basis": [], "reason": None}},
            },
        },
        "remaining": [
            {"obligation_id": "O3", "kind": "unknown_evidence", "owner": "controller",
             "check": "add prerun check TS-8.11"}
        ],
    }


class FramingTest(unittest.TestCase):
    def wrap(self, envelope: dict, outcome: str) -> str:
        return f"{REVIEW_BEGIN}\n{json.dumps(envelope)}\n{REVIEW_END}\nORCHESTRATOR_OUTCOME: {outcome}"

    def test_single_block_parses(self) -> None:
        envelope, outcome = parse_framing(self.wrap({"verdict": "accepted"}, "accepted"))
        self.assertEqual(envelope["verdict"], "accepted")
        self.assertEqual(outcome, "accepted")

    # Two blocks mean two answers; picking one would let a model hedge.
    def test_two_blocks_are_refused(self) -> None:
        text = self.wrap({"verdict": "accepted"}, "accepted")
        with self.assertRaises(EnvelopeInvalid):
            parse_framing(text + "\n" + text)

    def test_duplicate_json_keys_are_refused(self) -> None:
        text = (f'{REVIEW_BEGIN}\n{{"verdict": "accepted", "verdict": "blocked"}}\n'
                f"{REVIEW_END}\nORCHESTRATOR_OUTCOME: accepted")
        with self.assertRaises(EnvelopeInvalid):
            parse_framing(text)

    def test_missing_outcome_line(self) -> None:
        with self.assertRaises(EnvelopeInvalid):
            parse_framing(f"{REVIEW_BEGIN}\n{{}}\n{REVIEW_END}")


class ReviewValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.envelope = b_review()

    def validate(self, envelope: dict | None = None, outcome: str | None = None) -> None:
        envelope = envelope if envelope is not None else self.envelope
        validate_review(
            envelope,
            outcome if outcome is not None else envelope["verdict"],
            expected_header=ECHO,
            active_obligations=["O1", "O2", "O3"],
            bundle_observations=OBSERVATIONS,
            prior_findings=PRIOR_FINDINGS,
        )

    def expect(self, code: str, mutate) -> None:
        envelope = copy.deepcopy(self.envelope)
        mutate(envelope)
        with self.assertRaises(EnvelopeInvalid) as ctx:
            self.validate(envelope)
        self.assertEqual(ctx.exception.code, code)

    def test_base_envelope_is_valid(self) -> None:
        self.validate()

    # G-5: an echoed identity that drifted means the answer is about something else.
    def test_g5_candidate_echo_mismatch(self) -> None:
        self.expect("G-5", lambda e: e.update(candidate_fingerprint="sha256:" + "9" * 64))

    def test_g2_unknown_top_level_key(self) -> None:
        self.expect("G-2", lambda e: e.update(surprise=1))

    # EV-R-002: evidence_request is reserved for v2 (D-2026-09-14-14).
    def test_evr002_evidence_request_kind(self) -> None:
        self.expect("EV-R-002", lambda e: e.update(kind="evidence_request"))

    def test_evr001_printed_outcome_disagrees(self) -> None:
        with self.assertRaises(EnvelopeInvalid) as ctx:
            self.validate(outcome="accepted")
        self.assertEqual(ctx.exception.code, "EV-R-001")

    # R1: the obligation key set is the contract's, not the reviewer's choice.
    def test_r1_missing_obligation(self) -> None:
        self.expect("EV-R-030", lambda e: e["obligations"].pop("O3"))

    def test_evr030_extra_obligation(self) -> None:
        self.expect(
            "EV-R-030",
            lambda e: e["obligations"].update(O9={"status": "PASS", "basis": ["OBS-3"], "note": None}),
        )

    def test_evr032_pass_without_basis(self) -> None:
        self.expect("EV-R-032", lambda e: e["obligations"]["O1"].update(basis=[]))

    def test_evr032_unusable_basis(self) -> None:
        observations = {**OBSERVATIONS, "OBS-3": {**OBSERVATIONS["OBS-3"], "usable": False}}
        with self.assertRaises(EnvelopeInvalid) as ctx:
            validate_review(
                self.envelope, self.envelope["verdict"],
                expected_header=ECHO, active_obligations=["O1", "O2", "O3"],
                bundle_observations=observations, prior_findings=PRIOR_FINDINGS,
            )
        self.assertEqual(ctx.exception.code, "EV-R-032")

    # R2 / R3: a read or a SKIP cannot stand behind a selector-backed PASS.
    def test_r2_read_observation_cannot_back_selector_pass(self) -> None:
        envelope = copy.deepcopy(self.envelope)
        envelope["obligations"]["O1"]["basis"] = ["OBS-5"]
        with self.assertRaises(EnvelopeInvalid) as ctx:
            check_selector_backed(envelope, OBSERVATIONS, {"O1": ":app:test", "O2": None, "O3": None})
        self.assertEqual(ctx.exception.code, "EV-R-033")

    def test_r3_skip_cannot_back_a_pass(self) -> None:
        envelope = copy.deepcopy(self.envelope)
        envelope["obligations"]["O1"]["basis"] = ["OBS-8"]
        with self.assertRaises(EnvelopeInvalid) as ctx:
            check_selector_backed(envelope, OBSERVATIONS, {"O1": ":app:test", "O2": None, "O3": None})
        self.assertEqual(ctx.exception.code, "EV-R-033")

    def test_selector_backed_pass_accepts_matching_verify(self) -> None:
        check_selector_backed(self.envelope, OBSERVATIONS, {"O1": ":app:test", "O2": None, "O3": None})

    def test_evr035_unknown_without_remaining(self) -> None:
        self.expect("EV-R-035", lambda e: e.update(remaining=[]))

    # EV-R-036: a FAIL nobody wrote a finding about would be an unreported defect.
    def test_evr036_fail_without_finding(self) -> None:
        def mutate(e):
            e["obligations"]["O1"]["status"] = "FAIL"
            e["obligations"]["O1"]["basis"] = []

        self.expect("EV-R-036", mutate)

    # EV-R-037: the other direction - a finding cannot point at a passing
    # obligation, which would make the finding unfalsifiable.
    def test_evr037_finding_references_a_passing_obligation(self) -> None:
        def mutate(e):
            e["obligations"]["O2"] = {"status": "PASS", "basis": ["OBS-3"], "note": None}

        self.expect("EV-R-037", mutate)

    # R10 / R11: a finding is an unmet requirement by definition.
    def test_r10_non_blocking_finding(self) -> None:
        self.expect("EV-R-043", lambda e: e["findings"][0].update(blocking=False))

    def test_r11_low_severity_finding(self) -> None:
        self.expect("EV-R-043", lambda e: e["findings"][0].update(severity="Low"))

    # R4: an edge must point at a finding that existed last round.
    def test_r4_unknown_prior_finding(self) -> None:
        self.expect("EV-R-047", lambda e: e["findings"][0]["prior_refs"][0].update(finding_id="F1-9"))

    # R5b: the edge names a lineage the prior finding never carried.
    def test_r5b_lineage_not_carried_by_prior(self) -> None:
        def mutate(e):
            e["findings"][0]["prior_refs"][0]["lineage_id"] = "L-1"
            e["findings"][0]["lineage_ids"] = ["L-1", "L-2"]

        self.expect("EV-R-047", mutate)

    # R5: lineage_ids must equal the edge set exactly.
    def test_r5_lineage_ids_disagree_with_edges(self) -> None:
        self.expect("EV-R-041", lambda e: e["findings"][0].update(lineage_ids=["L-1", "L-2"]))

    # R8: a finding with no edges must introduce exactly one new lineage.
    def test_r8_new_finding_reusing_a_lineage(self) -> None:
        def mutate(e):
            e["findings"].append({
                **copy.deepcopy(e["findings"][0]),
                "id": "F2-2", "prior_refs": [], "lineage_ids": ["L-1", "L-2"],
            })

        self.expect("EV-R-041", mutate)

    # R6 / R7: dispositions must cover every prior finding and lineage.
    def test_r6_missing_disposition(self) -> None:
        self.expect("EV-R-051", lambda e: e["prior_round"]["dispositions"].pop("F1-1"))

    def test_r7_missing_lineage_disposition(self) -> None:
        self.expect("EV-R-051", lambda e: e["prior_round"]["dispositions"]["F1-1"].clear())

    def test_evr053_resolved_with_successors(self) -> None:
        self.expect(
            "EV-R-053",
            lambda e: e["prior_round"]["dispositions"]["F1-1"]["L-1"].update(successors=["F2-1"]),
        )

    # EV-R-048: the disposition and the successor's edge must describe each other.
    def test_evr048_successor_without_matching_edge(self) -> None:
        self.expect(
            "EV-R-048",
            lambda e: e["prior_round"]["dispositions"]["F1-2"]["L-2"].update(disposition="repeat"),
        )

    # EV-R-04B: one lineage cannot be both finished and still running.
    def test_evr04b_inconsistent_lineage_layer(self) -> None:
        self.expect(
            "EV-R-04B",
            lambda e: e["prior_round"]["dispositions"]["F1-3"]["L-2"].update(
                disposition="withdrawn", successors=[], reason="misread"
            ),
        )

    def test_evr050_round_one_must_have_null_prior(self) -> None:
        def mutate(e):
            e["review_round"] = 1
            e["findings"][0]["id"] = "F1-1"

        self.expect("EV-R-050", mutate)

    # EV-R-020: accepted is a claim about evidence - an outstanding
    # unknown_evidence entry contradicts it even when every status reads PASS.
    def test_evr020_accepted_with_outstanding_unknown_evidence(self) -> None:
        def mutate(e):
            e["verdict"] = "accepted"
            e["findings"] = []
            e["obligations"]["O2"] = {"status": "PASS", "basis": ["OBS-3"], "note": None}
            e["obligations"]["O3"] = {"status": "PASS", "basis": ["OBS-3"], "note": None}
            e["prior_round"]["dispositions"] = {
                "F1-1": {"L-1": {"disposition": "resolved", "successors": [], "basis": ["OBS-3"], "reason": None}},
                "F1-2": {"L-2": {"disposition": "resolved", "successors": [], "basis": ["OBS-3"], "reason": None}},
                "F1-3": {"L-1": {"disposition": "resolved", "successors": [], "basis": ["OBS-3"], "reason": None},
                         "L-2": {"disposition": "resolved", "successors": [], "basis": ["OBS-3"], "reason": None}},
            }

        self.expect("EV-R-020", mutate)

    def test_evr021_needs_repair_without_blocking(self) -> None:
        def mutate(e):
            e["findings"] = []
            e["obligations"]["O2"] = {"status": "PASS", "basis": ["OBS-3"], "note": None}
            e["prior_round"]["dispositions"] = {
                "F1-1": {"L-1": {"disposition": "resolved", "successors": [], "basis": ["OBS-3"], "reason": None}},
                "F1-2": {"L-2": {"disposition": "resolved", "successors": [], "basis": ["OBS-3"], "reason": None}},
                "F1-3": {"L-1": {"disposition": "resolved", "successors": [], "basis": ["OBS-3"], "reason": None},
                         "L-2": {"disposition": "resolved", "successors": [], "basis": ["OBS-3"], "reason": None}},
            }

        self.expect("EV-R-021", mutate)

    # A pre-run gap is not something the producer can fix, so it must not be
    # routed as a repair.
    def test_evr022_unknown_without_finding_must_be_evidence_gap(self) -> None:
        def mutate(e):
            e["verdict"] = "blocked"
            e["blocked_reason"] = "something_else"
            e["findings"] = []
            e["obligations"]["O2"] = {"status": "PASS", "basis": ["OBS-3"], "note": None}
            e["prior_round"]["dispositions"] = {
                "F1-1": {"L-1": {"disposition": "resolved", "successors": [], "basis": ["OBS-3"], "reason": None}},
                "F1-2": {"L-2": {"disposition": "resolved", "successors": [], "basis": ["OBS-3"], "reason": None}},
                "F1-3": {"L-1": {"disposition": "resolved", "successors": [], "basis": ["OBS-3"], "reason": None},
                         "L-2": {"disposition": "resolved", "successors": [], "basis": ["OBS-3"], "reason": None}},
            }

        self.expect("EV-R-022", mutate)


class VerifyStatusTest(unittest.TestCase):
    def execution(self, **overrides) -> dict:
        base = {"timed_out": False, "signal": None, "exit_code": 0, "outcome": "completed"}
        base.update(overrides)
        return base

    def test_outcome_order_is_fixed(self) -> None:
        self.assertEqual(derive_execution_outcome(self.execution(timed_out=True, signal=9)), "timed_out")
        self.assertEqual(derive_execution_outcome(self.execution(signal=9)), "signalled")
        self.assertEqual(derive_execution_outcome(self.execution(exit_code=None)), "launch_failed")
        self.assertEqual(derive_execution_outcome(self.execution()), "completed")

    # V1: zero tests is SKIP - nothing ran, so nothing was proven (G-7).
    def test_v1_zero_counts_is_skip_not_pass(self) -> None:
        counts = {"total": 0, "passed": 0, "failed": 0, "skipped": 0, "errors": 0}
        self.assertEqual(derive_test_status(self.execution(), counts), "SKIP")

    # E5-2 / V17e: a clean exit with no XML at all is also SKIP, never PASS.
    def test_v17e_completed_exit_zero_without_xml_is_skip(self) -> None:
        self.assertEqual(derive_test_status(self.execution(), None), "SKIP")

    def test_nonzero_exit_without_xml_is_error(self) -> None:
        self.assertEqual(derive_test_status(self.execution(exit_code=1), None), "ERROR")

    # V18: a green exit code with failures in the XML is still a FAIL.
    def test_v18_ignore_failures_still_fails(self) -> None:
        counts = {"total": 4, "passed": 3, "failed": 1, "skipped": 0, "errors": 0}
        self.assertEqual(derive_test_status(self.execution(exit_code=0), counts), "FAIL")

    # V3: counts that do not add up are not a quiet pass.
    def test_v3_inconsistent_counts_are_error(self) -> None:
        counts = {"total": 4, "passed": 3, "failed": 0, "skipped": 0, "errors": 0}
        self.assertEqual(derive_test_status(self.execution(), counts), "ERROR")

    def test_passing_counts(self) -> None:
        counts = {"total": 4, "passed": 4, "failed": 0, "skipped": 0, "errors": 0}
        self.assertEqual(derive_test_status(self.execution(), counts), "PASS")

    def test_command_status(self) -> None:
        self.assertEqual(derive_command_status(self.execution()), "PASS")
        self.assertEqual(derive_command_status(self.execution(exit_code=2)), "FAIL")
        self.assertEqual(derive_command_status(self.execution(timed_out=True)), "ERROR")


class VerifyEnvelopeTest(unittest.TestCase):
    def plan(self) -> dict:
        return {
            "verify_plan_id": "VP-1",
            "operation_id": "OP-1",
            "invocation_id": "O1@44136fa355b3678a",
            "candidate_fingerprint": "sha256:" + "a" * 64,
            "result_kind": "test",
            "subject": {"kind": "obligation", "id": "O1"},
            "workspace": "/w",
            "expected_argv": ["./build.sh", "--no-build-cache", ":app:test"],
            "selector": ":app:test",
            "assigned_resources": [{"resource_id": "db"}],
        }

    def envelope(self, **overrides) -> dict:
        base = {
            **{k: self.plan()[k] for k in (
                "verify_plan_id", "operation_id", "invocation_id",
                "candidate_fingerprint", "result_kind", "subject",
            )},
            "target_package_digest": "sha256:" + "p" * 64,
            "target_package_version": "0.1.0",
            "tool_versions": {"jdk": "21"},
            "execution": {"cwd": "/w", "argv": ["./build.sh", "--no-build-cache", ":app:test"],
                          "timed_out": False, "signal": None, "exit_code": 0, "outcome": "completed"},
            "result": {"status": "PASS", "selector": ":app:test",
                       "tests": {"total": 2, "passed": 2, "failed": 0, "skipped": 0, "errors": 0}},
            "artifacts": [{"path": "test-results/a.xml", "kind": "junit_xml"}],
            "cleanup": [{"resource_id": "db", "status": "released", "evidence": "SHOW SCHEMAS empty"}],
        }
        base.update(overrides)
        return base

    def validate(self, envelope: dict) -> None:
        validate_verify(
            envelope,
            plan=self.plan(),
            contract_tool_versions={"jdk": "21"},
            contract_package_digest="sha256:" + "p" * 64,
            contract_package_version="0.1.0",
        )

    def test_valid_envelope(self) -> None:
        self.validate(self.envelope())

    def test_evv001_echo_mismatch(self) -> None:
        with self.assertRaises(EnvelopeInvalid) as ctx:
            self.validate(self.envelope(operation_id="OP-9"))
        self.assertEqual(ctx.exception.code, "EV-V-001")

    # EV-V-004: argv is compared item by item, not by a joined string.
    def test_evv004_argv_must_match_exactly(self) -> None:
        envelope = self.envelope()
        envelope["execution"]["argv"] = ["./build.sh", ":app:test"]
        with self.assertRaises(EnvelopeInvalid) as ctx:
            self.validate(envelope)
        self.assertEqual(ctx.exception.code, "EV-V-004")

    def test_evv011_outcome_is_recomputed(self) -> None:
        envelope = self.envelope()
        envelope["execution"]["outcome"] = "completed"
        envelope["execution"]["timed_out"] = True
        with self.assertRaises(EnvelopeInvalid) as ctx:
            self.validate(envelope)
        self.assertEqual(ctx.exception.code, "EV-V-011")

    # E5-2: counts and XML must agree, so "PASS with no XML" has no shape.
    def test_evv012_pass_without_xml_has_no_representation(self) -> None:
        envelope = self.envelope(artifacts=[])
        with self.assertRaises(EnvelopeInvalid) as ctx:
            self.validate(envelope)
        self.assertEqual(ctx.exception.code, "EV-V-012")

    def test_evv012_status_is_recomputed(self) -> None:
        envelope = self.envelope()
        envelope["result"]["tests"] = {"total": 2, "passed": 1, "failed": 1, "skipped": 0, "errors": 0}
        with self.assertRaises(EnvelopeInvalid) as ctx:
            self.validate(envelope)
        self.assertEqual(ctx.exception.code, "EV-V-012")

    def test_evv014_artifact_path_escape(self) -> None:
        envelope = self.envelope(artifacts=[{"path": "../etc/passwd", "kind": "junit_xml"}])
        with self.assertRaises(EnvelopeInvalid) as ctx:
            self.validate(envelope)
        self.assertEqual(ctx.exception.code, "EV-V-014")

    def test_evv016_cleanup_must_cover_assigned_resources(self) -> None:
        envelope = self.envelope(cleanup=[])
        with self.assertRaises(EnvelopeInvalid) as ctx:
            self.validate(envelope)
        self.assertEqual(ctx.exception.code, "EV-V-016")

    # V19-V23: a leaked resource must not be usable as evidence.
    def test_usable_requires_released_resources(self) -> None:
        envelope = self.envelope()
        self.assertTrue(verify_usable(envelope, candidate_fingerprint="sha256:" + "a" * 64))
        envelope["cleanup"][0]["status"] = "failed"
        self.assertFalse(verify_usable(envelope, candidate_fingerprint="sha256:" + "a" * 64))

    def test_usable_requires_matching_candidate(self) -> None:
        self.assertFalse(verify_usable(self.envelope(), candidate_fingerprint="sha256:" + "9" * 64))


if __name__ == "__main__":
    unittest.main()


class ContractReviewOutcomeVocabularyTest(unittest.TestCase):
    """STATE-TABLE §406: a contract review answers contract_pass / contract_findings.

    `contract_hold` is the hold reason those findings produce downstream, not the
    token the reviewer prints.  Deriving the review-stage vocabulary here makes
    the stage unpassable, because every token it can derive lies outside the set
    `allowed_outcomes("contract_review")` admits - which is how a real reviewer
    round was rejected whichever token it printed.
    """

    def _envelope(self, contract_findings):
        envelope = {k: v for k, v in HEADER.items()}
        envelope.update({
            "review_round": None,
            "candidate_fingerprint": None,
            "verdict": "needs_repair" if contract_findings else "accepted",
            "blocked_reason": None,
            "obligations": {},
            "findings": [],
            "contract_findings": contract_findings,
            "improvements": [],
            "prior_round": None,
            "remaining": [],
        })
        return envelope

    def _validate(self, envelope, printed):
        echo = {k: envelope[k] for k in (
            "target_id", "change", "pack", "contract_hash", "bundle_hash",
            "plan_fingerprint", "requirement_fingerprint")}
        validate_review(envelope, printed, expected_header=echo,
                        active_obligations=[], bundle_observations={},
                        stage="contract_review")

    def test_clean_contract_review_passes_with_contract_pass(self) -> None:
        self._validate(self._envelope([]), "contract_pass")

    def test_findings_are_reported_as_contract_findings(self) -> None:
        finding = {"id": "C1-1", "kind": "missing_source",
                   "obligation_refs": [], "source_ref": "proposal.md",
                   "claim": "no source for the deliverable",
                   "requested_revision": "cite one"}
        self._validate(self._envelope([finding]), "contract_findings")

    def test_hold_reason_is_not_an_outcome_token(self) -> None:
        with self.assertRaises(EnvelopeInvalid) as raised:
            self._validate(self._envelope([]), "accepted")
        self.assertEqual(raised.exception.code, "EV-R-001")
