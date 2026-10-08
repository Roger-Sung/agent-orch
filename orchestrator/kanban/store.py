"""The kanban SQLite schema (spec S3).

Decisions here, rather than incidentals:

* **Additive only.**  Like the pack store, this issues nothing but
  ``CREATE TABLE IF NOT EXISTS`` / ``CREATE TRIGGER IF NOT EXISTS`` against the
  same connection the rest of the engine uses.  No existing table is altered;
  in particular the ``tasks`` status CHECK is untouched, because manual card
  intent is not a second execution status (S3).
* **Two columns deliberately carry no foreign key.**  ``kanban_events.task_id``
  and ``kanban_nights.task_id`` hold the fixed task UUID chosen *before*
  ``Controller.submit`` creates the row, and S7 still requires stop evidence for
  a reservation that never produced one.  A REFERENCES clause there would turn
  the ordinary crash-between-reservation-and-submit case into an insert failure,
  which is exactly the record recovery needs.  Everything that does point at an
  already-existing row keeps its foreign key.
* **Events are append-only in the schema, not only by convention.**  S3 says an
  accepted event is never edited or deleted - corrections are new events - and
  the threat model is a silent overwrite.  CHECK constraints cannot express
  that, so two triggers refuse UPDATE and DELETE outright.
* **Revision is the CAS column, not a lock.**  Writers update
  ``WHERE ... AND revision=?``; a zero row count is the ``revision_conflict``.
  ``operation_id`` being the events primary key is the other half: a resend of
  the same command hits the unique constraint instead of acting twice.
* **``claim_seq`` is AUTOINCREMENT** so a deleted or rolled-back night can never
  hand its sequence number to a later claim; the quota watermark in
  ``kanban_quota_snapshots.covered_claim_seq`` means "every claim up to here is
  already reflected in this manual reading", which only holds if the numbers are
  never reused (S5).
* **One JSON column holds the whole per-pool claim.**  S3 fixes the table count
  at four, and S5 makes freezing the snapshot IDs, the estimated debit and the
  budget they were judged against a single act inside the reservation
  transaction.  ``pool_claims`` is therefore
  ``{pool_key: {"snapshot_id": ..., "debit_bp": ..., "budget_bp": ...}}``:
  three parallel columns keyed by pool could disagree about which pools a night
  claimed, and a debit has no life of its own to settle separately.
* **The binding is immutable once reserved.**  S3 says a night fixes the task
  and request IDs, the approval, the workspace and the exact input bytes, and
  that recovery must still find them after a restart.  Only ``phase``,
  ``stop_reason`` and ``stop_evidence_event_id`` describe what later happened,
  so a trigger refuses an UPDATE that changes anything else - the same defence
  against a silent overwrite that the append-only events triggers give.

Times are UTC epoch milliseconds and quota values are integer basis points
(10000 = 100%), per S3; no float and no token arithmetic.
"""
from __future__ import annotations

import sqlite3

