"""Review and verify envelope validation (ENVELOPES §2-§3).

Everything a reviewer or a verify CLI hands back is untrusted until it has been
through here.  Two design points carry most of the weight:

* **Echo fields are the anti-drift mechanism** (G-5).  The engine sends the
  identity values it froze and the envelope must return them byte-identical; a
  reviewer that answered about a different candidate cannot look like it
  answered about this one.
* **A PASS must point at engine-produced evidence** (EV-R-032..034).  The
  reviewer's own prose can never be the basis for a PASS, which is what keeps a
  confident model from talking an obligation into being satisfied.

Validation order is pinned (§7) so that a fixture's expected code does not
depend on dict iteration; ``validate_review`` and ``validate_verify`` follow it
exactly.
"""
from __future__ import annotations

import json
from typing import Any, Iterable, Sequence

from ..execution import _unique_object
from .errors import EnvelopeInvalid

REVIEW_BEGIN = "<!-- orch-pack-review:v1 -->"
REVIEW_END = "<!-- /orch-pack-review -->"
OUTCOME_PREFIX = "ORCHESTRATOR_OUTCOME:"

REVIEW_HEADER_KEYS = frozenset({
    "envelope", "policy_version", "kind", "target_id", "change", "pack",
    "contract_version", "role", "review_round", "candidate_fingerprint",
    "contract_hash", "bundle_hash", "plan_fingerprint", "requirement_fingerprint", "ext",
})
REVIEW_BODY_KEYS = frozenset({
    "verdict", "blocked_reason", "obligations", "findings", "contract_findings",
    "improvements", "prior_round", "remaining",
})
OBLIGATION_RESULT_KEYS = frozenset({"status", "basis", "note"})
FINDING_KEYS = frozenset({
    "id", "severity", "blocking", "obligation_refs", "title", "evidence", "requested_change",
    "observations", "locators", "lineage_ids", "prior_refs", "recurrence_of",
})
PRIOR_REF_KEYS = frozenset({
    "finding_id", "lineage_id", "relation", "obligation_ref", "counterexample", "causal_note",
})
DISPOSITION_KEYS = frozenset({"disposition", "successors", "basis", "reason"})
PRIOR_ROUND_KEYS = frozenset({"round", "contract_hash", "review_sha256", "dispositions"})
REMAINING_KEYS = frozenset({"obligation_id", "kind", "owner", "check"})
IMPROVEMENT_KEYS = frozenset({"id", "benefit", "cost", "enable_condition"})
CONTRACT_FINDING_KEYS = frozenset({
    "id", "kind", "obligation_refs", "source_ref", "claim", "requested_revision",
})

STATUSES = frozenset({"PASS", "FAIL", "UNKNOWN"})
VERDICTS = frozenset({"accepted", "needs_repair", "blocked"})
DISPOSITIONS = frozenset({
    "resolved", "residual", "repeat", "withdrawn", "superseded_by_contract",
})
TERMINAL_DISPOSITIONS = frozenset({"resolved", "withdrawn", "superseded_by_contract"})
CONTINUING_DISPOSITIONS = frozenset({"residual", "repeat", "superseded_by_contract"})
REMAINING_KINDS = frozenset({"deferred", "unknown_evidence", "external"})


def _reject(code: str, detail: str) -> None:
    raise EnvelopeInvalid(code, detail)


def _exact_keys(code: str, where: str, value: Any, expected: frozenset[str]) -> None:
    if not isinstance(value, dict):
        _reject(code, f"{where} is not an object")
    actual = set(value)
    if actual != expected:
        _reject(code, f"{where} missing={sorted(expected - actual)} extra={sorted(actual - expected)}")


