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
"""


def connect(path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    if read_only:
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