# S5: a snapshot's reset must lie inside the seven-day window it describes.
WEEK_MS = 7 * 24 * 60 * 60 * 1000

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS kanban_cards(
  card_id TEXT PRIMARY KEY,
  revision INTEGER NOT NULL DEFAULT 0 CHECK(revision >= 0),
  title TEXT NOT NULL,
  note TEXT,
  priority TEXT NOT NULL CHECK(priority IN ('high','normal','low')),
  manual_state TEXT NOT NULL CHECK(manual_state IN
    ('inbox','needs_clarification','ready','returned','done','archived')),
  repo_path TEXT,
  worktree_path TEXT,
  git_common_dir TEXT,
  change_name TEXT,
  spec_path TEXT,
  spec_hash TEXT,
  acceptance_path TEXT,
  acceptance_hash TEXT,
  spec_review_pointer TEXT,
  spec_review_hash TEXT,
  base_head TEXT,
  candidate_fingerprint TEXT,
  risk TEXT,
  estimate_by_pool TEXT,
  allowed_commands TEXT,
  profile_name TEXT,
  profile_hash TEXT,
  provider TEXT,
  model TEXT,
  effort TEXT CHECK(effort IN ('low','medium','high')),
  routing_digest TEXT,
  config_digest TEXT,
  approval_generation INTEGER NOT NULL DEFAULT 0 CHECK(approval_generation >= 0),
  approval_actor TEXT,
  approval_at INTEGER,
  approval_hash TEXT,
  approval_event_id TEXT REFERENCES kanban_events(operation_id),
  task_id TEXT UNIQUE REFERENCES tasks(id),
  request_id TEXT,
  last_reason TEXT,
  last_evidence_event_id TEXT REFERENCES kanban_events(operation_id),
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  -- generation 0 is "never approved", so it cannot carry a frozen approval.
  -- A withdrawal clears the hash but keeps the generation, which is why the
  -- implication only runs one way.
  CHECK(approval_generation > 0 OR approval_hash IS NULL),
  -- S3/S6: an approval is actor + time + frozen payload + hash, or nothing.
  CHECK((approval_hash IS NULL AND approval_actor IS NULL
         AND approval_at IS NULL AND approval_event_id IS NULL)
        OR (approval_hash IS NOT NULL AND approval_actor IS NOT NULL
            AND approval_at IS NOT NULL AND approval_event_id IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS kanban_events(
  operation_id TEXT PRIMARY KEY,
  payload_hash TEXT NOT NULL,
  kind TEXT NOT NULL,
  -- Nullable: quota and generic stop-evidence commands have no card (S3).
  card_id TEXT REFERENCES kanban_cards(card_id),
  -- Intentionally no REFERENCES: see module docstring.
  task_id TEXT,
  expected_revision INTEGER,
  result_revision INTEGER,
  actor TEXT NOT NULL,
  at INTEGER NOT NULL,
  result TEXT NOT NULL CHECK(result IN ('accepted','rejected')),
  reason TEXT,
  payload TEXT,
  metadata_delta TEXT,
  -- S7: a stop-evidence event always names the task it unblocks, even when no
  -- task row was ever created; only card and night may be absent.
  CHECK(kind <> 'stop_evidence' OR task_id IS NOT NULL),
  -- S6: the approve event is where the immutable approval payload lives.
  CHECK(kind <> 'approve' OR result <> 'accepted' OR payload IS NOT NULL),
  -- A rejection that does not say why is not evidence.
  CHECK(result <> 'rejected' OR reason IS NOT NULL),
  -- S3: a rejected command records its result without moving the card.
  CHECK(result <> 'rejected' OR result_revision IS NULL)
);

CREATE TRIGGER IF NOT EXISTS kanban_events_append_only_update
BEFORE UPDATE ON kanban_events BEGIN
  SELECT RAISE(ABORT, 'kanban_events is append-only: record a new event');
END;

CREATE TRIGGER IF NOT EXISTS kanban_events_append_only_delete
BEFORE DELETE ON kanban_events BEGIN
  SELECT RAISE(ABORT, 'kanban_events is append-only: record a new event');
END;

CREATE TABLE IF NOT EXISTS kanban_quota_snapshots(
  snapshot_id TEXT PRIMARY KEY,
  pool_key TEXT NOT NULL,
  weekly_remaining_bp INTEGER NOT NULL
    CHECK(weekly_remaining_bp BETWEEN 0 AND 10000),
  observed_at INTEGER NOT NULL,
  recorded_at INTEGER NOT NULL,
  reset_at INTEGER NOT NULL,
  -- S5: there is no automatic reader; a snapshot is a human reading.
  source TEXT NOT NULL DEFAULT 'manual' CHECK(source = 'manual'),
  operator TEXT NOT NULL,
  covered_claim_seq INTEGER NOT NULL DEFAULT 0 CHECK(covered_claim_seq >= 0),
  stale INTEGER NOT NULL DEFAULT 0 CHECK(stale IN (0,1)),
  stale_event_id TEXT REFERENCES kanban_events(operation_id),
  CHECK(recorded_at >= observed_at),
  CHECK(observed_at < reset_at AND reset_at <= observed_at + {WEEK_MS}),
  -- Invalidation keeps the row and points back at the event that did it (S5).
  CHECK(stale = 1 OR stale_event_id IS NULL)
);

CREATE TABLE IF NOT EXISTS kanban_nights(
  claim_seq INTEGER PRIMARY KEY AUTOINCREMENT,
  night_id TEXT NOT NULL UNIQUE,
  window_start_ms INTEGER NOT NULL,
  window_end_ms INTEGER NOT NULL,
  card_id TEXT NOT NULL REFERENCES kanban_cards(card_id),
  approval_generation INTEGER NOT NULL CHECK(approval_generation > 0),
  approval_hash TEXT NOT NULL,
  -- Fixed before submit, so intentionally no REFERENCES (module docstring).
  task_id TEXT NOT NULL UNIQUE,
  request_id TEXT NOT NULL UNIQUE,
  workspace_dir TEXT NOT NULL,
  base_head TEXT NOT NULL,
  candidate_fingerprint TEXT NOT NULL,
  profile_hash TEXT NOT NULL,
  input_bytes BLOB NOT NULL,
  input_hash TEXT NOT NULL,
  pool_claims TEXT NOT NULL,
  reserved_at INTEGER NOT NULL,
  phase TEXT NOT NULL CHECK(phase IN ('reserved','submitted','stopped','unknown')),
  stop_reason TEXT,
  stop_evidence_event_id TEXT REFERENCES kanban_events(operation_id),
  -- S3: one night per approval generation of a card, so a re-approval cannot
  -- be claimed twice or reused on a later night.
  UNIQUE(card_id, approval_generation),
  CHECK(window_end_ms > window_start_ms),
  -- S3/S7: stop_reason is the authoritative hold field, written in the same
  -- transaction as the phase it explains.
  CHECK(phase IN ('reserved','submitted') OR stop_reason IS NOT NULL),
  CHECK(stop_evidence_event_id IS NULL OR phase IN ('stopped','unknown'))
);

CREATE TRIGGER IF NOT EXISTS kanban_nights_binding_is_immutable
BEFORE UPDATE ON kanban_nights
WHEN NEW.claim_seq IS NOT OLD.claim_seq
  OR NEW.night_id IS NOT OLD.night_id
  OR NEW.window_start_ms IS NOT OLD.window_start_ms
  OR NEW.window_end_ms IS NOT OLD.window_end_ms
  OR NEW.card_id IS NOT OLD.card_id
  OR NEW.approval_generation IS NOT OLD.approval_generation
  OR NEW.approval_hash IS NOT OLD.approval_hash
  OR NEW.task_id IS NOT OLD.task_id
  OR NEW.request_id IS NOT OLD.request_id
  OR NEW.workspace_dir IS NOT OLD.workspace_dir
  OR NEW.base_head IS NOT OLD.base_head
  OR NEW.candidate_fingerprint IS NOT OLD.candidate_fingerprint
  OR NEW.profile_hash IS NOT OLD.profile_hash
  OR NEW.input_bytes IS NOT OLD.input_bytes
  OR NEW.input_hash IS NOT OLD.input_hash
  OR NEW.pool_claims IS NOT OLD.pool_claims
  OR NEW.reserved_at IS NOT OLD.reserved_at
BEGIN
  SELECT RAISE(ABORT, 'kanban_nights binding is fixed at reservation');
END;

-- S3/S6: claims and their frozen quota debits are permanent history.  Deleting
-- one would reopen the unique night slot and remove usage from later budget
-- calculations.
CREATE TRIGGER IF NOT EXISTS kanban_nights_append_only_delete
BEFORE DELETE ON kanban_nights BEGIN
  SELECT RAISE(ABORT, 'kanban_nights is append-only: preserve the claim');
END;
"""


def create_schema_in_transaction(conn: sqlite3.Connection) -> None:
    """Create only our four tables inside the caller's preflight transaction."""
    if not conn.in_transaction:
        raise RuntimeError("kanban schema creation requires a transaction")
    statement = ""
    for character in SCHEMA:
        statement += character
        if character == ";" and sqlite3.complete_statement(statement):
            conn.execute(statement)
            statement = ""
    if statement.strip():
        raise RuntimeError("incomplete kanban schema")


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Atomic additive setup; never implicitly commit a caller's transaction."""
    if conn.in_transaction:
        raise RuntimeError("kanban schema creation must not run inside a transaction")
    conn.execute("BEGIN IMMEDIATE")
    try:
        create_schema_in_transaction(conn)
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