def parse_framing(text: str) -> tuple[dict[str, Any], str]:
    """Extract the single marker block and the trailing typed outcome (§2.1).

    A second marker block is rejected rather than "last one wins": two blocks
    mean the response contains two answers, and picking one silently would let a
    model hedge.
    """
    if text.count(REVIEW_BEGIN) != 1 or text.count(REVIEW_END) != 1:
        _reject("framing", f"expected exactly one marker block, found {text.count(REVIEW_BEGIN)}")
    start = text.index(REVIEW_BEGIN) + len(REVIEW_BEGIN)
    end = text.index(REVIEW_END)
    if end < start:
        _reject("framing", "end marker precedes begin marker")
    raw = text[start:end].strip()

    lines = [line for line in text.strip().splitlines() if line.strip()]
    if not lines or not lines[-1].startswith(OUTCOME_PREFIX):
        _reject("framing", "last line is not a typed outcome")
    outcome = lines[-1][len(OUTCOME_PREFIX):].strip()

    try:
        envelope = json.loads(raw, object_pairs_hook=_unique_object)
    except ValueError as exc:
        _reject("G-1", f"envelope JSON is not parseable: {exc}")
    if not isinstance(envelope, dict):
        _reject("G-1", "envelope is not a JSON object")
    return envelope, outcome


def derive_outcome(envelope: dict[str, Any], stage: str = "review") -> str:
    """The outcome the engine derives; the printed line is redundancy, not input.

    The two review stages do not share a vocabulary (STATE-TABLE §406, §411).
    A contract review answers `contract_pass` / `contract_findings`; `contract_hold`
    is the *hold reason* those findings produce downstream, never the outcome
    token itself.  Deriving review's vocabulary for a contract review makes the
    stage unpassable: every token it can derive is outside the set
    `allowed_outcomes("contract_review")` admits.
    """
    if stage == "contract_review":
        return "contract_findings" if envelope.get("contract_findings") else "contract_pass"
    if envelope.get("contract_findings"):
        return "contract_hold"
    return str(envelope.get("verdict"))


def validate_review(
    envelope: dict[str, Any],
    outcome: str,
    *,
    expected_header: dict[str, Any],
    active_obligations: Sequence[str],
    bundle_observations: dict[str, dict[str, Any]],
    prior_findings: dict[str, Sequence[str]] | None = None,
    stage: str = "review",
) -> None:
    """Validate a review envelope in the pinned order (§7).

    ``stage`` selects the branch: ``contract_review`` skips every obligation,
    finding and lineage rule because the contract has not been executed yet, so
    there is nothing for them to range over.
    """
    contract_branch = stage == "contract_review"

    # G-2: closed key set, header then body.
    _exact_keys("G-2", "envelope", envelope, REVIEW_HEADER_KEYS | REVIEW_BODY_KEYS)

    if envelope["envelope"] != "pack-review":
        _reject("G-2", f"envelope {envelope['envelope']!r}")
    if envelope["policy_version"] != "pack-v1":
        _reject("G-2", f"policy_version {envelope['policy_version']!r}")
    if envelope["kind"] != "final_review":
        # evidence_request is a v2 reserved word; v1 must not accept it.
        _reject("EV-R-002", f"kind {envelope['kind']!r}")
    if envelope["role"] != "reviewer":
        _reject("G-2", f"role {envelope['role']!r}")

    # Header nullability differs by branch (joint-r5).
    if contract_branch:
        for field in ("review_round",):
            if envelope[field] is not None:
                _reject("EV-R-002", f"contract_review must have {field} null")
        if envelope["prior_round"] is not None:
            _reject("EV-R-002", "contract_review must have prior_round null")
    else:
        if not isinstance(envelope["review_round"], int) or envelope["review_round"] < 1:
            _reject("G-5", f"review_round {envelope['review_round']!r}")

    # G-5: echo fields must come back byte-identical.
    for field, expected in expected_header.items():
        if expected is None:
            continue
        if envelope.get(field) != expected:
            _reject("G-5", f"{field}: echoed {envelope.get(field)!r} != sent {expected!r}")

    # EV-R-001: the printed outcome must agree with what the envelope means.
    derived = derive_outcome(envelope, stage)
    if outcome != derived:
        _reject("EV-R-001", f"printed {outcome!r} != derived {derived!r}")

    verdict = envelope["verdict"]
    if verdict not in VERDICTS:
        _reject("G-2", f"verdict {verdict!r}")

    if contract_branch:
        _validate_contract_branch(envelope)
        return

    _validate_obligations(envelope, active_obligations, bundle_observations)
    _validate_findings(envelope, active_obligations, bundle_observations, prior_findings or {})
    _validate_prior_round(envelope, prior_findings or {})
    _validate_verdict(envelope)


