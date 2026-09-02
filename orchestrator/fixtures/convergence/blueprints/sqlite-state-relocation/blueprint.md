# BP-1 — SQLite durable-state relocation blueprint

Reference blueprint. It shows what "minimally sufficient" looks like for a durable
state move. It is **not** an implementation task of the convergence work, and the
actual application-state migration is out of scope.

BP-1 and the `F-A8` fixture are different kinds of object and must not be
substituted for one another: `F-A8` is a routing fixture that pins which edge a
policy takes; BP-1 is a document that pins what a proportionate plan looks like.

## Original risk

`<EXAMPLE_APP_ROOT>/var/state/app.db` sits inside a protected root, so any orch
stage that touches `<EXAMPLE_APP_ROOT>` can produce `protected_root_drift`.
Separately, the writer performs `DB_PATH.parent.mkdir(...)` followed by
`CREATE TABLE IF NOT EXISTS`, so a run pointed at the wrong path silently creates
an empty database and every subsequent read returns zero rows rather than an error.

## Boundary

Changes land in `<EXAMPLE_APP_ROOT>`. The orch engine is not modified. SQLite
stays; the schema is unchanged; no replacement database is involved. No new
schedule and no new service.

## Goal

One canonical path, outside the protected root: `<EXTERNAL_STATE_ROOT>/app.db`.

## Steps (minimally sufficient)

1. Inventory every writer and reader by content search rather than by literal path
   match, including untracked files and executable `.bak` copies.
2. Add a single helper. The canonical path is a module constant: no env override,
   no file creation, no `chmod`, no `mkdir`. `connect()` verifies and never
   modifies; if the path does not exist it fails closed.
3. Point every writer and reader at the helper.
4. Quiesce writers, verify with sha256 and per-table row counts, copy, then cut
   over.
5. Set the old path to `0400` as passive damage limitation. Detection is carried
   by a positive `DB_BIND` log line, because SQLite can still open a `0400` file
   read-only.

## Failure handling

If any sha256 or row count mismatches before cutover, abort, restore the previous
writer state, and perform no partial migration. If a `DB_BIND` line pointing at
the old path appears after cutover, restore the helper constant and the file mode;
this is a reversible rollback.

## Acceptance (observation targets)

- Pre- and post-migration sha256 and per-table row counts are equal.
- One real scheduled execution logs `DB_BIND` pointing at the new path.
- The state writer no longer produces `protected_root_drift`.
- Helper tests show that a non-canonical env value is ignored, that a mode mismatch
  causes zero mutation, and that a missing path creates nothing.

## Explicitly not part of this blueprint

Multi-round containment probe choreography; per-attempt artifact indexes with their
own counters; window freeze attestation; layered evidence matrices. Those are
observation machinery. A blueprint records the risk and the observation target;
how the observation is made is chosen at apply time, as simply as it can be.
