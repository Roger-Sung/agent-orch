"""STATE-TABLE §3.4 - the cost failsafe, and the only thing that refuses a
pack-v1 dispatch.

Legacy's `max_transitions` / `attempt_cap` / `edge_cap` are deliberately
bypassed for pack-v1 (§3.4, r3 S3-9): they still count, but they no longer
refuse.  That bypass is only safe with this gate in place - without it a pack
has no spending limit of any kind.

Caps come from the contract's `budget_policy`; the values below are the spec's
defaults, used only where a contract does not state one.  `A_raise_cap` and the
`extra` on `A_redispatch` / `A_retry_verification` all raise the *same* counter
rather than resetting it or opening a new group, so recovery cannot be used to
spend without bound.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

# Policy key -> default cap (STATE-TABLE §3.4).
DEFAULTS: dict[str, int] = {
    "attempt_cap": 2,        # producer_failures, per work attempt
    "round_cap": 4,          # chargeable_rounds
    "reviewer_retry": 2,     # per (stage, output_id)
    "verify_retry": 1,       # per (output_id, contract_hash, invocation_id)
    "exception_grants": 1,
    "wait_cap": 3600,        # accumulated seconds
    "recovery_ops": 1,       # per attempt
    "call_budget": 24,
}

# Pre-deducted at dispatch and never refunded: the cost is already spent.
RESERVED = frozenset({"round_cap", "call_budget"})

# Reaching the cap holds the pack with this reason.  `reviewer_retry` is absent
# because its reason depends on what exhausted it (§3.4, line 233/69).
HOLD_REASON: dict[str, str] = {
    "attempt_cap": "producer_failed",
    "round_cap": "budget_exhausted(round)",
    "verify_retry": "verify_failed",
    "wait_cap": "rate_limit_wait_exceeded",
    "recovery_ops": "unknown_operation",
    "call_budget": "budget_exhausted(call)",
}

# An exhausted reviewer allowance reports what the reviewer was doing wrong, so
# the operator's exit is the matching `A_redispatch`, not a generic budget hold.
REVIEWER_CAUSES = frozenset({"envelope_invalid", "reviewer_failed"})


# What a hold reason calls the counter, mapped to the `budget_policy` key.
# `budget_exhausted(round)` is what the operator is shown, so `A_raise_cap` has
# to accept that spelling rather than rejecting the only name they were given.
ALIASES: dict[str, str] = {"round": "round_cap", "call": "call_budget"}


def normalise_kind(kind: str) -> str:
    return ALIASES.get(kind, kind)


class BudgetError(ValueError):
    """A caller asked about a counter that §3.4 does not define."""


def caps(budget_policy: Mapping[str, Any] | None = None,
         raises: Sequence[Mapping[str, Any]] = ()) -> dict[str, int]:
    """Effective caps: spec defaults, then the contract, then every raise.

    Raises accumulate rather than replace, because each one is a separate
    validity record an operator signed off; collapsing them would silently
    forgive the earlier spend.
    """
    effective = dict(DEFAULTS)
    for key, value in (budget_policy or {}).items():
        if key not in DEFAULTS:
            raise BudgetError(f"unknown budget policy key {key!r}")
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise BudgetError(f"{key}: cap must be a non-negative int, got {value!r}")
        effective[key] = value
    for record in raises:
        key = normalise_kind(record.get("kind"))
        if key not in DEFAULTS:
            raise BudgetError(f"unknown cap raise kind {key!r}")
        extra = record.get("extra", 0)
        if not isinstance(extra, int) or isinstance(extra, bool) or extra < 0:
            raise BudgetError(f"{key}: extra must be a non-negative int, got {extra!r}")
        effective[key] += extra
    return effective


def exhausted(kind: str, used: int, limit: int) -> bool:
    """Whether a counter at `used` may still spend one more against `limit`."""
    if kind not in DEFAULTS:
        raise BudgetError(f"unknown counter {kind!r}")
    return used >= limit


def hold_reason(kind: str, *, cause: str | None = None) -> str:
    if kind == "reviewer_retry":
        if cause not in REVIEWER_CAUSES:
            raise BudgetError(
                f"reviewer_retry needs a cause in {sorted(REVIEWER_CAUSES)}, got {cause!r}")
        return cause
    try:
        return HOLD_REASON[kind]
    except KeyError:
        raise BudgetError(f"{kind} has no hold reason; it refuses in place") from None
