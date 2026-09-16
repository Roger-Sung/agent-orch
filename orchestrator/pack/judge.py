"""The convergence decision (`judge-v1`, STATE-TABLE §4 / D-2026-09-14-11).

The judge answers one question: did this round make the producer's situation
strictly better than the round the producer was actually dispatched against?

Two things make it trustworthy rather than merely plausible:

* **The engine folds the history itself.**  The reviewer's own verdict is an
  input to step 2 only; every lineage set compared here is derived from sealed
  envelopes, so a reviewer cannot report progress into existence.
* **The baseline is the last dispatch, not the last round.**  Rounds where the
  producer never saw the findings (a contract hold, for instance) must not count
  as failures to fix them, which is why `D` is a dispatch record and not a
  round number.
"""
from __future__ import annotations

from typing import Any, Iterable, Sequence

SEVERITY_ORDER = ("Medium", "High")

TERMINAL_WITHDRAWN = "withdrawn"
TERMINAL_SUPERSEDED = "superseded_by_contract"
TERMINAL_RESOLVED = "resolved"
CONTINUING = {"residual", "repeat"}


class JudgeInputMissing(Exception):
    """Step 0: the folded history is unreadable or internally inconsistent."""


class Decision:
    def __init__(self, kind: str, *, reason: str = "", detail: dict[str, Any] | None = None) -> None:
        self.kind = kind
        self.reason = reason
        self.detail = detail or {}

    @property
    def dispatch_ok(self) -> bool:
        return self.kind in {"baseline", "first_dispatch", "improved"}

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Decision({self.kind!r}, {self.reason!r})"


def _stronger(left: str | None, right: str | None) -> str | None:
    """The higher of two severities; ``None`` loses to anything."""
    ranked = [s for s in (left, right) if s in SEVERITY_ORDER]
    if not ranked:
        return left or right
    return max(ranked, key=SEVERITY_ORDER.index)


def _max_severity(lineages: Iterable[str], severity_by_lineage: dict[str, str]) -> int:
    best = -1
    for lineage in lineages:
        severity = severity_by_lineage.get(lineage)
        if severity in SEVERITY_ORDER:
            best = max(best, SEVERITY_ORDER.index(severity))
    return best


def fold_dispositions(
    rounds: Sequence[dict[str, Any]],
) -> dict[str, str]:
    """Apply each round's dispositions in order and return each lineage's state.

    Later rounds win: a lineage resolved in round 2 and raised again in round 4
    ends up continuing, which is exactly the oscillation signal step 1 looks for.
    """
    state: dict[str, str] = {}
    for envelope in rounds:
        prior = envelope.get("prior_round") or {}
        for per_lineage in prior.get("dispositions", {}).values():
            for lineage, entry in per_lineage.items():
                state[lineage] = entry["disposition"]
        for finding in envelope.get("findings", []):
            for lineage in finding.get("lineage_ids", []):
                state[lineage] = "continuing"
    return state


def oscillation_hits(history: Sequence[dict[str, Any]], current: dict[str, Any]) -> set[str]:
    """Lineages that were declared resolved and then came back.

    Computed over the whole history, not just since the last dispatch: a
    regression two rounds later is still a regression.
    """
    resolved: set[str] = set()
    for envelope in history:
        prior = envelope.get("prior_round") or {}
        for per_lineage in prior.get("dispositions", {}).values():
            for lineage, entry in per_lineage.items():
                if entry["disposition"] == TERMINAL_RESOLVED:
                    resolved.add(lineage)

    current_lineages = {
        lineage
        for finding in current.get("findings", [])
        if finding.get("blocking")
        for lineage in finding.get("lineage_ids", [])
    }
    explicit = {
        finding["recurrence_of"]["lineage_id"]
        for finding in current.get("findings", [])
        if finding.get("recurrence_of")
    }
    return (resolved & current_lineages) | (resolved & explicit)


def blocking_lineages(envelope: dict[str, Any]) -> set[str]:
    return {
        lineage
        for finding in envelope.get("findings", [])
        if finding.get("blocking")
        for lineage in finding.get("lineage_ids", [])
    }


def severity_by_lineage(envelope: dict[str, Any]) -> dict[str, str]:
    """Severity pinned per lineage; the strongest occurrence wins."""
    out: dict[str, str] = {}
    for finding in envelope.get("findings", []):
        if not finding.get("blocking"):
            continue
        for lineage in finding.get("lineage_ids", []):
            current = out.get(lineage)
            if current is None or SEVERITY_ORDER.index(finding["severity"]) > SEVERITY_ORDER.index(current):
                out[lineage] = finding["severity"]
    return out