def _validate_contract_branch(envelope: dict[str, Any]) -> None:
    if envelope["findings"]:
        _reject("EV-R-002", "contract_review findings must be empty; use contract_findings")
    if envelope["remaining"]:
        _reject("EV-R-002", "contract_review remaining must be empty")
    if envelope["verdict"] == "blocked":
        _reject("EV-R-002", "contract_review cannot be blocked")
    for item in envelope["contract_findings"]:
        _exact_keys("EV-R-002", f"contract_finding {item.get('id')!r}", item, CONTRACT_FINDING_KEYS)
    if envelope["contract_findings"] and envelope["verdict"] != "needs_repair":
        _reject("EV-R-002", "contract_findings require needs_repair")


def _validate_obligations(
    envelope: dict[str, Any],
    active: Sequence[str],
    observations: dict[str, dict[str, Any]],
) -> None:
    results = envelope["obligations"]
    if not isinstance(results, dict):
        _reject("EV-R-030", "obligations is not an object")
    if set(results) != set(active):
        _reject(
            "EV-R-030",
            f"missing={sorted(set(active) - set(results))} extra={sorted(set(results) - set(active))}",
        )

    remaining_by_id = {item.get("obligation_id"): item for item in envelope["remaining"]}

    for ob_id, result in results.items():
        _exact_keys("EV-R-031", f"obligation {ob_id}", result, OBLIGATION_RESULT_KEYS)
        if result["status"] not in STATUSES:
            _reject("EV-R-031", f"obligation {ob_id}: status {result['status']!r}")
        if not isinstance(result["basis"], list):
            _reject("EV-R-031", f"obligation {ob_id}: basis is not an array")

        if result["status"] == "PASS":
            if not result["basis"]:
                _reject("EV-R-032", f"obligation {ob_id}: PASS needs a basis")
            for obs_id in result["basis"]:
                obs = observations.get(obs_id)
                if obs is None:
                    _reject("EV-R-032", f"obligation {ob_id}: unknown observation {obs_id!r}")
                if not obs.get("usable"):
                    _reject("EV-R-032", f"obligation {ob_id}: observation {obs_id} is not usable")

        if result["status"] == "UNKNOWN":
            item = remaining_by_id.get(ob_id)
            if item is None or item.get("kind") != "unknown_evidence":
                _reject("EV-R-035", f"obligation {ob_id}: UNKNOWN needs remaining.unknown_evidence")

    for item in envelope["remaining"]:
        _exact_keys("EV-R-035", "remaining item", item, REMAINING_KEYS)
        if item["kind"] not in REMAINING_KINDS:
            _reject("EV-R-035", f"remaining kind {item['kind']!r}")
        if not item["owner"] or not item["check"]:
            _reject("EV-R-035", "remaining owner/check must be non-empty")


def _selector_backed_pass(
    ob_id: str, basis: Sequence[str], observations: dict[str, dict[str, Any]]
) -> bool:
    for obs_id in basis:
        obs = observations[obs_id]
        projection = obs.get("projection", {})
        if (
            obs["kind"] == "verify"
            and projection.get("result_kind") == "test"
            and projection.get("subject") == {"kind": "obligation", "id": ob_id}
            and projection.get("status") == "PASS"
        ):
            return True
    return False


