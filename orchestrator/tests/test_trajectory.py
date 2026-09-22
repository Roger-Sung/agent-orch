from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path

from orchestrator.db import DDL, _migrate, connect
from orchestrator.pack.store import SCHEMA as PACK_SCHEMA
from orchestrator.trajectory import TrajectoryError, TrajectoryStore, canonical_event_bytes, seal_event


SHA = "sha256:" + "a" * 64


class TrajectoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state.db"
        self.conn = connect(self.path)
        self.addCleanup(self.conn.close)
        self.task_id = "task-a"
        self.conn.execute(
            """INSERT INTO tasks(
                   id,type,status,current_stage,owner,revision,profile_hash,input_hash,
                   profile_snapshot_path,input_snapshot_path,artifact_dir,max_transitions,
                   created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (self.task_id, "propose", "queued", "draft", "codex", 0, "p" * 64, "i" * 64,
             "/tmp/profile", "/tmp/input", "/tmp/artifact", 10, 1, 1),
        )

    def event(self, *, seq: int = 1, previous: str | None = None, event_type: str = "task.created") -> dict:
        body = {"profile_digest": SHA, "input_digest": "sha256:" + "b" * 64}
        return {
            "schema_version": 1,
            "trajectory_id": f"task:{self.task_id}",
            "seq": seq,
            "event_id": str(uuid.uuid4()),
            "event_type": event_type,
            "event_version": 1,
            "recorded_at_ms": 1000 + seq,
            "task": {"task_id": self.task_id, "revision": 0},
            "run": None,
            "invocation_id": None,
            "workflow_attempt_ref": None,
            "session_ref": None,
            "parent_event_id": None,
            "causal_event_ids": [],
            "actor": {"kind": "controller", "id": "native-orchestrator", "provider": None, "model": None},
            "body": body,
            "evidence_refs": [],
            "sensitivity": "internal",
            "retention_class": "structural",
            "normalizer_version": "trajectory-v1",
            "prev_event_hash": previous,
        }

    def append(self, event: dict) -> dict:
        sealed = seal_event(event)
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            TrajectoryStore(self.conn).append(sealed)
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        return sealed

    def add_run(self) -> str:
        run_token = str(uuid.uuid4())
        self.conn.execute(
            """INSERT INTO stage_runs(
                   run_token,task_id,stage,cycle,attempt,owner,status,lease_token,log_path,started_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (run_token, self.task_id, "draft", 1, 1, "codex", "running", "lease", "/tmp/log", 1),
        )
        return run_token

    def provider_event(
        self,
        event_type: str,
        run_token: str,
        invocation_id: str,
        *,
        seq: int = 1,
        previous: str | None = None,
    ) -> dict:
        event = self.event(seq=seq, previous=previous, event_type=event_type)
        event["run"] = {"run_token": run_token, "stage": "draft", "cycle": 1, "attempt": 1}
        event["invocation_id"] = invocation_id
        event["session_ref"] = "session-a"
        event["actor"] = {"kind": "controller", "id": "native-orchestrator", "provider": "codex", "model": "gpt-5.6-sol"}
        if event_type == "provider.dispatch_intent":
            event["body"] = {"policy_digest": SHA, "capability_digest": "sha256:" + "b" * 64}
        elif event_type == "provider.dispatched":
            ref_id = "evref:" + str(uuid.uuid4())
            event["body"] = {"transport_receipt_ref": ref_id}
            event["evidence_refs"] = [self.evidence_ref(ref_id)]
        elif event_type == "provider.settled":
            event["body"] = {
                "result_class": "success",
                "elapsed_ms": 10,
                "usage": {
                    "basis": "unavailable",
                    "input_tokens": None,
                    "output_tokens": None,
                    "total_tokens": None,
                    "unavailable_reason_code": "provider_cli_usage_not_reported",
                    "unavailable_reason_digest": None,
                },
                "final_response_ref": None,
            }
        else:
            raise AssertionError(event_type)
        return event

    @staticmethod
    def evidence_ref(ref_id: str) -> dict:
        return {
            "ref_id": ref_id,
            "kind": "transport-receipt",
            "relative_path": "runs/receipt.json",
            "sha256": "c" * 64,
            "size_bytes": 2,
            "media_type": "application/json",
            "seal": {"kind": "controller-receipt", "schema_version": 1, "run_token": None, "manifest_hash": None},
            "sensitivity": "internal",
            "retention_class": "sealed-evidence",
            "availability_at_append": "present",
        }

    def test_canonical_bytes_are_stable_and_nfc(self) -> None:
        event = self.event()
        event["actor"]["id"] = "cafe\u0301"
        with self.assertRaises(TrajectoryError):
            seal_event(event)  # normalized value is not a permitted identifier
        first = seal_event(self.event())
        reordered = {key: first[key] for key in reversed(first)}
        self.assertEqual(canonical_event_bytes(first), canonical_event_bytes(reordered))
        self.assertEqual(json.loads(canonical_event_bytes(first)), first)

    def test_append_requires_existing_transaction(self) -> None:
        event = seal_event(self.event())
        with self.assertRaisesRegex(TrajectoryError, "existing transaction"):
            TrajectoryStore(self.conn).append(event)

    def test_append_builds_one_contiguous_hash_chain(self) -> None:
        first = self.append(self.event())
        second_event = self.event(seq=2, previous=first["event_hash"])
        second_event["parent_event_id"] = first["event_id"]
        second = self.append(second_event)
        rows = self.conn.execute(
            "SELECT seq,event_hash,canonical_json FROM trajectory_events ORDER BY seq"
        ).fetchall()
        self.assertEqual([row["seq"] for row in rows], [1, 2])
        self.assertEqual(rows[1]["event_hash"], second["event_hash"])
        self.assertEqual(bytes(rows[1]["canonical_json"]), canonical_event_bytes(second))

    def test_gap_wrong_hash_and_unknown_parent_fail_closed(self) -> None:
        first = self.append(self.event())
        gap = seal_event(self.event(seq=3, previous=first["event_hash"]))
        self.conn.execute("BEGIN IMMEDIATE")
        with self.assertRaisesRegex(TrajectoryError, "next contiguous"):
            TrajectoryStore(self.conn).append(gap)
        self.conn.execute("ROLLBACK")

        wrong = self.event(seq=2, previous="sha256:" + "f" * 64)
        wrong["parent_event_id"] = str(uuid.uuid4())
        sealed = seal_event(wrong)
        self.conn.execute("BEGIN IMMEDIATE")
        with self.assertRaisesRegex(TrajectoryError, "prev_event_hash"):
            TrajectoryStore(self.conn).append(sealed)
        self.conn.execute("ROLLBACK")

    def test_cross_task_parent_is_rejected(self) -> None:
        other = "task-b"
        self.conn.execute(
            """INSERT INTO tasks(
                   id,type,status,current_stage,owner,revision,profile_hash,input_hash,
                   profile_snapshot_path,input_snapshot_path,artifact_dir,max_transitions,
                   created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (other, "propose", "queued", "draft", "codex", 0, "p" * 64, "i" * 64,
             "/tmp/profile-b", "/tmp/input-b", "/tmp/artifact-b", 10, 1, 1),
        )
        event = self.event()
        event["task"]["task_id"] = other
        event["trajectory_id"] = f"task:{other}"
        other_event = self.append(event)
        first = self.append(self.event())
        second = self.event(seq=2, previous=first["event_hash"])
        second["parent_event_id"] = other_event["event_id"]
        sealed = seal_event(second)
        self.conn.execute("BEGIN IMMEDIATE")
        with self.assertRaisesRegex(TrajectoryError, "same trajectory"):
            TrajectoryStore(self.conn).append(sealed)
        self.conn.execute("ROLLBACK")

    def test_reserved_future_type_and_future_schema_are_rejected(self) -> None:
        reserved = self.event(event_type="tool.requested")
        with self.assertRaisesRegex(TrajectoryError, "unsupported event_type"):
            seal_event(reserved)
        future = self.event()
        future["schema_version"] = 2
        with self.assertRaisesRegex(TrajectoryError, "schema_version"):
            seal_event(future)
        future_event = self.event()
        future_event["event_version"] = 2
        with self.assertRaisesRegex(TrajectoryError, "event_version"):
            seal_event(future_event)

    def test_body_rejects_arbitrary_text(self) -> None:
        event = self.event()
        event["body"]["reason"] = "arbitrary-provider-text"
        with self.assertRaisesRegex(TrajectoryError, "keys mismatch"):
            seal_event(event)

    def test_detectable_secrets_are_rejected_from_allowed_string_fields(self) -> None:
        cases = {
            "workflow_attempt_ref": ("workflow_attempt_ref", "token:trajectory-canary-12345678"),
            "actor_id": ("actor.id", "api-key:trajectory-canary-12345678"),
            "actor_model": ("actor.model", "gh" + "p_trajectorycanary12345678"),
        }
        for name, (field, canary) in cases.items():
            with self.subTest(name=name):
                event = self.event()
                if field == "workflow_attempt_ref":
                    event["workflow_attempt_ref"] = canary
                elif field == "actor.id":
                    event["actor"]["id"] = canary
                else:
                    event["actor"]["model"] = canary
                with self.assertRaisesRegex(TrajectoryError, "detectable secret") as caught:
                    seal_event(event)
                self.assertNotIn(canary, str(caught.exception))

    def test_body_code_fields_are_closed_and_other_requires_a_digest(self) -> None:
        transition = self.event(event_type="task.transition.committed")
        transition["body"] = {
            "transition_seq": 1,
            "operation_id": "operation-a",
            "from_status": "queued",
            "to_status": "running",
            "reason_code": "provider-invented-reason",
            "reason_digest": None,
            "outcome_code": None,
            "outcome_digest": None,
        }
        with self.assertRaisesRegex(TrajectoryError, "unsupported .*reason_code"):
            seal_event(transition)
        transition["body"]["reason_code"] = "other"
        with self.assertRaisesRegex(TrajectoryError, "required only"):
            seal_event(transition)
        transition["body"]["reason_digest"] = SHA
        seal_event(transition)
        transition["body"]["outcome_code"] = "provider-invented-outcome"
        with self.assertRaisesRegex(TrajectoryError, "unsupported .*outcome_code"):
            seal_event(transition)
        transition["body"]["outcome_code"] = "other"
        with self.assertRaisesRegex(TrajectoryError, "required only"):
            seal_event(transition)
        transition["body"]["outcome_digest"] = SHA
        seal_event(transition)

        run_token = self.add_run()
        stage = self.event(event_type="stage.settled")
        stage["run"] = {"run_token": run_token, "stage": "draft", "cycle": 1, "attempt": 1}
        stage["body"] = {
            "classification": "provider-defined-classification",
            "outcome_code": None,
            "outcome_digest": None,
            "elapsed_ms": 1,
            "usage": {
                "basis": "unavailable",
                "input_tokens": None,
                "output_tokens": None,
                "total_tokens": None,
                "unavailable_reason_code": "other",
                "unavailable_reason_digest": SHA,
            },
        }
        with self.assertRaisesRegex(TrajectoryError, "unsupported .*classification"):
            seal_event(stage)

        stage["body"]["classification"] = "success"
        stage["body"]["usage"]["unavailable_reason_code"] = "provider-invented-reason"
        stage["body"]["usage"]["unavailable_reason_digest"] = None
        with self.assertRaisesRegex(TrajectoryError, "unsupported .*unavailable_reason_code"):
            seal_event(stage)
        stage["body"]["usage"]["unavailable_reason_code"] = "other"
        stage["body"]["usage"]["unavailable_reason_digest"] = SHA
        seal_event(stage)

    def test_self_hash_is_verified(self) -> None:
        event = seal_event(self.event())
        event["recorded_at_ms"] += 1
        with self.assertRaisesRegex(TrajectoryError, "event_hash mismatch"):
            canonical_event_bytes(event)

    def test_update_and_delete_are_blocked_by_sqlite(self) -> None:
        self.append(self.event())
        with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
            self.conn.execute("UPDATE trajectory_events SET event_type='task.created'")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
            self.conn.execute("DELETE FROM trajectory_events")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM trajectory_events").fetchone()[0], 1)

    def test_run_identity_must_match_stage_run(self) -> None:
        run_token = self.add_run()
        event = self.event(event_type="stage.claimed")
        event["run"] = {"run_token": run_token, "stage": "wrong", "cycle": 1, "attempt": 1}
        event["body"] = {"lease_digest": SHA, "owner_role": "executor"}
        sealed = seal_event(event)
        self.conn.execute("BEGIN IMMEDIATE")
        with self.assertRaisesRegex(TrajectoryError, "run identity mismatch"):
            TrajectoryStore(self.conn).append(sealed)
        self.conn.execute("ROLLBACK")

    def test_task_revision_must_match_post_mutation_state(self) -> None:
        event = self.event()
        event["task"]["revision"] = 1
        sealed = seal_event(event)
        self.conn.execute("BEGIN IMMEDIATE")
        with self.assertRaisesRegex(TrajectoryError, "revision mismatch"):
            TrajectoryStore(self.conn).append(sealed)
        self.conn.execute("ROLLBACK")

    def test_provider_invocation_requires_one_intent_and_rejects_duplicate_events(self) -> None:
        run_token = self.add_run()
        invocation_id = "invocation-a"
        settled = seal_event(self.provider_event("provider.settled", run_token, invocation_id))
        self.conn.execute("BEGIN IMMEDIATE")
        with self.assertRaisesRegex(TrajectoryError, "exactly one earlier dispatch intent"):
            TrajectoryStore(self.conn).append(settled)
        self.conn.execute("ROLLBACK")

        intent = self.append(self.provider_event("provider.dispatch_intent", run_token, invocation_id))
        duplicate = seal_event(self.provider_event(
            "provider.dispatch_intent", run_token, invocation_id, seq=2, previous=intent["event_hash"]
        ))
        self.conn.execute("BEGIN IMMEDIATE")
        with self.assertRaisesRegex(TrajectoryError, "already has a dispatch intent"):
            TrajectoryStore(self.conn).append(duplicate)
        self.conn.execute("ROLLBACK")

        settled_event = self.provider_event(
            "provider.settled", run_token, invocation_id, seq=2, previous=intent["event_hash"]
        )
        settled = self.append(settled_event)
        duplicate_settled = seal_event(self.provider_event(
            "provider.settled", run_token, invocation_id, seq=3, previous=settled["event_hash"]
        ))
        self.conn.execute("BEGIN IMMEDIATE")
        with self.assertRaisesRegex(TrajectoryError, "duplicate provider.settled"):
            TrajectoryStore(self.conn).append(duplicate_settled)
        self.conn.execute("ROLLBACK")

    def test_provider_settled_requires_complete_usage_contract(self) -> None:
        run_token = self.add_run()
        event = self.provider_event("provider.settled", run_token, "invocation-a")
        del event["body"]["elapsed_ms"]
        with self.assertRaisesRegex(TrajectoryError, "keys mismatch"):
            seal_event(event)
        event = self.provider_event("provider.settled", run_token, "invocation-b")
        event["body"]["usage"]["unavailable_reason_code"] = None
        with self.assertRaisesRegex(TrajectoryError, "non-empty string"):
            seal_event(event)

    def test_unknown_causal_ref_is_rejected(self) -> None:
        first = self.append(self.event())
        second = self.event(seq=2, previous=first["event_hash"])
        second["causal_event_ids"] = [str(uuid.uuid4())]
        sealed = seal_event(second)
        self.conn.execute("BEGIN IMMEDIATE")
        with self.assertRaisesRegex(TrajectoryError, "same trajectory"):
            TrajectoryStore(self.conn).append(sealed)
        self.conn.execute("ROLLBACK")

    def test_session_events_require_an_opaque_session_ref(self) -> None:
        event = self.event(event_type="session.bound")
        ref_id = "evref:" + str(uuid.uuid4())
        event["body"] = {"role": "executor", "binding_ref": ref_id}
        event["evidence_refs"] = [self.evidence_ref(ref_id)]
        with self.assertRaisesRegex(TrajectoryError, "requires session_ref"):
            seal_event(event)

    def test_session_rebound_predecessor_is_durably_resolved(self) -> None:
        binding_ref = "evref:" + str(uuid.uuid4())
        bound = self.event(event_type="session.bound")
        bound["session_ref"] = "session-a"
        bound["body"] = {"role": "executor", "binding_ref": binding_ref}
        binding = self.evidence_ref(binding_ref)
        binding.update({"kind": "session-binding", "relative_path": "sessions/binding.json"})
        binding["seal"]["kind"] = "session-binding"
        bound["evidence_refs"] = [binding]
        bound = self.append(bound)

        ordinary = self.event(seq=2, previous=bound["event_hash"])
        ordinary = self.append(ordinary)

        other_task = "task-b"
        self.conn.execute(
            """INSERT INTO tasks(
                   id,type,status,current_stage,owner,revision,profile_hash,input_hash,
                   profile_snapshot_path,input_snapshot_path,artifact_dir,max_transitions,
                   created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (other_task, "propose", "queued", "draft", "codex", 0, "p" * 64, "i" * 64,
             "/tmp/profile-b", "/tmp/input-b", "/tmp/artifact-b", 10, 1, 1),
        )
        foreign_ref = "evref:" + str(uuid.uuid4())
        foreign = self.event(event_type="session.bound")
        foreign["task"]["task_id"] = other_task
        foreign["trajectory_id"] = f"task:{other_task}"
        foreign["session_ref"] = "session-foreign"
        foreign["body"] = {"role": "executor", "binding_ref": foreign_ref}
        foreign_binding = self.evidence_ref(foreign_ref)
        foreign_binding.update({"kind": "session-binding", "relative_path": "sessions/binding.json"})
        foreign_binding["seal"]["kind"] = "session-binding"
        foreign["evidence_refs"] = [foreign_binding]
        foreign = self.append(foreign)

        def rebound(predecessor: str, *, session_ref: str = "session-b") -> dict:
            checkpoint_ref = "evref:" + str(uuid.uuid4())
            event = self.event(
                seq=3,
                previous=ordinary["event_hash"],
                event_type="session.rebound",
            )
            event["session_ref"] = session_ref
            event["body"] = {
                "predecessor_event_id": predecessor,
                "reason_code": "resume",
                "reason_digest": None,
                "checkpoint_ref": checkpoint_ref,
            }
            checkpoint = self.evidence_ref(checkpoint_ref)
            checkpoint.update({"kind": "checkpoint", "relative_path": "sessions/checkpoint.json"})
            checkpoint["seal"]["kind"] = "checkpoint"
            event["evidence_refs"] = [checkpoint]
            return event

        failures = {
            "nonexistent": (str(uuid.uuid4()), "earlier in the same trajectory"),
            "cross-trajectory": (foreign["event_id"], "earlier in the same trajectory"),
            "wrong-type": (ordinary["event_id"], "session binding event"),
        }
        for name, (predecessor, message) in failures.items():
            with self.subTest(name=name):
                sealed = seal_event(rebound(predecessor))
                self.conn.execute("BEGIN IMMEDIATE")
                with self.assertRaisesRegex(TrajectoryError, message):
                    TrajectoryStore(self.conn).append(sealed)
                self.conn.execute("ROLLBACK")

        unknown_reason = rebound(bound["event_id"])
        unknown_reason["body"]["reason_code"] = "provider-invented-rebound"
        with self.assertRaisesRegex(TrajectoryError, "unsupported .*reason_code"):
            seal_event(unknown_reason)
        unknown_reason["body"]["reason_code"] = "other"
        with self.assertRaisesRegex(TrajectoryError, "required only"):
            seal_event(unknown_reason)
        unknown_reason["body"]["reason_digest"] = SHA
        seal_event(unknown_reason)

        same = seal_event(rebound(bound["event_id"], session_ref="session-a"))
        self.conn.execute("BEGIN IMMEDIATE")
        with self.assertRaisesRegex(TrajectoryError, "must replace"):
            TrajectoryStore(self.conn).append(same)
        self.conn.execute("ROLLBACK")

        accepted = self.append(rebound(bound["event_id"]))
        self.assertEqual(accepted["body"]["predecessor_event_id"], bound["event_id"])

    def test_database_schema_is_additive_and_existing_rows_and_manifest_are_unchanged(self) -> None:
        legacy_path = Path(self.tmp.name) / "legacy.db"
        manifest_path = Path(self.tmp.name) / "run.manifest.json"
        manifest_bytes = b'{"schema_version":3,"sealed":true}\n'
        manifest_path.write_bytes(manifest_bytes)
        manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
        legacy = sqlite3.connect(legacy_path, isolation_level=None)
        legacy.row_factory = sqlite3.Row
        legacy_ddl = DDL.split("\nCREATE TABLE IF NOT EXISTS trajectory_events(", 1)[0]
        legacy.executescript(legacy_ddl)
        legacy.executescript(PACK_SCHEMA)
        _migrate(legacy)
        legacy.execute(
            """INSERT INTO tasks(
                   id,type,status,current_stage,owner,revision,profile_hash,input_hash,
                   profile_snapshot_path,input_snapshot_path,artifact_dir,max_transitions,
                   created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("legacy-task", "apply", "done", "review", "codex", 4, "p" * 64, "i" * 64,
             "/tmp/profile", "/tmp/input", "/tmp/artifact", 10, 1, 2),
        )
        legacy.execute(
            """INSERT INTO stage_runs(
                   run_token,task_id,stage,cycle,attempt,owner,status,log_path,manifest_path,
                   manifest_hash,sealed,started_at,ended_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("legacy-run", "legacy-task", "review", 1, 1, "codex", "committed", "/tmp/log",
             str(manifest_path), manifest_hash, 1, 1, 2),
        )
        before_rows = {
            table: [dict(row) for row in legacy.execute(f"SELECT * FROM {table} ORDER BY rowid")]
            for table in ("tasks", "stage_runs", "transitions")
        }
        before_sql = {
            row["name"]: row["sql"] for row in legacy.execute(
                "SELECT name,sql FROM sqlite_master WHERE type IN ('table','index') AND name IN "
                "('tasks','stage_runs','transitions','ux_active_run')"
            )
        }
        legacy.close()

        migrated = connect(legacy_path)
        self.addCleanup(migrated.close)
        after_rows = {
            table: [dict(row) for row in migrated.execute(f"SELECT * FROM {table} ORDER BY rowid")]
            for table in ("tasks", "stage_runs", "transitions")
        }
        after_sql = {
            row["name"]: row["sql"] for row in migrated.execute(
                "SELECT name,sql FROM sqlite_master WHERE type IN ('table','index') AND name IN "
                "('tasks','stage_runs','transitions','ux_active_run')"
            )
        }
        self.assertEqual(before_rows, after_rows)
        self.assertEqual(before_sql, after_sql)
        self.assertEqual(manifest_path.read_bytes(), manifest_bytes)
        self.assertEqual(hashlib.sha256(manifest_path.read_bytes()).hexdigest(), manifest_hash)
        self.assertIsNotNone(migrated.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='trajectory_events'"
        ).fetchone())
        triggers = {row[0] for row in migrated.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'trajectory_events_no_%'"
        )}
        self.assertEqual(triggers, {"trajectory_events_no_update", "trajectory_events_no_delete"})