def closure(origin_lineages: set[str], rounds: Sequence[dict[str, Any]]) -> set[str]:
    """Lineages reachable from ``origin_lineages`` through prior_ref edges."""
    reachable = set(origin_lineages)
    changed = True
    while changed:
        changed = False
        for envelope in rounds:
            for finding in envelope.get("findings", []):
                sources = {edge["lineage_id"] for edge in finding.get("prior_refs", [])}
                if sources & reachable:
                    for lineage in finding.get("lineage_ids", []):
                        if lineage not in reachable:
                            reachable.add(lineage)
                            changed = True
    return reachable


def judge_v1(
    current: dict[str, Any],
    *,
    history: Sequence[dict[str, Any]],
    dispatch_record: dict[str, Any] | None,
    rounds_since_dispatch: Sequence[dict[str, Any]] = (),
    acceptance_floor_round: int = 0,
    first_dispatch_origin: dict[str, Any] | None = None,
    inputs_complete: bool = True,
) -> Decision:
    """Return the repair decision for the round just sealed."""
    # 0 - completeness.
    if not inputs_complete:
        raise JudgeInputMissing("history / dispatch records / history_snapshot unreadable")

    # 1 - a regression outranks everything, including an `accepted` verdict.
    hits = oscillation_hits(history, current)
    if hits:
        return Decision("oscillating", reason="resolved lineage reappeared", detail={"lineages": sorted(hits)})

    # 2 - acceptance, gated on the floor so a revoked acceptance cannot be
    # re-granted by an older round.
    if current.get("verdict") == "accepted":
        if current["review_round"] > acceptance_floor_round:
            return Decision("accepted", reason="all obligations PASS")
        return Decision(
            "stalled",
            reason="accepted below the acceptance floor",
            detail={"floor": acceptance_floor_round},
        )

    current_blocking = blocking_lineages(current)

    # 3 - the pack's first sealed review has nothing to compare against.
    if not history:
        return Decision("baseline", reason="first sealed final_review")

    # 4 - the producer has never been dispatched.
    if dispatch_record is None:
        if first_dispatch_origin is None:
            return Decision("stalled", reason="no dispatch record and no first_dispatch_origin")
        origin = set(first_dispatch_origin.get("lineage_set", []))
        allowed = closure(origin, list(history) + [current])
        outside = current_blocking - allowed
        has_recurrence = any(f.get("recurrence_of") for f in current.get("findings", []))
        if outside or has_recurrence:
            return Decision(
                "stalled",
                reason="findings outside the first-dispatch origin closure",
                detail={"outside": sorted(outside), "recurrence": has_recurrence},
            )
        return Decision("first_dispatch", reason="within the round-1 origin closure")

    dispatched = set(dispatch_record.get("lineage_set", []))
    folded = fold_dispositions(list(rounds_since_dispatch) + [current])

    # `resolved` stays in prior_eff on purpose: progress shows up by the lineage
    # being absent from `current`, not by shrinking the baseline it is measured
    # against - otherwise fixing something would also lower the bar.
    prior_eff = {
        lineage
        for lineage in dispatched
        if folded.get(lineage) not in {TERMINAL_WITHDRAWN, TERMINAL_SUPERSEDED}
    }

    # 5 - anything the producer was never handed is not a failure to fix it.
    new_vs_dispatch = current_blocking - dispatched
    if new_vs_dispatch:
        return Decision(
            "stalled",
            reason="blocking lineages never dispatched to the producer",
            detail={"new_vs_dispatch": sorted(new_vs_dispatch)},
        )

    # 6 - nothing left to improve on, yet findings remain.
    if not prior_eff and current_blocking:
        return Decision("stalled", reason="no effective prior set but findings remain")

    # Severity is pinned per side (STATE-TABLE §4): the baseline keeps the
    # severity the lineage carried when it was dispatched, so a reviewer cannot
    # manufacture "improvement" by re-grading an old finding downwards.  The
    # current side is graded by this round alone, so a genuine downgrade counts.
    prior_severities: dict[str, str] = {}
    for envelope in list(history) + list(rounds_since_dispatch):
        for lineage, severity in severity_by_lineage(envelope).items():
            prior_severities[lineage] = _stronger(prior_severities.get(lineage), severity)
    current_severities = severity_by_lineage(current)

    current_max = _max_severity(current_blocking, current_severities)
    prior_max = _max_severity(prior_eff, prior_severities)

    # 7 - strictly better: a lower top severity, or fewer at the same top.
    if current_max < prior_max:
        return Decision("improved", reason="top severity fell")
    if current_max == prior_max:
        level = SEVERITY_ORDER[current_max] if current_max >= 0 else None
        current_at_top = {l for l in current_blocking if current_severities.get(l) == level}
        prior_at_top = {l for l in prior_eff if prior_severities.get(l) == level}
        if len(current_at_top) < len(prior_at_top):
            return Decision("improved", reason="fewer findings at the top severity")

    # 8 - same level, same count: not progress, whatever the reviewer says.
    return Decision(
        "stalled",
        reason="no strict improvement against the dispatch baseline",
        detail={"current": sorted(current_blocking), "prior_eff": sorted(prior_eff)},
    )