def check_selector_backed(
    envelope: dict[str, Any],
    observations: dict[str, dict[str, Any]],
    selectors: dict[str, str | None],
) -> None:
    """EV-R-033/034: what may stand behind a PASS depends on the selector.

    An obligation that names a selector can only pass on a verify observation
    for *that* obligation - a read of the source, or a SKIPped run, is not
    evidence that the test passed.
    """
    for ob_id, result in envelope["obligations"].items():
        if result["status"] != "PASS":
            continue
        if selectors.get(ob_id):
            if not _selector_backed_pass(ob_id, result["basis"], observations):
                _reject("EV-R-033", f"obligation {ob_id}: PASS needs a passing verify observation")
        else:
            kinds = {observations[o]["kind"] for o in result["basis"]}
            if not kinds <= {"read", "contract", "manifest_slice"}:
                _reject("EV-R-034", f"obligation {ob_id}: unexpected basis kinds {sorted(kinds)}")


def _validate_findings(
    envelope: dict[str, Any],
    active: Sequence[str],
    observations: dict[str, dict[str, Any]],
    prior_findings: dict[str, Sequence[str]],
) -> None:
    findings = envelope["findings"]
    seen_ids: set[str] = set()
    round_no = envelope["review_round"]

    for finding in findings:
        _exact_keys("EV-R-040", f"finding {finding.get('id')!r}", finding, FINDING_KEYS)
        fid = finding["id"]
        if fid in seen_ids:
            _reject("EV-R-040", f"duplicate finding id {fid!r}")
        seen_ids.add(fid)
        if not fid.startswith(f"F{round_no}-"):
            _reject("EV-R-040", f"finding id {fid!r} does not match F{round_no}-<n>")

        # A finding is by definition an unmet requirement, so a non-blocking or
        # Low one is a category error: that belongs in `improvements`.
        if finding["blocking"] is not True or finding["severity"] not in {"High", "Medium"}:
            _reject("EV-R-043", f"finding {fid}: blocking/severity must be true/High|Medium")

        refs = finding["obligation_refs"]
        if not refs or not set(refs) <= set(active):
            _reject("EV-R-044", f"finding {fid}: obligation_refs {refs!r}")
        for field in ("title", "evidence", "requested_change"):
            if not finding[field]:
                _reject("EV-R-045", f"finding {fid}: {field} must be non-empty")
        if not set(finding["observations"]) <= set(observations):
            _reject("EV-R-046", f"finding {fid}: unknown observations")

        lineage_ids = finding["lineage_ids"]
        if not lineage_ids:
            _reject("EV-R-041", f"finding {fid}: lineage_ids must be non-empty")

        prior_refs = finding["prior_refs"]
        for edge in prior_refs:
            _exact_keys("EV-R-047", f"finding {fid} prior_ref", edge, PRIOR_REF_KEYS)
            prior_id = edge["finding_id"]
            if prior_id not in prior_findings:
                _reject("EV-R-047", f"finding {fid}: unknown prior finding {prior_id!r}")
            if edge["lineage_id"] not in prior_findings[prior_id]:
                _reject(
                    "EV-R-047",
                    f"finding {fid}: lineage {edge['lineage_id']!r} not carried by {prior_id}",
                )

        if prior_refs:
            edge_lineages = {edge["lineage_id"] for edge in prior_refs}
            if set(lineage_ids) != edge_lineages:
                _reject(
                    "EV-R-041",
                    f"finding {fid}: lineage_ids {sorted(lineage_ids)} != edges {sorted(edge_lineages)}",
                )
        elif finding["recurrence_of"] is not None:
            if lineage_ids != [finding["recurrence_of"]["lineage_id"]]:
                _reject("EV-R-041", f"finding {fid}: recurrence lineage mismatch")
        elif len(lineage_ids) != 1:
            _reject("EV-R-041", f"finding {fid}: a new finding carries exactly one lineage")

    # EV-R-036 / 037: findings and obligation statuses must agree both ways.
    referenced = {ref for finding in findings for ref in finding["obligation_refs"]}
    for ob_id, result in envelope["obligations"].items():
        if result["status"] == "FAIL" and ob_id not in referenced:
            _reject("EV-R-036", f"obligation {ob_id} FAILs with no finding referencing it")
    for ob_id in referenced:
        if envelope["obligations"][ob_id]["status"] not in {"FAIL", "UNKNOWN"}:
            _reject("EV-R-037", f"obligation {ob_id} is referenced by a finding but not FAIL/UNKNOWN")

    for item in envelope["improvements"]:
        _exact_keys("G-8", f"improvement {item.get('id')!r}", item, IMPROVEMENT_KEYS)
        for field in ("benefit", "cost", "enable_condition"):
            if not item[field]:
                _reject("G-8", f"improvement {item['id']}: {field} must be non-empty")


