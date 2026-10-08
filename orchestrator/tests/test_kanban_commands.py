"""T1: manual card commands and CAS (spec S3-S4, S6, S8; AC02/AC08/AC09).

Every test runs against a throwaway ORCH_HOME inside a temporary directory.
Nothing here starts a provider, submits a task or touches a real queue: the
point of this slice is that a card command is metadata only, so a test that
needed a runner would be testing the wrong thing.

The closeout stop-gate fixtures are built with the engine's own
``_write_gate_review_input`` and ``run_gate_decision``, not with hand-written
YAML, so the Done validator is checked against the artifacts the real
lifecycle produces rather than against this file's idea of them.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from orchestrator.controller import Controller
from orchestrator.cli import _kanban, _kanban_payload, build_parser
from orchestrator.daemon import _handle
from orchestrator.execution import PLAN_BEGIN
from orchestrator.ipc import atomic_write_json
from orchestrator.kanban.commands import (
    BINDING_BEGIN,
    BINDING_BLOCK_FIELDS,
    GATE_FORCING_RISK_KEYS,
    REQUIRED_RISK_KEYS,
    KanbanError,
    approval_hash,
    build_approval_payload,
    build_request,
    handle_request,
    payload_hash,
    render_closeout_binding,
)
from orchestrator.profile import canonical_json, load_profile
from orchestrator.runner import ENVELOPE_BEGIN
from orchestrator.start import _write_gate_review_input, _write_yaml, run_gate_decision


SPEC_TEXT = "# Change\n\nStatus: approved\n\nDo the bounded thing.\n"
ACCEPTANCE_TEXT = "- AC1: the bounded thing is observable\n"
REVIEW_TEXT = "Three-axis spec review: PASS/PASS/PASS\n"
PROFILE_TEXT = """version: 1
type: apply
initial_stage: apply
max_transitions: 1
stages:
  apply:
    owner: claude
    attempt_cap: 1
    timeout: 900
    prompt: bounded apply
    outcomes:
      applied: done
  done:
    terminal: done
edge_caps:
  apply.applied: 1
"""

CLEAN_RISK = {key: False for key in REQUIRED_RISK_KEYS}
GATED_RISK = {**CLEAN_RISK, "durable_state": True}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class KanbanCommandTestCase(unittest.TestCase):
    """Shared fixture: one home, one controller, one prepared worktree."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.controller = Controller(self.home)
        self.addCleanup(self.controller.close)

        self.repo = self.tmp / "repo"
        self.worktree = self.tmp / "worktree"
        self.common_dir = self.tmp / "repo.git"
        for directory in (self.repo, self.worktree, self.common_dir):
            directory.mkdir()
        self.spec = self.worktree / "spec.md"
        self.acceptance = self.worktree / "tasks.md"
        self.review = self.worktree / "spec-review.md"
        self.profile = self.worktree / "profile.yaml"
        self.spec.write_text(SPEC_TEXT, encoding="utf-8")
        self.acceptance.write_text(ACCEPTANCE_TEXT, encoding="utf-8")
        self.review.write_text(REVIEW_TEXT, encoding="utf-8")
        self.profile.write_text(PROFILE_TEXT, encoding="utf-8")

    # -- driving commands ------------------------------------------------

    def send(
        self,
        command: str,
        payload: dict,
        *,
        operation_id: str | None = None,
        controller: Controller | None = None,
    ) -> dict:
        payload = {"actor": "local:test", **payload}
        request = build_request(command, payload, operation_id=operation_id)
        return handle_request(controller or self.controller, request)

    def card_row(self, card_id: str) -> sqlite3.Row:
        row = self.controller.conn.execute(
            "SELECT * FROM kanban_cards WHERE card_id=?", (card_id,)
        ).fetchone()
        self.assertIsNotNone(row, f"no card {card_id}")
        return row

    def events(self, card_id: str | None = None) -> list[sqlite3.Row]:
        if card_id is None:
            return list(self.controller.conn.execute("SELECT * FROM kanban_events ORDER BY at"))
        return list(
            self.controller.conn.execute(
                "SELECT * FROM kanban_events WHERE card_id=? ORDER BY at", (card_id,)
            )
        )

    def scope_fields(self, **overrides) -> dict:
        fields = {
            "title": "Bounded thing",
            "priority": "normal",
            "repo_path": str(self.repo),
            "worktree_path": str(self.worktree),
            "git_common_dir": str(self.common_dir),
            "change_name": "t1-8-native-kanban",
            "spec_path": str(self.spec),
            "acceptance_path": str(self.acceptance),
            "spec_review_pointer": str(self.review),
            "spec_review_hash": _sha(REVIEW_TEXT),
            "base_head": "a" * 40,
            "candidate_fingerprint": "b" * 64,
            "risk": dict(CLEAN_RISK),
            "estimate_by_pool": {"claude": 200, "codex": 100},
            "allowed_commands": ["python3 -m unittest"],
            "profile_name": str(self.profile),
            "provider": "claude",
            "model": "claude-opus-5",
            "effort": "high",
            "routing_digest": "c" * 64,
            "config_digest": "d" * 64,
        }
        fields.update(overrides)
        return fields

    def make_card(self, **overrides) -> str:
        card_id = str(uuid.uuid4())
        result = self.send(
            "create", {"card_id": card_id, "fields": self.scope_fields(**overrides)}
        )
        self.assertEqual(result["result"], "accepted", result)
        return card_id

    def approved_card(self, **overrides) -> tuple[str, int]:
        card_id = self.make_card(**overrides)
        result = self.send("approve", {"card_id": card_id, "expected_revision": 0})
        self.assertEqual(result["result"], "accepted", result)
        return card_id, result["revision"]


