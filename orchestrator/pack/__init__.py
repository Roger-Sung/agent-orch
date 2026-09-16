"""pack-v1 policy modules (D-2026-09-14-09).

Everything here is reached only when a task's ``policy_version`` is
``pack-v1``; the legacy and execution-v1 paths never import it.  Step 0a of
IMPLEMENTATION-PLAN covers the pure-function layer only: no controller wiring,
no provider calls, no SQLite.
"""
