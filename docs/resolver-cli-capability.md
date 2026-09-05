# Resolver CLI capability record

Date: 2026-09-05. Host: macOS (Darwin 25.5).

`orchestrator/start.py` fixes the intake resolver to Claude and hands it four
isolation options — `RESOLVER_ISOLATION_FLAGS` — so the child that reads a
task's own text has no tools, no MCP servers, no user configuration and no
session state. Those four are an assumption about someone else's CLI. Until
now nothing checked it: a rename in a Claude release would have surfaced as a
provider exit code at every intake, with no statement of which capability had
vanished.

`orch doctor` now carries a `resolver_isolation` check
(`orchestrator/doctor.py`), backed by `start.resolver_isolation_support()`.
This file is the dated record of running it against the real CLI, in the shape
of [`l1-provider-verification.md`](l1-provider-verification.md).

## Method

`resolver_isolation_support()` derives the whole configured invocation from
`provider_command("claude")` — prefix, wrapper, interpreter and flags intact —
appends `--help` where the resolver appends its own flags, and runs it once
with `stdin` closed, a 5-second timeout, an empty temporary working directory,
and the resolver's own allowlisted environment. It then reports which of the
option names in `RESOLVER_ISOLATION_FLAGS` do not appear in the help text.

It is read-only, it never raises into its caller, and it is called from
nowhere but `orch doctor`. Intake, `claim_stage`, `commit_run` and the daemon
loop call nothing new; no recurring provider call is added.

## Result — pass

```
$HOME/.local/bin/claude --version   →  2.1.258 (Claude Code)
```

| Option | Present | Line in `claude --help` |
|---|---|---|
| `--tools` | yes | `--tools <tools...>  Specify the list of available tools from the built-in set. Use "" to disable all tools, "default" to use all tools, or specify tool names (e.g. ...` |
| `--strict-mcp-config` | yes | `--strict-mcp-config  Only use MCP servers from --mcp-config, ignoring all other MCP configurations` |
| `--setting-sources` | yes | `--setting-sources <sources>  Comma-separated list of setting sources to load (user, project, local).` |
| `--no-session-persistence` | yes | `--no-session-persistence  Disable session persistence - sessions will not be saved to disk and cannot be resumed (only works with --print)` |

Probe output on this host, called directly:

```
command:  $HOME/.local/bin/claude -p --help
verified: True
missing:  ()
detail:   4 of 4 isolation option(s) present in `--help`
```

And through `orch doctor`, with the deployment's own configured command
(`claude -p --dangerously-skip-permissions --model claude-opus-5`), 13 checks
`ok` and 0 `fail`:

```
ok  provider_claude      provider_preflight_pass: $HOME/.local/bin/claude -p …
ok  resolver_isolation   4 of 4 isolation option(s) present in `--help`;
                         resolver CLI reports 2.1.258 (Claude Code)
```

The version on the `resolver_isolation` line is the one the `provider_claude`
preflight above it already obtained; the check does not spawn `--version` a
second time.

The `--tools` entry is the one worth reading in full: it documents `""` as
"disable all tools", which is the availability control the boundary claims,
not a permission allowlist over tools that still exist.

## What a failure looks like

`resolver_isolation` is `fail` in two distinguishable ways, both exiting
`orch doctor` with 1:

- **absent from `--help`** — the CLI answered and the named options are gone.
  A release renamed them; `RESOLVER_ISOLATION_FLAGS` has to be updated before
  intake will work, and the boundary has to be re-argued in the new
  vocabulary.
- **unverified** — the command could not be derived, the CLI is not installed,
  it could not be started, it timed out, or `--help` exited non-zero. Nothing
  is known about the options; this is a wiring problem, not a CLI change.

## Reproducing

```sh
python3 -c "
from orchestrator.start import resolver_isolation_support
s = resolver_isolation_support()
print(' '.join(s.command), s.verified, s.missing, s.detail, sep=' | ')
"
```

or, with the rest of the deployment's wiring:

```sh
orch doctor | python3 -m json.tool | grep -A 2 resolver_isolation
```

## Scope of this run

One host, one CLI version, one moment. The record does not travel: a different
machine, a different `ORCH_CLAUDE_COMMAND` (a wrapper, a pinned version, a
different interpreter) or a later Claude release is a different fact, and the
check is what re-establishes it. Nothing here is asserted about whether the
options *behave* as documented — that is the L1 boundary's question, recorded
in [`l1-provider-verification.md`](l1-provider-verification.md) — only that
the CLI still offers them.
