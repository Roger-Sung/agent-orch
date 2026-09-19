"""The pack-v1 policy entry points the controller will branch into (step 3).

Step 1 defined the surface and the store-backed behaviour; step 3 wired the
controller into it.  Every branch is still guarded on `policy_version`, so
nothing in the legacy or execution-v1 paths can change.

The four primitives exist because the legacy ``commit_run`` does five jobs at
once (seal, outcome routing, edge counting, task status, lease) and pack-v1
needs them separable: a recovery has to seal without dispatching, and an
acceptance has to be revoked without pretending the work never happened.
"""
from __future__ import annotations

import time

from typing import Any, Sequence

from .state_machine import PackMachine
from .store import PackStore

POLICY_VERSION = "pack-v1"

# §3.2a: the allowed outcome set is a policy constant, not derived from whether
# an envelope happens to be present.
ALLOWED_OUTCOMES: dict[str, frozenset[str]] = {
    "apply": frozenset({"produced", "producer_failed", "hold"}),
    "repair": frozenset({"produced", "producer_failed", "hold"}),
    "prerun": frozenset({"prerun_done", "verify_failed", "hold"}),
    "review": frozenset({"review", "review_invalid", "reviewer_failed", "hold"}),
    "contract_review": frozenset({"contract_pass", "contract_findings", "reviewer_failed", "hold"}),
}


def is_pack_v1(task: dict[str, Any]) -> bool:
    """A missing ``policy_version`` means legacy - the default must not change."""
    return task.get("policy_version") == POLICY_VERSION


def allowed_outcomes(stage: str) -> frozenset[str]:
    try:
        return ALLOWED_OUTCOMES[stage]
    except KeyError:
        raise KeyError(f"no allowed-outcome set for pack stage {stage!r}") from None


class PackPolicy:
    """Groups the four primitives over one store."""

    def __init__(self, store: PackStore) -> None:
        self.store = store
        self.machine = PackMachine(store)

    def commit_call_result(self, op_id: str, **kwargs: Any) -> None:
        """Seal a call and leave its result unconsumed (§8)."""
        self.machine.commit_call_result(op_id, **kwargs)

    # Which stage a pack in this state owes next.  Without it the run loop
    # re-dispatches whatever stage it ran last, however far the pack moved.
    STAGE_FOR_STATE = {
        "contracting": "contract_review",
        "claimed": "apply",
        "producing": "apply",
        "submitted": "prerun",
        "reviewing": "review",
        "judging": "review",
        "repair_pending": "repair",
    }

    @classmethod
    def stage_for_state(cls, pack_state: str) -> str | None:
        return cls.STAGE_FOR_STATE.get(pack_state.split("(", 1)[0])

    def transition_pack_task(self, pack_id: str, to_state: str, *,
                             task_status: str | None = None,
                             stop_reason: str | None = None,
                             current_stage: str | None = None,
                             hold_reason: str | None = None,
                             return_point: str | None = None,
                             continuation: str | None = None) -> None:
        """Move the pack *and* its task together, with no active run (§8).

        One statement's worth of state each, in whatever transaction the caller
        opened: writing them separately lets a crash land in between and leave a
        task queued for a stage its pack has already left, or held for a reason
        the pack no longer carries.
        """
        fields: dict[str, Any] = {"state": to_state, "hold_reason": hold_reason}
        if return_point is not None:
            fields["return_point"] = return_point
        if continuation is not None:
            fields["continuation"] = continuation
        self.store.update_pack(pack_id, **fields)

        if task_status is None:
            return
        stage = current_stage or self.stage_for_state(to_state)
        assignments = ["status=?", "stop_reason=?", "updated_at=?",
                       "revision=revision+1", "transitions_count=transitions_count+1"]
        values: list[Any] = [task_status, stop_reason, int(time.time() * 1000)]
        if stage is not None:
            assignments.insert(2, "current_stage=?")
            values.insert(2, stage)
        self.store.conn.execute(
            f"UPDATE tasks SET {', '.join(assignments)} WHERE id=?", (*values, pack_id))

    def recovery_commit(self, pack_id: str, op_id: str, *, receipt_ref: str,
                        result: str = "completed", call_binding: dict[str, Any] | None = None) -> str:
        """Seal an operation reconcile found dead-with-receipt, then consume it.

        Idempotent by construction: ``commit_call_result`` ignores a second
        result and ``consume_result`` reports ``already_consumed``.
        """
        self.machine.commit_call_result(
            op_id, result=result, receipt_ref=receipt_ref, call_binding=call_binding
        )
        return self.machine.consume_result(pack_id, op_id)

    def invalidate_acceptance(self, pack_id: str, *, reason: str) -> int:
        """Revoke an acceptance and raise the floor so an old round cannot regrant it."""
        pack = self.store.get_pack(pack_id)
        generation = pack["revoked_generation"] + 1
        self.store.update_pack(
            pack_id,
            state=f"hold({reason})",
            hold_reason=reason,
            revoked_generation=generation,
            acceptance_floor_round=max(pack["acceptance_floor_round"], pack["review_round"]),
            decision=None,
        )
        return generation


