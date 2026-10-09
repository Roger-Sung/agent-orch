"""P1: the additive kanban schema migration (spec S3, AC11).

Everything here runs against a throwaway database created inside a temporary
directory.  No test touches an ORCH_HOME, a real queue or a provider.
"""
from __future__ import annotations

import gc
import json
import sqlite3
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch

from orchestrator import db
from orchestrator.kanban.store import WEEK_MS, ensure_schema

KANBAN_TABLES = (
    "kanban_cards",
    "kanban_events",
    "kanban_quota_snapshots",
    "kanban_nights",
)

# Legacy rows the fixture carries across the migration.  Column lists are
# spelled out so an accidental schema change shows up as a test failure here
# rather than as silently shifted data.
TASK_ROW = (
    "t-legacy", "apply", "done", None, "review", "claude", 3,
    "pf" * 32, "ih" * 32, "/tmp/p.yaml", "/tmp/i.md", "/tmp/art",
    2, 8, 0, None, 1_700_000_000, 1_700_000_100,
)


def _objects(conn: sqlite3.Connection, *, kanban: bool) -> dict[str, str]:
    """sqlite_master entries, split into the pre-existing and the new ones.

    Auto-indexes are named after their table (``sqlite_autoindex_kanban_...``)
    and ``sqlite_sequence`` only exists because of the AUTOINCREMENT on
    ``kanban_nights``, so both count as new.
    """
    out = {}
    for row in conn.execute("SELECT name, sql FROM sqlite_master ORDER BY name"):
        name = row["name"]
        is_kanban = "kanban_" in name or "cli_quota_observation" in name or name == "sqlite_sequence"
        if is_kanban is kanban:
            out[name] = row["sql"]
    return out


def _dump(conn: sqlite3.Connection) -> dict[str, list[tuple]]:
    names = [
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
            " AND name NOT LIKE 'kanban_%' AND name NOT LIKE 'sqlite_%' AND name <> 'cli_quota_observation'"
            " ORDER BY name"
        )
    ]
    return {
        name: [tuple(r) for r in conn.execute(f"SELECT * FROM {name}")]
        for name in names
    }


class KanbanSchemaTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.path = self.tmp / "orchestrator.db"

    def open(self) -> sqlite3.Connection:
        conn = db.connect(self.path)
        self.addCleanup(conn.close)
        return conn

    def legacy_db(self) -> None:
        """A database written by the engine as it was before this slice."""
        with patch("orchestrator.kanban.store.SCHEMA", ""):
            conn = db.connect(self.path)
        try:
            self.assertEqual(_objects(conn, kanban=True), {})
            conn.execute(
                "INSERT INTO tasks(id,type,status,stop_reason,current_stage,owner,"
                "revision,profile_hash,input_hash,profile_snapshot_path,"
                "input_snapshot_path,artifact_dir,transitions_count,max_transitions,"
                "resume_allowance,lease_token,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                TASK_ROW,
            )
            conn.execute(
                "INSERT INTO stage_runs(run_token,task_id,stage,cycle,attempt,owner,"
                "status,log_path,started_at) VALUES(?,?,?,?,?,?,?,?,?)",
                ("run-1", "t-legacy", "apply", 1, 1, "claude", "committed",
                 "/tmp/run.log", 1_700_000_010),
            )
            conn.execute(
                "INSERT INTO transitions(task_id,seq,operation_id,run_token,stage,"
                "owner,edge,outcome,from_status,to_status,reason,at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                ("t-legacy", 1, "op-legacy", "run-1", "apply", "claude", "applied",
                 "applied", "queued", "running", None, 1_700_000_020),
            )
            # Removed pack engine is not an integration dependency. Preserve
            # opaque pre-existing pack-shaped sentinels without importing it.
            conn.execute("CREATE TABLE pack_packs(pack_id TEXT PRIMARY KEY,target_id TEXT,change TEXT,state TEXT,updated_at INTEGER)")
            conn.execute("CREATE TABLE pack_records(record_id TEXT PRIMARY KEY,kind TEXT,pack_id TEXT,payload TEXT,created_at INTEGER)")
            conn.execute(
                "INSERT INTO pack_packs(pack_id,target_id,change,state,updated_at)"
                " VALUES(?,?,?,?,?)",
                ("pk-1", "tgt", "chg", "blocked_deps", 1_700_000_030),
            )
            conn.execute(
                "INSERT INTO pack_records(record_id,kind,pack_id,payload,created_at)"
                " VALUES(?,?,?,?,?)",
                ("rec-1", "stop_evidence", "pk-1", "{}", 1_700_000_040),
            )
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # helpers for the constraint probes
    # ------------------------------------------------------------------
    def card(self, conn: sqlite3.Connection, card_id: str = "c1", **over) -> None:
        values = {
            "card_id": card_id,
            "revision": 0,
            "title": "t",
            "priority": "normal",
            "manual_state": "inbox",
            "created_at": 1,
            "updated_at": 1,
        }
        values.update(over)
        cols = ",".join(values)
        marks = ",".join("?" * len(values))
        conn.execute(
            f"INSERT INTO kanban_cards({cols}) VALUES({marks})", tuple(values.values())
        )

    def event(self, conn: sqlite3.Connection, operation_id: str = "op1", **over) -> None:
        values = {
            "operation_id": operation_id,
            "payload_hash": "h",
            "kind": "create",
            "actor": "operator",
            "at": 1,
            "result": "accepted",
        }
        values.update(over)
        cols = ",".join(values)
        marks = ",".join("?" * len(values))
        conn.execute(
            f"INSERT INTO kanban_events({cols}) VALUES({marks})", tuple(values.values())
        )

    def night(self, conn: sqlite3.Connection, **over) -> None:
        values = {
            "night_id": "2026-09-22",
            "window_start_ms": 1_000,
            "window_end_ms": 2_000,
            "card_id": "c1",
            "approval_generation": 1,
            "approval_hash": "ah",
            "task_id": "night-task",
            "request_id": "night-req",
            "workspace_dir": "/tmp/wt",
            "base_head": "abc",
            "candidate_fingerprint": "cf",
            "profile_hash": "ph",
            "input_bytes": b"{}",
            "input_hash": "ih",
            "pool_claims": '{"claude/personal":{"snapshot_id":"s1",'
                             '"debit_bp":800,"budget_bp":800}}',
            "reserved_at": 1,
            "phase": "reserved",
        }
        values.update(over)
        cols = ",".join(values)
        marks = ",".join("?" * len(values))
        conn.execute(
            f"INSERT INTO kanban_nights({cols}) VALUES({marks})", tuple(values.values())
        )

    def snapshot(self, conn: sqlite3.Connection, snapshot_id: str = "s1", **over) -> None:
        values = {
            "snapshot_id": snapshot_id,
            "pool_key": "claude/personal",
            "weekly_remaining_bp": 10000,
            "observed_at": 1_000,
            "recorded_at": 1_100,
            "reset_at": 1_000 + WEEK_MS,
            "operator": "roger",
            "covered_claim_seq": 0,
        }
        values.update(over)
        cols = ",".join(values)
        marks = ",".join("?" * len(values))
        conn.execute(
            f"INSERT INTO kanban_quota_snapshots({cols}) VALUES({marks})",
            tuple(values.values()),
        )


