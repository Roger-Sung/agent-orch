"""The uniform verification procedure run at every checkpoint (IDENTITIES §6.0).

Stage order is the substance of this module, not an implementation detail:

    A0  event / reconcile triage   <- before any sampling
    A   sample everything          -> tagged ok(value) | fail(code)
    A'  first failure by fixed priority
    B   decide by mode

A0 comes first because the failure codes stage A' produces are exactly what the
recovery events repair.  Sampling first would mean a tree with a FIFO in it, or
an unreachable requirement CLI, could never be repaired: the event that fixes it
would be unreachable behind the failure it is meant to clear (joint-r1 J3).
"""
from __future__ import annotations

from typing import Any, Callable, Sequence

# A' priority.  Fixed so that two runs over the same broken state report the
# same code (IDENTITIES §6.0).
SAMPLE_FAILURE_ORDER: tuple[str, ...] = (
    "manifest_unreadable",
    "requirement_unavailable",
    "environment_unavailable",
)

CANDIDATE_REJECT_ORDER: tuple[str, ...] = (
    "nested_repo_untracked",
    "ignore_file_not_bound",
    "unsupported_entry",
    "gitlink_replaced",
    "path_aliasing",
    "path_name_mismatch",
    "scope_enumeration_failed",
    "symlink_dir_in_scope",
    "nested_repo_in_scope",
    "excluded_source_in_scope",
)

MODES = {
    "baseline", "submission_gate", "compare_input", "compare_output",
}


class Sample:
    """One sampled value: ``ok(value)`` or ``fail(code)``."""

    __slots__ = ("value", "code")

    def __init__(self, value: Any = None, code: str | None = None) -> None:
        self.value = value
        self.code = code

    @property
    def ok(self) -> bool:
        return self.code is None

    @classmethod
    def of(cls, value: Any) -> "Sample":
        return cls(value=value)

    @classmethod
    def failed(cls, code: str) -> "Sample":
        return cls(code=code)


class GateResult:
    def __init__(self, stage: str, code: str | None = None, detail: Any = None) -> None:
        self.stage = stage
        self.code = code
        self.detail = detail

    @property
    def passed(self) -> bool:
        return self.code is None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"GateResult({self.stage!r}, {self.code!r})"


def triage_event(
    event: dict[str, Any],
    *,
    verify_record: Callable[[dict[str, Any]], str | None],
    execute: Callable[[dict[str, Any]], None],
) -> GateResult:
    """Stage A0 for a manual ``A_*`` event.

    Only checks that need no sampled value happen here - HMAC, scope, purpose,
    idempotency, ``expected``.  Anything requiring a fresh candidate or
    requirement would reintroduce the deadlock this stage exists to remove.
    """
    code = verify_record(event)
    if code is not None:
        return GateResult("A0", code)
    execute(event)
    return GateResult("A0")


def first_failure(samples: dict[str, Sample]) -> str | None:
    """Stage A': the first failing sample, ranked by *code* not by sample name.

    The order is over reject codes because that is what the spec pins, and what
    the recovery events key off; ranking by which dict key happened to hold the
    failure would make the reported code depend on the caller's naming.
    """
    failed = {sample.code for sample in samples.values() if not sample.ok}
    if not failed:
        return None
    for code in SAMPLE_FAILURE_ORDER:
        if code in failed:
            return code
    for code in CANDIDATE_REJECT_ORDER:
        if code in failed:
            return code
    # An unranked code is a spec gap, not something to paper over with an
    # arbitrary pick - surface it deterministically instead.
    return sorted(failed)[0]


def run_gate(
    mode: str,
    samples: dict[str, Sample],
    *,
    expected: dict[str, Any] | None = None,
) -> GateResult:
    """Stages A' and B for a non-event checkpoint."""
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}; event_gate and reconcile are triaged in A0")

    code = first_failure(samples)
    if code is not None:
        return GateResult("A'", code)

    expected = expected or {}

    # Step 1 - validity, which `baseline` also runs: an approval that has been
    # revoked invalidates everything downstream of it, contract or not.
    approval = samples.get("approval")
    if approval is not None and approval.value:
        state = approval.value
        if state.get("revoked"):
            return GateResult("B", "approval_revoked")
        if state.get("scope_mismatch"):
            return GateResult("B", "approval_scope_mismatch")
        for field in ("requirement", "plan_digest", "manifest_sha256"):
            if field in expected and state.get(field) not in (None, expected[field]):
                return GateResult("B", "approval_stale", {field: state.get(field)})

    dependencies = samples.get("dependencies")
    if dependencies is not None and dependencies.value:
        for name, dep in dependencies.value.items():
            if dep.get("revoked"):
                return GateResult("B", "dependency_stale", {"dependency": name})
            if dep.get("expired"):
                return GateResult("B", "dependency_expired", {"dependency": name})

    if mode == "baseline":
        # No contract baseline exists yet, so there is nothing to compare to.
        return GateResult("B")

    for field, code in (
        ("requirement", "requirement_changed"),
        ("manifest", "manifest_changed"),
        ("environment", "environment_changed"),
    ):
        sample = samples.get(field)
        if sample is not None and field in expected and sample.value != expected[field]:
            return GateResult("B", code, {"sampled": sample.value, "expected": expected[field]})

    if mode in {"compare_input", "compare_output", "submission_gate"}:
        candidate = samples.get("candidate")
        key = "candidate_output" if mode in {"compare_output", "submission_gate"} else "candidate_input"
        if candidate is not None and key in expected and candidate.value != expected[key]:
            return GateResult("B", "candidate_changed",
                              {"sampled": candidate.value, "expected": expected[key]})

    return GateResult("B")
