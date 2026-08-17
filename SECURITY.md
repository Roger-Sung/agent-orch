# Security

## What this project is, for security purposes

A **reference and portfolio release**. It runs in one private deployment; it is
not a package, not a supported product, and not accepting contributions (see
[Project status](README.md#project-status) and [LICENSE](LICENSE)). There is no
release cadence, so there is no patch stream to subscribe to.

That framing matters for how you should read the containment work: the layers
are real and tested, but they were built for one operator's threat model, not
hardened against an adversary with a budget.

## This is not a sandbox for hostile code

**Do not use agent-orch to run code or agents you do not trust.**

The containment layers exist to stop a *capable but non-malicious* process from
acting outside its intended scope — an agent that confidently edits the wrong
directory, which is the failure this project was extracted to fix. They are not
an adversarial boundary:

- A stage runs as the **same UNIX user** as the daemon. It can read every file
  that account can read, including credentials outside the small set that gets
  stripped.
- **Network egress is unrestricted.** A stage can send anything it can read
  anywhere it likes.
- The write sandbox is macOS `sandbox-exec`, which Apple has deprecated, and
  the allowlist necessarily includes the temporary directories and the provider
  CLIs' own state directories.
- The daemon invokes provider CLIs with their approval prompts disabled. That
  is the point of a daemon, and it is why `ORCH_ALLOW_UNATTENDED=1` has to say
  so out loud: the bundled launcher requires it unconditionally, and the
  daemon requires it when it detects a *known* approval-disabling flag by name
  in the configured commands — a renamed flag or a config-file mechanism is
  invisible to that check. Either way it is an acknowledgement rather than a
  control: it records a decision, it does not restrain a stage.

[`docs/threat-model.md`](docs/threat-model.md) states the adversary model, what
each layer stops, what it does not, and which properties are tested versus
assumed. Read it before deciding what to point this at.

## Reporting a vulnerability

If you find a security problem — in the containment layers, the sanitization
scanner, or anything else — please report it privately rather than opening a
public issue:

- [GitHub Security Advisories](https://github.com/Roger-Sung/agent-orch/security/advisories/new)
  ("Report a vulnerability" on the Security tab) is the only reporting
  channel — issues are closed on this repository, consistent with its
  project status.

Please include what you were doing, what happened, and what you expected. A
proof of concept helps but is not required.

**What to expect.** A best-effort acknowledgement, and a fix if the issue is
real and the fix is within the scope described above. There is no service-level
commitment, no bounty, and no embargo process. If a report reveals something
that cannot be fixed within this design, the honest outcome is a documented
limitation in the threat model rather than a silent patch — that is how the
existing gaps (no process isolation, no egress control) came to be written
down.

## Scope notes

In scope, and worth reporting:

- A way for a stage to write outside its workspace that L1 permits and L2 does
  not detect.
- A configuration the overlap guard accepts that lets a declared write root
  cover a protected root.
- A way to make the sanitization scanner report a clean tree that is not clean.
- A way to defeat the evidence trail — for example making a sealed manifest
  attest to content that never ran.

Out of scope, because they are documented properties rather than defects:

- Reading files outside the workspace (L1 restricts writes only).
- Network access from a stage.
- Anything requiring the operator to have already set
  `--allow-unsandboxed`, or to have declared a write root over something
  precious.
- Prompt injection through task input causing a bad-but-permitted action
  inside the workspace. Caps and stop gates bound the damage; nothing prevents
  it.
