"""T1.8 T2: manual quota snapshots and deterministic budget (S5 / AC04)."""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from orchestrator.cli import _kanban_payload, build_parser
from orchestrator.controller import Controller
from orchestrator.kanban.commands import build_request, handle_request
from orchestrator.kanban.quota import (
    DAYTIME_RESERVE_BP,
    claims_after_watermark,
    evaluate_pool,
    evaluate_pools,
    latest_snapshot,
)

NOW = 2_000_000_000_000
DAY_MS = 24 * 60 * 60 * 1000


def snapshot(**overrides):
    value = {
        "snapshot_id": str(uuid.uuid4()),
        "pool_key": "codex-main",
        "weekly_remaining_bp": 10000,
        "observed_at": NOW - DAY_MS,
        "recorded_at": NOW - DAY_MS,
        "reset_at": NOW + 6 * DAY_MS,
        "source": "manual",
        "operator": "local:test",
        "covered_claim_seq": 0,
        "stale": 0,
    }
    value.update(overrides)
    return value


class PureBudgetTests(unittest.TestCase):
    def test_cap_and_exact_integer_boundary(self) -> None:
        accepted = evaluate_pool(snapshot(), estimate_bp=800, remaining_windows=7, at_ms=NOW)
        refused = evaluate_pool(snapshot(), estimate_bp=801, remaining_windows=7, at_ms=NOW)
        self.assertTrue(accepted["eligible"])
        self.assertEqual(accepted["budget_bp"], 800)
        self.assertEqual(refused["reason"], "quota_estimate_exceeds_budget")

    def test_budget_divides_only_the_amount_above_reserve(self) -> None:
        result = evaluate_pool(
            snapshot(weekly_remaining_bp=4000), estimate_bp=100,
            remaining_windows=5, at_ms=NOW,
        )
        self.assertTrue(result["eligible"])
        self.assertEqual(result["budget_bp"], 100)
        self.assertEqual(
            evaluate_pool(snapshot(weekly_remaining_bp=4000), estimate_bp=101,
                          remaining_windows=5, at_ms=NOW)["reason"],
            "quota_estimate_exceeds_budget",
        )

    def test_reserve_is_strict(self) -> None:
        result = evaluate_pool(
            snapshot(weekly_remaining_bp=DAYTIME_RESERVE_BP),
            estimate_bp=1, remaining_windows=1, at_ms=NOW,
        )
        self.assertEqual(result["remaining_bp"], DAYTIME_RESERVE_BP)
        self.assertEqual(result["reason"], "quota_reserve")

    def test_reset_reason_precedes_stale(self) -> None:
        result = evaluate_pool(
            snapshot(observed_at=NOW - 7 * DAY_MS, reset_at=NOW, stale=1),
            estimate_bp=1, remaining_windows=1, at_ms=NOW,
        )
        self.assertEqual(result["reason"], "quota_reset_passed")

    def test_age_policy_boundary_and_future_observation_fail_closed(self) -> None:
        stale = evaluate_pool(
            snapshot(observed_at=NOW - 6 * DAY_MS, reset_at=NOW + DAY_MS),
            estimate_bp=1, remaining_windows=1, at_ms=NOW,
            snapshot_max_age_ms=6 * DAY_MS,
        )
        future = evaluate_pool(
            snapshot(observed_at=NOW + 1, reset_at=NOW + DAY_MS),
            estimate_bp=1, remaining_windows=1, at_ms=NOW,
        )
        self.assertEqual(stale["reason"], "quota_stale")
        self.assertEqual(future["reason"], "quota_invalid")

    def test_debits_after_watermark_are_never_refunded(self) -> None:
        claims = [
            {"claim_seq": 3, "debit_bp": 700, "phase": "stopped"},
            {"claim_seq": 4, "debit_bp": 600, "phase": "unknown"},
        ]
        result = evaluate_pool(
            snapshot(weekly_remaining_bp=5000, covered_claim_seq=3),
            estimate_bp=300, remaining_windows=4, claims=claims, at_ms=NOW,
        )
        self.assertEqual(result["debited_bp"], 600)
        self.assertEqual(result["remaining_bp"], 4400)
        self.assertEqual(result["budget_bp"], 225)
        self.assertEqual(result["reason"], "quota_estimate_exceeds_budget")

    def test_multi_pool_requires_all_and_merges_duplicate_roles(self) -> None:
        results = evaluate_pools(
            ["codex-main", "codex-main", "claude-review"],
            {
                "codex-main": snapshot(pool_key="codex-main"),
                "claude-review": snapshot(pool_key="claude-review", weekly_remaining_bp=3500),
            },
            {"codex-main": 800, "claude-review": 1},
            remaining_windows=7,
            at_ms=NOW,
        )
        self.assertFalse(results["eligible"])
        self.assertEqual(set(results["pools"]), {"codex-main", "claude-review"})
        self.assertEqual(results["pools"]["claude-review"]["reason"], "quota_reserve")

    def test_missing_snapshot_or_estimate_is_missing(self) -> None:
        no_snapshot = evaluate_pools(
            ["codex-main"], {}, {"codex-main": 10}, remaining_windows=1, at_ms=NOW
        )
        no_estimate = evaluate_pools(
            ["codex-main"], {"codex-main": snapshot()}, {},
            remaining_windows=1, at_ms=NOW,
        )
        self.assertEqual(no_snapshot["pools"]["codex-main"]["reason"], "quota_missing")
        self.assertEqual(no_estimate["pools"]["codex-main"]["reason"], "quota_missing")


class ManualSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "home"
        self.home.mkdir()
        self.controller = Controller(self.home)
        self.addCleanup(self.controller.close)

    def send(self, command: str, payload: dict, *, operation_id: str | None = None):
        request = build_request(
            command, {"actor": "local:test", **payload}, operation_id=operation_id
        )
        with patch("orchestrator.kanban.quota.now_ms", return_value=NOW):
            return handle_request(self.controller, request)

    def record_snapshot(self, **overrides):
        payload = {
            "snapshot_id": str(uuid.uuid4()),
            "pool_key": "codex-main",
            "weekly_remaining_bp": 9000,
            "observed_at": NOW - 1000,
            "reset_at": NOW + DAY_MS,
        }
        payload.update(overrides)
        return self.send("quota-snapshot", payload), payload

    def create_card(self) -> str:
        card_id = str(uuid.uuid4())
        result = self.send(
            "create",
            {"card_id": card_id, "fields": {"title": "quota fixture", "priority": "normal"}},
        )
        self.assertEqual(result["result"], "accepted")
        return card_id

    def insert_claim(
        self,
        *,
        pool: str = "codex-main",
        phase: str,
        safe_stop_at: int | None = None,
        card_id: str | None = None,
        commit: bool = True,
    ) -> int:
        card_id = card_id or self.create_card()
        task_id = str(uuid.uuid4())
        evidence_id = None
        if safe_stop_at is not None:
            evidence_id = str(uuid.uuid4())
            self.controller.conn.execute(
                "INSERT INTO kanban_events(operation_id,payload_hash,kind,card_id,task_id,"
                "expected_revision,result_revision,actor,at,result,reason,payload,metadata_delta)"
                " VALUES(?,?,?,?,?,NULL,NULL,?,?,'accepted',NULL,NULL,NULL)",
                (evidence_id, "e" * 64, "night_stopped", card_id, task_id,
                 "local:test", safe_stop_at),
            )
        cursor = self.controller.conn.execute(
            "INSERT INTO kanban_nights("
            "night_id,window_start_ms,window_end_ms,card_id,approval_generation,approval_hash,"
            "task_id,request_id,workspace_dir,base_head,candidate_fingerprint,profile_hash,"
            "input_bytes,input_hash,pool_claims,reserved_at,phase,stop_reason,stop_evidence_event_id)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                str(uuid.uuid4()), NOW - DAY_MS, NOW + DAY_MS, card_id, 1, "a" * 64,
                task_id, str(uuid.uuid4()), "/tmp/worktree", "b" * 40, "c" * 64,
                "d" * 64, b"{}", "e" * 64,
                json.dumps({pool: {"snapshot_id": "old", "debit_bp": 100, "budget_bp": 100}}),
                NOW - 5000, phase, "test_stop" if phase in {"stopped", "unknown"} else None,
                evidence_id,
            ),
        )
        if commit:
            self.controller.conn.commit()
        return cursor.lastrowid

    def test_snapshot_records_manual_source_and_current_watermark(self) -> None:
        result, payload = self.record_snapshot(snapshot_id="codex-main-2026w39")
        self.assertEqual(result["result"], "accepted")
        row = self.controller.conn.execute(
            "SELECT * FROM kanban_quota_snapshots WHERE snapshot_id=?",
            (payload["snapshot_id"],),
        ).fetchone()
        self.assertEqual(row["source"], "manual")
        self.assertEqual(row["covered_claim_seq"], 0)

    def test_same_operation_replays_and_changed_payload_conflicts(self) -> None:
        operation_id = str(uuid.uuid4())
        payload = {
            "snapshot_id": str(uuid.uuid4()), "pool_key": "codex-main",
            "weekly_remaining_bp": 9000, "observed_at": NOW - 1,
            "reset_at": NOW + DAY_MS,
        }
        first = self.send("quota-snapshot", payload, operation_id=operation_id)
        replay = self.send("quota-snapshot", payload, operation_id=operation_id)
        conflict = self.send(
            "quota-snapshot", {**payload, "weekly_remaining_bp": 8000},
            operation_id=operation_id,
        )
        self.assertEqual(first["result"], "accepted")
        self.assertTrue(replay["replayed"])
        self.assertEqual(conflict["reason"], "idempotency_conflict")

    def test_invalidation_preserves_row_and_is_idempotent(self) -> None:
        _result, payload = self.record_snapshot()
        operation_id = str(uuid.uuid4())
        first = self.send(
            "quota-invalidate", {"snapshot_id": payload["snapshot_id"]},
            operation_id=operation_id,
        )
        replay = self.send(
            "quota-invalidate", {"snapshot_id": payload["snapshot_id"]},
            operation_id=operation_id,
        )
        row = self.controller.conn.execute(
            "SELECT * FROM kanban_quota_snapshots WHERE snapshot_id=?",
            (payload["snapshot_id"],),
        ).fetchone()
        self.assertEqual(first["result"], "accepted")
        self.assertTrue(replay["replayed"])
        self.assertEqual(row["stale"], 1)
        self.assertEqual(row["stale_event_id"], operation_id)

    def test_reserved_claim_blocks_replacement(self) -> None:
        self.insert_claim(phase="reserved")
        result, _payload = self.record_snapshot()
        self.assertEqual(result["reason"], "quota_claim_active")

    def test_unknown_claim_blocks_replacement(self) -> None:
        self.insert_claim(phase="unknown")
        result, _payload = self.record_snapshot()
        self.assertEqual(result["reason"], "quota_claim_active")

    def test_claim_transaction_wins_race_before_snapshot_watermark(self) -> None:
        card_id = self.create_card()
        self.controller.conn.execute("BEGIN IMMEDIATE")
        self.insert_claim(phase="reserved", card_id=card_id, commit=False)

        started = threading.Event()
        results: list[dict] = []

        def record_after_lock() -> None:
            started.set()
            other = Controller(self.home)
            request = build_request(
                "quota-snapshot",
                {
                    "actor": "local:test",
                    "snapshot_id": str(uuid.uuid4()),
                    "pool_key": "codex-main",
                    "weekly_remaining_bp": 9000,
                    "observed_at": NOW - 1,
                    "reset_at": NOW + DAY_MS,
                },
            )
            try:
                with patch("orchestrator.kanban.quota.now_ms", return_value=NOW):
                    results.append(handle_request(other, request))
            finally:
                other.close()

        worker = threading.Thread(target=record_after_lock)
        worker.start()
        self.assertTrue(started.wait(1))
        self.controller.conn.execute("COMMIT")
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results[0]["reason"], "quota_claim_active")
        self.assertEqual(
            self.controller.conn.execute(
                "SELECT COUNT(*) AS c FROM kanban_quota_snapshots"
            ).fetchone()["c"],
            0,
        )

    def test_observation_must_follow_verified_stop_and_covers_claim(self) -> None:
        claim_seq = self.insert_claim(phase="stopped", safe_stop_at=NOW - 500)
        early, _ = self.record_snapshot(observed_at=NOW - 501)
        accepted, payload = self.record_snapshot(observed_at=NOW - 500)
        self.assertEqual(early["reason"], "quota_observation_before_claim_stopped")
        self.assertEqual(accepted["result"], "accepted")
        row = self.controller.conn.execute(
            "SELECT covered_claim_seq FROM kanban_quota_snapshots WHERE snapshot_id=?",
            (payload["snapshot_id"],),
        ).fetchone()
        self.assertEqual(row["covered_claim_seq"], claim_seq)

    def test_stopped_claim_without_evidence_does_not_disappear_from_watermark(self) -> None:
        self.insert_claim(phase="stopped")
        result, _payload = self.record_snapshot()
        self.assertEqual(result["reason"], "quota_claim_stop_unverified")

    def test_newest_snapshot_remains_current_after_invalidation(self) -> None:
        _first, old = self.record_snapshot(weekly_remaining_bp=9000)
        _second, new = self.record_snapshot(weekly_remaining_bp=7000)
        current = latest_snapshot(self.controller.conn, "codex-main")
        self.assertEqual(current["snapshot_id"], new["snapshot_id"])
        self.send("quota-invalidate", {"snapshot_id": new["snapshot_id"]})
        current = latest_snapshot(self.controller.conn, "codex-main")
        self.assertEqual(current["snapshot_id"], new["snapshot_id"])
        self.assertEqual(current["stale"], 1)
        self.assertNotEqual(current["snapshot_id"], old["snapshot_id"])

    def test_claim_loader_keeps_stopped_and_unknown_debits_after_watermark(self) -> None:
        stopped = self.insert_claim(phase="stopped", safe_stop_at=NOW - 500)
        unknown = self.insert_claim(phase="unknown")
        claims = claims_after_watermark(self.controller.conn, "codex-main", stopped - 1)
        self.assertEqual([item["claim_seq"] for item in claims], [stopped, unknown])
        self.assertEqual([item["debit_bp"] for item in claims], [100, 100])

    def test_invalid_pool_future_observation_and_bad_reset_are_recorded_refusals(self) -> None:
        cases = (
            {"pool_key": "person@example.com"},
            {"observed_at": NOW + 1},
            {"reset_at": NOW + 8 * DAY_MS},
        )
        for change in cases:
            with self.subTest(change=change):
                result, _payload = self.record_snapshot(**change)
                self.assertEqual(result["reason"], "quota_invalid")
                self.assertTrue(result["recorded"])
        self.assertEqual(
            self.controller.conn.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()["c"],
            0,
        )

    def test_cli_builds_snapshot_and_invalidation_payloads(self) -> None:
        snapshot_id = str(uuid.uuid4())
        args = build_parser().parse_args(
            [
                "kanban", "quota-snapshot", "--snapshot", snapshot_id,
                "--pool", "codex-main", "--remaining-bp", "9000",
                "--observed-at-ms", str(NOW - 1), "--reset-at-ms", str(NOW + DAY_MS),
            ]
        )
        payload = _kanban_payload(args)
        self.assertEqual(payload["snapshot_id"], snapshot_id)
        self.assertEqual(payload["weekly_remaining_bp"], 9000)
        self.assertNotIn("card_id", payload)
        self.assertNotIn("expected_revision", payload)

        args = build_parser().parse_args(
            ["kanban", "quota-invalidate", "--snapshot", snapshot_id]
        )
        self.assertEqual(_kanban_payload(args)["snapshot_id"], snapshot_id)


if __name__ == "__main__":
    unittest.main()