class CardLifecycleTests(KanbanCommandTestCase):
    """AC02: approval, CAS, idempotency, history."""

    def test_create_starts_in_inbox_with_no_approval(self) -> None:
        card_id = self.make_card()
        card = self.card_row(card_id)
        self.assertEqual(card["manual_state"], "inbox")
        self.assertEqual(card["revision"], 0)
        self.assertEqual(card["approval_generation"], 0)
        self.assertIsNone(card["approval_hash"])
        self.assertIsNone(card["task_id"])

    def test_unapproved_card_cannot_reach_ready(self) -> None:
        """Only approve writes `ready`; no other command can name a state."""
        card_id = self.make_card()
        for field in ("manual_state", "approval_hash", "approval_generation", "task_id", "revision"):
            result = self.send(
                "edit",
                {"card_id": card_id, "expected_revision": 0, "fields": {field: "ready"}},
            )
            self.assertEqual(result["result"], "rejected")
            self.assertEqual(result["reason"], "unsupported_field")
        for command in ("withdraw", "return", "pause", "archive"):
            self.send(
                command,
                {"card_id": card_id, "expected_revision": self.card_row(card_id)["revision"]},
            )
            self.assertNotEqual(self.card_row(card_id)["manual_state"], "ready")

    def test_approve_moves_to_ready_and_freezes_the_payload(self) -> None:
        card_id, revision = self.approved_card()
        card = self.card_row(card_id)
        self.assertEqual(card["manual_state"], "ready")
        self.assertEqual(card["revision"], revision)
        self.assertEqual(card["approval_generation"], 1)
        self.assertEqual(card["approval_actor"], "local:test")
        self.assertEqual(card["spec_hash"], _sha(SPEC_TEXT))
        self.assertEqual(card["acceptance_hash"], _sha(ACCEPTANCE_TEXT))
        expected_profile_hash = hashlib.sha256(
            canonical_json(load_profile(self.profile).to_dict())
        ).hexdigest()
        self.assertEqual(card["profile_hash"], expected_profile_hash)

        event = self.controller.conn.execute(
            "SELECT * FROM kanban_events WHERE operation_id=?", (card["approval_event_id"],)
        ).fetchone()
        frozen = json.loads(event["payload"])
        self.assertEqual(approval_hash(frozen), card["approval_hash"])
        # The hash covers the payload and is not a member of it (S6).
        self.assertNotIn("approval_hash", frozen)
        # Display text is deliberately outside the approved scope.
        for excluded in ("title", "note", "priority"):
            self.assertNotIn(excluded, frozen)
        # No envelope and no execution plan in this MVP input (S6).
        self.assertFalse(frozen["interpretation_envelope"])
        self.assertFalse(frozen["execution_plan"])
        self.assertEqual(frozen["spec_text"], SPEC_TEXT)
        self.assertEqual(frozen["allowed_commands"], ["python3 -m unittest"])
        self.assertEqual(frozen["effort"], "high")
        self.assertEqual(frozen["routing_digest"], "c" * 64)
        self.assertEqual(frozen["config_digest"], "d" * 64)
        self.assertEqual(
            canonical_json(frozen["profile_snapshot"]),
            canonical_json(load_profile(self.profile).to_dict()),
        )

    def test_approval_hash_is_recomputable_from_committed_state(self) -> None:
        """A second process rebuilding the payload must get the same hash."""
        card_id, _ = self.approved_card()
        card = self.card_row(card_id)
        with sqlite3.connect(self.home / "orchestrator.db") as other:
            other.row_factory = sqlite3.Row
            fresh = other.execute(
                "SELECT * FROM kanban_cards WHERE card_id=?", (card_id,)
            ).fetchone()
            # Rebuild against the pre-approval generation the freeze saw.
            rebuilt = dict(fresh)
            rebuilt["approval_generation"] = fresh["approval_generation"] - 1
            payload, reason = build_approval_payload(rebuilt)
        self.assertIsNone(reason)
        self.assertEqual(approval_hash(payload), card["approval_hash"])

    def test_scope_edit_revokes_the_approval(self) -> None:
        card_id, revision = self.approved_card()
        other_spec = self.worktree / "spec2.md"
        other_spec.write_text(SPEC_TEXT, encoding="utf-8")
        result = self.send(
            "edit",
            {
                "card_id": card_id,
                "expected_revision": revision,
                "fields": {"spec_path": str(other_spec)},
            },
        )
        self.assertEqual(result["result"], "accepted", result)
        card = self.card_row(card_id)
        self.assertEqual(card["manual_state"], "inbox")
        self.assertIsNone(card["approval_hash"])
        self.assertIsNone(card["approval_actor"])
        self.assertIsNone(card["approval_event_id"])
        # The generation is kept: history must still say which world this came
        # from, and the next approve is generation 2.
        self.assertEqual(card["approval_generation"], 1)
        approve = self.send(
            "approve", {"card_id": card_id, "expected_revision": card["revision"]}
        )
        self.assertEqual(approve["approval_generation"], 2)

    def test_every_scope_field_revokes_and_descriptive_fields_do_not(self) -> None:
        for field, value in (
            ("repo_path", str(self.tmp)),
            ("worktree_path", str(self.tmp)),
            ("git_common_dir", str(self.tmp)),
            ("change_name", "other-change"),
            ("acceptance_path", str(self.review)),
            ("spec_review_pointer", str(self.spec)),
            ("spec_review_hash", _sha("other")),
            ("base_head", "c" * 40),
            ("candidate_fingerprint", "d" * 64),
            ("risk", dict(GATED_RISK)),
            ("estimate_by_pool", {"claude": 1}),
            ("allowed_commands", ["python3 -m unittest -v"]),
            ("profile_name", str(self.spec)),
            ("provider", "codex"),
            ("model", "gpt-5"),
            ("effort", "medium"),
            ("routing_digest", "e" * 64),
            ("config_digest", "f" * 64),
        ):
            with self.subTest(scope_field=field):
                card_id, revision = self.approved_card()
                self.send(
                    "edit",
                    {"card_id": card_id, "expected_revision": revision, "fields": {field: value}},
                )
                card = self.card_row(card_id)
                self.assertIsNone(card["approval_hash"], field)
                self.assertEqual(card["manual_state"], "inbox", field)

        for field, value in (("title", "renamed"), ("note", "a note"), ("priority", "high")):
            with self.subTest(descriptive_field=field):
                card_id, revision = self.approved_card()
                self.send(
                    "edit",
                    {"card_id": card_id, "expected_revision": revision, "fields": {field: value}},
                )
                card = self.card_row(card_id)
                self.assertIsNotNone(card["approval_hash"], field)
                self.assertEqual(card["manual_state"], "ready", field)

    def test_resent_identical_payload_applies_once(self) -> None:
        card_id = self.make_card()
        operation_id = str(uuid.uuid4())
        payload = {"card_id": card_id, "expected_revision": 0, "fields": {"note": "first"}}
        first = self.send("edit", payload, operation_id=operation_id)
        second = self.send("edit", payload, operation_id=operation_id)
        self.assertEqual(first["result"], "accepted")
        self.assertEqual(second["result"], "accepted")
        self.assertTrue(second["replayed"])
        self.assertEqual(first["revision"], second["revision"])
        self.assertEqual(self.card_row(card_id)["revision"], 1)
        self.assertEqual(
            len([e for e in self.events(card_id) if e["operation_id"] == operation_id]), 1
        )

    def test_same_operation_id_with_a_different_payload_is_refused(self) -> None:
        card_id = self.make_card()
        operation_id = str(uuid.uuid4())
        self.send(
            "edit",
            {"card_id": card_id, "expected_revision": 0, "fields": {"note": "first"}},
            operation_id=operation_id,
        )
        conflict = self.send(
            "edit",
            {"card_id": card_id, "expected_revision": 1, "fields": {"note": "second"}},
            operation_id=operation_id,
        )
        self.assertEqual(conflict["result"], "rejected")
        self.assertEqual(conflict["reason"], "idempotency_conflict")
        self.assertFalse(conflict["recorded"])
        self.assertEqual(self.card_row(card_id)["note"], "first")
        self.assertEqual(self.card_row(card_id)["revision"], 1)

    def test_same_operation_id_as_a_different_command_is_refused(self) -> None:
        card_id = self.make_card()
        operation_id = str(uuid.uuid4())
        payload = {"card_id": card_id, "expected_revision": 0}
        self.send("pause", payload, operation_id=operation_id)
        conflict = self.send("archive", payload, operation_id=operation_id)
        self.assertEqual(conflict["reason"], "idempotency_conflict")
        self.assertNotEqual(self.card_row(card_id)["manual_state"], "archived")

    def test_stale_revision_conflicts_and_is_recorded(self) -> None:
        card_id = self.make_card()
        self.send("edit", {"card_id": card_id, "expected_revision": 0, "fields": {"note": "one"}})
        stale = self.send(
            "edit", {"card_id": card_id, "expected_revision": 0, "fields": {"note": "two"}}
        )
        self.assertEqual(stale["result"], "rejected")
        self.assertEqual(stale["reason"], "revision_conflict")
        card = self.card_row(card_id)
        self.assertEqual(card["note"], "one")
        self.assertEqual(card["revision"], 1)
        recorded = [e for e in self.events(card_id) if e["result"] == "rejected"]
        self.assertEqual([e["reason"] for e in recorded], ["revision_conflict"])
        # A rejection records its result without moving the card (S3).
        self.assertIsNone(recorded[0]["result_revision"])
        self.assertEqual(recorded[0]["expected_revision"], 0)

    def test_concurrent_manual_events_have_a_unique_winner(self) -> None:
        card_id = self.make_card()
        barrier = threading.Barrier(2)
        results: list[dict] = []
        errors: list[BaseException] = []
        lock = threading.Lock()

        def attempt(index: int) -> None:
            # sqlite connections are thread-affine by default.  Construct each
            # competing daemon-like writer in the thread that uses it so this
            # test exercises BEGIN IMMEDIATE/CAS rather than sqlite's API guard.
            controller = Controller(self.home)
            try:
                payload = {
                    "card_id": card_id,
                    "expected_revision": 0,
                    "fields": {"note": f"writer-{index}"},
                }
                barrier.wait()
                outcome = self.send("edit", payload, controller=controller)
                with lock:
                    results.append(outcome)
            except BaseException as exc:
                with lock:
                    errors.append(exc)
            finally:
                controller.close()

        threads = [
            threading.Thread(target=attempt, args=(index,))
            for index in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
            self.assertFalse(thread.is_alive())

        self.assertEqual(errors, [])
        accepted = [r for r in results if r["result"] == "accepted"]
        rejected = [r for r in results if r["result"] == "rejected"]
        self.assertEqual(len(accepted), 1, results)
        self.assertEqual([r["reason"] for r in rejected], ["revision_conflict"])
        card = self.card_row(card_id)
        self.assertEqual(card["revision"], 1)
        self.assertIn(card["note"], {"writer-0", "writer-1"})

    def test_history_is_complete_without_any_task(self) -> None:
        card_id = self.make_card()
        self.send("edit", {"card_id": card_id, "expected_revision": 0, "fields": {"note": "n"}})
        self.send("edit", {"card_id": card_id, "expected_revision": 0, "fields": {"note": "x"}})
        self.send("approve", {"card_id": card_id, "expected_revision": 1})
        self.send("withdraw", {"card_id": card_id, "expected_revision": 2})
        self.send("return", {"card_id": card_id, "expected_revision": 3})

        history = self.events(card_id)
        self.assertEqual(
            [(e["kind"], e["result"]) for e in history],
            [
                ("create", "accepted"),
                ("edit", "accepted"),
                ("edit", "rejected"),
                ("approve", "accepted"),
                ("withdraw", "accepted"),
                ("return", "accepted"),
            ],
        )
        self.assertTrue(all(e["task_id"] is None for e in history))
        self.assertTrue(all(e["actor"] == "local:test" for e in history))
        # No task, stage run or transition was created by any of it.
        for table in ("tasks", "stage_runs", "transitions"):
            count = self.controller.conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]
            self.assertEqual(count, 0, table)

    def test_accepted_events_are_append_only(self) -> None:
        card_id = self.make_card()
        operation_id = self.events(card_id)[0]["operation_id"]
        with self.assertRaises(sqlite3.IntegrityError):
            self.controller.conn.execute(
                "UPDATE kanban_events SET reason='rewritten' WHERE operation_id=?", (operation_id,)
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.controller.conn.execute(
                "DELETE FROM kanban_events WHERE operation_id=?", (operation_id,)
            )


class ForgedApprovalTests(KanbanCommandTestCase):
    """AC02: approval wording inside an artifact is content, not a command."""

    def test_approval_text_in_an_agent_artifact_does_not_approve(self) -> None:
        card_id = self.make_card(
            note="The agent wrote: approved: true, approval_hash: " + "f" * 64
        )
        forged = self.worktree / "agent-report.md"
        forged.write_text("Status: approved\napproval_hash: " + "f" * 64 + "\n", encoding="utf-8")
        card = self.card_row(card_id)
        self.assertEqual(card["manual_state"], "inbox")
        self.assertIsNone(card["approval_hash"])
        self.assertEqual(card["approval_generation"], 0)

    def test_edit_cannot_write_approval_or_binding_fields(self) -> None:
        card_id = self.make_card()
        for field in ("approval_actor", "approval_at", "approval_event_id", "request_id",
                      "last_evidence_event_id", "created_at"):
            result = self.send(
                "edit", {"card_id": card_id, "expected_revision": 0, "fields": {field: "x"}}
            )
            self.assertEqual(result["reason"], "unsupported_field", field)

    def test_spec_without_an_explicit_marker_is_refused(self) -> None:
        self.spec.write_text(
            "# Change\n\nThe reviewer said this looks approved to them.\n", encoding="utf-8"
        )
        card_id = self.make_card()
        result = self.send("approve", {"card_id": card_id, "expected_revision": 0})
        self.assertEqual(result["reason"], "spec_not_approved")
        self.assertEqual(self.card_row(card_id)["manual_state"], "inbox")

    def test_incomplete_or_empty_approval_inputs_are_refused(self) -> None:
        cases = {
            "incomplete_risk": {"risk": {k: False for k in REQUIRED_RISK_KEYS[:-1]}},
            "invalid_estimate": {"estimate_by_pool": {"claude": -1}},
            "invalid_allowed_commands": {"allowed_commands": []},
            "spec_review_hash_mismatch": {"spec_review_hash": _sha("wrong")},
        }
        for reason, override in cases.items():
            with self.subTest(reason=reason):
                card_id = self.make_card(**override)
                result = self.send("approve", {"card_id": card_id, "expected_revision": 0})
                self.assertEqual(result["reason"], reason)

        for estimate in ({"person@example.com": 100}, {"codex": 0}, {"Codex": 100}):
            with self.subTest(estimate=estimate):
                card_id = self.make_card(estimate_by_pool=estimate)
                result = self.send("approve", {"card_id": card_id, "expected_revision": 0})
                self.assertEqual(result["reason"], "invalid_estimate")

        card_id = self.make_card(spec_path=str(self.worktree / "absent.md"))
        self.assertEqual(
            self.send("approve", {"card_id": card_id, "expected_revision": 0})["reason"],
            "missing_artifact:spec_path",
        )

        self.acceptance.write_text("   \n", encoding="utf-8")
        card_id = self.make_card()
        self.assertEqual(
            self.send("approve", {"card_id": card_id, "expected_revision": 0})["reason"],
            "acceptance_empty",
        )

    def test_invalid_profile_is_refused_before_approval(self) -> None:
        self.profile.write_text("name: not-an-engine-profile\n", encoding="utf-8")
        card_id = self.make_card()
        result = self.send("approve", {"card_id": card_id, "expected_revision": 0})
        self.assertEqual(result["reason"], "invalid_profile")
        self.assertIsNone(self.card_row(card_id)["approval_hash"])

    def test_missing_scope_is_refused_before_anything_is_frozen(self) -> None:
        fields = self.scope_fields()
        del fields["base_head"]
        card_id = str(uuid.uuid4())
        self.send("create", {"card_id": card_id, "fields": fields})
        result = self.send("approve", {"card_id": card_id, "expected_revision": 0})
        self.assertEqual(result["reason"], "missing_scope:base_head")


class ReservedFramingTests(KanbanCommandTestCase):
    """AC02: a reserved marker is refused, never silently stripped."""

    def test_card_text_carrying_a_reserved_marker_is_refused(self) -> None:
        for marker in (ENVELOPE_BEGIN, PLAN_BEGIN):
            with self.subTest(marker=marker):
                card_id = str(uuid.uuid4())
                result = self.send(
                    "create",
                    {"card_id": card_id, "fields": {"title": "t", "note": f"context {marker} more"}},
                )
                self.assertEqual(result["reason"], "unsupported_input_framing")
                self.assertIsNone(
                    self.controller.conn.execute(
                        "SELECT * FROM kanban_cards WHERE card_id=?", (card_id,)
                    ).fetchone()
                )

    def test_a_marker_in_a_json_field_is_refused(self) -> None:
        card_id = self.make_card()
        result = self.send(
            "edit",
            {
                "card_id": card_id,
                "expected_revision": 0,
                "fields": {"estimate_by_pool": {f"claude{ENVELOPE_BEGIN}": 1}},
            },
        )
        self.assertEqual(result["reason"], "unsupported_input_framing")

    def test_an_approved_source_carrying_a_marker_is_refused(self) -> None:
        self.spec.write_text(SPEC_TEXT + "\n" + ENVELOPE_BEGIN + "\n", encoding="utf-8")
        card_id = self.make_card()
        result = self.send("approve", {"card_id": card_id, "expected_revision": 0})
        self.assertEqual(result["reason"], "unsupported_input_framing")
        # Refused, not stripped: the file is untouched.
        self.assertIn(ENVELOPE_BEGIN, self.spec.read_text(encoding="utf-8"))


class ManualControlTests(KanbanCommandTestCase):
    """AC09: pending versus applied, and what a manual move must not do."""

    def test_pause_is_pending_and_keeps_the_card_where_it_is(self) -> None:
        card_id, revision = self.approved_card()
        result = self.send("pause", {"card_id": card_id, "expected_revision": revision})
        self.assertEqual(result["result"], "accepted")
        self.assertTrue(result["pending"])
        self.assertEqual(result["applies_at"], "next_stage_boundary")
        card = self.card_row(card_id)
        # A pause records intent; it does not claim a writer stopped.
        self.assertEqual(card["manual_state"], "ready")
        self.assertEqual(card["last_reason"], "manual_pause_pending")
        self.assertIsNotNone(card["approval_hash"])
        event = self.events(card_id)[-1]
        delta = json.loads(event["metadata_delta"])
        self.assertTrue(delta["pending"])
        self.assertEqual(delta["applies_at"], "next_stage_boundary")

    def test_return_invalidates_the_approval(self) -> None:
        card_id, revision = self.approved_card()
        result = self.send("return", {"card_id": card_id, "expected_revision": revision})
        self.assertEqual(result["result"], "accepted")
        card = self.card_row(card_id)
        self.assertEqual(card["manual_state"], "returned")
        self.assertIsNone(card["approval_hash"])
        self.assertEqual(card["approval_generation"], 1)
        self.assertEqual(card["last_reason"], "manually_returned")

    def test_withdraw_needs_an_approval_and_returns_the_card_to_inbox(self) -> None:
        card_id = self.make_card()
        self.assertEqual(
            self.send("withdraw", {"card_id": card_id, "expected_revision": 0})["reason"],
            "not_approved",
        )
        card_id, revision = self.approved_card()
        self.send("withdraw", {"card_id": card_id, "expected_revision": revision})
        card = self.card_row(card_id)
        self.assertEqual(card["manual_state"], "inbox")
        self.assertIsNone(card["approval_hash"])
        self.assertEqual(card["approval_generation"], 1)
        self.assertIsNone(card["spec_hash"])
        self.assertIsNone(card["acceptance_hash"])
        self.assertIsNone(card["profile_hash"])

    def test_archive_is_not_a_success_claim_and_is_terminal(self) -> None:
        card_id, revision = self.approved_card()
        self.send("archive", {"card_id": card_id, "expected_revision": revision})
        card = self.card_row(card_id)
        self.assertEqual(card["manual_state"], "archived")
        delta = json.loads(self.events(card_id)[-1]["metadata_delta"])
        self.assertFalse(delta["success_claimed"])
        later = self.send("edit", {"card_id": card_id, "expected_revision": card["revision"],
                                   "fields": {"note": "after"}})
        self.assertEqual(later["reason"], "card_is_terminal")

    def test_manual_moves_touch_no_lease_worktree_or_task(self) -> None:
        card_id, revision = self.approved_card()
        marker = self.worktree / "uncommitted.txt"
        marker.write_text("work in progress\n", encoding="utf-8")
        for command in ("pause", "return", "archive"):
            revision = self.card_row(card_id)["revision"]
            self.send(command, {"card_id": card_id, "expected_revision": revision})
        self.assertTrue(self.worktree.is_dir())
        self.assertTrue(marker.is_file())
        self.assertTrue(self.spec.is_file())
        for table in ("tasks", "stage_runs", "transitions"):
            count = self.controller.conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]
            self.assertEqual(count, 0, table)

    def test_a_command_for_an_unknown_card_is_recorded_not_crashed(self) -> None:
        result = self.send("edit", {"card_id": str(uuid.uuid4()), "expected_revision": 0,
                                    "fields": {"note": "x"}})
        self.assertEqual(result["reason"], "card_not_found")
        recorded = self.events()[-1]
        self.assertIsNone(recorded["card_id"])
        self.assertEqual(recorded["result"], "rejected")

    def test_a_malformed_request_is_an_error_not_a_silent_no_op(self) -> None:
        with self.assertRaises(KanbanError):
            handle_request(self.controller, {"command": "teleport", "operation_id": str(uuid.uuid4()),
                                             "payload": {"actor": "a"}})
        with self.assertRaises(KanbanError):
            handle_request(self.controller, {"command": "create", "operation_id": "not-a-uuid",
                                             "payload": {"actor": "a"}})
        with self.assertRaises(KanbanError):
            handle_request(self.controller, {"command": "create", "operation_id": str(uuid.uuid4()),
                                             "payload": {}})
        self.assertEqual(self.events(), [])