def pack_status(store: PackStore, pack_id: str) -> dict[str, Any]:
    """What ``orch pack status`` renders - the readable projection of one pack."""
    pack = store.get_pack(pack_id)
    attempt = store.current_attempt(pack_id)
    running = store.running_operations(pack_id)
    unconsumed = store.unconsumed_operations(pack_id)
    unknown = store.unknown_operations(pack_id)
    return {
        "pack": pack_id,
        "target": pack["target_id"],
        "change": pack["change"],
        "state": pack["state"],
        "hold_reason": pack["hold_reason"],
        "return_point": pack["return_point"],
        "continuation": pack["continuation"],
        "blockers": pack["blockers"],
        "contract_hash": pack["contract_hash"],
        "contract_version": pack["contract_version"],
        "contract_revision": pack["contract_revision"],
        "review_round": pack["review_round"],
        "review_seq": pack["review_seq"],
        "output_id": pack["k_last"],
        "attempt": attempt["attempt_id"] if attempt else None,
        "producer_failures": attempt["producer_failures"] if attempt else 0,
        "decision": pack["decision"],
        "evidence_todo": pack["evidence_todo"],
        "operations": {
            "running": [op["op_id"] for op in running],
            "unconsumed": [op["op_id"] for op in unconsumed],
            "unknown": [op["op_id"] for op in unknown],
        },
        "budgets": {
            "reviewer_retry": pack["reviewer_retry"],
            "verify_retry": pack["verify_retry"],
            "recovery_ops": pack["recovery_ops"],
            "exception_grants_issued": pack["exception_grants_issued"],
            "calls_reserved": pack["calls_reserved"],
            "calls_settled": pack["calls_settled"],
        },
    }


def render_status(status: dict[str, Any]) -> str:
    """One-screen human rendering; the JSON above stays the machine contract."""
    lines = [
        f"pack {status['pack']}  [{status['target']}/{status['change']}]",
        f"  state          {status['state']}",
    ]
    if status["hold_reason"]:
        lines.append(f"  hold reason    {status['hold_reason']}")
        lines.append(f"  return point   {status['return_point']}")
    if status["continuation"]:
        lines.append(f"  continuation   {status['continuation']}")
    if status["blockers"]:
        lines.append(f"  blockers       {len(status['blockers'])}")
    lines += [
        f"  contract       {status['contract_hash']} v{status['contract_version']}"
        f".r{status['contract_revision']}",
        f"  round / output {status['review_round']} / {status['output_id']}"
        f"  (seq {status['review_seq']})",
        f"  attempt        {status['attempt']} (producer failures"
        f" {status['producer_failures']})",
    ]
    ops = status["operations"]
    lines.append(
        f"  operations     running={len(ops['running'])}"
        f" unconsumed={len(ops['unconsumed'])} unknown={len(ops['unknown'])}"
    )
    if status["decision"]:
        lines.append(f"  decision       {status['decision']['kind']}")
    if status["evidence_todo"]:
        lines.append(f"  evidence todo  {', '.join(status['evidence_todo'])}")
    return "\n".join(lines)
