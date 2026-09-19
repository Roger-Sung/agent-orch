"""The pack-v1 state machine (STATE-TABLE §3, §5, §6).

The shape worth understanding before reading the code:

* **Results are consumed, not applied.**  A provider call ends by writing a
  result and an `unconsumed` marker in one transaction (`commit_call_result`);
  a *separate* transaction decides what that result means for the pack
  (`consume_result`).  Splitting them is what makes "exactly once" survive a
  crash between the two - the marker is the crash-visible record that work
  happened but was not yet accounted for.
* **Holds are states, not errors.**  Every hold reason has exactly one exit
  event and one target, so a pack can always be got out of a hold by an
  authorised human action rather than by editing the database.
* **Nothing infers process death.**  `spawned=false` and "no process found" are
  both silence, not evidence; only the enumerated stop evidence releases a
  recovery (joint-r4 R4-H1).
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Callable, Sequence

from . import budgets, revocation, sessions, trees
from .judge import Decision, judge_v1
from .store import PackStore

# §3.0b consumption outcomes.
CONSUME = "consume"
DEFER = "defer"
REBIND = "rebind"
STALE = "stale"
RESUME_AWAITING = "resume_awaiting"
RESOLVE_UNKNOWN = "resolve_unknown"
ALREADY_CONSUMED = "already_consumed"

# States in which a call result for `stage` may be applied directly.
STAGE_ACTIVE_STATES = {
    "apply": {"producing"},
    "repair": {"producing"},
    "prerun": {"submitted"},
    "review": {"reviewing"},
    "contract_review": {"contracting"},
}

# §1.2a - invalidating holds stop a live producer; non-invalidating ones let it
# finish.  `unknown_operation` / `orphaned` are outside this table entirely
# (joint-r2): they *are* the result of an in-flight operation's state being
# unknown, rather than a hold that then has to deal with one.
INVALIDATING_HOLDS = {
    "approval_revoked", "approval_scope_mismatch", "approval_stale", "approval_missing",
    "requirement_changed", "manifest_changed", "environment_changed", "candidate_changed",
    "workspace_dirty", "contract_invalid", "containment_stop",
}

STOP_EVIDENCE_KINDS = (revocation.KIND, "host_rebooted")


class PackStateError(Exception):
    """An event that the table has no entry for (STATE-TABLE S-2)."""


def _base_state(state: str) -> str:
    return state.split("(", 1)[0]


def _payload_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class PackMachine:
    """Drives one pack.  All methods expect the caller to hold a transaction."""

    def __init__(self, store: PackStore) -> None:
        self.store = store

    # ------------------------------------------------------------------
    # call lifecycle (§3.0b)
    # ------------------------------------------------------------------

    def commit_call_result(self, op_id: str, *, result: str, result_ref: str | None = None,
                           call_binding: dict[str, Any] | None = None,
                           receipt_ref: str | None = None) -> None:
        """Seal one call: write its result and leave it unconsumed.

        This deliberately does not touch pack state.  A crash immediately after
        it leaves a recoverable record ("this call finished, nobody has acted on
        it yet") instead of a half-applied transition.
        """
        op = self.store.get_operation(op_id)
        if op["result"] is not None:
            return  # idempotent: the same call reported twice is one result.
        fields: dict[str, Any] = {"result": result, "result_ref": result_ref}
        # Only written when supplied.  Both are recorded *before* this call - the
        # binding when the call is dispatched, the receipt reference when the
        # provider's output is sealed - so writing None over them here would
        # destroy the very records recovery reads (§5).
        if call_binding is not None:
            fields["call_binding"] = call_binding
        if receipt_ref is not None:
            fields["receipt_ref"] = receipt_ref
        self.store.update_operation(op_id, **fields)

    def classify_consumption(self, pack_id: str, op_id: str) -> str:
        """Decide how a closed operation's result should be taken up (§3.0b)."""
        op = self.store.get_operation(op_id)
        if op["consumed_at"] is not None:
            return ALREADY_CONSUMED
        if op["result"] is None:
            raise PackStateError(f"operation {op_id} has no result to consume")

        pack = self.store.get_pack(pack_id)
        state = _base_state(pack["state"])
        binding = op["call_binding"] or {}

        if state == "hold" and pack["hold_reason"] == "unknown_operation":
            # The blocker names which operation the pack is waiting on, and that
            # is the identity to match: the moment the operation reports, it is
            # no longer `unknown`, so keying off its current result would miss
            # exactly the case this branch exists for (joint-r4).
            if any(b.get("op_id") == op_id for b in pack["blockers"]):
                return RESOLVE_UNKNOWN
        if state == "awaiting_inflight":
            return RESUME_AWAITING
        if state == "hold":
            return DEFER

        if not self._binding_matches(pack, op, binding):
            if self._is_dependency_rebind(pack, binding):
                return REBIND
            return STALE

        stage = binding.get("stage")
        if stage and state not in STAGE_ACTIVE_STATES.get(stage, set()):
            return DEFER
        return CONSUME

    def _binding_matches(self, pack: dict[str, Any], op: dict[str, Any],
                         binding: dict[str, Any]) -> bool:
        """Equality fields depend on stage and timing (IDENTITIES §2.7)."""
        if binding.get("contract_hash") != pack["contract_hash"]:
            return False
        if binding.get("stage") == "contract_review":
            # Initial contract review predates the work attempt, so the other
            # fields are legitimately null on both sides.
            if binding.get("attempt_id") is None:
                return True
            return (
                binding.get("attempt_id") == op["attempt_id"]
                and binding.get("output_id") == op["produced_output_id"]
            )
        if binding.get("review_round") is not None and binding["review_round"] != pack["review_round"]:
            return False
        current = self.store.current_attempt(pack["pack_id"])
        if current and binding.get("attempt_id") not in (None, current["attempt_id"]):
            return False
        return True

    def _is_dependency_rebind(self, pack: dict[str, Any], binding: dict[str, Any]) -> bool:
        """Only a dependency-refresh contract change lets a frozen output carry over."""
        for record in self.store.records_of_kind("transition", pack["pack_id"]):
            payload = record["payload"]
            if payload.get("from_hash") != binding.get("contract_hash"):
                continue
            if payload.get("to_hash") != pack["contract_hash"]:
                continue
            return payload.get("author_class") == "none" and payload.get("refresh") == ["dependency"]
        return False

    def consume_result(self, pack_id: str, op_id: str,
                       apply_fn: Callable[[str, dict[str, Any]], None] | None = None) -> str:
        """Take up one closed result and mark it consumed, exactly once."""
        outcome = self.classify_consumption(pack_id, op_id)
        if outcome in {ALREADY_CONSUMED, DEFER}:
            return outcome

        op = self.store.get_operation(op_id)
        if outcome == STALE:
            self.store.update_operation(op_id, result="failed_stale_result")
            self.store.mark_consumed(op_id)
            return outcome

        if outcome == RESOLVE_UNKNOWN:
            # Whatever the binding says, the operation now has an outcome, so
            # the reason this pack was held no longer holds.
            pack = self.store.get_pack(pack_id)
            blockers = [b for b in pack["blockers"] if b.get("op_id") != op_id]
            self.store.mark_consumed(op_id)
            if blockers:
                self.store.update_pack(pack_id, blockers=blockers)
            else:
                self.store.update_pack(
                    pack_id, blockers=[], state=pack["return_point"] or "claimed",
                    hold_reason=None,
                )
            return outcome

        if outcome == RESUME_AWAITING:
            pack = self.store.get_pack(pack_id)
            self.store.mark_consumed(op_id)
            if apply_fn is not None:
                apply_fn(CONSUME, op)
            still_running = [o for o in self.store.running_operations(pack_id) if o["op_id"] != op_id]
            if pack["blockers"] or still_running:
                return outcome
            self.store.update_pack(
                pack_id, state=pack["return_point"] or "claimed", hold_reason=None,
                continuation=pack["continuation"],
            )
            return outcome

        self.store.mark_consumed(op_id)
        if apply_fn is not None:
            apply_fn(outcome, op)
        return outcome

    def route_event(self, pack_id: str, kind: str, *, op_id: str | None = None) -> str:
        """§3.0 routing. Anything the table has no entry for is dropped.

        Dropped, not held: an event the table does not recognise has no
        authorised effect, so recording and discarding it leaves the pack
        exactly where it was rather than inventing a state for it (S-2).
        """
        known = {
            "E_deps_ready", "E_contract_review", "E_prep_done", "E_prep_needs",
            "E_produced", "E_producer_failed", "E_prerun", "E_prerun_done",
            "E_review", "E_review_invalid", "E_reviewer_failed", "E_fail",
            "E_orphan", "E_stopped",
        }
        if kind not in known:
            return "unexpected_event"
        if op_id is not None:
            try:
                op = self.store.get_operation(op_id)
            except KeyError:
                # An operation id that is not ours cannot describe our pack.
                return "unexpected_event"
            if op["pack_id"] != pack_id:
                return "unexpected_event"
        return "routed"

    def stop_and_release(self, pack_id: str, *, next_state: str) -> str:
        """`E_stopped` for a `stopping(next)` pack (§3.6).

        The running operation is closed as superseded rather than failed - it
        was stopped by the engine, so counting it against the producer would
        charge it for the engine's decision.  The writer lease stays with the
        original writer whenever the next state is a hold, because the tree is
        still theirs to be restored from.
        """
        pack = self.store.get_pack(pack_id)
        for op in self.store.running_operations(pack_id):
            self.store.update_operation(op["op_id"], result="failed_superseded_by_hold")
        if next_state.startswith("hold("):
            reason = next_state[5:-1]
            self.store.update_pack(pack_id, state=next_state, hold_reason=reason)
        else:
            self.store.update_pack(pack_id, state=next_state, hold_reason=None,
                                   lease_token=None)
        return self.store.get_pack(pack_id)["state"]

    # ------------------------------------------------------------------
    # holds (§3.0a)
    # ------------------------------------------------------------------

    def enter_hold(self, pack_id: str, reason: str, *, return_point: str | None = None,
                   continuation: str | None = None,
                   alive: Callable[[dict[str, Any]], bool] | None = None) -> str:
        """Enter a hold, dealing with in-flight operations by reason class."""
        pack = self.store.get_pack(pack_id)
        return_point = return_point or pack["return_point"] or pack["state"]
        running = self.store.running_operations(pack_id)
        invalidating = reason.split("(", 1)[0] in INVALIDATING_HOLDS

        if invalidating and running and any((alive or (lambda op: True))(op) for op in running):
            # Stop first: a writer that is still running would keep changing the
            # very tree the hold exists to freeze.
            self.store.update_pack(
                pack_id, state=f"stopping(hold({reason}))", hold_reason=reason,
                return_point=return_point, continuation=continuation,
            )
            return f"stopping(hold({reason}))"

        self.store.update_pack(
            pack_id, state=f"hold({reason})", hold_reason=reason,
            return_point=return_point, continuation=continuation,
        )
        return f"hold({reason})"

    def exit_hold(self, pack_id: str, *, alive: Callable[[dict[str, Any]], bool] | None = None) -> str:
        """Leave a hold once its exit event has passed the gate (§3.0a step 1-5)."""
        pack = self.store.get_pack(pack_id)
        running = [op for op in self.store.running_operations(pack_id)
                   if (alive or (lambda op: True))(op)]
        if running:
            self.store.update_pack(
                pack_id,
                state=f"awaiting_inflight({pack['return_point']},{pack['continuation']})",
                hold_reason=None,
            )
            return "awaiting_inflight"

        # Move to the return point *before* consuming: while the pack still
        # reads as `hold(...)`, every pending result classifies as `defer`, so
        # consuming here would quietly strand the very results this exit exists
        # to take up (§3.0a step 4).
        target = pack["return_point"] or "claimed"
        self.store.update_pack(pack_id, state=target, hold_reason=None)
        for op in self.store.unconsumed_operations(pack_id):
            self.consume_result(pack_id, op["op_id"])

        pack = self.store.get_pack(pack_id)
        if pack["blockers"]:
            # A blocker raised while consuming puts the pack back in a hold
            # rather than letting it continue with an unresolved problem.
            return self.enter_hold(pack_id, pack["blockers"][0].get("reason", "unknown_operation"),
                                   return_point=target)
        return self.store.get_pack(pack_id)["state"]

    # ------------------------------------------------------------------
    # reconcile (§5)
    # ------------------------------------------------------------------

    STOP_EVIDENCE = "stop_evidence"

    def revoke_write_capability(self, op_id: str, *, roots: Sequence[Any],
                                quarantine: Any, record_id: str,
                                holders: Callable[[Any], list[str] | None] | None = None,
                                ) -> dict[str, Any]:
        """E-1 - take the operation's write capability away, then verify it.

        The result is recorded whether or not it succeeded: a refusal is the
        operator's only account of why the operation is still unknown, and
        without it the next attempt looks like the first.
        """
        op = self.store.get_operation(op_id)
        result = revocation.revoke(
            roots, quarantine=quarantine,
            **({"holders": holders} if holders is not None else {}))
        self.store.add_record(record_id, self.STOP_EVIDENCE, {"op_id": op_id, **result},
                              pack_id=op["pack_id"])
        return result

    def revocation_evidence(self, op: dict[str, Any]) -> dict[str, Any] | None:
        """The successful revocation recorded for this operation, if any."""
        for record in self.store.records_of_kind(self.STOP_EVIDENCE, op["pack_id"]):
            payload = record["payload"]
            if (not record["revoked"] and payload.get("op_id") == op["op_id"]
                    and payload.get("ok")):
                return payload
        return None

    def stop_evidence(self, op: dict[str, Any], *, current_boot_id: str | None,
                      op_boot_id: str | None) -> str | None:
        """Return the evidence kind that proves the tree is safe, or None.

        Absence of proof is not proof of absence: with no recorded revocation
        and an unchanged boot id the answer is None, and the operation stays
        unknown for as long as that takes (joint-r4).

        E-1 no longer asks whether the writer died.  A provider can move its
        helpers into process groups of their own, so an empty original group is
        not the conclusion it was being read as (D-2026-09-16-01); what is
        checked instead is that the engine took the write capability away and
        confirmed nothing still holds it.
        """
        if self.revocation_evidence(op) is not None:
            return revocation.KIND
        if current_boot_id and op_boot_id and current_boot_id != op_boot_id:
            return "host_rebooted"
        return None

    def recovery_allowed(self, pack_id: str, op_id: str, action: str, **evidence_args: Any) -> bool:
        """Only `verify_stopped` is available while the writer might be alive."""
        if action == "verify_stopped":
            return True
        op = self.store.get_operation(op_id)
        return self.stop_evidence(op, **evidence_args) is not None

    def resolve_operation(self, pack_id: str, op_id: str, action: str,
                          **evidence_args: Any) -> str:
        """`A_resolve_operation` - refuses any tree-touching action without evidence."""
        if action not in {"verify_stopped", "restore_input", "restore_output", "recovery_run"}:
            raise PackStateError(f"unknown resolve action {action!r}")
        if not self.recovery_allowed(pack_id, op_id, action, **evidence_args):
            return "refused_no_stop_evidence"
        if action == "verify_stopped":
            op = self.store.get_operation(op_id)
            kind = self.stop_evidence(op, **evidence_args)
            if kind is None:
                return "still_unknown"
            self.store.update_operation(op_id, spawn_outcome=f"stopped:{kind}")
            return f"stopped:{kind}"
        return f"allowed:{action}"

    def startup_scan(self, pack_id: str, *, alive: Callable[[dict[str, Any]], bool] | None = None,
                     sealed: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
                     ) -> dict[str, int]:
        """Route every operation left over by a crash (§3.0b startup scan).

        A dead operation is only unknown when nothing verifiable was left behind:
        §5 says to look for a sealed receipt first, and replay its `E_*` when one
        verifies.  `sealed` returns the *already verified* receipt or None - the
        verification lives in `receipts.load`, and a receipt that fails any of
        its checks must arrive here as None, never as a receipt.
        """
        counts = {"unknown": 0, "consumed": 0, "deferred": 0, "not_spawned": 0,
                  "recovered": 0, "session_lost": 0}
        session_lost: list[dict[str, Any]] = []
        for op in self.store.running_operations(pack_id):
            if op["spawn_outcome"] and op["spawn_outcome"].startswith("launch_failed"):
                # Positive evidence the launch never happened: refund is safe.
                self.store.update_operation(op["op_id"], result="failed_not_spawned")
                counts["not_spawned"] += 1
                continue
            if alive is not None and alive(op):
                continue
            receipt = sealed(op) if sealed is not None else None
            if receipt is not None:
                self.commit_call_result(
                    op["op_id"], result="completed",
                    receipt_ref=receipt["sha256"],
                    call_binding=receipt["call_binding"],
                )
                counts["recovered"] += 1
                continue
            self.store.update_operation(op["op_id"], result="unknown")
            counts["unknown"] += 1
            if self.store.pending_session_for(op["op_id"]) is not None:
                # A review call that was dispatched and never answered is not
                # just an unknown operation: the session it was speaking on is
                # lost too, and a fresh session would route around the hold
                # rather than resume the conversation (§5).
                session_lost.append(op)

        for op in self.store.unconsumed_operations(pack_id):
            outcome = self.consume_result(pack_id, op["op_id"])
            if outcome in {CONSUME, RESUME_AWAITING, RESOLVE_UNKNOWN}:
                counts["consumed"] += 1
            elif outcome == DEFER:
                counts["deferred"] += 1

        if session_lost:
            # Entered once, after every operation has been routed: holding part
            # way through would hide whatever the rest of the scan found.
            lost = session_lost[0]
            counts["session_lost"] = len(session_lost)
            self.enter_hold(
                pack_id, "review_session_lost",
                return_point=sessions.return_state(
                    lost["stage"], lost["input_output_id"] or self.store.get_pack(pack_id)["k_last"]),
            )
        return counts

    # ------------------------------------------------------------------
    # budgets (§3.4)
    # ------------------------------------------------------------------

    CAP_RAISE = "cap_raise"
    RETRY_USED = "reviewer_retry_used"

    def budget_caps(self, pack_id: str,
                    budget_policy: dict[str, Any] | None = None) -> dict[str, int]:
        raises = [r["payload"] for r in self.store.records_of_kind(self.CAP_RAISE, pack_id)
                  if not r["revoked"]]
        return budgets.caps(budget_policy, raises)

    def raise_cap(self, pack_id: str, kind: str, extra: int, *, record_id: str) -> None:
        """`A_raise_cap` - a validity record, not a reset (§3.4)."""
        kind = budgets.normalise_kind(kind)
        budgets.caps(None, [{"kind": kind, "extra": extra}])  # rejects a bad kind/extra here
        self.store.add_record(record_id, self.CAP_RAISE, {"kind": kind, "extra": extra},
                              pack_id=pack_id)

    def reserve_dispatch(self, pack_id: str, *, budget_policy: dict[str, Any] | None = None,
                         counts_round: bool = False) -> str | None:
        """Pre-deduct a provider call, and a round when one is being dispatched.

        Returns the hold reason when the reservation is refused, else None and
        the counters have moved.  Refusing *before* spending is the whole point:
        the deduction is never refunded, because the cost is already real by the
        time a call fails.
        """
        caps = self.budget_caps(pack_id, budget_policy)
        pack = self.store.get_pack(pack_id)
        if counts_round and budgets.exhausted(
                "round_cap", self.store.count_dispatch_records(pack_id), caps["round_cap"]):
            return budgets.hold_reason("round_cap")
        if budgets.exhausted("call_budget", pack["calls_reserved"], caps["call_budget"]):
            return budgets.hold_reason("call_budget")
        self.store.bump(pack_id, "calls_reserved", 1)
        return None

    def reviewer_retry_used(self, pack_id: str, *, stage: str, output_id: int | None) -> int:
        return sum(1 for r in self.store.records_of_kind(self.RETRY_USED, pack_id)
                   if not r["revoked"]
                   and r["payload"].get("stage") == stage
                   and r["payload"].get("output_id") == output_id)

    def use_reviewer_retry(self, pack_id: str, *, stage: str, output_id: int | None,
                           cause: str, record_id: str,
                           budget_policy: dict[str, Any] | None = None) -> str | None:
        """Spend one reviewer redispatch for this `(stage, output_id)` group.

        Returns the hold reason when the allowance is gone.  The reason is the
        cause rather than a generic budget hold, because the operator's exit is
        the matching `A_redispatch`, which raises this same counter (§3.4).
        """
        if cause not in budgets.REVIEWER_CAUSES:
            raise budgets.BudgetError(f"unknown reviewer retry cause {cause!r}")
        caps = self.budget_caps(pack_id, budget_policy)
        used = self.reviewer_retry_used(pack_id, stage=stage, output_id=output_id)
        if budgets.exhausted("reviewer_retry", used, caps["reviewer_retry"]):
            return budgets.hold_reason("reviewer_retry", cause=cause)
        self.store.add_record(record_id, self.RETRY_USED,
                              {"stage": stage, "output_id": output_id, "cause": cause},
                              pack_id=pack_id)
        return None

    ALLOW_APPLY = "allow_apply"

    def _recheck_preconditions(self, pack_id: str,
                               recheck: Callable[[], str | None]) -> str:
        """Re-verify a hold's preconditions and route on what still fails.

        A different failure is not the same hold cleared and a new one entered:
        the blocker moves while the continuation stays, because the work the
        pack still owes did not change just because the reason it is stuck did.
        """
        blocker = recheck()
        if blocker is None:
            # The re-verification is authoritative: a blocker recorded by an
            # earlier recheck would otherwise keep the pack held for a reason
            # this one just found no longer true.
            self.store.update_pack(pack_id, blockers=[], hold_reason=None)
            return self.exit_hold(pack_id, alive=lambda op: False)
        self.store.update_pack(pack_id, state=f"hold({blocker})", hold_reason=blocker,
                               blockers=[{"reason": blocker}])
        return f"hold({blocker})"

    def restore_tree(self, pack_id: str, *, workspace: Any, tree: dict[str, Any],
                     blob_store: Any, recheck: Callable[[], str | None]) -> str:
        """`A_restore_tree` - put the candidate back, then re-verify.

        Only from `hold(candidate_changed)`: restoring a tree the pack is not
        held over would overwrite whatever is legitimately there now.
        """
        pack = self.store.get_pack(pack_id)
        if not pack["state"].startswith("hold(candidate_changed"):
            raise PackStateError(
                f"A_restore_tree is not available from {pack['state']!r}")
        trees.restore(blob_store, workspace, tree)
        return self._recheck_preconditions(pack_id, recheck)

    def allow_apply(self, pack_id: str, *, record_id: str,
                    recheck: Callable[[], str | None]) -> str:
        """`A_allow_apply` - record the authorisation, then re-verify.

        The grant is a record rather than a state change so that the same
        re-verification decides the outcome; approving does not by itself say
        the other preconditions now hold.
        """
        self.store.add_record(record_id, self.ALLOW_APPLY, {"pack_id": pack_id},
                              pack_id=pack_id)
        return self._recheck_preconditions(pack_id, recheck)

    def rebind_review_session(self, pack_id: str, *, role: str, attempt_id: str | None,
                              lost_op_id: str, stage: str, record_id: str,
                              checkpoint_ref: str | None = None,
                              budget_policy: dict[str, Any] | None = None,
                              ) -> dict[str, Any]:
        """`A_rebind_session` - the only authorised exit from review_session_lost.

        The successor keeps the superseded row's binding as its `predecessor`,
        and the lost call's pending row is left standing: it is the record that
        a call was dispatched and never answered, so clearing it would erase the
        reason this hold exists (§5).

        Returns what the caller has to act on - whether to redispatch, and the
        hold it fell into if the reviewer allowance was already gone.
        """
        pack = self.store.get_pack(pack_id)
        if not pack["state"].startswith("hold(review_session_lost"):
            raise PackStateError(
                f"A_rebind_session is not available from {pack['state']!r}")

        lost = self.store.get_operation(lost_op_id)
        sealed = lost["result"] if lost["result"] in {"completed", "failed"} else None
        outcome = sessions.rebind_session(self.store, pack_id, role, attempt_id,
                                          sealed_result=sealed)
        if checkpoint_ref is not None:
            self.store.update_session(pack_id, role, attempt_id, outcome["seq"],
                                      checkpoint_ref=checkpoint_ref)

        if outcome["charge_retry"]:
            refused = self.use_reviewer_retry(
                pack_id, stage=stage, output_id=pack["k_last"], cause="reviewer_failed",
                record_id=record_id, budget_policy=budget_policy)
            if refused is not None:
                # The allowance is gone, so there is no redispatch to authorise;
                # the pack moves from one hold to the one that says why.
                self.store.update_pack(pack_id, state=f"hold({refused})",
                                       hold_reason=refused)
                return {**outcome, "rerun": False, "held": refused}

        state = self.exit_hold(pack_id, alive=lambda op: False)
        return {**outcome, "held": None, "state": state}

    def settle_rebound_call(self, pack_id: str, *, lost_op_id: str, new_op_id: str,
                            result: str) -> None:
        """Record what the redispatched call B did to the unknown call A.

        Only a completed B supersedes A.  A failed B leaves A unknown, because
        nothing about B's failure says what A did to the tree (§5).
        """
        if result == "completed":
            self.store.update_operation(lost_op_id, superseded_by=new_op_id)

    # ------------------------------------------------------------------
    # dispatch gate (§3.3a)
    # ------------------------------------------------------------------

    def decision_valid(self, pack: dict[str, Any]) -> bool:
        """A decision only speaks for the exact round / output / contract it saw."""
        decision = pack["decision"]
        if not decision:
            return False
        bound = decision.get("bound_to", {})
        return (
            bound.get("review_round") == pack["review_round"]
            and bound.get("output_id") == pack["k_last"]
            and bound.get("contract_hash") == pack["contract_hash"]
        )

    def dispatch_gate(self, pack_id: str) -> str:
        """Recompute where the pack should go next.

        Continuation wins over decision: a pack that owes a re-verification has
        to do that first, whatever the last judgement said.
        """
        pack = self.store.get_pack(pack_id)
        continuation = pack["continuation"]
        k = pack["k_last"]

        if continuation == f"reverify_then_review({k})":
            return f"submitted({k})"
        if continuation == f"redo_review({k})":
            return f"reviewing({k})"

        if not self.decision_valid(pack):
            return f"submitted({k})"

        decision = pack["decision"]
        kind = decision["kind"]
        if kind == "accepted":
            return "accepted"
        if pack["evidence_todo"]:
            return "hold(evidence_gap)"
        if kind in {"stalled", "oscillating"}:
            grant = self.store.usable_grant(
                pack_id, output_id=k, decision_id=decision.get("decision_id", "")
            )
            if grant is None:
                return f"hold({kind})"
            return f"repair_pending({k})"
        if kind.startswith("hold("):
            return kind
        return f"repair_pending({k})"

    # ------------------------------------------------------------------
    # judging (§3.3)
    # ------------------------------------------------------------------

    def judge_round(self, pack_id: str, envelope: dict[str, Any], *,
                    history: Sequence[dict[str, Any]],
                    rounds_since_dispatch: Sequence[dict[str, Any]] = (),
                    decision_id: str = "DEC-1") -> str:
        """Seal one review round and route it (§3.3 judging)."""
        pack = self.store.get_pack(pack_id)
        k = pack["k_last"]
        self.store.bump(pack_id, "review_seq")

        if envelope.get("contract_findings"):
            # The contract is the wrong shape; the producer is not the addressee.
            self.store.update_pack(pack_id, decision=None)
            return self.enter_hold(pack_id, "contract_hold", return_point=f"submitted({k})")

        self.store.bump(pack_id, "review_round")
        pack = self.store.get_pack(pack_id)

        verdict = envelope.get("verdict")
        if verdict == "blocked":
            reason = envelope.get("blocked_reason")
            self.store.update_pack(pack_id, decision=None)
            if reason == "evidence_gap":
                unknown = [
                    ob for ob, result in envelope["obligations"].items()
                    if result["status"] == "UNKNOWN"
                ]
                self.store.update_pack(pack_id, evidence_todo=unknown)
                return self.enter_hold(
                    pack_id, "evidence_gap", return_point=f"submitted({k})",
                    continuation=f"reverify_then_review({k})",
                )
            return self.enter_hold(
                pack_id, "reviewer_blocked", return_point=f"reviewing({k})",
                continuation=f"redo_review({k})",
            )

        decision: Decision = judge_v1(
            envelope,
            history=history,
            dispatch_record=self.store.last_dispatch_record(pack_id),
            rounds_since_dispatch=rounds_since_dispatch,
            acceptance_floor_round=pack["acceptance_floor_round"],
            first_dispatch_origin=pack["first_dispatch_origin"],
        )
        self.store.update_pack(
            pack_id,
            decision={
                "kind": "accepted" if decision.kind == "accepted"
                else ("dispatch_ok" if decision.dispatch_ok else decision.kind),
                "decision_id": decision_id,
                "reason": decision.reason,
                "bound_to": {
                    "review_round": pack["review_round"],
                    "output_id": k,
                    "contract_hash": pack["contract_hash"],
                },
            },
            continuation=f"resume_repair_gate({k})",
        )
        target = self.dispatch_gate(pack_id)
        if target.startswith("hold("):
            reason = target[5:-1]
            return self.enter_hold(pack_id, reason, return_point=f"submitted({k})")
        self.store.update_pack(pack_id, state=target)
        return target