class DaemonAndCliTests(KanbanCommandTestCase):
    """The public CLI -> inbox -> daemon -> processed path, not just the store API."""

    def test_daemon_publishes_and_replays_one_kanban_operation(self) -> None:
        processing = self.home / "processing"
        processed = self.home / "processed"
        processing.mkdir()
        processed.mkdir()
        card_id = str(uuid.uuid4())
        operation_id = str(uuid.uuid4())
        payload = {
            "actor": "local:test",
            "card_id": card_id,
            "fields": self.scope_fields(),
        }

        def dispatch(request_id: str, submitted_payload: dict) -> dict:
            request = build_request(
                "create",
                submitted_payload,
                request_id=request_id,
                operation_id=operation_id,
            )
            request_path = processing / f"request-{request_id}.json"
            atomic_write_json(request_path, request)
            _handle(self.controller, request_path, processed)
            result_path = next(processed.glob(f"{request_path.stem}.*.result.json"))
            return json.loads(result_path.read_text(encoding="utf-8"))

        first = dispatch(str(uuid.uuid4()), payload)
        replay = dispatch(str(uuid.uuid4()), payload)
        conflict = dispatch(
            str(uuid.uuid4()),
            {**payload, "fields": {**payload["fields"], "title": "different"}},
        )

        self.assertNotIn("status", first)
        self.assertEqual(first["kanban"]["result"], "accepted")
        self.assertTrue(replay["kanban"]["replayed"])
        self.assertEqual(conflict["kanban"]["reason"], "idempotency_conflict")
        self.assertEqual(
            self.controller.conn.execute(
                "SELECT COUNT(*) AS c FROM kanban_events WHERE operation_id=?",
                (operation_id,),
            ).fetchone()["c"],
            1,
        )

    def test_daemon_records_malformed_expected_revision_without_poison_replay(self) -> None:
        processing = self.home / "processing"
        processed = self.home / "processed"
        processing.mkdir()
        processed.mkdir()
        request_id = str(uuid.uuid4())
        request = build_request(
            "edit",
            {
                "actor": "local:test",
                "card_id": str(uuid.uuid4()),
                "expected_revision": {"not": "an integer"},
                "fields": {"note": "invalid revision"},
            },
            request_id=request_id,
        )
        request_path = processing / f"request-{request_id}.json"
        atomic_write_json(request_path, request)

        _handle(self.controller, request_path, processed)

        result_path = next(processed.glob(f"{request_path.stem}.*.result.json"))
        outcome = json.loads(result_path.read_text(encoding="utf-8"))
        self.assertEqual(outcome["kanban"]["reason"], "card_not_found")
        self.assertFalse(request_path.exists())
        self.assertIsNone(self.events()[-1]["expected_revision"])

    def test_daemon_publishes_sqlite_errors_instead_of_leaving_poison_request(self) -> None:
        processing = self.home / "processing"
        processed = self.home / "processed"
        processing.mkdir()
        processed.mkdir()
        request_id = str(uuid.uuid4())
        request = build_request(
            "edit",
            {"actor": "local:test", "card_id": str(uuid.uuid4()), "expected_revision": 0},
            request_id=request_id,
        )
        request_path = processing / f"request-{request_id}.json"
        atomic_write_json(request_path, request)

        with patch(
            "orchestrator.daemon.handle_kanban_request",
            side_effect=sqlite3.ProgrammingError("malformed bind"),
        ):
            _handle(self.controller, request_path, processed)

        result_path = next(processed.glob(f"{request_path.stem}.*.result.json"))
        outcome = json.loads(result_path.read_text(encoding="utf-8"))
        self.assertIn("ProgrammingError: malformed bind", outcome["error"])
        self.assertFalse(request_path.exists())

    def test_cli_builds_json_fields_and_refuses_a_stopped_daemon(self) -> None:
        args = build_parser().parse_args(
            [
                "kanban", "create", "--card", str(uuid.uuid4()),
                "--title", "Card", "--risk", json.dumps(CLEAN_RISK),
                "--estimate", '{"claude":200}',
                "--allowed-commands", '["python3 -m unittest"]',
            ]
        )
        payload = _kanban_payload(args)
        self.assertEqual(payload["fields"]["estimate_by_pool"], {"claude": 200})
        self.assertEqual(payload["fields"]["allowed_commands"], ["python3 -m unittest"])
        with patch("orchestrator.cli.daemon_is_running", return_value=False):
            self.assertEqual(_kanban(self.home, args), 2)

    def test_cli_exit_code_reflects_daemon_result(self) -> None:
        args = build_parser().parse_args(
            ["kanban", "archive", "--card", str(uuid.uuid4()), "--expected-revision", "0"]
        )
        with (
            patch("orchestrator.cli.daemon_is_running", return_value=True),
            patch("orchestrator.cli.enqueue_request", return_value=self.home / "inbox" / "x.json"),
            patch(
                "orchestrator.cli.wait_for_result",
                return_value={"kanban": {"result": "accepted"}},
            ),
        ):
            self.assertEqual(_kanban(self.home, args), 0)
        with (
            patch("orchestrator.cli.daemon_is_running", return_value=True),
            patch("orchestrator.cli.enqueue_request", return_value=self.home / "inbox" / "x.json"),
            patch(
                "orchestrator.cli.wait_for_result",
                return_value={"kanban": {"result": "rejected", "reason": "revision_conflict"}},
            ),
        ):
            self.assertEqual(_kanban(self.home, args), 2)


