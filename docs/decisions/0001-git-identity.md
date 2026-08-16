# ADR 0001 — Commit identity inside containment

- Status: **proposed** (M0 deliverable; implementation belongs to M1)
- Date: 2026-08-17
- Scope: `runner.py` containment gitconfig only. This document changes no code.

## Context

`prepare_containment()` writes a private gitconfig for every contained stage,
because containment clears `GIT_CONFIG_GLOBAL` and a commit with no
`user.name` / `user.email` fails outright. The current implementation seeds
that gitconfig with a fixed fallback identity and then **overwrites it with
the machine's global identity**:

| Location (upstream file) | Behaviour |
|---|---|
| `runner.py:83-94` | writes `containment/gitconfig` from `identity` |
| `runner.py:84` | `identity = _git_identity()` |
| `runner.py:112-129` | fallback dict, then a loop over `("name", "email")` running `git config --global user.<key>`; a non-empty result **replaces** the fallback |

The fallback (`ai-os orchestrator` / a `.local` address) therefore only
applies on a machine with no global git identity. On any developer machine —
i.e. every machine this has actually run on — the operator's real name and
real email are written into the stage's gitconfig and end up as the author of
every commit an agent makes inside the workspace.

That is fine while the workspace and its evidence stay private. It stops
being fine the moment this repository is published:

1. Demo evidence, fixtures, or recorded stage artifacts can carry a real
   name/email pair that was never meant to be part of the portfolio.
2. The public repository is intended to be attributable to a chosen public
   identity, not to whatever identity happens to be configured on the machine
   that ran the demo.
3. The leak is silent: nothing in the current code path reports which
   identity was used, so a sanitization pass has to catch the value rather
   than the mechanism.

A previous review scored this as low risk on the assumption that the fixed
fallback usually wins. The dispute against that reading was upheld
(2026-08-17): the loop is an unconditional overwrite, so the global identity
wins whenever it exists.

## Decision

**Contained stages get a fixed synthetic identity by default. The real
identity is opt-in, explicit, and per-run.**

Recommended shape:

1. Default identity becomes a constant, with no lookup at all:
   `orchestrator <orchestrator@orch.invalid>`. A reserved-TLD address cannot
   be delivered to and cannot be mistaken for a person.
2. `ORCH_GIT_IDENTITY` opts in to something else, with two accepted forms:
   - `ORCH_GIT_IDENTITY=global` — resolve from `git config --global`, i.e.
     today's behaviour, now requested rather than assumed;
   - `ORCH_GIT_IDENTITY="Name <address@host>"` — an explicit literal, which
     is what a deployment wanting stable authorship should use.
3. `_git_identity()` keeps its signature but takes the environment as an
   argument so it is testable without touching the machine's git config.
4. The resolved identity source (`default` | `env-literal` | `global`) is
   recorded in the stage record. Never record the resolved value itself when
   the source is `global` — record only the fact that a global identity was
   used, so evidence files stay sanitizable by construction.
5. Malformed `ORCH_GIT_IDENTITY` fails the stage rather than falling back to
   `global`. Fail-closed, consistent with the containment posture.

Rejected alternatives:

- *Keep the global lookup, sanitize the output instead.* Rejected: makes
  every future evidence artifact a sanitization liability, and the scanner
  only catches values it was told about.
- *Drop the identity entirely.* Rejected: commits fail without one, and
  commit-inside-workspace is a supported operation (`test_commit_still_works
  _under_containment`).
- *Read a repo-local `user.name` instead of global.* Rejected: same leak,
  narrower blast radius, no benefit.

## Consequences

- Demo and test evidence become attributable to a synthetic identity by
  default, which is what a public repository needs.
- Any deployment that relied on real-identity commits must now set
  `ORCH_GIT_IDENTITY` — a one-line configuration change, and a visible one.
- One more environment variable in the containment surface; it is read before
  the credential-stripping step and must itself be excluded from the
  stripped-env allowlist review.

## Test requirements (M1 acceptance)

Extend the containment test module:

1. **Default is synthetic** — prepare containment with a global git identity
   present in the test environment; assert the written gitconfig contains
   neither the global name nor the global email, and does contain the
   constant default.
2. **Explicit literal wins** — `ORCH_GIT_IDENTITY="Test Person <t@x.invalid>"`
   is written verbatim into the gitconfig.
3. **`global` opt-in restores old behaviour** — with `ORCH_GIT_IDENTITY=global`
   the configured global identity is used (test asserts against a fake git
   config, not the machine's).
4. **Malformed value fails closed** — `ORCH_GIT_IDENTITY="not-an-identity"`
   raises / fails the stage with a distinct reason, and no gitconfig
   containing a partial identity is left behind.
5. **Commit still works** — the existing commit-under-containment test passes
   unchanged with the synthetic default, proving the default is a working
   identity and not just a placeholder.
6. **Evidence has no identity leak** — after a contained stage with
   `ORCH_GIT_IDENTITY=global`, the stage record contains the source marker
   and does not contain the resolved email.

The sanitization scanner is the backstop, not the fix: rule
`email_address` in `risk-patterns.yaml` would flag a leaked address, but it
must never be the thing that catches this in a release run.
