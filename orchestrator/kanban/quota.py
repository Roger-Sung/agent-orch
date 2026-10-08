"""Manual weekly-quota snapshots and deterministic admission math (S5).

This module deliberately has no provider reader and no scheduler.  A human
records a weekly remaining percentage in integer basis points; callers may
then use the pure evaluation helpers to explain whether every required pool
can afford one approved card.  Claim creation remains a later slice.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from .store import WEEK_MS

DAYTIME_RESERVE_BP = 3500
PER_NIGHT_CAP_BP = 800
SNAPSHOT_MAX_AGE_MS = WEEK_MS

ACTIVE_OR_UNKNOWN_PHASES = frozenset({"reserved", "submitted", "unknown"})
POOL_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,63}$")
SNAPSHOT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def now_ms() -> int:
    return int(time.time() * 1000)


def valid_pool_key(value: Any) -> bool:
    """Pool keys are short non-secret aliases, never accounts or credentials."""
    return isinstance(value, str) and POOL_KEY_RE.fullmatch(value) is not None


def _integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def evaluate_pool(
    snapshot: Mapping[str, Any] | None,
    *,
    estimate_bp: Any,
    remaining_windows: Any,
    claims: Iterable[Mapping[str, Any]] = (),
    at_ms: int | None = None,
    daytime_reserve_bp: int = DAYTIME_RESERVE_BP,
    per_night_cap_bp: int = PER_NIGHT_CAP_BP,
    snapshot_max_age_ms: int = SNAPSHOT_MAX_AGE_MS,
) -> dict[str, Any]:
    """Evaluate one pool without reading SQLite or mutating state.

    ``claims`` contains conservative debits.  Every debit after the snapshot's
    covered watermark counts regardless of the run's later outcome; T1.8 never
    refunds a failed/no-spawn/unknown reservation automatically.
    """
    at_ms = now_ms() if at_ms is None else at_ms
    explanation: dict[str, Any] = {
        "eligible": False,
        "reason": None,
        "remaining_bp": None,
        "windows": remaining_windows,
        "budget_bp": None,
        "estimate_bp": estimate_bp,
        "debited_bp": None,
        "snapshot_id": snapshot.get("snapshot_id") if isinstance(snapshot, Mapping) else None,
    }
    if snapshot is None:
        explanation["reason"] = "quota_missing"
        return explanation

    required_ints = (
        snapshot.get("weekly_remaining_bp"),
        snapshot.get("observed_at"),
        snapshot.get("recorded_at"),
        snapshot.get("reset_at"),
        snapshot.get("covered_claim_seq"),
        at_ms,
        remaining_windows,
        estimate_bp,
        daytime_reserve_bp,
        per_night_cap_bp,
        snapshot_max_age_ms,
    )
    if (
        not valid_pool_key(snapshot.get("pool_key"))
        or snapshot.get("source") != "manual"
        or not all(_integer(value) for value in required_ints)
        or not 0 <= snapshot["weekly_remaining_bp"] <= 10000
        or snapshot["covered_claim_seq"] < 0
        or snapshot.get("stale") not in (0, 1)
        or snapshot["recorded_at"] < snapshot["observed_at"]
        or snapshot["recorded_at"] > at_ms
        or remaining_windows < 0
        or estimate_bp <= 0
        or not 0 <= daytime_reserve_bp <= 10000
        or not 0 < per_night_cap_bp <= 10000
        or snapshot_max_age_ms <= 0
        or snapshot["observed_at"] > at_ms
        or snapshot["observed_at"] >= snapshot["reset_at"]
        or snapshot["reset_at"] > snapshot["observed_at"] + WEEK_MS
    ):
        explanation["reason"] = "quota_invalid"
        return explanation

    # Reset has priority over an explicit/age stale mark (S5).
    if at_ms >= snapshot["reset_at"]:
        explanation["reason"] = "quota_reset_passed"
        return explanation
    if snapshot.get("stale") not in (0, False) or at_ms - snapshot["observed_at"] >= snapshot_max_age_ms:
        explanation["reason"] = "quota_stale"
        return explanation

    debit = 0
    try:
        for claim in claims:
            seq = claim.get("claim_seq")
            value = claim.get("debit_bp")
            if not _integer(seq) or seq <= 0 or not _integer(value) or value < 0:
                raise ValueError
            if seq > snapshot["covered_claim_seq"]:
                debit += value
    except (AttributeError, TypeError, ValueError):
        explanation["reason"] = "quota_invalid"
        return explanation

    remaining = snapshot["weekly_remaining_bp"] - debit
    windows = max(1, remaining_windows)
    budget = min(per_night_cap_bp, max(0, remaining - daytime_reserve_bp) // windows)
    explanation.update(
        {
            "remaining_bp": remaining,
            "windows": windows,
            "budget_bp": budget,
            "debited_bp": debit,
        }
    )
    if remaining <= daytime_reserve_bp:
        explanation["reason"] = "quota_reserve"
    elif estimate_bp > budget:
        explanation["reason"] = "quota_estimate_exceeds_budget"
    else:
        explanation["eligible"] = True
    return explanation


def evaluate_pools(
    required_pools: Sequence[str],
    snapshots: Mapping[str, Mapping[str, Any]],
    estimates_by_pool: Mapping[str, Any],
    *,
    claims_by_pool: Mapping[str, Iterable[Mapping[str, Any]]] | None = None,
    remaining_windows: int,
    at_ms: int | None = None,
) -> dict[str, Any]:
    """Evaluate all distinct required pools, merging duplicate role usage."""
    claims_by_pool = claims_by_pool or {}
    pools: list[str] = []
    seen: set[str] = set()
    for pool in required_pools:
        if pool not in seen:
            pools.append(pool)
            seen.add(pool)

    results: dict[str, dict[str, Any]] = {}
    for pool in pools:
        if not valid_pool_key(pool):
            results[str(pool)] = {
                "eligible": False,
                "reason": "quota_invalid",
                "remaining_bp": None,
                "windows": remaining_windows,
                "budget_bp": None,
                "estimate_bp": estimates_by_pool.get(pool),
                "debited_bp": None,
                "snapshot_id": None,
            }
            continue
        estimate = estimates_by_pool.get(pool)
        if estimate is None:
            results[pool] = {
                "eligible": False,
                "reason": "quota_missing",
                "remaining_bp": None,
                "windows": remaining_windows,
                "budget_bp": None,
                "estimate_bp": None,
                "debited_bp": None,
                "snapshot_id": snapshots.get(pool, {}).get("snapshot_id"),
            }
            continue
        results[pool] = evaluate_pool(
            snapshots.get(pool),
            estimate_bp=estimate,
            remaining_windows=remaining_windows,
            claims=claims_by_pool.get(pool, ()),
            at_ms=at_ms,
        )
    return {
        "eligible": bool(pools) and all(item["eligible"] for item in results.values()),
        "pools": results,
    }


def snapshot_command(
    conn: sqlite3.Connection,
    operation_id: str,
    digest: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Record a new human observation and its covered-claim watermark."""
    pool = payload.get("pool_key")
    snapshot_id = payload.get("snapshot_id")
    remaining = payload.get("weekly_remaining_bp")
    observed = payload.get("observed_at")
    reset = payload.get("reset_at")
    recorded = now_ms()

    reason: str | None = None
    if not valid_pool_key(pool):
        reason = "quota_invalid"
    elif not isinstance(snapshot_id, str) or SNAPSHOT_ID_RE.fullmatch(snapshot_id) is None:
        reason = "quota_invalid"
    if reason is None and (
        not _integer(remaining)
        or not 0 <= remaining <= 10000
        or not _integer(observed)
        or not _integer(reset)
        or observed > recorded
        or observed >= reset
        or reset > observed + WEEK_MS
    ):
        reason = "quota_invalid"

    relevant: list[tuple[sqlite3.Row, int | None]] = []
    if reason is None:
        rows = conn.execute(
            "SELECT n.*, e.at AS safe_stop_at FROM kanban_nights n "
            "LEFT JOIN kanban_events e ON e.operation_id=n.stop_evidence_event_id "
            "ORDER BY n.claim_seq"
        ).fetchall()
        for row in rows:
            try:
                claim = json.loads(row["pool_claims"])
            except (TypeError, json.JSONDecodeError):
                reason = "quota_invalid"
                break
            if not isinstance(claim, dict) or pool not in claim:
                continue
            relevant.append((row, row["safe_stop_at"]))
            if row["phase"] in ACTIVE_OR_UNKNOWN_PHASES:
                reason = "quota_claim_active"
                break
            if row["phase"] != "stopped" or row["safe_stop_at"] is None:
                reason = "quota_claim_stop_unverified"
                break
            if observed < row["safe_stop_at"]:
                reason = "quota_observation_before_claim_stopped"
                break

    if reason is not None:
        return _quota_event(conn, "quota-snapshot", operation_id, digest, payload, reason=reason)

    # The watermark is global even though validation/debits are pool-specific:
    # it means that this observation happened after every claim sequence that
    # existed in this transaction.  A later pool claim therefore always has a
    # greater sequence and cannot disappear behind the snapshot.
    covered = conn.execute(
        "SELECT COALESCE(MAX(claim_seq), 0) AS seq FROM kanban_nights"
    ).fetchone()["seq"]
    try:
        conn.execute(
            "INSERT INTO kanban_quota_snapshots("
            "snapshot_id,pool_key,weekly_remaining_bp,observed_at,recorded_at,reset_at,"
            "source,operator,covered_claim_seq,stale,stale_event_id)"
            " VALUES(?,?,?,?,?,?,'manual',?,?,0,NULL)",
            (snapshot_id, pool, remaining, observed, recorded, reset, payload["actor"], covered),
        )
    except sqlite3.IntegrityError:
        return _quota_event(
            conn, "quota-snapshot", operation_id, digest, payload, reason="snapshot_id_conflict"
        )
    return _quota_event(
        conn,
        "quota-snapshot",
        operation_id,
        digest,
        payload,
        metadata={"snapshot_id": snapshot_id, "pool_key": pool, "covered_claim_seq": covered},
    )