class DoneGateTests(KanbanCommandTestCase):
    """AC08: Done is manual, bound to one candidate, and needs the real ALLOW."""

    def setUp(self) -> None:
        super().setUp()
        self.apply_report = self.tmp / "apply-report.md"
        self.review_report = self.tmp / "review-report.md"
        self.audit_report = self.tmp / "audit-report.md"
        for path, text in (
            (self.apply_report, "# Apply\n"),
            (self.review_report, "# Review: PASS\n"),
            (self.audit_report, "# Failure audit: PASS\n"),
        ):
            path.write_text(text, encoding="utf-8")

    # -- fixtures --------------------------------------------------------

    def binding(self, source_card_id: str, *, closeout_task_id: str, **overrides) -> dict:
        card = self.card_row(source_card_id)
        binding = {
            "night_task_id": str(uuid.uuid4()),
            "night_request_id": str(uuid.uuid4()),
            "night_id": "2026-09-23",
            "card_id": source_card_id,
            "approval_generation": card["approval_generation"],
            "approval_hash": card["approval_hash"],
            "input_hash": "1" * 64,
            "profile_hash": card["profile_hash"],
            "final_candidate_fingerprint": "9" * 64,
            "review_seal_hash": "2" * 64,
            "audit_seal_hash": "3" * 64,
            "apply_report_path": str(self.apply_report),
            "apply_report_hash": _sha(self.apply_report.read_text(encoding="utf-8")),
            "review_report_path": str(self.review_report),
            "review_report_hash": _sha(self.review_report.read_text(encoding="utf-8")),
            "audit_report_path": str(self.audit_report),
            "audit_report_hash": _sha(self.audit_report.read_text(encoding="utf-8")),
            "closeout_task_id": closeout_task_id,
            "terminal_done_at": "2020-01-01T00:00:00+00:00",
            "last_seal_at": "2020-01-01T00:00:01+00:00",
            "remaining_evidence": [],
        }
        binding.update(overrides)
        return binding

    def closeout(
        self,
        binding: dict,
        *,
        stop_gate: bool = True,
        decide: bool = True,
        binding_in_summary: bool = True,
    ) -> Path:
        """One real operator closeout lifecycle, built with the engine's writers."""
        closeout_task_id = binding["closeout_task_id"]
        block = render_closeout_binding(binding)
        description = "Closeout review for the night task.\n\n" + block
        gate = {"type": "stop_gate", "status": "pending", "stage": "waiting_user"}
        execution_result = {
            "gate_required": True,
            "lifecycle_stage": "waiting_user",
            "controller_lifecycle_stage": "done",
            "controller_status": "done",
            "controller_task_id": str(uuid.uuid4()),
            "request_id": str(uuid.uuid4()),
            "processed_result_path": str(self.home / "processed" / "closeout.result.json"),
        }
        routing = {
            "task_id": closeout_task_id,
            # start.py writes description.strip() here and nothing else; a
            # binding that reached only --scope never appears in it.
            "task_summary": description if binding_in_summary else "Closeout review for the night task.",
            "pattern": "claude_apply_codex_review",
            "executor": "claude",
            "reviewer": "codex",
            "route_source": "rule",
            "stop_gate": stop_gate,
            "gate": gate,
            "execution_result": execution_result,
            "execution": {"profile": "closeout.yaml", "request_path": "closeout.json"},
        }
        task_record = {
            "task_id": closeout_task_id,
            "task_type_hint": "review",
            "stage": "waiting_user",
            # The scope always carries the block, so "only in scope" is the
            # difference between the two fixtures, not its absence everywhere.
            "scope": "Closeout scope.\n\n" + block,
            "gate": gate,
            "execution_result": execution_result,
        }
        tasks = self.home / "tasks"
        task_path = tasks / f"{closeout_task_id}.yaml"
        routing_path = tasks / f"{closeout_task_id}-routing.yaml"
        _write_yaml(task_path, task_record)
        _write_yaml(routing_path, routing)
        _write_gate_review_input(
            home=self.home,
            task_id=closeout_task_id,
            task_path=task_path,
            routing_path=routing_path,
            task_record=task_record,
            routing=routing,
            gate=gate,
            execution_result=execution_result,
            expected_output_path=tasks / f"{closeout_task_id}-gate-review-output.md",
        )
        if decide:
            run_gate_decision(self.home, closeout_task_id, "ALLOW")
        return tasks / f"{closeout_task_id}-gate-decision.yaml"

    def done(self, card_id: str, binding: dict, decision_path: Path | None, **overrides) -> dict:
        payload = {
            "card_id": card_id,
            "expected_revision": self.card_row(card_id)["revision"],
            "binding": binding,
            "final_candidate_fingerprint": binding["final_candidate_fingerprint"],
        }
        if decision_path is not None:
            payload["gate_decision_path"] = str(decision_path)
            payload["gate_decision_hash"] = _sha(decision_path.read_text(encoding="utf-8"))
        payload.update(overrides)
        return self.send("done", payload)

    def gated_card(self) -> str:
        card_id, _ = self.approved_card(risk=dict(GATED_RISK))
        return card_id

    # -- the happy path --------------------------------------------------

    def test_gate_is_required_when_the_approved_risk_says_so(self) -> None:
        for key in GATE_FORCING_RISK_KEYS:
            with self.subTest(risk=key):
                card_id, _ = self.approved_card(risk={**CLEAN_RISK, key: True})
                event = self.controller.conn.execute(
                    "SELECT payload FROM kanban_events WHERE operation_id=?",
                    (self.card_row(card_id)["approval_event_id"],),
                ).fetchone()
                self.assertTrue(json.loads(event["payload"])["gate_required"])

    def test_done_accepts_a_complete_manual_allow_chain(self) -> None:
        card_id = self.gated_card()
        binding = self.binding(card_id, closeout_task_id=str(uuid.uuid4()))
        decision = self.closeout(binding)
        result = self.done(card_id, binding, decision)
        self.assertEqual(result["result"], "accepted", result)
        card = self.card_row(card_id)
        self.assertEqual(card["manual_state"], "done")
        self.assertEqual(card["last_evidence_event_id"], result["operation_id"])
        delta = json.loads(self.events(card_id)[-1]["metadata_delta"])
        self.assertEqual(delta["binding"]["closeout_task_id"], binding["closeout_task_id"])
        self.assertEqual(delta["gate_decision_hash"], _sha(decision.read_text(encoding="utf-8")))
        # Done is not deployment (S8).
        self.assertFalse(delta["deployed"])

    def test_done_accepts_the_idempotently_resynced_allow_projection(self) -> None:
        card_id = self.gated_card()
        binding = self.binding(card_id, closeout_task_id=str(uuid.uuid4()))
        decision = self.closeout(binding)
        tasks = self.home / "tasks"
        task_path = tasks / f"{binding['closeout_task_id']}.yaml"
        routing_path = tasks / f"{binding['closeout_task_id']}-routing.yaml"
        task_record = _routing_of(task_path)
        routing = _routing_of(routing_path)
        for record in (task_record, routing):
            execution = dict(record["execution_result"])
            execution["gate_required"] = False
            execution["lifecycle_stage"] = "done"
            record["execution_result"] = execution
        _write_yaml(task_path, task_record)
        _write_yaml(routing_path, routing)
        self.assertEqual(self.done(card_id, binding, decision)["result"], "accepted")

    def test_done_without_a_required_gate_when_the_approved_risk_allows_it(self) -> None:
        card_id, _ = self.approved_card()
        binding = self.binding(card_id, closeout_task_id=str(uuid.uuid4()))
        result = self.done(card_id, binding, None)
        self.assertEqual(result["result"], "accepted", result)
        self.assertEqual(self.card_row(card_id)["manual_state"], "done")

    # -- candidate and ALLOW hashes: absent and mismatched --------------

    def test_done_refuses_an_absent_or_mismatched_candidate_hash(self) -> None:
        card_id = self.gated_card()
        binding = self.binding(card_id, closeout_task_id=str(uuid.uuid4()))
        decision = self.closeout(binding)
        absent = self.done(card_id, binding, decision, final_candidate_fingerprint="")
        self.assertEqual(absent["reason"], "final_candidate_missing")
        wrong = self.done(card_id, binding, decision, final_candidate_fingerprint="e" * 64)
        self.assertEqual(wrong["reason"], "final_candidate_mismatch")
        self.assertEqual(self.card_row(card_id)["manual_state"], "ready")

    def test_done_refuses_an_absent_or_mismatched_allow_hash(self) -> None:
        card_id = self.gated_card()
        binding = self.binding(card_id, closeout_task_id=str(uuid.uuid4()))
        decision = self.closeout(binding)
        missing_path = self.done(card_id, binding, None)
        self.assertEqual(missing_path["reason"], "gate_decision_missing")
        absent = self.done(card_id, binding, decision, gate_decision_hash=None)
        self.assertEqual(absent["reason"], "gate_decision_hash_missing")
        wrong = self.done(card_id, binding, decision, gate_decision_hash="a" * 64)
        self.assertEqual(wrong["reason"], "gate_decision_hash_mismatch")
        # An ALLOW artifact edited after it was hashed no longer matches.
        decision.write_text(decision.read_text(encoding="utf-8") + "note: edited\n", encoding="utf-8")
        tampered = self.done(card_id, binding, None,
                             gate_decision_path=str(decision),
                             gate_decision_hash=_sha("something else"))
        self.assertEqual(tampered["reason"], "gate_decision_hash_mismatch")
        self.assertEqual(self.card_row(card_id)["manual_state"], "ready")

    def test_done_refuses_an_absent_or_mismatched_report_hash(self) -> None:
        card_id = self.gated_card()
        binding = self.binding(card_id, closeout_task_id=str(uuid.uuid4()),
                               review_report_hash=_sha("a different report"))
        decision = self.closeout(binding)
        self.assertEqual(self.done(card_id, binding, decision)["reason"], "report_hash_mismatch")

        binding = self.binding(card_id, closeout_task_id=str(uuid.uuid4()), audit_report_hash="")
        decision = self.closeout(binding)
        self.assertEqual(self.done(card_id, binding, decision)["reason"], "report_hash_missing")

        binding = self.binding(card_id, closeout_task_id=str(uuid.uuid4()),
                               apply_report_path=str(self.tmp / "absent.md"))
        decision = self.closeout(binding)
        self.assertEqual(self.done(card_id, binding, decision)["reason"], "report_missing")

    # -- the four named P0-C reason codes --------------------------------

    def test_gate_not_required_route(self) -> None:
        """A closeout lifecycle that was never routed as a stop gate."""
        card_id = self.gated_card()
        binding = self.binding(card_id, closeout_task_id=str(uuid.uuid4()))
        decision = self.closeout(binding, stop_gate=False, decide=False)
        # `gate-allow` itself refuses a non-stop-gate lifecycle, so the
        # artifact is written directly here: the point is that Done refuses it
        # even when an ALLOW file exists.
        _write_yaml(
            decision,
            {
                "type": "stop_gate_decision",
                "task_id": binding["closeout_task_id"],
                "decision": "ALLOW",
                "decided_at": "2026-09-23T10:00:00+08:00",
                "final_stage": "done",
                "decision_artifact_path": str(decision),
            },
        )
        self.assertEqual(self.done(card_id, binding, decision)["reason"], "gate_not_required_route")

    def test_gate_not_decided(self) -> None:
        """A gate-sync recommendation is never a gate-allow."""
        card_id = self.gated_card()
        binding = self.binding(card_id, closeout_task_id=str(uuid.uuid4()))
        decision = self.closeout(binding, decide=False)
        routing_path = self.home / "tasks" / f"{binding['closeout_task_id']}-routing.yaml"
        routing = json.loads(json.dumps(_routing_of(routing_path)))
        routing["gate_review_result"] = {"recommendation": "ALLOW", "source": "gate-sync"}
        routing["gate_review_execution"] = {"status": "done"}
        _write_yaml(routing_path, routing)
        _write_yaml(
            decision,
            {
                "type": "stop_gate_decision",
                "task_id": binding["closeout_task_id"],
                "decision": "ALLOW",
                "decided_at": "2026-09-23T10:00:00+08:00",
                "final_stage": "done",
                "decision_artifact_path": str(decision),
            },
        )
        self.assertEqual(self.done(card_id, binding, decision)["reason"], "gate_not_decided")

    def test_gate_block_is_not_an_allow(self) -> None:
        card_id = self.gated_card()
        binding = self.binding(card_id, closeout_task_id=str(uuid.uuid4()))
        decision = self.closeout(binding, decide=False)
        run_gate_decision(self.home, binding["closeout_task_id"], "BLOCK")
        self.assertEqual(self.done(card_id, binding, decision)["reason"], "gate_not_decided")

    def test_gate_decision_must_be_the_canonical_lifecycle_artifact(self) -> None:
        card_id = self.gated_card()
        binding = self.binding(card_id, closeout_task_id=str(uuid.uuid4()))
        decision = self.closeout(binding)
        outside = self.tmp / "copied-gate-decision.yaml"
        outside.write_bytes(decision.read_bytes())
        self.assertEqual(self.done(card_id, binding, outside)["reason"], "gate_binding_mismatch")

    def test_gate_task_and_routing_records_must_agree(self) -> None:
        card_id = self.gated_card()
        binding = self.binding(card_id, closeout_task_id=str(uuid.uuid4()))
        decision = self.closeout(binding)
        routing_path = self.home / "tasks" / f"{binding['closeout_task_id']}-routing.yaml"
        routing = _routing_of(routing_path)
        routing["task_id"] = str(uuid.uuid4())
        _write_yaml(routing_path, routing)
        self.assertEqual(self.done(card_id, binding, decision)["reason"], "gate_binding_mismatch")

    def test_gate_binding_mismatch_when_the_block_is_only_in_scope(self) -> None:
        card_id = self.gated_card()
        binding = self.binding(card_id, closeout_task_id=str(uuid.uuid4()))
        decision = self.closeout(binding, binding_in_summary=False)
        review_input = (
            self.home / "tasks" / f"{binding['closeout_task_id']}-gate-review-input.md"
        ).read_text(encoding="utf-8")
        # The block really is in the file - inside the task record's scope -
        # and it still must not count.
        self.assertIn(BINDING_BEGIN, review_input)
        self.assertEqual(self.done(card_id, binding, decision)["reason"], "gate_binding_mismatch")

    def test_gate_binding_mismatch_when_one_field_differs(self) -> None:
        card_id = self.gated_card()
        binding = self.binding(card_id, closeout_task_id=str(uuid.uuid4()))
        decision = self.closeout(binding)
        for field in BINDING_BLOCK_FIELDS:
            if field in {"card_id", "approval_generation", "approval_hash",
                         "final_candidate_fingerprint"}:
                continue  # these fail earlier, against committed card state
            with self.subTest(field=field):
                altered = {**binding, field: "changed-value"}
                result = self.done(card_id, altered, decision)
                self.assertIn(
                    result["reason"],
                    {"gate_binding_mismatch", "report_hash_mismatch", "report_missing",
                     "report_hash_missing", "seal_missing", "binding_mismatch"},
                    field,
                )
                self.assertNotEqual(result["result"], "accepted")

    def test_gate_stale_candidate_when_the_allow_predates_the_last_seal(self) -> None:
        card_id = self.gated_card()
        binding = self.binding(
            card_id,
            closeout_task_id=str(uuid.uuid4()),
            terminal_done_at="2030-01-01T00:00:00+00:00",
            last_seal_at="2030-01-01T00:00:01+00:00",
        )
        decision = self.closeout(binding)
        self.assertEqual(self.done(card_id, binding, decision)["reason"], "gate_stale_candidate")

    def test_closeout_task_id_reuse_is_refused(self) -> None:
        first = self.gated_card()
        closeout_task_id = str(uuid.uuid4())
        binding = self.binding(first, closeout_task_id=closeout_task_id)
        decision = self.closeout(binding)
        self.assertEqual(self.done(first, binding, decision)["result"], "accepted")

        second = self.gated_card()
        reused = self.binding(second, closeout_task_id=closeout_task_id)
        # Same lifecycle, new card: the description must still carry this
        # card's block, so the artifacts are rewritten for it.
        (self.home / "tasks" / f"{closeout_task_id}-gate-decision.yaml").unlink()
        decision = self.closeout(reused)
        self.assertEqual(self.done(second, reused, decision)["reason"], "closeout_task_id_reused")
        self.assertEqual(self.card_row(second)["manual_state"], "ready")

    def test_closeout_may_not_be_the_night_task_itself(self) -> None:
        card_id = self.gated_card()
        night_task_id = str(uuid.uuid4())
        binding = self.binding(card_id, closeout_task_id=night_task_id,
                               night_task_id=night_task_id)
        decision = self.closeout(binding)
        self.assertEqual(self.done(card_id, binding, decision)["reason"], "gate_binding_mismatch")

    # -- other Done predicates -------------------------------------------

    def test_done_refuses_a_stale_generation_or_another_card(self) -> None:
        card_id = self.gated_card()
        binding = self.binding(card_id, closeout_task_id=str(uuid.uuid4()),
                               approval_generation=99)
        decision = self.closeout(binding)
        self.assertEqual(self.done(card_id, binding, decision)["reason"], "binding_mismatch")

        binding = self.binding(card_id, closeout_task_id=str(uuid.uuid4()),
                               card_id=str(uuid.uuid4()))
        decision = self.closeout(binding)
        self.assertEqual(self.done(card_id, binding, decision)["reason"], "binding_mismatch")

    def test_done_refuses_a_card_that_was_never_approved(self) -> None:
        card_id = self.make_card()
        binding = self.binding(card_id, closeout_task_id=str(uuid.uuid4()))
        self.assertEqual(self.done(card_id, binding, None)["reason"], "card_never_approved")

    def test_done_refuses_any_remaining_evidence(self) -> None:
        card_id = self.gated_card()
        binding = self.binding(
            card_id,
            closeout_task_id=str(uuid.uuid4()),
            remaining_evidence=[{"axis": "verification_evidence", "note": "daemon restart"}],
        )
        decision = self.closeout(binding)
        self.assertEqual(
            self.done(card_id, binding, decision)["reason"], "remaining_evidence_incomplete"
        )

        binding = self.binding(
            card_id,
            closeout_task_id=str(uuid.uuid4()),
            remaining_evidence=[
                {"axis": "verification_evidence", "owner": "operator", "gate": "G1"}
            ],
        )
        decision = self.closeout(binding)
        self.assertEqual(
            self.done(card_id, binding, decision)["reason"],
            "remaining_evidence_incomplete",
        )

    def test_done_refuses_a_profile_hash_not_bound_to_the_card(self) -> None:
        card_id = self.gated_card()
        binding = self.binding(
            card_id,
            closeout_task_id=str(uuid.uuid4()),
            profile_hash="f" * 64,
        )
        decision = self.closeout(binding)
        self.assertEqual(self.done(card_id, binding, decision)["reason"], "binding_mismatch")

    def test_done_refuses_an_incomplete_binding(self) -> None:
        card_id = self.gated_card()
        binding = self.binding(card_id, closeout_task_id=str(uuid.uuid4()))
        del binding["input_hash"]
        self.assertEqual(self.done(card_id, binding, None)["reason"], "binding_incomplete")


