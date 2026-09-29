from __future__ import annotations

import hashlib
import io
import json
import os
import copy
import shutil
import sqlite3
import socket
import subprocess
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from orchestrator.controller import Controller, ControllerError, PostCommitTrajectoryError
from orchestrator.cli import main as cli_main
from orchestrator.db import DDL, _migrate, connect
from orchestrator.runner import RunResult
from orchestrator.trajectory import (
    BASELINE_NAMESPACE,
    MISSING_DOMAINS,
    TrajectoryError,
    TrajectoryStore,
    TrajectoryWriter,
    canonical_event_bytes,
    digest_text,
    seal_event,
    trajectory_mode,
)
from orchestrator.trajectory_replay import (
    canonical_json_bytes,
    freeze_snapshot,
    projection_bytes,
    reduce_snapshot,
    render_projection,
)


SHA = "sha256:" + "a" * 64


class TrajectoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state.db"
        self.conn = connect(self.path)
        self.addCleanup(self.conn.close)
        self.task_id = "task-a"
        self.artifact_dir = Path(self.tmp.name) / "artifacts"
        self.artifact_dir.mkdir()
        self.conn.execute(
            """INSERT INTO tasks(
                   id,type,status,current_stage,owner,revision,profile_hash,input_hash,
                   profile_snapshot_path,input_snapshot_path,artifact_dir,max_transitions,
                   created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (self.task_id, "propose", "queued", "draft", "codex", 0, "p" * 64, "i" * 64,
             "/tmp/profile", "/tmp/input", str(self.artifact_dir), 10, 1, 1),
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
            event["evidence_refs"] = [self.evidence_ref(ref_id, run_token)]
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
    def evidence_ref(ref_id: str, run_token: str = "fixture-run") -> dict:
        return {
            "ref_id": ref_id,
            "kind": "transport-receipt",
            "relative_path": "runs/receipt.json",
            "sha256": "c" * 64,
            "size_bytes": 2,
            "media_type": "application/json",
            "seal": {"kind": "controller-receipt", "schema_version": 1, "run_token": run_token, "manifest_hash": "d" * 64},
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
        for event_type in ("tool.requested", "approval.requested", "subagent.spawned", "join.decided"):
            with self.subTest(event_type=event_type):
                reserved = self.event(event_type=event_type)
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
            "sealed": False,
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
        wrong_session = self.provider_event(
            "provider.dispatched", run_token, invocation_id, seq=2, previous=intent["event_hash"]
        )
        wrong_session["session_ref"] = "session-b"
        self.conn.execute("BEGIN IMMEDIATE")
        with self.assertRaisesRegex(TrajectoryError, "session changed after binding"):
            TrajectoryStore(self.conn).append(seal_event(wrong_session))
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

    def test_dispatch_intent_crash_window_projects_unknown_without_retry(self) -> None:
        first = self.append(self.event())
        run_token = self.add_run()
        intent = self.provider_event(
            "provider.dispatch_intent", run_token, "invocation-crash",
            seq=2, previous=first["event_hash"],
        )
        self.append(intent)

        snapshot = freeze_snapshot(self.conn, self.task_id, captured_at_ms=2000)
        projection = reduce_snapshot(snapshot)
        self.assertEqual(projection.integrity_status, "incomplete")
        self.assertEqual(len(projection.invocations), 1)
        self.assertEqual(projection.invocations[0]["dispatch_state"], "unknown")
        self.assertEqual(projection.invocations[0]["result_state"], "unknown")
        self.assertEqual(
            {item["code"] for item in projection.unknowns},
            {"provider_handoff_unknown", "provider_result_unknown"},
        )

    def test_dispatched_without_settled_keeps_result_unknown(self) -> None:
        first = self.append(self.event())
        run_token = self.add_run()
        intent = self.append(self.provider_event(
            "provider.dispatch_intent", run_token, "invocation-dispatched",
            seq=2, previous=first["event_hash"],
        ))
        self.append(self.provider_event(
            "provider.dispatched", run_token, "invocation-dispatched",
            seq=3, previous=intent["event_hash"],
        ))
        projection = reduce_snapshot(
            freeze_snapshot(self.conn, self.task_id, captured_at_ms=2000)
        )
        invocation = projection.invocations[0]
        self.assertEqual(invocation["dispatch_state"], "dispatched")
        self.assertEqual(invocation["result_state"], "unknown")
        codes = {item["code"] for item in projection.unknowns}
        self.assertIn("provider_result_unknown", codes)
        self.assertNotIn("provider_handoff_unknown", codes)

    def test_evidence_ref_path_seal_and_run_binding_fail_closed(self) -> None:
        run_token = self.add_run()
        event = self.provider_event("provider.dispatched", run_token, "invocation-a")

        for path in ("../receipt.json", "/tmp/receipt.json", "runs//receipt.json", "runs/./receipt.json"):
            with self.subTest(path=path):
                candidate = json.loads(json.dumps(event))
                candidate["evidence_refs"][0]["relative_path"] = path
                with self.assertRaisesRegex(TrajectoryError, "relative_path"):
                    seal_event(candidate)

        wrong_run = json.loads(json.dumps(event))
        wrong_run["evidence_refs"][0]["seal"]["run_token"] = "another-run"
        with self.assertRaisesRegex(TrajectoryError, "run binding"):
            seal_event(wrong_run)

        unsupported = json.loads(json.dumps(event))
        unsupported["evidence_refs"][0]["seal"]["schema_version"] = 4
        with self.assertRaisesRegex(TrajectoryError, "unsupported .*schema_version"):
            seal_event(unsupported)

        wrong_kind = json.loads(json.dumps(event))
        wrong_kind["evidence_refs"][0]["seal"]["kind"] = "checkpoint"
        with self.assertRaisesRegex(TrajectoryError, "does not match evidence kind"):
            seal_event(wrong_kind)

        wrong_availability = json.loads(json.dumps(event))
        wrong_availability["evidence_refs"][0]["availability_at_append"] = "expired"
        with self.assertRaisesRegex(TrajectoryError, "unsupported .*availability"):
            seal_event(wrong_availability)

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

    def test_mixed_legacy_baseline_is_seq_gt_one_deterministic_and_idempotent(self) -> None:
        writer = TrajectoryWriter(self.conn, "write")
        self.conn.execute("BEGIN IMMEDIATE")
        writer.append(
            self.task_id,
            "task.created",
            recorded_at_ms=1000,
            body={"profile_digest": "sha256:" + "a" * 64, "input_digest": "sha256:" + "b" * 64},
            retention_class="structural",
        )
        self.conn.execute("COMMIT")

        # Simulate a canonical transition committed while the gate was off.
        self.conn.execute(
            """INSERT INTO transitions(
                   task_id,seq,operation_id,run_token,stage,owner,edge,outcome,
                   from_status,to_status,reason,at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (self.task_id, 1, "legacy-operation", None, None, None, None, None,
             None, "queued", "submitted", 1001),
        )
        self.conn.execute(
            "UPDATE tasks SET revision=1,transitions_count=1,updated_at=1001 WHERE id=?",
            (self.task_id,),
        )

        self.conn.execute("BEGIN IMMEDIATE")
        baseline_id = writer.ensure_legacy_baseline(self.task_id, recorded_at_ms=1002)
        self.conn.execute("COMMIT")
        expected = str(uuid.uuid5(BASELINE_NAMESPACE, f"task:{self.task_id}"))
        self.assertEqual(baseline_id, expected)

        self.conn.execute("BEGIN IMMEDIATE")
        repeated = writer.ensure_legacy_baseline(self.task_id, recorded_at_ms=1003)
        self.conn.execute("COMMIT")
        self.assertIsNone(repeated)
        rows = self.conn.execute(
            "SELECT canonical_json FROM trajectory_events WHERE trajectory_id=? ORDER BY seq",
            (f"task:{self.task_id}",),
        ).fetchall()
        events = [json.loads(bytes(row["canonical_json"])) for row in rows]
        self.assertEqual([event["seq"] for event in events], [1, 2])
        baseline = events[1]
        self.assertEqual(baseline["event_id"], expected)
        self.assertEqual(baseline["body"]["completeness"], "partial")
        self.assertEqual(baseline["body"]["missing_domains"], sorted(MISSING_DOMAINS))
        self.assertEqual(baseline["body"]["coverage_through_transition_seq"], 1)

        # Live recording resumes after the partial coverage window.  The
        # baseline covers only transition 1; transition 2 remains a normal,
        # independently verified live event.
        self.conn.execute("BEGIN IMMEDIATE")
        self.conn.execute(
            """INSERT INTO transitions(
                   task_id,seq,operation_id,run_token,stage,owner,edge,outcome,
                   from_status,to_status,reason,at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (self.task_id, 2, "live-operation", None, None, None, None, "allow",
             "queued", "done", "stage_completed", 1004),
        )
        self.conn.execute(
            "UPDATE tasks SET status='done',revision=2,transitions_count=2,updated_at=1004 WHERE id=?",
            (self.task_id,),
        )
        writer.append(
            self.task_id,
            "task.transition.committed",
            recorded_at_ms=1004,
            body={
                "transition_seq": 2,
                "operation_id": "live-operation",
                "from_status": "queued",
                "to_status": "done",
                "reason_code": "stage_completed",
                "reason_digest": None,
                "outcome_code": "allow",
                "outcome_digest": None,
            },
        )
        self.conn.execute("COMMIT")

        snapshot = freeze_snapshot(self.conn, self.task_id, captured_at_ms=1005)
        projection = reduce_snapshot(snapshot)
        self.assertEqual(projection.integrity_status, "ok")
        self.assertEqual(projection.completeness, "partial")
        self.assertEqual(projection.task_lifecycle["status"], "done")
        self.assertEqual(projection.task_lifecycle["transition_seq"], 2)

    def test_unknown_gate_value_fails_closed_without_echoing_the_value(self) -> None:
        stderr = io.StringIO()
        with patch("sys.stderr", stderr):
            mode = trajectory_mode({"ORCH_TRAJECTORY_V1": "token-do-not-echo"})
        self.assertEqual(mode, "off")
        self.assertEqual(stderr.getvalue().count("\n"), 1)
        self.assertNotIn("token-do-not-echo", stderr.getvalue())

    def test_concurrent_writers_serialize_without_gaps_or_duplicate_retry(self) -> None:
        barrier = threading.Barrier(2)
        errors: list[BaseException] = []

        def append_transition(index: int) -> None:
            conn = connect(self.path)
            try:
                barrier.wait(timeout=5)
                conn.execute("BEGIN IMMEDIATE")
                TrajectoryWriter(conn, "write").append(
                    self.task_id,
                    "task.transition.committed",
                    recorded_at_ms=2000 + index,
                    body={
                        "transition_seq": index + 1,
                        "operation_id": f"concurrent-{index}",
                        "from_status": None,
                        "to_status": "queued",
                        "reason_code": "stage_completed",
                        "reason_digest": None,
                        "outcome_code": None,
                        "outcome_digest": None,
                    },
                )
                conn.execute("COMMIT")
            except BaseException as exc:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                errors.append(exc)
            finally:
                conn.close()

        workers = [threading.Thread(target=append_transition, args=(index,)) for index in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=10)
        self.assertFalse(any(worker.is_alive() for worker in workers))
        if errors:
            raise errors[0]

        rows = self.conn.execute(
            "SELECT seq,event_id,event_hash,canonical_json FROM trajectory_events "
            "WHERE trajectory_id=? ORDER BY seq",
            (f"task:{self.task_id}",),
        ).fetchall()
        self.assertEqual([row["seq"] for row in rows], [1, 2])
        self.assertEqual(len({row["event_id"] for row in rows}), 2)
        events = [json.loads(bytes(row["canonical_json"])) for row in rows]
        self.assertEqual(events[0]["prev_event_hash"], None)
        self.assertEqual(events[1]["prev_event_hash"], events[0]["event_hash"])
        for event, row in zip(events, rows, strict=True):
            self.assertEqual(canonical_event_bytes(event), bytes(row["canonical_json"]))

        retry = connect(self.path)
        self.addCleanup(retry.close)
        retry.execute("BEGIN IMMEDIATE")
        with self.assertRaisesRegex(TrajectoryError, "conflicts with durable state"):
            TrajectoryWriter(retry, "write").append(
                self.task_id,
                "task.transition.committed",
                recorded_at_ms=2003,
                event_id=rows[0]["event_id"],
                body={
                    "transition_seq": 3,
                    "operation_id": "concurrent-retry",
                    "from_status": None,
                    "to_status": "queued",
                    "reason_code": "stage_completed",
                    "reason_digest": None,
                    "outcome_code": None,
                    "outcome_digest": None,
                },
            )
        retry.execute("ROLLBACK")
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM trajectory_events WHERE trajectory_id=?",
                (f"task:{self.task_id}",),
            ).fetchone()[0],
            2,
        )


ROOT = Path(__file__).resolve().parents[2]
DEMO_PROFILE = ROOT / "orchestrator" / "examples" / "demo-loop.yaml"
DEMO_INPUT = ROOT / "orchestrator" / "examples" / "demo-input.md"

#: The five canonical lifecycle emit points. Every other v1 type is either a
#: provider/session detail or the migration baseline, and none of them stands
#: in a one-to-one relation with a canonical row.
CANONICAL_EMIT_POINTS = (
    "task.created",
    "task.transition.committed",
    "stage.claimed",
    "stage.settled",
    "evidence.sealed",
)


class _ScriptedRunner:
    """A runner that never leaves this process: it writes the outcome line."""

    def __init__(self, outcomes: list[str]):
        self.outcomes = iter(outcomes)

    def run(self, owner: str, prompt: str, timeout: int, log_path: Path) -> RunResult:
        output = f"ORCHESTRATOR_OUTCOME: {next(self.outcomes)}\n"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(output, encoding="utf-8")
        return RunResult(0, output, None, "raw", "raw")


SECRET_CANARY = "token:trajectory-canary-12345678"
PROVIDER_SESSION = "provider-native-session-should-not-persist"


class _ReceiptRunner:
    def __init__(self, outcomes: list[str]):
        self.outcomes = iter(outcomes)
        self.session_binding = {"session_id": PROVIDER_SESSION}
        self.tick = 1000

    def run(self, owner: str, prompt: str, timeout: int, log_path: Path) -> RunResult:
        outcome = next(self.outcomes)
        output = f"{SECRET_CANARY}\nORCHESTRATOR_OUTCOME: {outcome}\n"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(output, encoding="utf-8")
        started = self.tick
        self.tick += 10
        return RunResult(
            0,
            output,
            None,
            "raw",
            "raw",
            started_at_ms=started,
            ended_at_ms=started + 5,
            duration_ms=5,
            usage_unavailable_reason="provider_cli_usage_not_reported",
            execution_receipt={
                "schema_version": 1,
                "invocation_verified": True,
                "provider_session_id": PROVIDER_SESSION,
                "session_binding": self.session_binding,
                "role": "executor",
            },
        )


class _RebindingReceiptRunner:
    """Two explicit provider sessions whose counters are session-cumulative."""

    def __init__(self) -> None:
        self.outcomes = iter(("submit", "allow"))
        self.sessions = ("provider-session-a", "provider-session-b")
        self.index = 0
        self.session_binding = {"session_id": self.sessions[0]}

    def run(self, owner: str, prompt: str, timeout: int, log_path: Path) -> RunResult:
        outcome = next(self.outcomes)
        session_id = self.sessions[self.index]
        total = (self.index + 1) * 100
        output = f"ORCHESTRATOR_OUTCOME: {outcome}\n"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(output, encoding="utf-8")
        self.index += 1
        if self.index < len(self.sessions):
            self.session_binding = {"session_id": self.sessions[self.index]}
        return RunResult(
            0,
            output,
            None,
            "raw",
            "raw",
            started_at_ms=1000 + self.index * 10,
            ended_at_ms=1005 + self.index * 10,
            duration_ms=5,
            model="unspecified",
            usage_input_tokens=total - 10,
            usage_output_tokens=10,
            usage_total_tokens=total,
            execution_receipt={
                "schema_version": 1,
                "invocation_verified": True,
                "provider_session_id": session_id,
                "session_binding": {"session_id": session_id},
                "usage_basis": "cumulative",
                "role": "executor",
            },
        )


class InvocationAdapterTest(unittest.TestCase):
    def controller(self) -> Controller:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        with patch.dict(os.environ, {"ORCH_TRAJECTORY_V1": "write"}):
            controller = Controller(
                Path(directory.name) / "runtime",
                runner=_ReceiptRunner(["submit", "allow"]),
            )
        self.addCleanup(controller.close)
        return controller

    @staticmethod
    def events(controller: Controller, task_id: str) -> list[dict]:
        return [
            json.loads(bytes(row["canonical_json"]))
            for row in controller.conn.execute(
                "SELECT canonical_json FROM trajectory_events WHERE task_id=? ORDER BY seq",
                (task_id,),
            )
        ]

    def completed(self) -> tuple[Controller, str]:
        controller = self.controller()
        task_id = controller.submit("demo-loop", DEMO_PROFILE, DEMO_INPUT)
        status = controller.run_until_stop(task_id)
        self.assertEqual(status["task"]["status"], "done")
        return controller, task_id

    def test_valid_provider_session_and_seal_chain_is_opaque_and_secret_free(self) -> None:
        controller, task_id = self.completed()
        events = self.events(controller, task_id)
        encoded = json.dumps(events, ensure_ascii=False, sort_keys=True)
        expected_session = Controller._trajectory_session_ref(PROVIDER_SESSION)

        self.assertNotIn(PROVIDER_SESSION, encoded)
        self.assertNotIn(SECRET_CANARY, encoded)
        invocations: dict[str, list[dict]] = {}
        for event in events:
            if event["event_type"].startswith("provider."):
                invocations.setdefault(event["invocation_id"], []).append(event)
        self.assertEqual(len(invocations), 2)
        for invocation in invocations.values():
            self.assertEqual(
                [event["event_type"] for event in invocation],
                ["provider.dispatch_intent", "provider.dispatched", "provider.settled"],
            )
            self.assertEqual(
                {event["run"]["run_token"] for event in invocation},
                {invocation[0]["invocation_id"]},
            )
            self.assertEqual({event["session_ref"] for event in invocation}, {expected_session})
            dispatched = invocation[1]
            settled = invocation[2]
            self.assertEqual(
                dispatched["body"]["transport_receipt_ref"],
                dispatched["evidence_refs"][0]["ref_id"],
            )
            self.assertEqual(
                settled["body"]["final_response_ref"],
                settled["evidence_refs"][0]["ref_id"],
            )

        sessions = [event for event in events if event["event_type"] == "session.bound"]
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["session_ref"], expected_session)
        self.assertTrue(any(event["event_type"] == "evidence.sealed" for event in events))
        for event in events:
            for ref in event["evidence_refs"]:
                self.assertFalse(Path(ref["relative_path"]).is_absolute())
                self.assertNotIn("..", Path(ref["relative_path"]).parts)
                run = controller.conn.execute(
                    "SELECT task_id,manifest_hash FROM stage_runs WHERE run_token=?",
                    (ref["seal"]["run_token"],),
                ).fetchone()
                self.assertEqual(run["task_id"], task_id)
                self.assertEqual(run["manifest_hash"], ref["seal"]["manifest_hash"])

        snapshot = freeze_snapshot(controller.conn, task_id, captured_at_ms=3000)
        projection = reduce_snapshot(snapshot)
        self.assertEqual(projection.integrity_status, "ok")
        self.assertEqual(
            {item["dispatch_state"] for item in projection.invocations}, {"dispatched"}
        )
        self.assertEqual(
            {item["result_state"] for item in projection.invocations}, {"success"}
        )
        rendered = json.dumps(
            render_projection(snapshot, projection), ensure_ascii=False, sort_keys=True
        )
        self.assertNotIn(PROVIDER_SESSION, rendered)
        self.assertNotIn(SECRET_CANARY, rendered)

    def test_waiting_user_review_is_a_settled_provider_result_not_a_crash_unknown(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        with patch.dict(os.environ, {"ORCH_TRAJECTORY_V1": "write"}):
            controller = Controller(
                Path(directory.name) / "runtime", runner=_ScriptedRunner([]),
            )
        self.addCleanup(controller.close)
        task_id = controller.submit("demo-loop", DEMO_PROFILE, DEMO_INPUT)
        run_token, _, profile, _ = controller.claim_stage(task_id)
        controller.commit_run(
            task_id,
            run_token,
            RunResult(
                0,
                "ORCHESTRATOR_OUTCOME: needs_user_decision\n",
                "needs_user_decision",
                "waiting_user",
                "review_requires_astra_decision",
                started_at_ms=1000,
                ended_at_ms=1005,
                duration_ms=5,
                usage_unavailable_reason="provider_cli_usage_not_reported",
            ),
            profile,
        )

        events = self.events(controller, task_id)
        settled = [event for event in events if event["event_type"] == "provider.settled"]
        self.assertEqual(len(settled), 1)
        self.assertEqual(settled[0]["body"]["result_class"], "success")
        snapshot = freeze_snapshot(controller.conn, task_id, captured_at_ms=2000)
        projection = reduce_snapshot(snapshot)
        self.assertEqual(projection.invocations[0]["result_state"], "success")
        self.assertNotIn(
            "provider_result_unknown", {item["code"] for item in projection.unknowns}
        )

    def test_in_flight_claim_matches_canonical_running_revision(self) -> None:
        controller = self.controller()
        task_id = controller.submit("demo-loop", DEMO_PROFILE, DEMO_INPUT)
        controller.claim_stage(task_id)
        snapshot = freeze_snapshot(controller.conn, task_id, captured_at_ms=2000)
        projection = reduce_snapshot(snapshot)
        canonical = snapshot["canonical_state"]["task"]
        self.assertEqual(projection.task_lifecycle["status"], "running")
        self.assertEqual(projection.task_lifecycle["revision"], canonical["revision"])
        self.assertNotIn(
            "canonical_mismatch", {item["code"] for item in projection.diagnostics}
        )
        self.assertEqual(projection.integrity_status, "incomplete")

    def test_stage_attempt_projection_matches_canonical_identity_and_seal(self) -> None:
        controller, task_id = self.completed()
        snapshot = freeze_snapshot(controller.conn, task_id, captured_at_ms=3000)
        projection = reduce_snapshot(snapshot)
        projected = {item["run_token"]: item for item in projection.stage_attempts}
        for canonical in snapshot["canonical_state"]["stage_runs"]:
            attempt = projected[canonical["run_token"]]
            self.assertEqual(
                (attempt["stage"], attempt["cycle"], attempt["attempt"]),
                (canonical["stage"], canonical["cycle"], canonical["attempt"]),
            )
            self.assertEqual(attempt["status"], canonical["status"])
            self.assertEqual(attempt["sealed"], bool(canonical["sealed"]))
            self.assertEqual(attempt["manifest_hash"], canonical["manifest_hash"])

    def test_reducer_fails_closed_on_canonical_run_identity_and_seal_mismatch(self) -> None:
        controller, task_id = self.completed()
        identity = freeze_snapshot(controller.conn, task_id, captured_at_ms=3000)
        identity["canonical_state"]["stage_runs"][0]["attempt"] += 1
        source_hash = "sha256:" + hashlib.sha256(
            canonical_json_bytes(identity["canonical_state"])
        ).hexdigest()
        identity["source_db_snapshot_hash"] = source_hash
        identity["manifest"]["source_db_snapshot_hash"] = source_hash
        self.rehash_snapshot(identity)
        projection = reduce_snapshot(identity)
        self.assertEqual(projection.integrity_status, "corrupt")
        self.assertIn(
            "run_identity_mismatch", {item["code"] for item in projection.diagnostics}
        )

        seal = freeze_snapshot(controller.conn, task_id, captured_at_ms=3000)
        seal["canonical_state"]["stage_runs"][0]["manifest_hash"] = "0" * 64
        source_hash = "sha256:" + hashlib.sha256(
            canonical_json_bytes(seal["canonical_state"])
        ).hexdigest()
        seal["source_db_snapshot_hash"] = source_hash
        seal["manifest"]["source_db_snapshot_hash"] = source_hash
        self.rehash_snapshot(seal)
        projection = reduce_snapshot(seal)
        self.assertEqual(projection.integrity_status, "mismatch")
        self.assertIn(
            "canonical_mismatch", {item["code"] for item in projection.diagnostics}
        )

    def test_resumed_session_lineage_cumulative_usage_and_boundaries_are_golden(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        with patch.dict(os.environ, {"ORCH_TRAJECTORY_V1": "write"}):
            controller = Controller(
                Path(directory.name) / "runtime", runner=_RebindingReceiptRunner(),
            )
        self.addCleanup(controller.close)
        task_id = controller.submit("demo-loop", DEMO_PROFILE, DEMO_INPUT)
        self.assertEqual(controller.run_until_stop(task_id)["task"]["status"], "done")
        projection = reduce_snapshot(
            freeze_snapshot(controller.conn, task_id, captured_at_ms=3000)
        )
        self.assertEqual(projection.integrity_status, "ok")
        self.assertEqual(
            [item["event_type"] for item in projection.sessions],
            ["session.bound", "session.rebound"],
        )
        self.assertEqual(
            projection.sessions[1]["predecessor_event_id"],
            projection.sessions[0]["event_id"],
        )
        self.assertEqual(
            projection.sessions[1]["reason_code"], "provider_session_replaced"
        )
        self.assertEqual(set(projection.usage_by_basis), {"cumulative"})
        self.assertEqual(
            [item["total_tokens"] for item in projection.usage_by_basis["cumulative"]],
            [100, 200],
        )
        self.assertEqual(len(projection.model_visible_boundary_index), 2)
        self.assertEqual(
            {item["provider"] for item in projection.invocations}, {"codex", "claude"}
        )
        self.assertEqual(
            {item["model"] for item in projection.invocations}, {"unspecified"}
        )

    def test_counts_without_an_adapter_basis_are_not_labeled_per_turn(self) -> None:
        result = RunResult(
            0, "", None, "success", "success",
            usage_input_tokens=10, usage_output_tokens=5, usage_total_tokens=15,
        )
        self.assertEqual(
            Controller._trajectory_usage(result),
            {
                "basis": "unavailable",
                "input_tokens": None,
                "output_tokens": None,
                "total_tokens": None,
                "unavailable_reason_code": "usage_basis_unverified",
                "unavailable_reason_digest": None,
            },
        )

    @staticmethod
    def rehash_snapshot(snapshot: dict) -> None:
        snapshot["manifest"]["evidence_inventory_digest"] = (
            "sha256:" + hashlib.sha256(canonical_json_bytes(snapshot["evidence_inventory"])).hexdigest()
        )
        unhashed = {key: value for key, value in snapshot.items() if key != "snapshot_digest"}
        snapshot["snapshot_digest"] = (
            "sha256:" + hashlib.sha256(canonical_json_bytes(unhashed)).hexdigest()
        )

    @classmethod
    def reseal_event_chain(cls, snapshot: dict, events: list[dict]) -> None:
        previous = None
        encoded = []
        for event in events:
            candidate = copy.deepcopy(event)
            candidate.pop("event_hash", None)
            candidate["prev_event_hash"] = previous
            sealed = seal_event(candidate)
            previous = sealed["event_hash"]
            encoded.append(canonical_event_bytes(sealed).decode("utf-8"))
        snapshot["ordered_events"] = encoded
        snapshot["event_head_hash"] = previous
        snapshot["manifest"].update(
            event_count=len(events),
            first_seq=events[0]["seq"] if events else None,
            last_seq=events[-1]["seq"] if events else None,
            event_head_hash=previous,
            ordered_events_digest="sha256:" + hashlib.sha256(
                canonical_json_bytes(encoded)
            ).hexdigest(),
        )
        cls.rehash_snapshot(snapshot)

    def test_reducer_detects_task_revision_regression(self) -> None:
        controller, task_id = self.completed()
        snapshot = freeze_snapshot(controller.conn, task_id, captured_at_ms=3000)
        events = [json.loads(raw) for raw in snapshot["ordered_events"]]
        target = next(
            index for index in range(1, len(events))
            if max(item["task"]["revision"] for item in events[:index]) > 0
        )
        events[target]["task"]["revision"] = (
            max(item["task"]["revision"] for item in events[:target]) - 1
        )
        self.reseal_event_chain(snapshot, events)
        projection = reduce_snapshot(snapshot)
        self.assertEqual(projection.integrity_status, "corrupt")
        self.assertIn(
            "task_revision_regression", {item["code"] for item in projection.diagnostics}
        )

    def test_snapshot_isolation_stays_frozen_across_a_concurrent_writer_commit(self) -> None:
        controller = self.controller()
        controller.conn.execute("PRAGMA journal_mode=WAL")
        task_id = controller.submit("demo-loop", DEMO_PROFILE, DEMO_INPUT)
        reader = connect(controller.home / "orchestrator.db", read_only=True)
        self.addCleanup(reader.close)
        reader.execute("BEGIN")
        before = freeze_snapshot(reader, task_id, captured_at_ms=3000)

        status = controller.run_until_stop(task_id)
        self.assertEqual(status["task"]["status"], "done")
        during = freeze_snapshot(reader, task_id, captured_at_ms=3000)
        self.assertEqual(before, during)

        reader.execute("ROLLBACK")
        after = freeze_snapshot(reader, task_id, captured_at_ms=3000)
        self.assertNotEqual(before["snapshot_digest"], after["snapshot_digest"])
        self.assertGreater(len(after["ordered_events"]), len(before["ordered_events"]))

    def test_snapshot_survives_missing_artifact_root_and_reports_unavailable_refs(self) -> None:
        controller, task_id = self.completed()
        artifact_dir = Path(controller._task(task_id)["artifact_dir"])
        shutil.rmtree(artifact_dir)
        snapshot = freeze_snapshot(controller.conn, task_id, captured_at_ms=3000)
        self.assertTrue(snapshot["evidence_inventory"])
        self.assertTrue(all(
            item["availability"] in {"unavailable", "expired"}
            for item in snapshot["evidence_inventory"]
        ))
        projection = reduce_snapshot(snapshot)
        self.assertEqual(projection.integrity_status, "incomplete")

    def test_inventory_must_exactly_match_event_evidence_refs(self) -> None:
        controller, task_id = self.completed()
        snapshot = freeze_snapshot(controller.conn, task_id, captured_at_ms=3000)
        snapshot["evidence_inventory"].pop()
        self.rehash_snapshot(snapshot)
        projection = reduce_snapshot(snapshot)
        self.assertEqual(projection.integrity_status, "corrupt")
        self.assertIn(
            "evidence_inventory_mismatch",
            {item["code"] for item in projection.diagnostics},
        )

    def test_rehashed_malformed_canonical_state_still_fails_closed(self) -> None:
        controller, task_id = self.completed()
        snapshot = freeze_snapshot(controller.conn, task_id, captured_at_ms=3000)
        snapshot["canonical_state"] = {}
        source_hash = "sha256:" + hashlib.sha256(canonical_json_bytes({})).hexdigest()
        snapshot["source_db_snapshot_hash"] = source_hash
        snapshot["manifest"]["source_db_snapshot_hash"] = source_hash
        self.rehash_snapshot(snapshot)
        projection = reduce_snapshot(snapshot)
        self.assertEqual(projection.integrity_status, "corrupt")
        self.assertEqual(
            {item["code"] for item in projection.diagnostics}, {"snapshot_invalid"}
        )

    def test_default_render_compacts_sensitive_evidence_metadata(self) -> None:
        controller, task_id = self.completed()
        snapshot = freeze_snapshot(controller.conn, task_id, captured_at_ms=3000)
        projection = reduce_snapshot(snapshot)
        rendered = render_projection(snapshot, projection)
        sensitive_ids = {
            item["ref_id"]
            for item in snapshot["evidence_inventory"]
            if item["sensitivity"] == "sensitive"
        }
        compact = [
            item for item in rendered["projection"]["evidence_graph"]
            if item["ref_id"] in sensitive_ids
        ]
        self.assertTrue(compact)
        self.assertTrue(all(
            set(item) == {"ref_id", "sha256", "availability"} for item in compact
        ))

    def test_r0_core_has_no_io_or_process_side_effects_for_required_input_classes(self) -> None:
        controller, task_id = self.completed()
        valid = freeze_snapshot(controller.conn, task_id, captured_at_ms=3000)
        corrupt = copy.deepcopy(valid)
        corrupt["snapshot_digest"] = "sha256:" + "0" * 64
        missing = copy.deepcopy(valid)
        for item in missing["evidence_inventory"]:
            item["availability"] = "unavailable"
        self.rehash_snapshot(missing)
        future = copy.deepcopy(valid)
        future["snapshot_version"] = 2
        self.rehash_snapshot(future)

        with (
            patch("builtins.open", side_effect=AssertionError("filesystem read/write")),
            patch("sqlite3.connect", side_effect=AssertionError("sqlite connection")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("subprocess")),
            patch.object(socket, "socket", side_effect=AssertionError("network")),
            patch.object(tempfile, "NamedTemporaryFile", side_effect=AssertionError("tempfile")),
        ):
            outputs = []
            for candidate in (valid, corrupt, missing, future):
                result = reduce_snapshot(candidate)
                outputs.append(projection_bytes(render_projection(candidate, result)))
        self.assertEqual(len(outputs), 4)

    def test_trajectory_r0_cli_is_stdout_only_and_fails_closed(self) -> None:
        controller, task_id = self.completed()
        snapshot = freeze_snapshot(controller.conn, task_id, captured_at_ms=3000)

        def invoke(payload: object, mode: str = "read") -> tuple[int, str, str]:
            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                patch.dict(os.environ, {
                    "ORCH_TRAJECTORY_V1": mode,
                    "ORCH_HOME": str(controller.home),
                }),
                patch("sys.stdin", io.StringIO(json.dumps(payload))),
                patch("sys.stdout", stdout),
                patch("sys.stderr", stderr),
            ):
                code = cli_main(["trajectory-r0"])
            return code, stdout.getvalue(), stderr.getvalue()

        code, stdout, stderr = invoke(snapshot)
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(json.loads(stdout)["projection"]["integrity_status"], "ok")

        code, stdout, stderr = invoke({})
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(stdout)["projection"]["integrity_status"], "corrupt")
        self.assertNotIn("Traceback", stderr)

        corrupt = copy.deepcopy(snapshot)
        corrupt["snapshot_digest"] = "sha256:" + "0" * 64
        code, stdout, stderr = invoke(corrupt)
        self.assertEqual(code, 2)
        self.assertTrue(stdout)
        self.assertEqual(stderr, "")

        code, stdout, stderr = invoke(snapshot, mode="write")
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "orchestrator: trajectory-r0 failed\n")

        stdout_buffer = io.StringIO()
        stderr_buffer = io.StringIO()
        deeply_nested = "[" * 2000 + "0" + "]" * 2000
        with (
            patch.dict(os.environ, {
                "ORCH_TRAJECTORY_V1": "read",
                "ORCH_HOME": str(controller.home),
            }),
            patch("sys.stdin", io.StringIO(deeply_nested)),
            patch("sys.stdout", stdout_buffer),
            patch("sys.stderr", stderr_buffer),
        ):
            code = cli_main(["trajectory-r0"])
        self.assertEqual(code, 2)
        if stdout_buffer.getvalue():
            self.assertEqual(
                json.loads(stdout_buffer.getvalue())["projection"]["integrity_status"],
                "corrupt",
            )
            self.assertEqual(stderr_buffer.getvalue(), "")
        else:
            self.assertEqual(
                stderr_buffer.getvalue(), "orchestrator: trajectory-r0 failed\n"
            )

    def test_r0_cli_preserves_filesystem_and_sqlite_sidecar_metadata(self) -> None:
        controller, task_id = self.completed()
        controller.conn.execute("PRAGMA journal_mode=WAL")
        controller.conn.execute("BEGIN IMMEDIATE")
        controller.conn.execute(
            "UPDATE tasks SET updated_at=updated_at WHERE id=?", (task_id,)
        )
        controller.conn.execute("COMMIT")
        snapshot = freeze_snapshot(controller.conn, task_id, captured_at_ms=3000)

        def metadata(root: Path) -> dict[str, tuple[int, int, int, int]]:
            result = {}
            for path in sorted((root, *root.rglob("*"))):
                stat = path.stat(follow_symlinks=False)
                result[str(path.relative_to(root))] = (
                    stat.st_mode,
                    stat.st_ino,
                    stat.st_size,
                    stat.st_mtime_ns,
                )
            return result

        before = metadata(controller.home)
        self.assertTrue(any(name.endswith("-wal") for name in before))
        self.assertTrue(any(name.endswith("-shm") for name in before))
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch.dict(os.environ, {
                "ORCH_TRAJECTORY_V1": "read",
                "ORCH_HOME": str(controller.home),
            }),
            patch("sys.stdin", io.StringIO(json.dumps(snapshot))),
            patch("sys.stdout", stdout),
            patch("sys.stderr", stderr),
            patch("sqlite3.connect", side_effect=AssertionError("sqlite connection")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("subprocess")),
            patch.object(socket, "socket", side_effect=AssertionError("network")),
        ):
            code = cli_main(["trajectory-r0"])
        after = metadata(controller.home)
        self.assertEqual(code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(before, after)

    def test_evidence_adapter_rejects_missing_or_corrupt_manifest_and_wrong_binding(self) -> None:
        controller, task_id = self.completed()
        task = controller._task(task_id)
        run = controller.conn.execute(
            "SELECT * FROM stage_runs WHERE task_id=? ORDER BY started_at,rowid LIMIT 1",
            (task_id,),
        ).fetchone()
        manifest_path = Path(run["manifest_path"])
        original_manifest = manifest_path.read_bytes()
        original_hash = run["manifest_hash"]
        manifest = json.loads(original_manifest)
        final_path = Path(manifest["final_response_path"])
        original_final = final_path.read_bytes()

        valid = controller._trajectory_evidence_ref(
            task,
            run,
            final_path,
            kind="final-response",
            sensitivity="sensitive",
            seal_kind="db-committed-run-manifest",
            manifest_hash=original_hash,
        )
        self.assertEqual(valid["availability_at_append"], "present")

        outside = controller.home.parent / "outside-evidence.txt"
        outside.write_text("outside", encoding="utf-8")
        with self.assertRaisesRegex(ControllerError, "escaped the task artifact root"):
            controller._trajectory_evidence_ref(
                task, run, outside, kind="run-manifest", sensitivity="internal",
                seal_kind="db-committed-run-manifest", manifest_hash=original_hash,
            )

        symlink = Path(task["artifact_dir"]) / "manifest-link.json"
        symlink.symlink_to(manifest_path)
        with self.assertRaisesRegex(ControllerError, "symlink"):
            controller._trajectory_evidence_ref(
                task, run, symlink, kind="run-manifest", sensitivity="internal",
                seal_kind="db-committed-run-manifest", manifest_hash=original_hash,
            )

        manifest_path.unlink()
        with self.assertRaisesRegex(ControllerError, "regular task artifact"):
            controller._trajectory_evidence_ref(
                task, run, final_path, kind="final-response", sensitivity="sensitive",
                seal_kind="db-committed-run-manifest", manifest_hash=original_hash,
            )
        manifest_path.write_bytes(original_manifest)

        manifest_path.write_bytes(original_manifest + b"\n")
        with self.assertRaisesRegex(ControllerError, "manifest hash mismatch"):
            controller._trajectory_evidence_ref(
                task, run, final_path, kind="final-response", sensitivity="sensitive",
                seal_kind="db-committed-run-manifest", manifest_hash=original_hash,
            )
        manifest_path.write_bytes(original_manifest)

        for field, value, message in (
            ("schema_version", 4, "version is unsupported"),
            ("task_id", "other-task", "manifest binding mismatch"),
            ("run_token", "other-run", "manifest binding mismatch"),
        ):
            with self.subTest(field=field):
                changed = dict(manifest)
                changed[field] = value
                raw = json.dumps(changed, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
                manifest_path.write_bytes(raw)
                changed_hash = hashlib.sha256(raw).hexdigest()
                controller.conn.execute(
                    "UPDATE stage_runs SET manifest_hash=? WHERE run_token=?",
                    (changed_hash, run["run_token"]),
                )
                changed_run = controller.conn.execute(
                    "SELECT * FROM stage_runs WHERE run_token=?", (run["run_token"],)
                ).fetchone()
                with self.assertRaisesRegex(ControllerError, message) as caught:
                    controller._trajectory_evidence_ref(
                        task, changed_run, manifest_path, kind="run-manifest",
                        sensitivity="internal", seal_kind="db-committed-run-manifest",
                        manifest_hash=changed_hash,
                    )
                self.assertNotIn(SECRET_CANARY, str(caught.exception))
                manifest_path.write_bytes(original_manifest)
                controller.conn.execute(
                    "UPDATE stage_runs SET manifest_hash=? WHERE run_token=?",
                    (original_hash, run["run_token"]),
                )

        final_path.write_bytes(original_final + b"tampered")
        with self.assertRaisesRegex(ControllerError, "payload hash mismatch") as caught:
            controller._trajectory_evidence_ref(
                task, run, final_path, kind="final-response", sensitivity="sensitive",
                seal_kind="db-committed-run-manifest", manifest_hash=original_hash,

            )
        self.assertNotIn(SECRET_CANARY, str(caught.exception))
        final_path.write_bytes(original_final)

class CanonicalEmitPointTest(unittest.TestCase):
    """The controller's emit points against the canonical rows they mirror.

    These drive the real controller transactions rather than the store alone,
    because the property under test is not "an event validates" — the tests
    above already prove that — but "one canonical mutation produces exactly one
    event, in the same transaction that produced the row".
    """

    def controller(self, outcomes: list[str], *, mode: str = "write") -> Controller:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        with patch.dict(os.environ, {"ORCH_TRAJECTORY_V1": mode}):
            controller = Controller(Path(directory.name) / "runtime", runner=_ScriptedRunner(outcomes))
        self.addCleanup(controller.close)
        return controller

    @staticmethod
    def events(controller: Controller, task_id: str) -> list[dict]:
        return [
            json.loads(bytes(row["canonical_json"]))
            for row in controller.conn.execute(
                "SELECT canonical_json FROM trajectory_events WHERE trajectory_id=? ORDER BY seq",
                (f"task:{task_id}",),
            )
        ]

    @staticmethod
    def of_type(events: list[dict], event_type: str) -> list[dict]:
        return [event for event in events if event["event_type"] == event_type]

    def assert_exactly_once(self, controller: Controller, task_id: str) -> None:
        """Each canonical row is mirrored by exactly one event, and vice versa."""
        events = self.events(controller, task_id)
        rows = lambda sql: controller.conn.execute(sql, (task_id,)).fetchall()

        # task.created: one per task, carrying that task's frozen digests.
        created = self.of_type(events, "task.created")
        self.assertEqual(len(created), 1)
        task = controller.conn.execute(
            "SELECT profile_hash,input_hash FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        self.assertEqual(
            created[0]["body"],
            {
                "profile_digest": f"sha256:{task['profile_hash']}",
                "input_digest": f"sha256:{task['input_hash']}",
            },
        )

        # task.transition.committed: the multiset of mirrored sequences is the
        # set of committed transition sequences — no duplicate, no gap.
        committed = [
            event["body"]["transition_seq"]
            for event in self.of_type(events, "task.transition.committed")
        ]
        transition_seqs = [row["seq"] for row in rows("SELECT seq FROM transitions WHERE task_id=? ORDER BY seq")]
        self.assertEqual(sorted(committed), transition_seqs)
        self.assertEqual(len(set(committed)), len(committed))

        # stage.claimed: one per *leased* run. A run that never took a lease —
        # a provider-preflight stop writes one — was never claimed, so it
        # correctly has no claim event and no lease digest to carry.
        run_rows = rows(
            "SELECT run_token,lease_token,sealed,status FROM stage_runs WHERE task_id=? ORDER BY started_at,rowid"
        )
        leased = {row["run_token"]: row["lease_token"] for row in run_rows if row["lease_token"]}
        claimed = self.of_type(events, "stage.claimed")
        self.assertEqual(
            sorted(event["run"]["run_token"] for event in claimed), sorted(leased)
        )
        for event in claimed:
            self.assertEqual(
                event["body"]["lease_digest"], digest_text(leased[event["run"]["run_token"]])
            )

        # stage.settled / evidence.sealed: one per run that reached a terminal
        # status, and the seal event only for a run the DB records as sealed.
        settled_runs = {row["run_token"] for row in run_rows if row["status"] != "running"}
        sealed_runs = {row["run_token"] for row in run_rows if row["sealed"]}
        settled = self.of_type(events, "stage.settled")
        sealed = self.of_type(events, "evidence.sealed")
        self.assertEqual(sorted(event["run"]["run_token"] for event in settled), sorted(settled_runs))
        self.assertEqual(sorted(event["run"]["run_token"] for event in sealed), sorted(sealed_runs))
        for event in settled:
            self.assertEqual(event["body"]["sealed"], event["run"]["run_token"] in sealed_runs)

        # The chain itself stays contiguous across every emit point.
        self.assertEqual([event["seq"] for event in events], list(range(1, len(events) + 1)))

    def test_a_completed_task_emits_each_canonical_event_exactly_once(self):
        controller = self.controller(["submit", "allow"])
        task_id = controller.submit("demo-loop", DEMO_PROFILE, DEMO_INPUT)
        status = controller.run_until_stop(task_id)

        self.assertEqual(status["task"]["status"], "done")
        self.assert_exactly_once(controller, task_id)
        present = {
            event["event_type"] for event in self.events(controller, task_id)
        } & set(CANONICAL_EMIT_POINTS)
        self.assertEqual(present, set(CANONICAL_EMIT_POINTS))

    def test_a_capped_task_still_emits_one_event_per_canonical_transition(self):
        """The cap stop commits a transition from inside `claim_stage`.

        It is the one canonical transition written outside `commit_run`, so it
        is also the one most likely to be missed by an emit point bolted onto
        the success path.
        """
        controller = self.controller(["submit", "block", "submit", "block"])
        task_id = controller.submit("demo-loop", DEMO_PROFILE, DEMO_INPUT)
        status = controller.run_until_stop(task_id)

        self.assertEqual(status["task"]["stop_reason"], "edge_cap")
        self.assert_exactly_once(controller, task_id)

    def test_the_gate_off_leaves_the_lifecycle_and_the_event_table_untouched(self):
        controller = self.controller(["submit", "allow"], mode="off")
        task_id = controller.submit("demo-loop", DEMO_PROFILE, DEMO_INPUT)
        status = controller.run_until_stop(task_id)

        self.assertEqual(status["task"]["status"], "done")
        self.assertEqual(self.events(controller, task_id), [])

    def test_submit_preserves_committed_artifacts_on_post_commit_parity_failure(self):
        controller = self.controller([])
        failure = [{"code": "task_created_mismatch", "event_id": "x", "event_seq": 1}]
        with patch(
            "orchestrator.controller.parity_diagnostics", side_effect=[[], failure]
        ):
            with self.assertRaisesRegex(
                PostCommitTrajectoryError, "parity failed after commit"
            ):
                controller.submit("demo-loop", DEMO_PROFILE, DEMO_INPUT)

        task = controller.conn.execute("SELECT * FROM tasks").fetchone()
        self.assertIsNotNone(task)
        self.assertFalse(controller.conn.in_transaction)
        self.assertTrue(Path(task["artifact_dir"]).is_dir())
        self.assertTrue(Path(task["profile_snapshot_path"]).is_file())
        self.assertTrue(Path(task["input_snapshot_path"]).is_file())


class SameTransactionRollbackTest(unittest.TestCase):
    """A trajectory failure must take the canonical mutation down with it.

    The event store is additive audit state, so the dangerous failure is not a
    lost event: it is a committed canonical row whose event never landed, or an
    event whose row was rolled back. Both are tested here by failing the write
    at the last emit point of a `commit_run` that has already mutated
    `stage_runs`, `transitions` and `tasks`.
    """

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        with patch.dict(os.environ, {"ORCH_TRAJECTORY_V1": "write"}):
            self.controller = Controller(
                Path(directory.name) / "runtime", runner=_ScriptedRunner([]),
            )
        self.addCleanup(self.controller.close)
        self.task_id = self.controller.submit("demo-loop", DEMO_PROFILE, DEMO_INPUT)
        self.run_token, self.stage, self.profile, _ = self.controller.claim_stage(self.task_id)
        self.result = RunResult(
            0, "ORCHESTRATOR_OUTCOME: submit\n", "submit", "success", "stage_completed"
        )

    def snapshot(self) -> dict:
        def table(sql: str) -> list[dict]:
            return [dict(row) for row in self.controller.conn.execute(sql, (self.task_id,))]

        return {
            "tasks": table("SELECT * FROM tasks WHERE id=?"),
            "stage_runs": table("SELECT * FROM stage_runs WHERE task_id=? ORDER BY rowid"),
            "transitions": table("SELECT * FROM transitions WHERE task_id=? ORDER BY seq"),
            "events": table("SELECT * FROM trajectory_events WHERE task_id=? ORDER BY seq"),
        }

    def assert_rolled_back(self, before: dict) -> None:
        self.assertFalse(self.controller.conn.in_transaction)
        self.assertEqual(self.snapshot(), before)

    def test_a_failed_event_append_rolls_the_canonical_mutation_back(self):
        before = self.snapshot()
        real_append = TrajectoryStore.append

        def fail_at_seal(store, event):
            if event["event_type"] == "evidence.sealed":
                raise TrajectoryError("injected append failure")
            return real_append(store, event)

        with patch.object(TrajectoryStore, "append", fail_at_seal):
            with self.assertRaisesRegex(TrajectoryError, "injected append failure"):
                self.controller.commit_run(
                    self.task_id, self.run_token, self.result, self.profile
                )
        self.assert_rolled_back(before)

        # The same run then commits cleanly, which proves the rollback left no
        # half-written chain behind: a stale seq or prev hash would fail here.
        self.controller.commit_run(self.task_id, self.run_token, self.result, self.profile)
        after = self.snapshot()
        self.assertEqual(after["stage_runs"][0]["status"], "committed")
        self.assertEqual(len(after["transitions"]), len(before["transitions"]) + 1)
        self.assertEqual(
            [row["seq"] for row in after["events"]], list(range(1, len(after["events"]) + 1))
        )

    def test_a_parity_mismatch_rolls_back_before_the_commit(self):
        """Parity is checked while the transaction can still be abandoned."""
        before = self.snapshot()
        with patch(
            "orchestrator.controller.parity_diagnostics",
            return_value=[{"code": "transition_mismatch", "event_id": "x", "event_seq": 1}],
        ):
            with self.assertRaisesRegex(ControllerError, "parity failed before commit"):
                self.controller.commit_run(
                    self.task_id, self.run_token, self.result, self.profile
                )
        self.assert_rolled_back(before)
