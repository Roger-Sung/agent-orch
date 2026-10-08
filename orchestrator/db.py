from __future__ import annotations

import sqlite3
from pathlib import Path


DDL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS tasks(
  id TEXT PRIMARY KEY,
  type TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('queued','running','waiting_user','blocked','done','failed','paused')),
  stop_reason TEXT,
  current_stage TEXT NOT NULL,
  owner TEXT CHECK(owner IN ('claude','codex') OR owner IS NULL),
  revision INTEGER NOT NULL DEFAULT 0,
  profile_hash TEXT NOT NULL,
  input_hash TEXT NOT NULL,
  profile_snapshot_path TEXT NOT NULL,
  input_snapshot_path TEXT NOT NULL,
  artifact_dir TEXT NOT NULL,
  transitions_count INTEGER NOT NULL DEFAULT 0 CHECK(transitions_count >= 0),
  max_transitions INTEGER NOT NULL CHECK(max_transitions > 0),
  resume_allowance INTEGER NOT NULL DEFAULT 0 CHECK(resume_allowance IN (0,1)),
  lease_token TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS stage_runs(
  run_token TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(id),
  stage TEXT NOT NULL,
  cycle INTEGER NOT NULL CHECK(cycle > 0),
  attempt INTEGER NOT NULL CHECK(attempt > 0),
  owner TEXT NOT NULL CHECK(owner IN ('claude','codex')),
  status TEXT NOT NULL CHECK(status IN ('running','committed','paused','blocked')),
  lease_token TEXT,
  exit_code INTEGER,
  outcome TEXT,
  log_path TEXT NOT NULL,
  manifest_path TEXT,
  manifest_hash TEXT,
  sealed INTEGER NOT NULL DEFAULT 0 CHECK(sealed IN (0,1)),
  model TEXT,
  duration_ms INTEGER,
  usage_input_tokens INTEGER,
  usage_output_tokens INTEGER,
  usage_total_tokens INTEGER,
  usage_unavailable_reason TEXT,
  provider_preflight_status TEXT,
  provider_preflight_reason TEXT,
  started_at INTEGER NOT NULL,
  ended_at INTEGER,
  UNIQUE(task_id, stage, cycle, attempt)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_active_run ON stage_runs(task_id) WHERE status='running';

CREATE TABLE IF NOT EXISTS transitions(
  task_id TEXT NOT NULL REFERENCES tasks(id),
  seq INTEGER NOT NULL,
  operation_id TEXT NOT NULL UNIQUE,
  run_token TEXT REFERENCES stage_runs(run_token),
  stage TEXT,
  owner TEXT,
  edge TEXT,
  outcome TEXT,
  from_status TEXT,
  to_status TEXT NOT NULL,
  reason TEXT,
  at INTEGER NOT NULL,
  PRIMARY KEY(task_id, seq)
);

CREATE TABLE IF NOT EXISTS edge_counts(
  task_id TEXT NOT NULL REFERENCES tasks(id),
  edge TEXT NOT NULL,
  count INTEGER NOT NULL DEFAULT 0 CHECK(count >= 0),
  cap INTEGER NOT NULL CHECK(cap > 0),
  PRIMARY KEY(task_id, edge)
);

CREATE TABLE IF NOT EXISTS notifications(
  task_id TEXT NOT NULL REFERENCES tasks(id),
  transition_seq INTEGER NOT NULL,
  reason TEXT NOT NULL,
  message TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  PRIMARY KEY(task_id, transition_seq),
  FOREIGN KEY(task_id, transition_seq) REFERENCES transitions(task_id, seq)
);

CREATE TABLE IF NOT EXISTS quarantine(
  id TEXT PRIMARY KEY,
  task_id TEXT REFERENCES tasks(id),
  request_path TEXT,
  artifact_path TEXT,
  reason TEXT NOT NULL,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS trajectory_events(
  trajectory_id TEXT NOT NULL,
  seq INTEGER NOT NULL CHECK(seq > 0),
  event_id TEXT NOT NULL UNIQUE,
  schema_version INTEGER NOT NULL CHECK(schema_version = 1),
  event_type TEXT NOT NULL,
  event_version INTEGER NOT NULL CHECK(event_version > 0),
  task_id TEXT NOT NULL REFERENCES tasks(id),
  run_token TEXT REFERENCES stage_runs(run_token),
  recorded_at_ms INTEGER NOT NULL CHECK(recorded_at_ms >= 0),
  canonical_json BLOB NOT NULL CHECK(typeof(canonical_json) = 'blob'),
  prev_event_hash TEXT,
  event_hash TEXT NOT NULL,
  PRIMARY KEY(trajectory_id, seq)
);

-- The primary key already answers "this trajectory, in order" and the UNIQUE
-- event_id answers "this one event". These two cover the remaining lookups the
-- writer and the parity check make on every canonical mutation: the post-commit
-- parity read is keyed on (task_id, event_id), and run-bound events are read
-- back by run_token.
CREATE INDEX IF NOT EXISTS ix_trajectory_events_task ON trajectory_events(task_id, event_id);
CREATE INDEX IF NOT EXISTS ix_trajectory_events_run ON trajectory_events(run_token)
  WHERE run_token IS NOT NULL;

CREATE TRIGGER IF NOT EXISTS trajectory_events_no_update
BEFORE UPDATE ON trajectory_events
BEGIN
  SELECT RAISE(ABORT, 'trajectory_events is append-only');
END;

CREATE TRIGGER IF NOT EXISTS trajectory_events_no_delete
BEFORE DELETE ON trajectory_events
BEGIN
  SELECT RAISE(ABORT, 'trajectory_events is append-only');
END;
"""


def connect(path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    if read_only:
        if not path.is_file():
            # A read-only open of a missing database is sqlite's least helpful
            # error; the actual answer is almost always a wrong ORCH_HOME.
            raise FileNotFoundError(
                f"no orchestrator state at {path} — nothing has run against this ORCH_HOME, "
                "or the CLI and the daemon resolve different homes"
            )
        uri = path.resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, timeout=30, isolation_level=None, uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        return conn
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.executescript(DDL)
    _migrate(conn)
    from .kanban.store import ensure_schema
    ensure_schema(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    task_columns = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
    for name, definition in {"lease_token": "TEXT", "workspace_dir": "TEXT"}.items():
        if name not in task_columns:
            conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {definition}")

    existing = {row["name"] for row in conn.execute("PRAGMA table_info(stage_runs)")}
    columns = {
        "lease_token": "TEXT",
        "manifest_path": "TEXT",
        "manifest_hash": "TEXT",
        "sealed": "INTEGER NOT NULL DEFAULT 0",
        "model": "TEXT",
        "duration_ms": "INTEGER",
        "usage_input_tokens": "INTEGER",
        "usage_output_tokens": "INTEGER",
        "usage_total_tokens": "INTEGER",
        "usage_unavailable_reason": "TEXT",
        "provider_preflight_status": "TEXT",
        "provider_preflight_reason": "TEXT",
    }
    for name, definition in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE stage_runs ADD COLUMN {name} {definition}")


def connect_progress(path: Path) -> sqlite3.Connection:
    """Existing DB only: one preflight + four-table transaction, no legacy init."""
    from .kanban.store import ensure_schema, create_schema_in_transaction
    import re
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=rw", timeout=5, isolation_level=None, uri=True)
    conn.row_factory = sqlite3.Row
    reference = sqlite3.connect(":memory:", isolation_level=None)
    reference.row_factory = sqlite3.Row
    try:
        # Reference schema lives only in memory. Never run DDL/_migrate on
        # legacy state in the target connection.
        reference.executescript(DDL)
        ensure_schema(reference)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        for table in ("tasks", "stage_runs", "transitions", "edge_counts", "notifications", "quarantine", "trajectory_events"):
            expected = {r["name"]: tuple(r)[2:] for r in reference.execute("PRAGMA table_info(" + table + ")")}
            actual = {r["name"]: tuple(r)[2:] for r in conn.execute("PRAGMA table_info(" + table + ")")}
            if not expected or any(actual.get(k) != v for k, v in expected.items()):
                raise ValueError("incompatible legacy schema: " + table)
        def normalize(sql):
            return re.sub(r"\s+", " ", sql.lower()).strip().replace(" if not exists", "")
        required = reference.execute("SELECT name,sql FROM sqlite_master WHERE type IN ('index','trigger') AND sql IS NOT NULL AND name NOT LIKE 'kanban_%'").fetchall()
        for obj in required:
            actual = conn.execute("SELECT sql FROM sqlite_master WHERE name=?", (obj["name"],)).fetchone()
            if actual is None or normalize(actual["sql"]) != normalize(obj["sql"]):
                raise ValueError("incompatible legacy schema object: " + obj["name"])
        expected = {r["name"]: (r["type"], normalize(r["sql"])) for r in reference.execute("SELECT name,type,sql FROM sqlite_master WHERE name LIKE 'kanban_%' AND sql IS NOT NULL")}
        actual = {r["name"]: (r["type"], normalize(r["sql"])) for r in conn.execute("SELECT name,type,sql FROM sqlite_master WHERE (name LIKE 'kanban_%' OR tbl_name LIKE 'kanban_%') AND sql IS NOT NULL")}
        if actual and actual != expected:
            raise ValueError("incompatible kanban schema")
        if conn.execute("SELECT count(*) FROM tasks WHERE status IS NULL OR status NOT IN ('waiting_user','blocked','done','failed','paused')").fetchone()[0]:
            raise ValueError("active or unknown task state")
        if conn.execute("SELECT count(*) FROM stage_runs WHERE status IS NULL OR status NOT IN ('committed','paused','blocked')").fetchone()[0]:
            raise ValueError("active or unknown stage state")
        if actual and conn.execute("SELECT count(*) FROM kanban_nights WHERE phase IS NULL OR phase <> 'stopped'").fetchone()[0]:
            raise ValueError("active or unknown night state")
        create_schema_in_transaction(conn)
        conn.execute("COMMIT")
        return conn
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        conn.close()
        raise
    finally:
        reference.close()