def _routing_of(path: Path) -> dict:
    from orchestrator.start import _read_yaml

    return _read_yaml(path)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

# Approved report-progress slice: minimal synthetic writer, no daemon loop.
from types import SimpleNamespace
from orchestrator.db import connect as progress_connect
from orchestrator.kanban.read import snapshot as progress_snapshot
from orchestrator.kanban.view import project as progress_project, render as progress_render
from orchestrator.ipc import enqueue_request as progress_enqueue

class ReportProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.home=Path(self.temp.name);self.conn=progress_connect(self.home/'orchestrator.db');self.addCleanup(self.conn.close)
        def forbidden(*a,**k): raise AssertionError('execution forbidden')
        self.writer=SimpleNamespace(conn=self.conn,home=self.home,submit=forbidden,resume=forbidden,run=forbidden)
        self.conn.execute("INSERT INTO kanban_cards(card_id,title,priority,manual_state,created_at,updated_at) VALUES ('c','合成卡','normal','inbox',1,1)")
    def payload(self,**overrides):
        return {'card_id':'c','expected_revision':0,'actor':'assistant:synthetic','report_status':'in_progress','summary':'正在做隔離驗證','blocker':None,'decision':None,'next_step':'核對結果','source_refs':['artifact:<pointer>'],**overrides}
    def send(self,payload=None,operation=None,command='report-progress'):
        return handle_request(self.writer,build_request(command,payload or self.payload(),operation_id=operation))
    def card(self):return dict(self.conn.execute("SELECT * FROM kanban_cards WHERE card_id='c'").fetchone())
    def test_unbounded_revisions_reject_once_without_card_changes(self):
        before=self.card()
        for revision in (10**100, -(10**100), -1):
            for command in ('edit','report-progress','archive','create'):
                with self.subTest(revision=revision,command=command):
                    payload=self.payload(expected_revision=revision)
                    if command=='edit':payload={'actor':'synthetic','card_id':'c','expected_revision':revision,'fields':{'note':'bad'}}
                    if command=='create':payload={'actor':'synthetic','card_id':'new','expected_revision':revision,'fields':{'title':'bad'}}
                    operation=str(uuid.uuid4())
                    result=self.send(payload,operation,command)
                    self.assertEqual('rejected',result['result'])
                    self.assertEqual('invalid_expected_revision',result['reason'])
                    self.assertTrue(self.send(payload,operation,command)['replayed'])
                    event=self.conn.execute('SELECT expected_revision FROM kanban_events WHERE operation_id=?',(operation,)).fetchone()
                    self.assertIsNone(event[0])
                    self.assertEqual(before,self.card())
        self.assertEqual(12,self.conn.execute('SELECT count(*) FROM kanban_events').fetchone()[0])
        self.assertEqual(1,self.conn.execute('SELECT count(*) FROM kanban_cards').fetchone()[0])
    def test_max_revision_never_overflows_increment(self):
        maximum=(1<<63)-1
        self.conn.execute("UPDATE kanban_cards SET revision=? WHERE card_id='c'",(maximum,))
        before=self.card()
        result=self.send(self.payload(expected_revision=maximum))
        self.assertEqual('revision_exhausted',result['reason']);self.assertEqual(before,self.card())
        self.assertEqual(maximum,self.conn.execute('SELECT expected_revision FROM kanban_events').fetchone()[0])
    def test_replay_conflict_stale_and_handler_clock(self):
        operation=str(uuid.uuid4())
        with patch('orchestrator.kanban.commands._now_ms',return_value=100):
            first=self.send(operation=operation);replay=self.send(operation=operation)
        self.assertEqual('accepted',first['result']);self.assertTrue(replay['replayed'])
        self.assertEqual(1,self.card()['revision']);self.assertEqual(100,self.card()['updated_at'])
        self.assertEqual(100,self.conn.execute("SELECT at FROM kanban_events").fetchone()[0])
        self.assertEqual('idempotency_conflict',self.send(self.payload(summary='不同'),operation)['reason'])
        self.assertEqual('revision_conflict',self.send()['reason'])
        self.assertEqual(2,self.conn.execute('SELECT count(*) FROM kanban_events').fetchone()[0])
    def test_event_failure_rolls_back_card(self):
        before=self.card()
        self.conn.execute("CREATE TRIGGER reject_progress BEFORE INSERT ON kanban_events WHEN NEW.kind='report-progress' BEGIN SELECT RAISE(ABORT,'injected failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):self.send()
        self.assertEqual(before,self.card());self.assertEqual(0,self.conn.execute('SELECT count(*) FROM kanban_events').fetchone()[0]);self.assertFalse(self.conn.in_transaction)
    def test_task_night_approval_and_lifecycle_unchanged(self):
        self.conn.execute("INSERT INTO tasks(id,type,status,current_stage,profile_hash,input_hash,profile_snapshot_path,input_snapshot_path,artifact_dir,max_transitions,created_at,updated_at) VALUES ('t','apply','running','apply','p','i','','','',1,1,1)")
        event=str(uuid.uuid4());self.conn.execute("INSERT INTO kanban_events(operation_id,payload_hash,kind,card_id,actor,at,result,payload) VALUES (?, 'fixture','approve','c','fixture',1,'accepted','{}')",(event,))
        self.conn.execute("UPDATE kanban_cards SET manual_state='ready',task_id='t',approval_generation=1,approval_hash='h',approval_actor='fixture',approval_at=1,approval_event_id=? WHERE card_id='c'",(event,))
        self.conn.execute("INSERT INTO kanban_nights(night_id,window_start_ms,window_end_ms,card_id,approval_generation,approval_hash,task_id,request_id,workspace_dir,base_head,candidate_fingerprint,profile_hash,input_bytes,input_hash,pool_claims,reserved_at,phase) VALUES ('n',1,2,'c',1,'h','t','req','synthetic','base','finger','p',x'01','input','{}',1,'submitted')")
        before=self.card();records={table:[tuple(row) for row in self.conn.execute('SELECT * FROM '+table)] for table in ('tasks','kanban_nights','stage_runs','transitions')}
        result=self.send(self.payload(report_status='reported_done'))
        self.assertEqual('accepted',result['result'])
        self.assertEqual({k:v for k,v in before.items() if k not in ('revision','updated_at')},{k:v for k,v in self.card().items() if k not in ('revision','updated_at')})
        for table,rows in records.items():self.assertEqual(rows,[tuple(row) for row in self.conn.execute('SELECT * FROM '+table)])
        item=progress_project(progress_snapshot(self.home,at_ms=1000))[0]
        self.assertNotEqual('已驗證完成',item['group']);self.assertEqual('running',item['task']['status'])
        self.assertIn('助手回報完成（未驗證）',progress_render(progress_snapshot(self.home,at_ms=1000)))
    def test_scope_aba_and_generation_do_not_resurrect_report(self):
        self.send();self.send({'card_id':'c','expected_revision':1,'actor':'operator','fields':{'model':'different'}},command='edit')
        self.send({'card_id':'c','expected_revision':2,'actor':'operator','fields':{'model':None}},command='edit')
        item=progress_project(progress_snapshot(self.home,at_ms=1000))[0]
        self.assertIsNone(item['progress_report']);self.assertIn('只保留歷史',item['progress_report_note'])
        self.send(self.payload(expected_revision=3))
        self.conn.execute("UPDATE kanban_cards SET approval_generation=1 WHERE card_id='c'")
        self.assertIsNone(progress_project(progress_snapshot(self.home,at_ms=1000))[0]['progress_report'])
        self.assertEqual(2,self.conn.execute("SELECT count(*) FROM kanban_events WHERE kind='report-progress' AND result='accepted'").fetchone()[0])
    def test_descriptive_edit_and_latest_report(self):
        self.send();self.send({'card_id':'c','expected_revision':1,'actor':'operator','fields':{'title':'新名稱'}},command='edit')
        self.assertIsNotNone(progress_project(progress_snapshot(self.home,at_ms=1000))[0]['progress_report'])
        self.send(self.payload(expected_revision=2,summary='最新',report_status='reported_done'))
        snap=progress_snapshot(self.home,at_ms=1000);item=progress_project(snap)[0]
        self.assertEqual('最新',item['progress_report']['summary']);self.assertEqual('完成',item['group'])
        self.assertNotEqual('已驗',item['group']);self.assertIn('未驗收：完成證據未驗證，不列入已驗。',progress_render(snap))
        self.assertEqual('inbox',item['card']['manual_state']);self.assertEqual(progress_render(snap),progress_render(progress_snapshot(self.home,at_ms=1000)))
    def test_validation_never_writes_protected_fields(self):
        cases=[({'manual_state':'done'},'unsupported_progress_fields'),({'progress_binding':{}},'unsupported_progress_fields'),({'summary':''},'summary_required'),({'report_status':'verified_done'},'invalid_report_status'),({'source_refs':['']},'invalid_source_refs'),({'decision':42},'invalid_progress_text'),({'summary':ENVELOPE_BEGIN},'unsupported_input_framing')]
        before=self.card()
        for fields,reason in cases:self.assertEqual(reason,self.send(self.payload(**fields))['reason'])
        self.assertEqual(before,self.card())
    def test_single_inbox_dispatch_leaves_other_request_untouched(self):
        path=progress_enqueue(self.home,build_request('report-progress',self.payload()))
        other=self.home/'inbox'/'untouched.json';other.write_text('{"action":"run"}')
        _handle(self.writer,path,self.home/'processed')
        self.assertEqual('{"action":"run"}',other.read_text())
        outcome=json.loads(next((self.home/'processed').glob('*.result.json')).read_text())
        self.assertEqual('accepted',outcome['kanban']['result']);self.assertEqual(1,self.card()['revision'])

class ReportProgressConcurrencyTests(unittest.TestCase):
    setUp=ReportProgressTests.setUp
    payload=ReportProgressTests.payload
    send=ReportProgressTests.send
    card=ReportProgressTests.card
    # Deliberately run only additional cases; base behavior is already covered.
    def test_two_revision_writers_one_winner(self):
        barrier=threading.Barrier(2);results=[];errors=[]
        def writer_thread(index):
            conn=progress_connect(self.home/'orchestrator.db',read_only=True)
            conn.close()
            # Schema already exists. This test's second writer connection does
            # not create/migrate it and is only synthetic race injection.
            conn=sqlite3.connect(self.home/'orchestrator.db',isolation_level=None,timeout=5)
            conn.row_factory=sqlite3.Row
            try:
                barrier.wait(timeout=5)
                results.append(handle_request(SimpleNamespace(conn=conn,home=self.home),build_request('report-progress',self.payload(summary='writer '+str(index)))))
            except Exception as exc:errors.append(exc)
            finally:conn.close()
        threads=[threading.Thread(target=writer_thread,args=(i,)) for i in range(2)]
        for thread in threads:thread.start()
        for thread in threads:thread.join(timeout=10)
        self.assertFalse(any(thread.is_alive() for thread in threads));self.assertEqual([],errors)
        self.assertEqual(['accepted','rejected'],sorted(row['result'] for row in results));self.assertEqual(1,self.card()['revision'])
        self.assertEqual(2,self.conn.execute('SELECT count(*) FROM kanban_events').fetchone()[0])
    def test_replay_after_scope_change_stays_historical(self):
        operation=str(uuid.uuid4());self.send(operation=operation)
        self.send({'card_id':'c','expected_revision':1,'actor':'operator','fields':{'model':'new'}},command='edit')
        outcome=self.send(operation=operation)
        self.assertTrue(outcome['replayed']);self.assertEqual(2,self.card()['revision'])
        self.assertIsNone(progress_project(progress_snapshot(self.home,at_ms=1000))[0]['progress_report'])
