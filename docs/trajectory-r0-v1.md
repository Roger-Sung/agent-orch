# Trajectory R0 v1 implementation provenance

Status: T1-T7 implemented and locally verified; not merged or deployed

## Frozen authority

- Governing AI-OS change: `changes/trajectory-r0-replay/`
- Approved spec SHA-256: `faefffa032171c2389bd970d5a9bd77202892712d31ab7ad5a61fb19c7d837c3`
- Proposal SHA-256: `1d5963155c70a52a88fda4e065a4e7ce16d39e10ee4685ff31b0275c4a59e58c`
- Tasks SHA-256: `a4d5f2555779417e81f84f3a9ba86c37181a6591ee0e04c2af738ea37a5c2c64`
- Agent-orch implementation base: `0b2629e734d8d9a3fabaa1222349874639ece392`
- T1 commits: `e058c05`, `fa14f2a`
- T2-T6 commit: `5902b0dafa05092a463106bfe31a48f428083934`
- Implementation branch: `codex/trajectory-r0-rollout`
- Independent spec review: Claude Fable round 2 `APPROVE`; all three axes `PASS`

## Implemented boundary

T1-T6 add the strict append-only trajectory-v1 event schema, controller lifecycle and
provider/session/evidence emit points, frozen snapshot acquisition, pure R0 reducer,
stdout-only reader CLI, additive migration, legacy partial baseline, and the single
`ORCH_TRAJECTORY_V1=off|write|read` gate. Canonical `tasks`, `stage_runs` and
`transitions` remain authoritative. R0 never invokes an actor or repairs canonical state.

No daemon environment, launchd definition, production DB, bills DB, or rollout flag is
changed by these commits. R1/R2, tool replay, DAG/plugin work, dashboards and a retention
scheduler remain outside v1.

## T7 verification evidence

All commands ran in `/private/tmp/agent-orch-trajectory-r0-rollout` on 2026-09-29.

- `python3 -m py_compile orchestrator/cli.py orchestrator/controller.py orchestrator/db.py orchestrator/trajectory.py orchestrator/trajectory_replay.py orchestrator/tests/test_trajectory.py` — exit 0.
- `git diff --check` — exit 0.
- `python3 -m unittest orchestrator.tests.test_trajectory` — exit 0; `Ran 40 tests in 0.443s`; `OK`.
- `python3 -m unittest discover -s orchestrator/tests` — exit 0; `Ran 619 tests in 112.310s`; `OK (skipped=1)`.

Focused fixtures cover schema/canonicalization/hash-chain corruption, reserved/future types,
atomic rollback, provider crash-window unknowns, session/evidence binding, secret canaries,
retention availability, snapshot isolation, malformed/future/missing R0 inputs, sensitive
rendering, stdout-only CLI behavior, side-effect traps, gate rollback, mixed legacy baseline,
and additive migration.

Production-shaped fixtures additionally establish:

- Two real SQLite connections concurrently append to one trajectory under
  `BEGIN IMMEDIATE`; committed seq is contiguous, the hash chain is continuous, event IDs
  are unique, and a duplicate-id retry rolls back without a duplicate commit.
- R0 CLI execution leaves the complete temporary fixture tree unchanged by mode, inode,
  size and `mtime_ns`; SQLite DB, `-wal` and `-shm` entries are present and byte-metadata
  stable. SQLite connect, subprocess and socket traps remain at zero.
- A read-only SQLite `.backup` of the live orchestrator DB was migrated only at
  `/private/tmp/trajectory-r0-prod-copy.TxF6gw/orchestrator.db`. Before and after migration:
  `tasks=294`, `stage_runs=830`, `transitions=1256`, integrity is `ok`, and the canonical
  tables dump SHA-256 is
  `3c03cc60efd848e13798fc9c84486ffb2ebc901cf7d1e3e90097beee7694c9a9`.
- All `756` referenced sealed run manifests existed and matched their stored hashes before
  and after migration. Their aggregate verification digest remained
  `73aa2cf1b27b7037accfab83240505f2cd51e52bbd88695271bb0b9dd388f0cb`.
- The migrated copy has an empty additive `trajectory_events` table and both append-only
  triggers. No raw DB data or manifest content is committed to this repository.

The full suite still emits pre-existing `ResourceWarning` messages from
`test_runner_lifecycle_parity.py`; they do not fail the suite and were not introduced by
trajectory code.

## Remaining gates

- T8: independent three-axis implementation review against this exact candidate and evidence.
- T9: canonical stop-gate, then operator-controlled additive migration, dark launch parity
  observation, rollback drill, daemon restart and real task smoke.
- Production `ORCH_TRAJECTORY_V1` remains unset/off until T9 ALLOW.