class MigrationTest(KanbanSchemaTestCase):
    def test_a_fresh_database_gets_the_four_tables(self) -> None:
        conn = self.open()
        names = set(_objects(conn, kanban=True))
        self.assertTrue(set(KANBAN_TABLES) <= names)

    # AC11: the migration is additive, so an old database keeps every row and
    # every pre-existing object definition byte for byte.
    def test_an_old_database_is_unchanged_by_the_migration(self) -> None:
        self.legacy_db()
        before_conn = sqlite3.connect(self.path)
        before_conn.row_factory = sqlite3.Row
        before_objects = _objects(before_conn, kanban=False)
        before_rows = _dump(before_conn)
        before_conn.close()

        conn = self.open()
        self.assertEqual(_objects(conn, kanban=False), before_objects)
        self.assertEqual(_dump(conn), before_rows)
        self.assertTrue(set(KANBAN_TABLES) <= set(_objects(conn, kanban=True)))
        # The rows are the point, not just the counts.
        self.assertEqual(
            conn.execute("SELECT count(*) FROM tasks").fetchone()[0], 1
        )
        self.assertEqual(
            conn.execute("SELECT status FROM tasks WHERE id='t-legacy'").fetchone()[0],
            "done",
        )

    # S3: the task status CHECK is explicitly out of scope; an old task is not
    # re-labelled and no card status is grafted onto it.
    def test_the_task_status_check_is_not_touched(self) -> None:
        self.legacy_db()
        conn = self.open()
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='tasks'"
        ).fetchone()[0]
        self.assertIn(
            "status TEXT NOT NULL CHECK(status IN "
            "('queued','running','waiting_user','blocked','done','failed','paused'))",
            sql,
        )
        self.assertNotIn("kanban", sql)

    def test_rerunning_the_migration_changes_nothing(self) -> None:
        self.legacy_db()
        first = self.open()
        self.card(first)
        objects = dict(_objects(first, kanban=True), **_objects(first, kanban=False))
        rows = _dump(first)
        first.close()

        second = self.open()
        self.assertEqual(
            dict(_objects(second, kanban=True), **_objects(second, kanban=False)),
            objects,
        )
        self.assertEqual(_dump(second), rows)
        self.assertEqual(
            second.execute("SELECT count(*) FROM kanban_cards").fetchone()[0], 1
        )

    # A failure partway through the schema script must leave no half-shaped
    # table behind; IF NOT EXISTS could never repair that shape on a later open.
    def test_a_failed_migration_rolls_back_then_reopens_and_completes(self) -> None:
        self.legacy_db()
        partial = "CREATE TABLE IF NOT EXISTS kanban_cards(card_id TEXT PRIMARY KEY);"
        with patch("orchestrator.kanban.store.SCHEMA", partial + " NOT SQL AT ALL;"):
            # `db.connect` drops its half-open handle when the script raises;
            # collecting it here keeps that unrelated ResourceWarning out of
            # the suite instead of leaving it to fire in a later test.
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", ResourceWarning)
                try:
                    db.connect(self.path)
                except sqlite3.OperationalError:
                    pass
                else:  # pragma: no cover - the script is invalid by construction
                    self.fail("the broken schema script should have raised")
                gc.collect()

        broken = sqlite3.connect(self.path)
        broken.row_factory = sqlite3.Row
        tables = {n for n in _objects(broken, kanban=True) if n.startswith("kanban_")}
        self.assertEqual(tables, set())
        broken.close()

        conn = self.open()
        self.assertTrue(set(KANBAN_TABLES) <= set(_objects(conn, kanban=True)))
        card_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(kanban_cards)")
        }
        self.assertTrue(
            {
                "card_id", "revision", "title", "manual_state",
                "allowed_commands", "effort", "routing_digest", "config_digest",
            }
            <= card_columns
        )
        self.assertEqual(
            conn.execute("SELECT count(*) FROM tasks").fetchone()[0], 1
        )
        self.assertEqual(
            conn.execute("SELECT count(*) FROM pack_packs").fetchone()[0], 1
        )

    def test_ensure_schema_refuses_to_run_inside_a_transaction(self) -> None:
        conn = self.open()
        conn.execute("BEGIN IMMEDIATE")
        try:
            with self.assertRaises(RuntimeError):
                ensure_schema(conn)
        finally:
            conn.execute("ROLLBACK")


class ReadOnlyOpenTest(KanbanSchemaTestCase):
    def test_a_missing_database_is_not_created(self) -> None:
        with self.assertRaises(FileNotFoundError):
            db.connect(self.path, read_only=True)
        self.assertFalse(self.path.exists())

    def test_a_read_only_open_runs_no_migration(self) -> None:
        self.legacy_db()
        conn = db.connect(self.path, read_only=True)
        self.addCleanup(conn.close)
        self.assertEqual(_objects(conn, kanban=True), {})
        with self.assertRaises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE kanban_cards(card_id TEXT)")
        conn.close()

        after = sqlite3.connect(self.path)
        after.row_factory = sqlite3.Row
        self.assertEqual(_objects(after, kanban=True), {})
        after.close()


