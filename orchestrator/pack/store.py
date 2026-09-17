"""The pack-v1 SQLite store (STATE-TABLE §1, §6, §8).

Schema notes that are decisions, not incidentals:

* **Tables are added, never altered.**  `ensure_schema` only issues
  ``CREATE TABLE IF NOT EXISTS`` against the same connection the legacy
  orchestrator uses, so opening an old database changes nothing about the
  existing tables (IMPLEMENTATION-PLAN §2).
* **`unconsumed` is a column, not a table** (§1.4).  An operation is waiting to
  be consumed exactly when it has a result and no ``consumed_at``; making that a
  separate table would let the two drift.  The query must test both - a
  ``consumed_at IS NULL`` alone would also select running operations.
* **Approvals and derivations share one append-only table.**  They differ in
  ``kind`` and payload, not in lifecycle: both are immutable records whose
  validity is decided by walking a chain.
* **Budgets are counters on the owning row.**  The reservation and the
  settlement stay two distinct columns, because a crash between them is exactly
  what the reservation exists to survive.
"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Iterable, Sequence

SCHEMA = """
CREATE TABLE IF NOT EXISTS pack_packs(
  pack_id TEXT PRIMARY KEY,
  target_id TEXT NOT NULL,
  change TEXT NOT NULL,
  state TEXT NOT NULL,
  hold_reason TEXT,
  return_point TEXT,
  continuation TEXT,
  blockers TEXT NOT NULL DEFAULT '[]',
  decision TEXT,
  evidence_todo TEXT NOT NULL DEFAULT '[]',
  first_dispatch_origin TEXT,
  contract_hash TEXT,
  contract_version INTEGER NOT NULL DEFAULT 1,
  contract_revision INTEGER NOT NULL DEFAULT 1,
  review_round INTEGER NOT NULL DEFAULT 0,
  review_seq INTEGER NOT NULL DEFAULT 0,
  k_last INTEGER NOT NULL DEFAULT 0,
  acceptance_floor_round INTEGER NOT NULL DEFAULT 0,
  revoked_generation INTEGER NOT NULL DEFAULT 0,
  lease_token TEXT,
  host_boot_id TEXT,
  reviewer_retry INTEGER NOT NULL DEFAULT 0,
  verify_retry INTEGER NOT NULL DEFAULT 0,
  recovery_ops INTEGER NOT NULL DEFAULT 0,
  exception_grants_issued INTEGER NOT NULL DEFAULT 0,
  calls_reserved INTEGER NOT NULL DEFAULT 0,
  calls_settled INTEGER NOT NULL DEFAULT 0,
  wait_accumulated_s INTEGER NOT NULL DEFAULT 0,
  updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS pack_work_attempts(
  attempt_id TEXT PRIMARY KEY,
  pack_id TEXT NOT NULL REFERENCES pack_packs(pack_id),
  base_revision TEXT NOT NULL,
  candidate_input TEXT,
  next_output_id INTEGER NOT NULL,
  producer_failures INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS pack_operations(
  op_id TEXT PRIMARY KEY,
  pack_id TEXT NOT NULL REFERENCES pack_packs(pack_id),
  attempt_id TEXT,
  logical_op_id TEXT NOT NULL,
  type TEXT NOT NULL,
  stage TEXT,
  invocation_id TEXT,
  input_output_id INTEGER,
  produced_output_id INTEGER,
  reserved_counters TEXT,
  grant_id TEXT,
  spawned INTEGER NOT NULL DEFAULT 0,
  spawn_outcome TEXT,
  process_identity TEXT,
  result TEXT,
  result_ref TEXT,
  call_binding TEXT,
  superseded_by TEXT,
  receipt_ref TEXT,
  consumed_at INTEGER,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_pack_ops_pending
  ON pack_operations(pack_id) WHERE result='completed' AND consumed_at IS NULL;

CREATE TABLE IF NOT EXISTS pack_outputs(
  pack_id TEXT NOT NULL REFERENCES pack_packs(pack_id),
  output_id INTEGER NOT NULL,
  attempt_id TEXT NOT NULL,
  candidate_fingerprint TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  PRIMARY KEY(pack_id, output_id)
);

CREATE TABLE IF NOT EXISTS pack_dispatch_records(
  d_id TEXT PRIMARY KEY,
  pack_id TEXT NOT NULL REFERENCES pack_packs(pack_id),
  source_review_seq INTEGER NOT NULL,
  repair_op_id TEXT NOT NULL,
  target_output_id INTEGER NOT NULL,
  lineage_set TEXT NOT NULL,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS pack_records(
  record_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  pack_id TEXT,
  payload TEXT NOT NULL,
  hmac TEXT,
  revoked INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS pack_grants(
  grant_id TEXT PRIMARY KEY,
  pack_id TEXT NOT NULL REFERENCES pack_packs(pack_id),
  output_id INTEGER NOT NULL,
  decision_id TEXT NOT NULL,
  state TEXT NOT NULL,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS pack_sessions(
  pack_id TEXT NOT NULL REFERENCES pack_packs(pack_id),
  role TEXT NOT NULL,
  attempt_id TEXT,
  seq INTEGER NOT NULL,
  state TEXT NOT NULL,
  provider_binding TEXT,
  predecessor TEXT,
  checkpoint_ref TEXT,
  pending_op_id TEXT,
  created_at INTEGER NOT NULL,
  PRIMARY KEY(pack_id, role, attempt_id, seq)
);

CREATE TABLE IF NOT EXISTS pack_receipts(
  receipt_id TEXT PRIMARY KEY,
  pack_id TEXT NOT NULL REFERENCES pack_packs(pack_id),
  kind TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  generation INTEGER NOT NULL DEFAULT 0,
  payload TEXT NOT NULL,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS pack_events(
  event_id TEXT PRIMARY KEY,
  pack_id TEXT NOT NULL REFERENCES pack_packs(pack_id),
  kind TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  result TEXT,
  consumed_at INTEGER,
  created_at INTEGER NOT NULL
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Idempotent: only ``CREATE TABLE IF NOT EXISTS``, never ALTER.

    Must be called at connection setup, never inside a transaction:
    ``executescript`` issues an implicit COMMIT first, which would silently end
    a caller's ``BEGIN IMMEDIATE`` and make its later COMMIT fail.
    """
    if conn.in_transaction:
        raise RuntimeError("pack schema creation must not run inside a transaction")
    conn.executescript(SCHEMA)


def _now() -> int:
    return int(time.time())


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load(value: str | None, default: Any = None) -> Any:
    if value is None:
        return default
    return json.loads(value)


class PackStore:
    """Thin, explicit accessors over one SQLite connection.

    Every method that changes more than one row is written to be called inside
    a caller-held transaction; the store never opens one itself, because the
    atomicity that matters (consume + transition + settle) spans several of
    these calls.
    """

    def __init__(self, conn: sqlite3.Connection, *, create: bool = True) -> None:
        self.conn = conn
        # `db.connect` already creates the tables at open time; the default here
        # keeps standalone use (tests, tools) working without a second step.
        if create and not conn.in_transaction:
            ensure_schema(conn)

    # ---------------- packs ----------------

    def create_pack(self, pack_id: str, *, target_id: str, change: str,
                    state: str = "blocked_deps", host_boot_id: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO pack_packs(pack_id, target_id, change, state, host_boot_id, updated_at)"
            " VALUES(?,?,?,?,?,?)",
            (pack_id, target_id, change, state, host_boot_id, _now()),
        )

    def get_pack(self, pack_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM pack_packs WHERE pack_id=?", (pack_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown pack {pack_id!r}")
        pack = dict(row)
        pack["blockers"] = _load(pack["blockers"], [])
        pack["evidence_todo"] = _load(pack["evidence_todo"], [])
        pack["decision"] = _load(pack["decision"])
        pack["first_dispatch_origin"] = _load(pack["first_dispatch_origin"])
        return pack

    def update_pack(self, pack_id: str, **fields: Any) -> None:
        if not fields:
            return
        encoded: dict[str, Any] = {}
        for key, value in fields.items():
            if key in {"blockers", "evidence_todo", "decision", "first_dispatch_origin"}:
                encoded[key] = None if value is None else _json(value)
            else:
                encoded[key] = value
        encoded["updated_at"] = _now()
        assignments = ", ".join(f"{k}=?" for k in encoded)
        self.conn.execute(
            f"UPDATE pack_packs SET {assignments} WHERE pack_id=?",
            (*encoded.values(), pack_id),
        )

    def bump(self, pack_id: str, column: str, delta: int = 1) -> int:
        self.conn.execute(
            f"UPDATE pack_packs SET {column}={column}+?, updated_at=? WHERE pack_id=?",
            (delta, _now(), pack_id),
        )
        return self.conn.execute(
            f"SELECT {column} AS v FROM pack_packs WHERE pack_id=?", (pack_id,)
        ).fetchone()["v"]

    # ---------------- work attempts ----------------

    def create_attempt(self, attempt_id: str, pack_id: str, *, base_revision: str,
                       candidate_input: str | None, next_output_id: int) -> None:
        self.conn.execute(
            "INSERT INTO pack_work_attempts(attempt_id, pack_id, base_revision, candidate_input,"
            " next_output_id, created_at) VALUES(?,?,?,?,?,?)",
            (attempt_id, pack_id, base_revision, candidate_input, next_output_id, _now()),
        )

    def get_attempt(self, attempt_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM pack_work_attempts WHERE attempt_id=?", (attempt_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown attempt {attempt_id!r}")
        return dict(row)

    def current_attempt(self, pack_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM pack_work_attempts WHERE pack_id=? ORDER BY created_at DESC, rowid DESC"
            " LIMIT 1",
            (pack_id,),
        ).fetchone()
        return dict(row) if row else None

    def update_attempt(self, attempt_id: str, **fields: Any) -> None:
        assignments = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(
            f"UPDATE pack_work_attempts SET {assignments} WHERE attempt_id=?",
            (*fields.values(), attempt_id),
        )

    # ---------------- operations ----------------

    def create_operation(self, op_id: str, pack_id: str, *, type: str,
                         attempt_id: str | None = None, logical_op_id: str | None = None,
                         stage: str | None = None, invocation_id: str | None = None,
                         input_output_id: int | None = None,
                         reserved_counters: dict[str, int] | None = None,
                         grant_id: str | None = None) -> None:
        """Create an operation together with its budget reservation.

        Reservation and operation are one insert on purpose: a reservation
        without an operation would leak budget, and an operation without one
        would spend budget nobody counted.
        """
        self.conn.execute(
            "INSERT INTO pack_operations(op_id, pack_id, attempt_id, logical_op_id, type, stage,"
            " invocation_id, input_output_id, reserved_counters, grant_id, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (op_id, pack_id, attempt_id, logical_op_id or op_id, type, stage, invocation_id,
             input_output_id, _json(reserved_counters) if reserved_counters else None,
             grant_id, _now()),
        )

    def get_operation(self, op_id: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM pack_operations WHERE op_id=?", (op_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown operation {op_id!r}")
        op = dict(row)
        op["reserved_counters"] = _load(op["reserved_counters"], {})
        op["call_binding"] = _load(op["call_binding"])
        op["process_identity"] = _load(op["process_identity"])
        return op

    def update_operation(self, op_id: str, **fields: Any) -> None:
        encoded = {
            k: (_json(v) if k in {"reserved_counters", "call_binding", "process_identity"} and v is not None else v)
            for k, v in fields.items()
        }
        assignments = ", ".join(f"{k}=?" for k in encoded)
        self.conn.execute(
            f"UPDATE pack_operations SET {assignments} WHERE op_id=?",
            (*encoded.values(), op_id),
        )

    def operations(self, pack_id: str, *, stage: str | None = None) -> list[dict[str, Any]]:
        """Every operation of this pack in creation order."""
        if stage is None:
            rows = self.conn.execute(
                "SELECT op_id FROM pack_operations WHERE pack_id=? ORDER BY created_at, rowid",
                (pack_id,)).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT op_id FROM pack_operations WHERE pack_id=? AND stage=?"
                " ORDER BY created_at, rowid", (pack_id, stage)).fetchall()
        return [self.get_operation(row["op_id"]) for row in rows]

    def running_operations(self, pack_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT op_id FROM pack_operations WHERE pack_id=? AND result IS NULL", (pack_id,)
        ).fetchall()
        return [self.get_operation(row["op_id"]) for row in rows]

    def unknown_operations(self, pack_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT op_id FROM pack_operations WHERE pack_id=? AND result='unknown'"
            " AND superseded_by IS NULL",
            (pack_id,),
        ).fetchall()
        return [self.get_operation(row["op_id"]) for row in rows]

    def unconsumed_operations(self, pack_id: str) -> list[dict[str, Any]]:
        """Operations that delivered a result nobody has acted on yet.

        The startup scan treats ``running`` / ``unknown`` / ``unconsumed`` as
        three disjoint sets (STATE-TABLE §3.0b), so this deliberately excludes:

        * ``result IS NULL`` - still running, nothing to consume;
        * ``result = 'unknown'`` - the whole point is that there is no known
          result, and feeding it through the consumption protocol would
          "consume" the very thing reconcile still has to determine;
        * failure results - their effect was applied when the call was
          classified (§3.2a); there is no payload to take up.
        """
        rows = self.conn.execute(
            "SELECT op_id FROM pack_operations WHERE pack_id=? AND result='completed'"
            " AND consumed_at IS NULL ORDER BY created_at, rowid",
            (pack_id,),
        ).fetchall()
        return [self.get_operation(row["op_id"]) for row in rows]

    def mark_consumed(self, op_id: str) -> None:
        self.conn.execute(
            "UPDATE pack_operations SET consumed_at=? WHERE op_id=? AND consumed_at IS NULL",
            (_now(), op_id),
        )

    # ---------------- outputs / dispatch records ----------------

    def freeze_output(self, pack_id: str, output_id: int, attempt_id: str,
                      candidate_fingerprint: str) -> None:
        self.conn.execute(
            "INSERT INTO pack_outputs(pack_id, output_id, attempt_id, candidate_fingerprint,"
            " created_at) VALUES(?,?,?,?,?)",
            (pack_id, output_id, attempt_id, candidate_fingerprint, _now()),
        )
        self.conn.execute(
            "UPDATE pack_packs SET k_last=MAX(k_last, ?), updated_at=? WHERE pack_id=?",
            (output_id, _now(), pack_id),
        )

    def create_dispatch_record(self, d_id: str, pack_id: str, *, source_review_seq: int,
                               repair_op_id: str, target_output_id: int,
                               lineage_set: Sequence[str]) -> None:
        self.conn.execute(
            "INSERT INTO pack_dispatch_records(d_id, pack_id, source_review_seq, repair_op_id,"
            " target_output_id, lineage_set, created_at) VALUES(?,?,?,?,?,?,?)",
            (d_id, pack_id, source_review_seq, repair_op_id, target_output_id,
             _json(sorted(lineage_set)), _now()),
        )

    def count_dispatch_records(self, pack_id: str) -> int:
        """`chargeable_rounds` is defined as the dispatch record count (§3.4)."""
        return self.conn.execute(
            "SELECT COUNT(*) AS n FROM pack_dispatch_records WHERE pack_id=?", (pack_id,)
        ).fetchone()["n"]

    def last_dispatch_record(self, pack_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM pack_dispatch_records WHERE pack_id=? ORDER BY source_review_seq DESC,"
            " rowid DESC LIMIT 1",
            (pack_id,),
        ).fetchone()
        if row is None:
            return None
        record = dict(row)
        record["lineage_set"] = _load(record["lineage_set"], [])
        return record

    # ---------------- append-only records ----------------

    def add_record(self, record_id: str, kind: str, payload: dict[str, Any], *,
                   pack_id: str | None = None, hmac: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO pack_records(record_id, kind, pack_id, payload, hmac, created_at)"
            " VALUES(?,?,?,?,?,?)",
            (record_id, kind, pack_id, _json(payload), hmac, _now()),
        )

    def get_record(self, record_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM pack_records WHERE record_id=?", (record_id,)
        ).fetchone()
        if row is None:
            return None
        record = dict(row)
        record["payload"] = _load(record["payload"], {})
        return record

    def revoke_record(self, record_id: str) -> None:
        self.conn.execute("UPDATE pack_records SET revoked=1 WHERE record_id=?", (record_id,))

    def records_of_kind(self, kind: str, pack_id: str | None = None) -> list[dict[str, Any]]:
        if pack_id is None:
            rows = self.conn.execute(
                "SELECT record_id FROM pack_records WHERE kind=? ORDER BY created_at, rowid", (kind,)
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT record_id FROM pack_records WHERE kind=? AND pack_id=?"
                " ORDER BY created_at, rowid",
                (kind, pack_id),
            ).fetchall()
        return [self.get_record(row["record_id"]) for row in rows]

    # ---------------- grants ----------------

    def issue_grant(self, grant_id: str, pack_id: str, *, output_id: int, decision_id: str) -> None:
        self.conn.execute(
            "INSERT INTO pack_grants(grant_id, pack_id, output_id, decision_id, state, created_at)"
            " VALUES(?,?,?,?,'issued',?)",
            (grant_id, pack_id, output_id, decision_id, _now()),
        )
        self.bump(pack_id, "exception_grants_issued")

    def set_grant_state(self, grant_id: str, state: str) -> None:
        self.conn.execute("UPDATE pack_grants SET state=? WHERE grant_id=?", (state, grant_id))

    def usable_grant(self, pack_id: str, *, output_id: int, decision_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM pack_grants WHERE pack_id=? AND output_id=? AND decision_id=?"
            " AND state IN ('issued','reserved') ORDER BY rowid LIMIT 1",
            (pack_id, output_id, decision_id),
        ).fetchone()
        return dict(row) if row else None

    # ---------------- sessions ----------------

    def session_rows(self, pack_id: str, role: str, attempt_id: str | None) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM pack_sessions WHERE pack_id=? AND role=? AND attempt_id IS ?"
            " ORDER BY seq",
            (pack_id, role, attempt_id),
        ).fetchall()
        out = []
        for row in rows:
            record = dict(row)
            record["provider_binding"] = _load(record["provider_binding"])
            record["predecessor"] = _load(record["predecessor"])
            out.append(record)
        return out

    def pending_session_for(self, op_id: str) -> dict[str, Any] | None:
        """The session row that is still waiting on this operation, if any.

        A pending row is the record that a call was dispatched and never
        answered; §5 reads it to tell `review_session_lost` apart from a plain
        unknown operation.
        """
        row = self.conn.execute(
            "SELECT * FROM pack_sessions WHERE pending_op_id=? AND state='pending'", (op_id,)
        ).fetchone()
        if row is None:
            return None
        record = dict(row)
        record["provider_binding"] = _load(record["provider_binding"])
        record["predecessor"] = _load(record["predecessor"])
        return record

    def latest_session(self, pack_id: str, role: str, attempt_id: str | None) -> dict[str, Any] | None:
        rows = self.session_rows(pack_id, role, attempt_id)
        return rows[-1] if rows else None

    def add_session(self, pack_id: str, role: str, attempt_id: str | None, *,
                    state: str = "new", predecessor: dict[str, Any] | None = None,
                    checkpoint_ref: str | None = None) -> int:
        rows = self.session_rows(pack_id, role, attempt_id)
        seq = (rows[-1]["seq"] + 1) if rows else 1
        self.conn.execute(
            "INSERT INTO pack_sessions(pack_id, role, attempt_id, seq, state, predecessor,"
            " checkpoint_ref, created_at) VALUES(?,?,?,?,?,?,?,?)",
            (pack_id, role, attempt_id, seq, state,
             _json(predecessor) if predecessor else None, checkpoint_ref, _now()),
        )
        return seq

    def update_session(self, pack_id: str, role: str, attempt_id: str | None, seq: int,
                       **fields: Any) -> None:
        encoded = {
            k: (_json(v) if k in {"provider_binding", "predecessor"} and v is not None else v)
            for k, v in fields.items()
        }
        assignments = ", ".join(f"{k}=?" for k in encoded)
        self.conn.execute(
            f"UPDATE pack_sessions SET {assignments} WHERE pack_id=? AND role=? AND attempt_id IS ?"
            " AND seq=?",
            (*encoded.values(), pack_id, role, attempt_id, seq),
        )

    # ---------------- receipts ----------------

    def add_receipt(self, receipt_id: str, pack_id: str, *, kind: str, sha256: str,
                    payload: dict[str, Any], generation: int = 0) -> None:
        self.conn.execute(
            "INSERT INTO pack_receipts(receipt_id, pack_id, kind, sha256, generation, payload,"
            " created_at) VALUES(?,?,?,?,?,?,?)",
            (receipt_id, pack_id, kind, sha256, generation, _json(payload), _now()),
        )

    def receipts(self, pack_id: str, kind: str | None = None) -> list[dict[str, Any]]:
        if kind is None:
            rows = self.conn.execute(
                "SELECT * FROM pack_receipts WHERE pack_id=? ORDER BY created_at, rowid", (pack_id,)
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM pack_receipts WHERE pack_id=? AND kind=? ORDER BY created_at, rowid",
                (pack_id, kind),
            ).fetchall()
        out = []
        for row in rows:
            record = dict(row)
            record["payload"] = _load(record["payload"], {})
            out.append(record)
        return out

    # ---------------- events (idempotency) ----------------

    def record_event(self, event_id: str, pack_id: str, kind: str, payload_hash: str) -> dict[str, Any] | None:
        """Return the prior outcome if this exact event was already applied.

        Idempotency is keyed on ``event_id`` plus the payload hash so that a
        replay of the same authorisation is a no-op, while a *different* payload
        under a reused id is a new event rather than a silent match.
        """
        row = self.conn.execute(
            "SELECT * FROM pack_events WHERE event_id=?", (event_id,)
        ).fetchone()
        if row is not None:
            if row["payload_hash"] != payload_hash:
                raise ValueError(f"event {event_id!r} replayed with a different payload")
            return dict(row)
        self.conn.execute(
            "INSERT INTO pack_events(event_id, pack_id, kind, payload_hash, created_at)"
            " VALUES(?,?,?,?,?)",
            (event_id, pack_id, kind, payload_hash, _now()),
        )
        return None

    def settle_event(self, event_id: str, result: str) -> None:
        self.conn.execute(
            "UPDATE pack_events SET result=?, consumed_at=? WHERE event_id=?",
            (result, _now(), event_id),
        )
