"""A stub pack driver: the whole flow with no model and no subprocess.

Step 1 of IMPLEMENTATION-PLAN validates the state machine, not the providers,
so every call here is a function that writes the same records a real adapter
would.  That is what lets the fault-injection fixtures kill the "controller"
at an arbitrary point: there is no process to lose.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from orchestrator.pack.state_machine import PackMachine
from orchestrator.pack.store import PackStore

CONTRACT_H1 = "sha256:" + "1" * 64
BOOT_A = "11111111-1111-4111-8111-111111111111"
BOOT_B = "22222222-2222-4222-8222-222222222222"


def new_store() -> PackStore:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return PackStore(conn)


class BudgetRefused(RuntimeError):
    """The §3.4 gate refused the dispatch; the hold reason is the argument."""


class StubPack:
    """One pack driven through the machine by explicit steps."""

    def __init__(self, pack_id: str = "P2", *, boot_id: str = BOOT_A) -> None:
        self.store = new_store()
        self.machine = PackMachine(self.store)
        self.pack_id = pack_id
        self.boot_id = boot_id
        self.store.create_pack(pack_id, target_id="acme", change="c1", state="ready",
                               host_boot_id=boot_id)
        self.store.update_pack(pack_id, contract_hash=CONTRACT_H1, return_point="claimed")
        self._op_seq = 0

    # -- helpers ----------------------------------------------------------

    def state(self) -> str:
        return self.store.get_pack(self.pack_id)["state"]

    def pack(self) -> dict[str, Any]:
        return self.store.get_pack(self.pack_id)

    def next_op(self, type: str, *, stage: str | None = None, attempt_id: str | None = None,
                logical_op_id: str | None = None, reserve: int = 1,
                counts_round: bool = False) -> str:
        self._op_seq += 1
        op_id = f"OP-{self._op_seq}"
        if reserve:
            # The stub dispatches through the §3.4 gate for the same reason a
            # real one must: the reservation and the operation belong to one
            # step, and a refusal has to happen before the call is made.
            refused = self.machine.reserve_dispatch(self.pack_id, counts_round=counts_round)
            if refused is not None:
                raise BudgetRefused(refused)
        self.store.create_operation(
            op_id, self.pack_id, type=type, stage=stage, attempt_id=attempt_id,
            logical_op_id=logical_op_id,
            reserved_counters={"call_budget": reserve} if reserve else None,
        )
        return op_id

    def binding(self, *, stage: str, attempt_id: str | None = None,
                output_id: int | None = None, review_round: int | None = None,
                contract_hash: str = CONTRACT_H1) -> dict[str, Any]:
        return {
            "stage": stage,
            "attempt_id": attempt_id,
            "output_id": output_id,
            "review_round": review_round,
            "contract_hash": contract_hash,
        }

    # -- flow -------------------------------------------------------------

    def contract_review(self, *, passes: bool, findings: list[dict] | None = None) -> str:
        """`contracting` -> pass returns to the return point, findings hold."""
        self.store.update_pack(self.pack_id, state="contracting")
        op_id = self.next_op("contract_review", stage="contract_review")
        self.store.update_operation(op_id, spawned=1)
        self.machine.commit_call_result(
            op_id, result="completed",
            call_binding=self.binding(stage="contract_review"),
        )
        self.store.bump(self.pack_id, "review_seq")
        self.machine.consume_result(self.pack_id, op_id)
        if passes:
            self.store.update_pack(self.pack_id, state="claimed")
            return "claimed"
        return self.machine.enter_hold(self.pack_id, "contract_hold", return_point="contracting")

    def claim(self, attempt_id: str = "WA-1", base_revision: str = "abc") -> str:
        pack = self.pack()
        self.store.create_attempt(
            attempt_id, self.pack_id, base_revision=base_revision,
            candidate_input="sha256:" + "a" * 64, next_output_id=pack["k_last"] + 1,
        )
        self.store.update_pack(self.pack_id, state="claimed", lease_token="LEASE-1")
        return attempt_id

    def produce(self, attempt_id: str = "WA-1", *, crash_before_spawn_flag: bool = False,
                launch_failed: str | None = None, stage: str = "apply") -> str:
        attempt = self.store.get_attempt(attempt_id)
        k = attempt["next_output_id"]
        self.store.update_pack(self.pack_id, state=f"producing({k})")
        op_id = self.next_op("producer", stage=stage, attempt_id=attempt_id)
        if launch_failed is not None:
            self.store.update_operation(op_id, spawn_outcome=f"launch_failed({launch_failed})")
            return op_id
        if crash_before_spawn_flag:
            # fork succeeded, controller died before persisting the flag.
            return op_id
        self.store.update_operation(
            op_id, spawned=1, process_identity={"pid": 4242, "pgid": 4242, "start": 1},
        )
        return op_id

    def submit(self, op_id: str, attempt_id: str = "WA-1") -> int:
        attempt = self.store.get_attempt(attempt_id)
        k = attempt["next_output_id"]
        self.machine.commit_call_result(
            op_id, result="completed",
            call_binding=self.binding(stage="apply", attempt_id=attempt_id),
        )
        self.machine.consume_result(self.pack_id, op_id)
        self.store.freeze_output(self.pack_id, k, attempt_id, "sha256:" + "b" * 64)
        self.store.update_attempt(attempt_id, next_output_id=k + 1)
        self.store.update_operation(op_id, produced_output_id=k)
        self.store.update_pack(self.pack_id, state=f"submitted({k})")
        return k

    def prerun(self, k: int) -> None:
        op_id = self.next_op("prerun", stage="prerun", reserve=0)
        self.store.update_operation(op_id, spawned=1)
        self.machine.commit_call_result(
            op_id, result="completed", call_binding=self.binding(stage="prerun"),
        )
        self.machine.consume_result(self.pack_id, op_id)
        self.store.update_pack(self.pack_id, state=f"reviewing({k})")

    def review(self, k: int, envelope: dict[str, Any], *, history: list[dict] | None = None) -> str:
        op_id = self.next_op("review", stage="review")
        self.store.update_operation(op_id, spawned=1)
        self.machine.commit_call_result(
            op_id, result="completed",
            call_binding=self.binding(stage="review", review_round=None),
        )
        self.store.mark_consumed(op_id)
        self.store.update_pack(self.pack_id, state=f"judging({k})")
        return self.machine.judge_round(self.pack_id, envelope, history=history or [])


def envelope(round_no: int, *, verdict: str, obligations: dict[str, str],
             findings: list[dict] | None = None, blocked_reason: str | None = None,
             contract_findings: list[dict] | None = None) -> dict[str, Any]:
    """A review envelope reduced to what the machine and the judge read."""
    return {
        "review_round": round_no,
        "verdict": verdict,
        "blocked_reason": blocked_reason,
        "obligations": {ob: {"status": status, "basis": [], "note": None}
                        for ob, status in obligations.items()},
        "findings": findings or [],
        "contract_findings": contract_findings or [],
        "prior_round": None,
        "remaining": [],
    }


def high_finding(fid: str, lineage: str, obligation: str) -> dict[str, Any]:
    return {
        "id": fid,
        "severity": "High",
        "blocking": True,
        "obligation_refs": [obligation],
        "lineage_ids": [lineage],
        "prior_refs": [],
        "recurrence_of": None,
    }
