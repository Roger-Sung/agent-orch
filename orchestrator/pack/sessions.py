"""Provider session registry lifecycle (STATE-TABLE §8).

Two rules decide everything here:

* **fresh vs resume comes from the registry, not from a round counter.**  The
  first `final_review` continues the session the contract review already
  established, so "round >= 2 means resume" would open a second reviewer
  session for the same pack (joint-r4).
* **A sealed *success* is never re-run.**  Losing the session identity does not
  invalidate the work the call produced; re-running would spend budget and, for
  a producer, throw away an already-frozen candidate.  Only a sealed failure or
  no result at all justifies another call - and only those cost a retry
  (joint-r3).
"""
from __future__ import annotations

from typing import Any

from .store import PackStore

FRESH = "fresh"
RESUME = "resume"
NEEDS_REBIND = "needs_rebind"

# The registry has two roles; `contract_review` is a *stage* of the reviewer
# session, not a third identity (joint-r2 J4).
ROLES = ("producer", "reviewer")

STAGE_TO_ROLE = {
    "apply": "producer",
    "repair": "producer",
    "review": "reviewer",
    "contract_review": "reviewer",
}

STAGE_RETURN_STATE = {
    "apply": "producing",
    "repair": "producing",
    "review": "reviewing",
    "contract_review": "contracting",
}


def role_for_stage(stage: str) -> str:
    try:
        return STAGE_TO_ROLE[stage]
    except KeyError:
        raise KeyError(f"no session role for stage {stage!r}") from None


def select_session(store: PackStore, pack_id: str, role: str,
                   attempt_id: str | None) -> tuple[str, dict[str, Any] | None]:
    """Decide how the next call for this key starts."""
    latest = store.latest_session(pack_id, role, attempt_id)
    if latest is None:
        # A normal first call: no predecessor exists, so requiring a rebind
        # here would block the pack's very first dispatch (joint-r4).
        return FRESH, None
    if latest["state"] == "ready":
        return RESUME, latest
    if latest["state"] == "new":
        return FRESH, latest
    # `unconfirmed` / `superseded` with no successor needs an authorised rebind;
    # starting a fresh session here would route around hold(session_unconfirmed).
    return NEEDS_REBIND, latest


def open_session(store: PackStore, pack_id: str, role: str, attempt_id: str | None,
                 *, op_id: str) -> int:
    """Write the `pending` row *before* spawning, so a crash stays visible."""
    mode, latest = select_session(store, pack_id, role, attempt_id)
    if mode == NEEDS_REBIND:
        raise ValueError(f"session for {role}/{attempt_id} needs A_rebind_session first")
    seq = latest["seq"] if (latest and latest["state"] == "new") else store.add_session(
        pack_id, role, attempt_id
    )
    store.update_session(pack_id, role, attempt_id, seq,
                         state=f"pending", pending_op_id=op_id)
    return seq


def confirm_session(store: PackStore, pack_id: str, role: str, attempt_id: str | None,
                    seq: int, binding: dict[str, Any] | None) -> str:
    """`ready` only on a confirmed identity; otherwise `unconfirmed`."""
    state = "ready" if binding else "unconfirmed"
    store.update_session(pack_id, role, attempt_id, seq,
                         state=state, provider_binding=binding, pending_op_id=None)
    return state


def rebind_session(store: PackStore, pack_id: str, role: str, attempt_id: str | None,
                   *, sealed_result: str | None) -> dict[str, Any]:
    """`A_rebind_session`: supersede the old row and decide what happens next.

    Returns the branch taken, whether a new call is needed and whether the
    retry counter moves - the three questions the caller has to answer.
    """
    latest = store.latest_session(pack_id, role, attempt_id)
    if latest is None:
        raise ValueError(f"nothing to rebind for {role}/{attempt_id}")

    store.update_session(pack_id, role, attempt_id, latest["seq"], state="superseded")
    # The successor is `new`, not `superseded`: a successor in the superseded
    # state would be refused by the very next inspect (joint-r5).
    seq = store.add_session(pack_id, role, attempt_id, state="new",
                            predecessor=latest.get("provider_binding"))

    if sealed_result == "completed":
        return {"branch": "sealed_success", "rerun": False, "charge_retry": False, "seq": seq}
    if sealed_result is not None:
        return {"branch": "sealed_failure", "rerun": True, "charge_retry": True, "seq": seq}
    return {"branch": "no_result", "rerun": True, "charge_retry": True, "seq": seq}


def return_state(stage: str, output_id: int) -> str:
    """Where a rebound call resumes - from the pending op's stage, not the registry."""
    base = STAGE_RETURN_STATE[stage]
    if base == "contracting":
        return base
    return f"{base}({output_id})"