def latest_snapshot(conn: sqlite3.Connection, pool_key: str) -> dict[str, Any] | None:
    """Return the newest observation, including when it is stale.

    Invalidation must not make an older, more optimistic observation current
    again, so callers always select the latest row first and then evaluate its
    validity.
    """
    if not valid_pool_key(pool_key):
        return None
    row = conn.execute(
        "SELECT * FROM kanban_quota_snapshots WHERE pool_key=? "
        "ORDER BY recorded_at DESC, rowid DESC LIMIT 1",
        (pool_key,),
    ).fetchone()
    return dict(row) if row is not None else None


def claims_after_watermark(
    conn: sqlite3.Connection, pool_key: str, covered_claim_seq: int
) -> list[dict[str, int]]:
    """Load every conservative debit after a snapshot watermark.

    Phase is intentionally not a filter: success, failure, no-spawn and
    unknown reservations all stay debited until a later manual observation.
    """
    if not valid_pool_key(pool_key) or not _integer(covered_claim_seq) or covered_claim_seq < 0:
        raise ValueError("invalid quota watermark input")
    result: list[dict[str, int]] = []
    rows = conn.execute(
        "SELECT claim_seq,pool_claims FROM kanban_nights WHERE claim_seq>? ORDER BY claim_seq",
        (covered_claim_seq,),
    ).fetchall()
    for row in rows:
        try:
            pools = json.loads(row["pool_claims"])
            claim = pools.get(pool_key) if isinstance(pools, dict) else None
            debit = claim.get("debit_bp") if isinstance(claim, dict) else None
        except (TypeError, json.JSONDecodeError):
            raise ValueError("invalid stored quota claim") from None
        if claim is None:
            continue
        if not _integer(debit) or debit < 0:
            raise ValueError("invalid stored quota debit")
        result.append({"claim_seq": row["claim_seq"], "debit_bp": debit})
    return result


