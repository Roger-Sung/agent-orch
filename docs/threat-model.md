# Threat model

What this system defends against, what it does not, and which of its defences
are verified rather than assumed. The gaps are stated plainly: a containment
story that only lists wins is not a threat model, and the boundary is the part
a reader actually needs.

## What is being defended

An agent stage is an LLM given a shell, a working directory, and a task. It is
not malicious, but it is unreliable in a specific way: it will confidently do
something adjacent to what was asked, in a place nobody expected. The concrete
incident behind this work is exactly that — a stage ignored its workspace and
rewrote a live data store elsewhere on the machine, reported success, and was
caught only by a human reading the result.

So the adversary model here is **a capable, non-malicious process that may act
outside its intended scope**, not an attacker trying to defeat a sandbox. A
determined attacker with code execution is out of scope, and the design does
not pretend otherwise.

Assets, in priority order:

1. State outside the workspace — repositories, data stores, configuration.
2. Credentials reachable from the stage's environment.
3. The evidence trail itself: if a stage can rewrite its own logs or manifests,
   nothing else here is trustworthy.
4. Provider quota, which an unbounded loop can consume.

## Layers, and what each actually stops

| Layer | Mechanism | Stops | Does not stop |
|---|---|---|---|
| Git containment | dedicated worktree; credentials stripped from the environment; `GIT_ASKPASS`/`GIT_SSH_COMMAND` → `/usr/bin/false`; unconditional `pre-push` hook | results leaving through git | any other exfiltration path |
| L1 prevention | `sandbox-exec` write allowlist | writes outside the workspace, artifact dir, temp dirs, provider state dirs | reads, network, writes to allowlisted paths |
| L2 detection | sentinel snapshot of declared protected roots around each stage | reports and quarantines writes that happened anyway | anything outside the declared roots |
| Caps | attempt caps, per-edge transition caps, `max_transitions` | unbounded loops and quota burn | a single expensive stage |
| Stop gate | reviewer from a different provider family; only `allow` reaches `done` | a model clearing its own work | a reviewer that is wrong in the same direction |
| Sealed manifests | log hash + output hash recorded at commit | silent after-the-fact edits going unnoticed | a stage that corrupts its own log before the seal |

L3 — a separate UNIX identity for the executor, plus a network egress
allowlist — **is not implemented**. Its absence is the single largest gap and
is discussed under Residual risk.

## Known limitations

### L1: temporary directories are writable, so nothing in them is protected

`/tmp`, `/private/tmp`, `/private/var/tmp`, `$TMPDIR` and `/var/folders` are on
the allowlist because provider CLIs write there constantly; excluding them
breaks every stage. The consequence is direct: anything a deployment keeps
under a temporary directory — a scratch database, a lock file, an export
staged for later — is unprotected by L1.

This is not theoretical. The first version of the L1 acceptance tests put
their "protected" fixture under `/tmp`, so every assertion passed while
proving nothing. The tests now choose a fixture location at runtime that is
genuinely outside the allowlist, and skip loudly if none exists. Any future
test asserting "this write was blocked" must do the same.

### L1: rules must be built from resolved paths

`sandbox-exec` matches on resolved paths. `/tmp` is a symlink to `/private/tmp`
on macOS, so a `(subpath "/tmp")` rule looks correct and never fires. Every
path entering a generated profile goes through `os.path.realpath`. The failure
mode is silent — a rule that does nothing is indistinguishable from a rule
that works, right up until something legitimate is denied.

`subpath` matching is path-component aware, which was verified rather than
assumed: allowing `/base/workspace` does not allow `/base/workspace-extra`. A
test covers it, because an allowlist built from string prefixes would quietly
reopen the hole.

### L1: the provider allowlist is now verified for both CLIs

The provider state directories in the allowlist started as an observed-write
probe — a directory walk, i.e. a guess. Both CLIs have since been run
end-to-end inside the generated profile
([`l1-provider-verification.md`](l1-provider-verification.md)): Codex and
Claude each completed a real task, wrote their state files (Codex 18,
including SQLite `-wal` siblings; Claude 4, including a cwd-slug session
transcript), and were still refused a write outside the workspace under the
same profile. No allowlist changes were needed for either.

One path remains inferred rather than observed: what a provider CLI writes
while *refreshing* an expired token. Neither verification run triggered a
refresh. The inference is now structural rather than empirical — the profile
denies file writes only, so keychain access and network egress are untouched,
and a refresh writing into the CLI's own state directory lands inside the
allowlist. A missing path here would surface as a stage failure, not as silent
damage, and L2 watches independently.

### L1: declared extra write roots are a trust expansion, owned by the deployment

`ORCH_EXTRA_WRITE_ROOTS` lets a deployment name additional directories a stage
may write to. It exists because real work needs it — a JVM build writes to its
dependency cache, a package manager to its store — and because the alternative
was worse: without it the only escape is `--allow-unsandboxed`, which drops the
write allowlist entirely for every path at once. One legitimate cache directory
should not cost the whole layer.

It is still an expansion of what a stage may damage, and the deployment owns
that decision. Three properties bound it:

- **Narrow by construction.** Only the named roots are added; everything else
  stays denied. Declaring one directory does not widen the rest.