def _validate_prior_round(
    envelope: dict[str, Any], prior_findings: dict[str, Sequence[str]]
) -> None:
    prior = envelope["prior_round"]
    if envelope["review_round"] == 1:
        if prior is not None:
            _reject("EV-R-050", "round 1 must have prior_round null")
        return
    if prior is None:
        _reject("EV-R-050", "prior_round is required from round 2")
    _exact_keys("EV-R-050", "prior_round", prior, PRIOR_ROUND_KEYS)
    if prior["round"] != envelope["review_round"] - 1:
        _reject("EV-R-050", f"prior_round.round {prior['round']} != {envelope['review_round'] - 1}")

    dispositions = prior["dispositions"]
    if set(dispositions) != set(prior_findings):
        _reject(
            "EV-R-051",
            f"dispositions keys {sorted(dispositions)} != prior findings {sorted(prior_findings)}",
        )
    for fid, per_lineage in dispositions.items():
        if set(per_lineage) != set(prior_findings[fid]):
            _reject("EV-R-051", f"dispositions[{fid}] lineage keys mismatch")
        for lineage, entry in per_lineage.items():
            _exact_keys("EV-R-052", f"dispositions[{fid}][{lineage}]", entry, DISPOSITION_KEYS)
            if entry["disposition"] not in DISPOSITIONS:
                _reject("EV-R-052", f"dispositions[{fid}][{lineage}]: {entry['disposition']!r}")
            _validate_disposition_shape(fid, lineage, entry, envelope)

    _validate_lineage_layer(dispositions)


def _validate_disposition_shape(
    fid: str, lineage: str, entry: dict[str, Any], envelope: dict[str, Any]
) -> None:
    disposition = entry["disposition"]
    successors = entry["successors"]
    finding_ids = {f["id"] for f in envelope["findings"]}

    if disposition == "resolved":
        if successors or not entry["basis"] or entry["reason"] is not None:
            _reject("EV-R-053", f"{fid}/{lineage}: resolved needs basis, no successors, no reason")
    elif disposition == "withdrawn":
        if successors or not entry["reason"]:
            _reject("EV-R-053", f"{fid}/{lineage}: withdrawn needs a reason and no successors")
    elif disposition in {"residual", "repeat"}:
        if not successors:
            _reject("EV-R-053", f"{fid}/{lineage}: {disposition} needs successors")
        for successor in successors:
            if successor not in finding_ids:
                _reject("EV-R-053", f"{fid}/{lineage}: unknown successor {successor!r}")
            # EV-R-048: the edge and the disposition must describe each other.
            edges = {
                (e["finding_id"], e["lineage_id"], e["relation"])
                for f in envelope["findings"] if f["id"] == successor
                for e in f["prior_refs"]
            }
            if (fid, lineage, disposition) not in edges:
                _reject("EV-R-048", f"{fid}/{lineage} -> {successor}: no matching prior_ref edge")


