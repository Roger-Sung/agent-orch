# Trajectory R0 v1 implementation provenance

Status: T1 implementation slice only; not deployed

- Governing AI-OS change: `changes/trajectory-r0-replay/`
- Approved spec SHA-256: `c6b351c1231d047da6026ed79391896a40cd870e67987da111484ec2d594a370`
- Proposal SHA-256: `23e5d62a9564bafe01b380103409d4752e5d9e1afafe5bb933bf242814eecaa5`
- Tasks SHA-256: `8a97730397b49027fffa2a386dffe56b506b3cb84c5dade2e521c6f3a1fa8a1b`
- Agent-orch implementation base: `001c9bea1e20e5be56f78e76ec17be75c361ee70`
- AI-OS drafting base: `bc61b52e7e8208a097b90a27bd83ac5bef2fd62d`
- AI-OS main observed before apply: `fed3f3bb9121a85e0433367e33deb91055a62e62`
- Independent spec review: Claude Fable round 2 `APPROVE`; all three axes `PASS`

This slice implements only T1: the strict trajectory-v1 event schema,
canonical JSON and hash chain, additive SQLite table, append-only triggers and a
transaction-bound store API with synthetic fixtures.  It does not emit events
from the controller, enable `ORCH_TRAJECTORY_V1`, migrate legacy tasks, expose
R0 replay, change daemon configuration, or touch a production database.