class ConstraintTest(KanbanSchemaTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.conn = self.open()

    # ---------------- cards ----------------

    def test_priority_and_manual_state_are_closed_enums(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            self.card(self.conn, "c-bad", priority="expedite")
        with self.assertRaises(sqlite3.IntegrityError):
            self.card(self.conn, "c-bad2", manual_state="running")
        with self.assertRaises(sqlite3.IntegrityError):
            self.card(self.conn, "c-bad3", effort="ultra")

    def test_an_approval_hash_needs_a_generation_actor_time_and_event(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            self.card(self.conn, "c-a", approval_hash="h")  # generation 0
        self.event(self.conn, "op-approve", kind="approve", payload="{}")
        with self.assertRaises(sqlite3.IntegrityError):
            self.card(self.conn, "c-b", approval_generation=1, approval_hash="h")
        for card_id, partial in (
            ("c-actor-only", {"approval_actor": "roger"}),
            ("c-time-only", {"approval_at": 5}),
            ("c-event-only", {"approval_event_id": "op-approve"}),
            ("c-metadata-no-hash", {
                "approval_actor": "roger",
                "approval_at": 5,
                "approval_event_id": "op-approve",
            }),
        ):
            with self.subTest(card_id=card_id):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.card(
                        self.conn,
                        card_id,
                        approval_generation=1,
                        **partial,
                    )
        self.card(
            self.conn, "c-ok", approval_generation=1, approval_hash="h",
            approval_actor="roger", approval_at=5, approval_event_id="op-approve",
        )
        # A withdrawal clears the hash and keeps the generation.
        self.conn.execute(
            "UPDATE kanban_cards SET approval_hash=NULL, approval_event_id=NULL,"
            " approval_actor=NULL, approval_at=NULL WHERE card_id='c-ok'"
        )

    # Threat model: a concurrent writer must not silently overwrite.  The
    # revision column is the CAS; a stale expectation updates no row.
    def test_revision_cas_rejects_a_stale_writer(self) -> None:
        self.card(self.conn, "c1", revision=4)
        stale = self.conn.execute(
            "UPDATE kanban_cards SET title='x', revision=revision+1"
            " WHERE card_id='c1' AND revision=?", (3,)
        )
        self.assertEqual(stale.rowcount, 0)
        fresh = self.conn.execute(
            "UPDATE kanban_cards SET title='x', revision=revision+1"
            " WHERE card_id='c1' AND revision=?", (4,)
        )
        self.assertEqual(fresh.rowcount, 1)
        self.assertEqual(
            self.conn.execute(
                "SELECT revision FROM kanban_cards WHERE card_id='c1'"
            ).fetchone()[0],
            5,
        )

    def test_a_card_binds_at_most_one_task(self) -> None:
        self.conn.execute(
            "INSERT INTO tasks(id,type,status,stop_reason,current_stage,owner,"
            "revision,profile_hash,input_hash,profile_snapshot_path,"
            "input_snapshot_path,artifact_dir,transitions_count,max_transitions,"
            "resume_allowance,lease_token,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            TASK_ROW,
        )
        self.card(self.conn, "c1", task_id="t-legacy")
        with self.assertRaises(sqlite3.IntegrityError):
            self.card(self.conn, "c2", task_id="t-legacy")
        # And a card cannot point at a task that does not exist.
        with self.assertRaises(sqlite3.IntegrityError):
            self.card(self.conn, "c3", task_id="t-missing")

    # ---------------- events ----------------

    # Threat model: the same operation ID resent must not act twice.
    def test_an_operation_id_can_only_be_recorded_once(self) -> None:
        self.event(self.conn, "op1")
        with self.assertRaises(sqlite3.IntegrityError):
            self.event(self.conn, "op1", payload_hash="different")

    def test_events_are_append_only(self) -> None:
        self.event(self.conn, "op1")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE kanban_events SET result='rejected'")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM kanban_events WHERE operation_id='op1'")
        self.assertEqual(
            self.conn.execute(
                "SELECT result FROM kanban_events WHERE operation_id='op1'"
            ).fetchone()[0],
            "accepted",
        )

    def test_a_generic_orphan_event_needs_no_card_and_no_task_row(self) -> None:
        self.event(
            self.conn, "op-stop", kind="stop_evidence", task_id="t-never-created",
            expected_revision=2, result_revision=3, payload="{}",
        )
        row = self.conn.execute(
            "SELECT card_id, task_id FROM kanban_events WHERE operation_id='op-stop'"
        ).fetchone()
        self.assertIsNone(row["card_id"])
        self.assertEqual(row["task_id"], "t-never-created")

    def test_stop_evidence_always_names_a_task(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            self.event(self.conn, "op-stop", kind="stop_evidence")

    def test_an_accepted_approval_carries_its_frozen_payload(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            self.event(self.conn, "op-a", kind="approve")
        # A refused approval records the refusal, not an approval payload.
        self.event(
            self.conn, "op-a", kind="approve", result="rejected",
            reason="revision_conflict",
        )
        self.event(self.conn, "op-b", kind="approve", payload='{"schema":1}')
        self.assertEqual(
            self.conn.execute(
                "SELECT payload FROM kanban_events WHERE operation_id='op-b'"
            ).fetchone()[0],
            '{"schema":1}',
        )

    def test_a_refusal_states_a_reason_and_moves_no_revision(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            self.event(self.conn, "op-r", result="rejected")
        with self.assertRaises(sqlite3.IntegrityError):
            self.event(
                self.conn, "op-r2", result="rejected", reason="idempotency_conflict",
                result_revision=3,
            )

    def test_an_event_cannot_name_an_unknown_card(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            self.event(self.conn, "op-x", card_id="c-missing")

    # ---------------- quota snapshots ----------------

    def test_a_snapshot_is_a_manual_reading_in_basis_points(self) -> None:
        self.snapshot(self.conn, "s1")
        with self.assertRaises(sqlite3.IntegrityError):
            self.snapshot(self.conn, "s2", source="api")
        with self.assertRaises(sqlite3.IntegrityError):
            self.snapshot(self.conn, "s3", weekly_remaining_bp=10001)
        with self.assertRaises(sqlite3.IntegrityError):
            self.snapshot(self.conn, "s4", weekly_remaining_bp=-1)

    def test_the_reset_must_lie_inside_the_week_it_describes(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            self.snapshot(self.conn, "s5", observed_at=1_000, reset_at=1_000)
        with self.assertRaises(sqlite3.IntegrityError):
            self.snapshot(
                self.conn, "s6", observed_at=1_000, reset_at=1_000 + WEEK_MS + 1
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.snapshot(self.conn, "s7", observed_at=1_000, recorded_at=999)

    def test_invalidation_keeps_the_row_and_points_at_its_event(self) -> None:
        self.snapshot(self.conn, "s1")
        self.event(self.conn, "op-stale", kind="quota_invalidate")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "UPDATE kanban_quota_snapshots SET stale_event_id='op-stale'"
                " WHERE snapshot_id='s1'"
            )
        self.conn.execute(
            "UPDATE kanban_quota_snapshots SET stale=1, stale_event_id='op-stale'"
            " WHERE snapshot_id='s1'"
        )
        row = self.conn.execute(
            "SELECT weekly_remaining_bp, stale FROM kanban_quota_snapshots"
        ).fetchone()
        self.assertEqual((row["weekly_remaining_bp"], row["stale"]), (10000, 1))

    # ---------------- nights ----------------

    def test_one_claim_per_night_and_per_approval_generation(self) -> None:
        self.card(self.conn, "c1")
        self.card(self.conn, "c2")
        self.night(self.conn)
        # Threat model: the same approval generation claimed on a later night.
        with self.assertRaises(sqlite3.IntegrityError):
            self.night(
                self.conn, night_id="2026-09-23", task_id="t2", request_id="r2",
            )
        # And two cards competing for the same night.
        with self.assertRaises(sqlite3.IntegrityError):
            self.night(
                self.conn, card_id="c2", task_id="t3", request_id="r3",
            )
        # A new approval generation of the same card gets its own night.
        self.night(
            self.conn, night_id="2026-09-23", approval_generation=2,
            task_id="t4", request_id="r4",
        )

    def test_the_fixed_task_and_request_ids_are_unique(self) -> None:
        self.card(self.conn, "c1")
        self.night(self.conn)
        with self.assertRaises(sqlite3.IntegrityError):
            self.night(
                self.conn, night_id="n2", approval_generation=2,
                task_id="night-task", request_id="other",
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.night(
                self.conn, night_id="n3", approval_generation=3,
                task_id="other", request_id="night-req",
            )

    # S7: the reservation exists before the task row, so the fixed task id is
    # deliberately not a foreign key.
    def test_a_reservation_survives_having_no_task_row(self) -> None:
        self.card(self.conn, "c1")
        self.night(self.conn, task_id="t-not-yet-submitted")
        self.assertEqual(
            self.conn.execute(
                "SELECT task_id FROM kanban_nights"
            ).fetchone()[0],
            "t-not-yet-submitted",
        )

    def test_a_stopped_night_must_say_why(self) -> None:
        self.card(self.conn, "c1")
        self.night(self.conn)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE kanban_nights SET phase='unknown'")
        self.conn.execute(
            "UPDATE kanban_nights SET phase='unknown',"
            " stop_reason='writer_stop_unknown'"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE kanban_nights SET phase='settled'")

    def test_stop_evidence_points_at_a_recorded_event(self) -> None:
        self.card(self.conn, "c1")
        self.night(self.conn)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "UPDATE kanban_nights SET phase='stopped',"
                " stop_reason='restart_review_required',"
                " stop_evidence_event_id='op-missing'"
            )
        self.event(
            self.conn, "op-stop", kind="stop_evidence", task_id="night-task",
            card_id="c1",
        )
        self.conn.execute(
            "UPDATE kanban_nights SET phase='stopped',"
            " stop_reason='restart_review_required',"
            " stop_evidence_event_id='op-stop'"
        )
        # A stop-evidence pointer belongs only to a stopped or unknown night.
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "UPDATE kanban_nights SET phase='submitted', stop_reason=NULL"
            )

    def test_claim_seq_is_monotonic_and_nights_cannot_be_deleted(self) -> None:
        self.card(self.conn, "c1")
        self.card(self.conn, "c2")
        self.night(self.conn)
        first = self.conn.execute("SELECT claim_seq FROM kanban_nights").fetchone()[0]
        self.night(
            self.conn, card_id="c2", night_id="2026-09-23",
            task_id="t2", request_id="r2",
        )
        second = self.conn.execute(
            "SELECT MAX(claim_seq) FROM kanban_nights"
        ).fetchone()[0]
        self.assertGreater(second, first)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "DELETE FROM kanban_nights WHERE claim_seq=?", (first,)
            )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM kanban_nights").fetchone()[0],
            2,
        )

    # S5: the reservation freezes which snapshot each pool debit was taken
    # against and the budget it was judged against, in one act.
    def test_a_night_freezes_the_snapshot_debit_and_budget_per_pool(self) -> None:
        self.card(self.conn, "c1")
        self.night(self.conn)
        claims = json.loads(
            self.conn.execute("SELECT pool_claims FROM kanban_nights").fetchone()[0]
        )
        self.assertEqual(
            claims,
            {"claude/personal": {
                "snapshot_id": "s1", "debit_bp": 800, "budget_bp": 800,
            }},
        )

    # Threat model: a silent overwrite of what the night is bound to.  Only the
    # phase and its stop evidence may change after the reservation.
    def test_the_night_binding_cannot_be_rewritten(self) -> None:
        self.card(self.conn, "c1")
        self.card(self.conn, "c2")
        self.night(self.conn)
        for column, value in (
            ("task_id", "'t-other'"),
            ("request_id", "'r-other'"),
            ("card_id", "'c2'"),
            ("approval_generation", "2"),
            ("approval_hash", "'other'"),
            ("night_id", "'2026-09-23'"),
            ("window_start_ms", "5"),
            ("window_end_ms", "9000"),
            ("workspace_dir", "'/tmp/other'"),
            ("base_head", "'def'"),
            ("candidate_fingerprint", "'cf2'"),
            ("profile_hash", "'ph2'"),
            ("input_bytes", "x'00'"),
            ("input_hash", "'other'"),
            ("pool_claims", "'{}'"),
            ("reserved_at", "99"),
            ("claim_seq", "claim_seq + 10"),
        ):
            with self.subTest(column=column):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.conn.execute(
                        f"UPDATE kanban_nights SET {column}={value}"
                    )
        # The phase and its explanation are exactly what may still move.
        self.conn.execute(
            "UPDATE kanban_nights SET phase='submitted' WHERE night_id='2026-09-22'"
        )
        self.assertEqual(
            self.conn.execute("SELECT phase FROM kanban_nights").fetchone()[0],
            "submitted",
        )

    def test_a_night_stores_the_exact_input_bytes(self) -> None:
        self.card(self.conn, "c1")
        self.night(self.conn, input_bytes=b'{"a":1}', input_hash="deadbeef")
        row = self.conn.execute(
            "SELECT input_bytes, input_hash FROM kanban_nights"
        ).fetchone()
        self.assertEqual(row["input_bytes"], b'{"a":1}')
        self.assertIsInstance(row["input_bytes"], bytes)
        self.assertEqual(row["input_hash"], "deadbeef")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