def _validate_lineage_layer(dispositions: dict[str, dict[str, dict[str, Any]]]) -> None:
    """EV-R-04B: one lineage cannot be both finished and still running."""
    by_lineage: dict[str, set[str]] = {}
    for per_lineage in dispositions.values():
        for lineage, entry in per_lineage.items():
            by_lineage.setdefault(lineage, set()).add(entry["disposition"])

    for lineage, kinds in by_lineage.items():
        if kinds <= TERMINAL_DISPOSITIONS and len(kinds) == 1:
            continue
        if kinds <= CONTINUING_DISPOSITIONS:
            continue
        _reject("EV-R-04B", f"lineage {lineage}: inconsistent dispositions {sorted(kinds)}")


def _validate_verdict(envelope: dict[str, Any]) -> None:
    verdict = envelope["verdict"]
    statuses = {r["status"] for r in envelope["obligations"].values()}
    blocking = [f for f in envelope["findings"] if f["blocking"]]
    unknown_remaining = any(i["kind"] == "unknown_evidence" for i in envelope["remaining"])

    if envelope["contract_findings"]:
        # The contract is wrong, so the producer is the wrong addressee - that
        # exit is taken regardless of what else the reviewer found.
        if verdict not in {"needs_repair", "blocked"}:
            _reject("EV-R-020", "contract_findings require needs_repair or blocked")

    if verdict == "accepted":
        if statuses != {"PASS"} or blocking or unknown_remaining:
            _reject("EV-R-020", "accepted requires all PASS, no blocking finding, no unknown_evidence")
    elif verdict == "needs_repair":
        if not blocking:
            _reject("EV-R-021", "needs_repair requires at least one blocking finding")
    elif verdict == "blocked":
        if envelope["blocked_reason"] is None:
            _reject("EV-R-022", "blocked requires a blocked_reason")
        if "UNKNOWN" in statuses and not blocking and envelope["blocked_reason"] != "evidence_gap":
            # The producer cannot fix a missing pre-run, so routing it as a
            # repair would spend a round on the wrong actor.
            _reject("EV-R-022", "UNKNOWN without a blocking finding must be blocked(evidence_gap)")


# --------------------------------------------------------------------------
# verify envelope (§3)
# --------------------------------------------------------------------------

def derive_execution_outcome(execution: dict[str, Any]) -> str:
    """EV-V-011: recomputed by the engine in this fixed order."""
    if execution.get("timed_out") is True:
        return "timed_out"
    if execution.get("signal") is not None:
        return "signalled"
    if execution.get("exit_code") is None:
        return "launch_failed"
    return "completed"


def derive_test_status(execution: dict[str, Any], tests: dict[str, int] | None) -> str:
    """EV-V-012: one legal representation per real situation.

    ``tests is None`` means no JUnit XML was produced.  Combined with a clean
    exit that is a SKIP - nothing ran, nothing was proven - and never a PASS;
    G-7 exists precisely so an empty run cannot be read as success.
    """
    outcome = derive_execution_outcome(execution)
    if outcome != "completed":
        return "ERROR"
    if tests is None:
        return "ERROR" if execution.get("exit_code") != 0 else "SKIP"
    total = tests.get("total", 0)
    if total == 0:
        return "SKIP"
    if tests.get("errors", 0) + tests.get("failed", 0) > 0:
        return "FAIL"
    if tests.get("passed", 0) + tests.get("skipped", 0) != total:
        return "ERROR"
    return "PASS"


def derive_command_status(execution: dict[str, Any]) -> str:
    """EV-V-017."""
    if derive_execution_outcome(execution) != "completed":
        return "ERROR"
    return "PASS" if execution.get("exit_code") == 0 else "FAIL"