def invalidate_command(
    conn: sqlite3.Connection,
    operation_id: str,
    digest: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Only lower trust in an exact snapshot; never delete or replace it."""
    snapshot_id = payload.get("snapshot_id")
    row = None
    if isinstance(snapshot_id, str) and snapshot_id:
        row = conn.execute(
            "SELECT * FROM kanban_quota_snapshots WHERE snapshot_id=?", (snapshot_id,)
        ).fetchone()
    if row is None:
        return _quota_event(
            conn, "quota-invalidate", operation_id, digest, payload, reason="quota_missing"
        )
    if row["stale"]:
        return _quota_event(
            conn, "quota-invalidate", operation_id, digest, payload, reason="quota_stale"
        )

    result = _quota_event(
        conn,
        "quota-invalidate",
        operation_id,
        digest,
        payload,
        metadata={"snapshot_id": snapshot_id, "pool_key": row["pool_key"], "stale": True},
    )
    conn.execute(
        "UPDATE kanban_quota_snapshots SET stale=1, stale_event_id=? WHERE snapshot_id=? AND stale=0",
        (operation_id, snapshot_id),
    )
    return result


def _quota_event(
    conn: sqlite3.Connection,
    kind: str,
    operation_id: str,
    digest: str,
    payload: dict[str, Any],
    *,
    reason: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = "rejected" if reason else "accepted"
    conn.execute(
        "INSERT INTO kanban_events(operation_id,payload_hash,kind,card_id,task_id,"
        "expected_revision,result_revision,actor,at,result,reason,payload,metadata_delta)"
        " VALUES(?,?,?,NULL,NULL,NULL,NULL,?,?,?,?,?,?)",
        (
            operation_id,
            digest,
            kind,
            payload["actor"],
            now_ms(),
            result,
            reason,
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            json.dumps(metadata, ensure_ascii=False, sort_keys=True) if metadata else None,
        ),
    )
    return {
        "command": kind,
        "operation_id": operation_id,
        "card_id": None,
        "result": result,
        "reason": reason,
        "revision": None,
        "recorded": True,
        "replayed": False,
        **(metadata or {}),
    }