- **Resolved before use.** Paths go through realpath, so a symlinked root
  becomes the real target in the profile rather than a rule that never fires.
- **It cannot overlap a protected root.** A write root that contains, or sits
  inside, anything named in `ORCH_PROTECTED_ROOTS` is a configuration
  contradiction: L1 would permit writes to precisely the tree L2 watches, so a
  stage could damage a protected root and the run would look clean because the
  write was allowed. Overlap in either direction refuses the stage before it
  starts, with the stop reason `containment_config_conflict`. Comparison is on
  resolved paths and whole path components, so a symlink cannot smuggle a
  protected root past the check and an adjacent name (`/x/cache-notes` beside
  `/x/cache`) is not mistaken for containment.

What remains the deployment's responsibility: a declared root is not watched by
L2 unless it is also a protected root — and it cannot be both. Anything
genuinely precious does not belong on this list.

### L1 depends on a deprecated tool

`sandbox-exec` is deprecated by Apple and may be removed. If it disappears,
mutating stages refuse to run rather than running unconfined — the correct
failure, but a failure. L2 is deliberately independent so that detection
survives prevention. `--allow-unsandboxed` exists for hosts without it, and it
has to be passed explicitly and out loud.

### L2: off until a deployment declares roots

`ORCH_PROTECTED_ROOTS` is empty by default; the engine has no business
guessing which directories on someone else's machine matter. The corollary is
easy to miss and worth stating: **a deployment that never sets it has L1 only.**
The migration checklist in
[`extraction-inventory.md`](extraction-inventory.md) covers setting and
verifying it.

### L2: the hash threshold, and its deliberate asymmetry

The sentinel records mtime and size for every file under the protected roots,
plus a content hash for files below 256 KiB. When a file looks modified, the
hash decides whether it really changed — that is what keeps a harmless `touch`
from being reported as a violation.

Above the threshold there is no hash, so a change cannot be disproved, and any
mtime or size difference counts as a violation. The expensive-to-verify case
fails closed. The cost is noise on protected roots holding large files that
legitimately change: build outputs, media, package caches. The exclusion list
is the right escape hatch for those; raising the threshold makes every stage
slower for every file.

### L2: a snapshot is not a monitor

Detection brackets the stage. A file written *and restored* during a stage is
invisible to it, and so is anything under an excluded subtree. Exclusions are
matched on whole path components precisely so that an exclusion cannot swallow
its neighbours by prefix, but an over-broad exclusion is still the easiest way
to defeat this layer, and it is a configuration mistake nothing here can catch.

### Sanitization scanner: line-by-line matching

`tools/sanitize-lint.py` evaluates one line at a time, so a sensitive value
split across lines — a wrapped URL, a base64 blob broken by a formatter, a
name spanning a YAML fold — does not match. Whole-file matching would fix it
at the cost of line numbers and of quadratic behaviour on large files. The
scanner is a backstop against mistakes, not a defence against someone
deliberately smuggling a value past it.

Its secret-backed rules also fail closed: without `--secrets-file` the scan
exits non-zero rather than reporting a clean tree it never fully checked.

## Residual risk

Ranked by what an operator should worry about first.

1. **No process isolation (L3).** The stage runs as the same UNIX user. It can
   read every file the operator can read, including credentials outside the
   stripped set, and browser or CLI state. L1 restricts writes only.
2. **No network egress control.** A stage can send anything it can read
   anywhere it likes. Combined with (1), read-and-exfiltrate is unmitigated;
   the git layer only closes the git-shaped path.
3. **Prompt injection through task input.** Task descriptions and any files a
   stage reads are untrusted content. Caps and gates bound the damage; nothing
   prevents a stage from being talked into a bad-but-permitted action inside
   its workspace.
4. **Same-family gate degradation.** The cross-provider property holds only if
   both provider families are actually available. When one is not, a gate run
   with the same family is worth much less, and the routing does not currently
   refuse it.
5. **Evidence is tamper-evident, not tamper-proof.** Manifests seal a hash of
   the log at commit time. A stage that corrupts its own log *before* the seal
   produces a faithful hash of corrupted content.

## Assurance status

| Property | How it is known |
|---|---|
| L1 blocks out-of-workspace writes | automated tests, incl. an adjacent-name sibling case |
| L1 permits legitimate workspace, artifact, and temp writes | automated tests |
| L1 permits a real provider CLI to work (Codex) | real end-to-end run under the sandbox |
| Token-refresh write paths | **inferred** — no verification run triggered a refresh |
| L1 permits a real provider CLI to work (Claude) | real end-to-end run under the sandbox |
| L1 fails closed without `sandbox-exec` | automated test, with a distinct stop reason |
| Declared write roots widen only the named paths | automated tests |
| A write root overlapping a protected root refuses the stage | automated tests, both directions |
| L2 detects modification, creation, deletion | automated tests |
| L2 ignores unchanged touches and excluded subtrees | automated tests |
| L2 still flags paths adjacent to an exclusion | automated test |
| Escape blocks and quarantines the task with evidence | automated tests through the controller |
| Commit identity is synthetic unless requested | automated tests |
| Caps bound both attempts and edges | automated tests, plus the shipped demo |