def validate_verify(
    envelope: dict[str, Any],
    *,
    plan: dict[str, Any],
    contract_tool_versions: dict[str, str],
    contract_package_digest: str,
    contract_package_version: str,
) -> None:
    """Run the EV-V codes in order (§7)."""
    for field in ("verify_plan_id", "operation_id", "invocation_id", "candidate_fingerprint",
                  "result_kind", "subject"):
        if envelope.get(field) != plan.get(field):
            _reject("EV-V-001", f"{field}: {envelope.get(field)!r} != plan {plan.get(field)!r}")

    if envelope.get("target_package_digest") != contract_package_digest:
        _reject("EV-V-002", "target_package_digest does not match the frozen contract")
    if envelope.get("target_package_version") != contract_package_version:
        _reject("EV-V-002", "target_package_version does not match the frozen contract")

    if envelope.get("tool_versions") != contract_tool_versions:
        _reject("EV-V-003", "tool_versions must equal the contract's per-invocation set")

    execution = envelope["execution"]
    if execution.get("cwd") != plan.get("workspace"):
        _reject("EV-V-004", f"cwd {execution.get('cwd')!r} != workspace {plan.get('workspace')!r}")
    if execution.get("argv") != plan.get("expected_argv"):
        _reject("EV-V-004", "argv must equal expected_argv item by item")

    result = envelope["result"]
    if envelope["result_kind"] == "test":
        if result.get("selector") != plan.get("selector"):
            _reject("EV-V-005", "result.selector != plan.selector")
    elif result.get("selector") is not None:
        _reject("EV-V-005", "command results carry no selector")

    if execution.get("outcome") != derive_execution_outcome(execution):
        _reject("EV-V-011", f"outcome {execution.get('outcome')!r} disagrees with the recomputation")

    artifacts = envelope.get("artifacts", [])
    has_xml = any(a.get("kind") == "junit_xml" for a in artifacts)
    tests = result.get("tests")
    if envelope["result_kind"] == "test":
        # Representation is unique: XML present iff counts present.
        if has_xml != (tests is not None):
            _reject("EV-V-012", "tests counts and junit_xml artifacts must agree")
        recomputed = derive_test_status(execution, tests)
        if result.get("status") != recomputed:
            _reject("EV-V-012", f"status {result.get('status')!r} != recomputed {recomputed!r}")
    else:
        recomputed = derive_command_status(execution)
        if result.get("status") != recomputed:
            _reject("EV-V-017", f"status {result.get('status')!r} != recomputed {recomputed!r}")
        if result.get("exit_code") != execution.get("exit_code"):
            _reject("EV-V-017", "command.exit_code != execution.exit_code")

    for artifact in artifacts:
        path = artifact.get("path", "")
        if path.startswith("/") or ".." in path.split("/"):
            _reject("EV-V-014", f"artifact path escapes artifacts_root: {path!r}")
        if artifact.get("kind") not in {"junit_xml", "log", "coverage", "other"}:
            _reject("EV-V-014", f"artifact kind {artifact.get('kind')!r}")

    cleanup = envelope.get("cleanup", [])
    assigned = {r["resource_id"] for r in plan.get("assigned_resources", [])}
    reported = [c["resource_id"] for c in cleanup]
    if len(reported) != len(set(reported)) or set(reported) != assigned:
        _reject("EV-V-016", f"cleanup resources {sorted(set(reported))} != assigned {sorted(assigned)}")
    for entry in cleanup:
        if entry.get("status") not in {"released", "failed", "not_needed"}:
            _reject("EV-V-016", f"cleanup status {entry.get('status')!r}")
        if not entry.get("evidence"):
            _reject("EV-V-016", "cleanup evidence must be non-empty")


def verify_usable(envelope: dict[str, Any], *, candidate_fingerprint: str) -> bool:
    """§3.4: only a clean, complete, released run may back a PASS."""
    if envelope.get("candidate_fingerprint") != candidate_fingerprint:
        return False
    if envelope["execution"].get("outcome") != "completed":
        return False
    return all(c.get("status") in {"released", "not_needed"} for c in envelope.get("cleanup", []))
