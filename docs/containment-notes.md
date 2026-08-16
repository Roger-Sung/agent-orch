# Containment notes (superseded)

> **These notes have been folded into [`threat-model.md`](threat-model.md).**
> They are kept because they are the raw working record — what actually bit
> during development, in the order it bit — while the threat model is the
> organised version a reader should start from.

Working notes on how the containment layers behave in practice.

## L1 — what the allowlist necessarily gives up

**Temporary directories are writable, so nothing in them is protected.**
`/tmp`, `/private/tmp`, `/private/var/tmp`, `$TMPDIR` and `/var/folders` are on
the allowlist because provider CLIs write there constantly; excluding them
breaks every stage. The consequence is direct: anything a deployment keeps
under a temporary directory — a scratch database, a lock file, an export
staged for later — gets no protection from L1 at all.

This bit during development. The first version of the acceptance tests put
their "protected" fixture under `/tmp`, so the test passed while asserting
nothing. Fixtures now live under the repository. Any future test that means to
prove a write was blocked must place its target outside every allowlisted
root, and that is not obvious from reading the test.

**Provider state directories are allowed wholesale, not per file.** A SQLite
database is three files (`db`, `db-wal`, `db-shm`); allowing only the first
corrupts the database rather than merely blocking a write. Precision here buys
nothing and costs debuggability.

## L1 — resolve paths, or write rules that silently never match

`sandbox-exec` matches on resolved paths. `/tmp` is a symlink to
`/private/tmp` on macOS, so a `(subpath "/tmp")` rule looks correct and never
fires. Every path entering the profile goes through `os.path.realpath` for
this reason. The failure mode is silent — a rule that does nothing looks
exactly like a rule that works, until something is denied that should not be.

`subpath` matching *is* path-component aware, verified rather than assumed:
allowing `/base/workspace` does not allow `/base/workspace-extra`. A test
covers this, because an allowlist built from string prefixes would quietly
reopen the hole the layer exists to close.

## L2 — the hash threshold and its asymmetry

The sentinel stores mtime and size for every file, plus a content hash for
files below 256 KiB. When a file looks modified, the hash decides whether it
actually changed — that is what keeps a harmless `touch` from being reported
as a violation.

Above the threshold there is no hash, so a change cannot be disproved, and any
mtime or size difference counts as a violation. The asymmetry is deliberate:
the case that is expensive to verify fails closed. The cost shows up as noise
on protected roots holding large files that legitimately change (build
outputs, media, package caches). The exclusion list is the escape hatch, and
it should be used for those directories rather than raising the threshold,
since raising it makes every stage slower for every file.

## L2 — off unless a deployment opts in

`ORCH_PROTECTED_ROOTS` is empty by default, so detection does nothing until a
deployment declares what it wants watched. The engine has no business assuming
which directories on someone else's machine matter. The corollary is worth
stating plainly, because it is an easy way to believe you are protected when
you are not: **a deployment that never sets `ORCH_PROTECTED_ROOTS` has L1 only.**

## Residual gaps

Neither layer touches these, and neither should be described as if it did:

- The stage runs as the same UNIX user, so it can read anything the operator
  can read. L1 restricts writes, not reads.
- Network egress is unrestricted. A stage can send anything it can read.
- `sandbox-exec` is deprecated by Apple. If it disappears, L1 stops being
  available and mutating stages refuse to run rather than running unconfined —
  which is the correct failure, but it is a failure, and L2 is the only layer
  that survives.
