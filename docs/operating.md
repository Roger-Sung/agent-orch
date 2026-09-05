# Operating agent-orch

The operator's manual: everything you need to run the daemon against real
provider CLIs, and nothing a reader who only wants to understand the design
has to wade through. The [README](../README.md) covers what the system is and
why it is shaped this way; this document covers how to stand it up, wire it,
check the wiring, and act on the stops it produces.

## Requirements

- **Python 3.12 or newer.** The engine uses only the standard library. CI
  exercises 3.12 on Linux and macOS.
- **Both provider CLIs installed and already authenticated.** `claude` and
  `codex` must be on the `PATH` *of the daemon process*, which is usually not
  the same PATH as your shell — a service manager starts with a minimal one, so
  the bundled launcher sets it explicitly. Log in with each CLI first: the
  daemon runs unattended and cannot complete an interactive sign-in, and a
  stage whose CLI is unauthenticated fails as a provider error.
- **macOS or Linux.** Windows is not supported — the IPC layer uses `fcntl`
  file locks. L1 is macOS-only on top of that: the write sandbox uses
  `sandbox-exec`, and on other platforms a mutating stage refuses to run
  unless `--allow-unsandboxed` is passed; L2 detection and the overlap guard
  work on both supported systems.

## Configure and start

```sh
export ORCH_HOME=~/.local/state/agent-orch     # where state and evidence live
export ORCH_ALLOW_UNATTENDED=1                 # see below before setting this
export ORCH_CLAUDE_MODEL=claude-opus-5         # required, no default
export ORCH_CODEX_MODEL=gpt-5.5                # required, no default
export ORCH_PROTECTED_ROOTS="$HOME/important-repo"   # L2 watches these
./packaging/run-daemon.sh
```

`ORCH_ALLOW_UNATTENDED=1` is a deliberate acknowledgement, enforced twice with
different precision. The bundled launcher requires it **unconditionally**,
because it composes commands that disable the approval prompts. The daemon
independently scans the configured commands for **known approval-disabling
flags** (`--dangerously-skip-permissions`, `--approve-for-me`) and requires the
same acknowledgement when it detects one — so a deployment with its own
launcher still hits the gate, but only if a known flag is spelled out: a
wrapper script, a renamed flag, or a CLI config file that disables approvals
another way is invisible to it. A clean scan means "no known flag was spelled
out here", not "this deployment is attended". The launcher gate stays the
first line because it does not depend on recognising anything.

The provider CLIs are invoked with their approval prompts disabled — that is
what makes unattended operation possible — so a stage acts with the full
authority of the daemon's UNIX user: it can read anything that account can
read, and nothing isolates it at the process level. Run it as an account whose
reach you are comfortable handing to an agent, and read the
[threat model](threat-model.md) first.

### One place for the variables, instead of eight exports

The variables above can live in a config instead of a shell history, and the
worst intake trap — a CLI and a daemon silently resolving different
`ORCH_HOME`s — has two purpose-built answers:

- `~/.config/agent-orch/orch.toml` (or `ORCH_CONFIG`): a flat TOML table of
  `ORCH_*` keys the CLI loads at startup. Variables already set in the
  environment always win, and the two acknowledgement gates
  (`ORCH_ALLOW_UNATTENDED`, `ORCH_ALLOW_UNSANDBOXED`) are refused in the file
  on purpose — they record a decision, and decisions do not belong in a file
  nobody re-reads.
- `packaging/orch`, a PATH shim that sources the *same* env file as
  `run-daemon.sh` (`~/.config/agent-orch/env.sh`, template in
  `packaging/env.sh.template`) before invoking the CLI — so the CLI and the
  daemon cannot each pick their own state directory.

```toml
# ~/.config/agent-orch/orch.toml
ORCH_HOME = "~/.local/state/agent-orch"
ORCH_PROTECTED_ROOTS = ["~/important-repo"]        # lists join with the path separator
ORCH_CLAUDE_COMMAND = "claude -p --model claude-opus-5"
```

### Check the wiring before a task discovers it

```sh
python3 -m orchestrator doctor
```

Read-only. Reports whether `ORCH_HOME` is set or silently defaulted, whether
both provider CLIs resolve and answer from *this* environment, whether L1 is
available, whether L2 is actually on (`ORCH_PROTECTED_ROOTS` empty means it is
OFF), and whether a declared write root contradicts a protected root. Exit
code 1 when anything would make a stage refuse, so it can gate a provisioning
script.

### Smoke test it end to end

```sh
python3 -m orchestrator enqueue \
    --type provider-smoke \
    --profile orchestrator/profiles/provider_smoke.yaml \
    --input /path/to/one-line-brief.md

python3 -m orchestrator status <task_id>       # expect: done
```

The smoke profile runs one stage per provider with a trivial prompt, which is
the cheapest way to confirm that both CLIs, the model pins, and the daemon are
actually wired together.

## Providers

agent-orch does not host models and does not call model APIs. It orchestrates
locally installed, authenticated agent CLIs — the process it spawns for a stage
is the same CLI you would run by hand. Two are supported today: **Claude Code
CLI** and **Codex CLI**. Authentication, accounts, and model costs stay with
your own CLIs, and no API key is held here.

The bundled launcher deliberately clears the Anthropic API overrides
(`ANTHROPIC_BASE_URL`, `ANTHROPIC_API_KEY`) so the Claude CLI cannot silently
inherit pay-per-token billing or a custom endpoint from the parent environment.
That is the extent of it: the Codex CLI's own endpoint and billing
configuration live in its config files, which the launcher does not police. A
deployment that authenticates by API key on purpose edits its own copy of the
launcher.

The engine reads one variable per owner, `ORCH_CLAUDE_COMMAND` and
`ORCH_CODEX_COMMAND`, and runs exactly what they contain. Model selection is
therefore whatever flag the CLI takes — the orchestrator has no model registry,
it only records the model it can read back from the command it ran.

```sh
# Using the bundled launcher, which composes the commands for you:
export ORCH_CLAUDE_MODEL=claude-opus-5
export ORCH_CODEX_MODEL=gpt-5.5
./packaging/run-daemon.sh

# Or drive the daemon directly, in which case the full command is yours to
# set — including the non-interactive flags the launcher would have added, and
# the acknowledgement the daemon demands once it sees them:
export ORCH_CLAUDE_COMMAND='claude -p --dangerously-skip-permissions --model claude-opus-5'
export ORCH_CODEX_COMMAND='codex exec --approve-for-me --model gpt-5.5'
export ORCH_ALLOW_UNATTENDED=1
python3 -m orchestrator daemon
```

`ORCH_*_MODEL` is a convenience of the launcher, not something the engine
consults; if you set the command variables yourself, the model belongs inside
them.

**What "cross-provider" requires.** Two executable CLIs from *different provider
families* — not two model names from one vendor. The stop gate's claim is that a
reviewer does not share the executor's blind spots, and two models from one
family largely do. Pointing both owners at the same family leaves the machinery
working and the safety argument gone. The engine does not verify the commands
behind the `claude` and `codex` slots; enforcement is planned, not implemented.

### Planned, not implemented

- A generic CLI adapter contract, so any agent CLI can be an owner. The wrapper
  requirements are already implied by the runner: non-interactive, prompt passed
  as an argument, and exactly one valid `ORCHESTRATOR_OUTCOME` line on stdout.
- Additional provider families.
- Provider capability discovery, instead of today's fixed `claude` / `codex`
  owner names.
- Enforced cross-family reviewer selection, so a same-family pairing is refused
  rather than merely discouraged.

## Stop reasons

Which stop reason lands where, and who can move it on:

| Stop reason | State | Moved on by |
|---|---|---|
| `attempt_cap` | `waiting_user` | human |
| `edge_cap` | `waiting_user` | human |
| `transition_cap` | `waiting_user` | human |
| `orphaned_running` (daemon died mid-stage) | `blocked` | human |
| `rate_limited` | `paused` | automatic backoff |
| `missing_outcome` | `blocked` | human |
| `ambiguous_outcome` | `blocked` | human |
| `unknown_outcome` | `blocked` | human |
| `timeout` | `blocked` | human |
| `sandbox_unavailable` | `blocked` | human |
| `sandbox_setup_failed` | `blocked` | human |
| `containment_config_conflict` | `blocked` | human |
| `runner_cannot_enforce_guard` | `blocked` | human |
| `workspace_escape` (legacy manifests) | `blocked` | human |
| `protected_root_drift` | `blocked` | independent containment review |

`waiting_user` and `blocked` differ by cause, not severity. The first means a
bound was reached — the loop was working, it just ran out of rope. The second
means a run produced something the machine refuses to act on. Both need a
human; both keep every artifact.

## Protected-root drift and retained output

L2 compares filesystem snapshots; it does **not** identify the process that
wrote a file. New snapshot differences stop as `protected_root_drift`, with
`attribution: unknown`, rather than asserting the task caused a workspace escape.
This is still a fail-closed quarantine. A configured sandbox, a successful
post-run probe, or an agent's explanation does not clear it. Genuine concurrent
content changes can therefore still require operator review.

Each stage retains its own `<log-stem>.containment/` profile/git artifacts,
`<log-stem>.containment-drift.json`, and exact `<log-stem>.output.txt`. A v2
sealed manifest binds the output, drift evidence, input and profile hashes. A
`candidate_outcome` is the provider's unaccepted result, **not** a workflow edge.

```sh
python3 -m orchestrator containment-inspect TASK_ID
```

This command verifies the retained evidence using a read-only DB connection. It
does not invoke a provider, acquire a task lease, change counters, restore a
stage, or authorise advancement. Legacy v1 output can be reconstructed only when
it matches the sealed output hash; old shared drift/profile artifacts are not
historical attribution evidence. Workspace source snapshot verification is not
claimed by this inspector. No automatic result-reuse/clearance API is provided.

An ordinary resume of a drift-blocked task refuses with
`containment_review_required`, so it cannot silently spend another provider run.
After inspection, an explicitly authorised **new attempt** can be requested:

```sh
python3 -m orchestrator enqueue --resume TASK_ID --rerun-stage
```

`resume TASK_ID --rerun-stage` is the waiting equivalent. This is not acceptance
of the interrupted result: it retains its sealed manifest and quarantine, runs
the stage again under the normal guards/caps, and records `manual_rerun_stage`.
Never use it as a repeated automatic workaround for unexplained drift.

The default sentinel hash threshold is 16 MiB, streamed in 1 MiB chunks.
Identical content with only a timestamp change is not drift; above the
threshold, changes remain unverified and stop. Failed or unstable attempted
hashes fail closed.

## Containment configuration

Three environment variables shape the layers. `ORCH_PROTECTED_ROOTS` declares
what L2 watches (empty means detection is off). `ORCH_SENTINEL_EXCLUDES` adds
deployment-specific whole components or component-relative subtrees that L2
ignores; exclusions are separated by the platform path separator (`:` on
macOS) and each one reduces L2 coverage, so keep them as narrow as possible.
`ORCH_EXTRA_WRITE_ROOTS` declares additional directories a stage may write to —
a build cache, for instance — instead of turning L1 off wholesale.

A write root that overlaps a protected root refuses the stage rather than
quietly punching a hole in the layer that watches it. `ORCH_HOME` must itself
sit outside every protected root: stage logs and reports are written under it,
so a protected root covering it would make L2 flag the engine's own
bookkeeping — the daemon refuses to start on that configuration, and
`orch doctor` reports it.

Commits inside containment use a synthetic identity by default; using a real
one is explicit and per-run ([ADR 0001](decisions/0001-git-identity.md)).

## Profiles in practice

Stage reports (the executor's report, the reviewer's verdict) are directed to
a `reports/` directory inside the task's artifact directory, not into the
workspace, so the workspace diff stays the actual change. Directed, not
enforced: the prompts name the path and the sandbox allows it, but the
workspace itself remains writable, so a stage that ignores the instruction can
still create files there — that shows up in review as diff noise, not as a
containment stop. One exception is stated in the prompt itself: a deployment
whose custom runner cannot accept the external path falls back to `reports/`
inside the workspace, and the prompt header says so.

For `apply` work the default is Claude implementing and Codex reviewing.
`orch start --executor codex` selects the opposite pairing explicitly; the
older keyword forms in the brief (`executor=codex`, `codex implement`,
`let codex`) still work as a fallback.

Intake's risk vocabulary works the same way: generic defaults ship here, and a
deployment supplies its own keywords through `ORCH_RISK_RULES`. A malformed
rules file refuses to load rather than silently classifying everything as low
risk. See [`risk-rules.yaml`](../risk-rules.yaml) for the format.

## Publishing a change

CI runs both test suites on both platforms plus a **partial** sanitization
scan — partial because the scanner's site-local rules need literals (an
operator's name, a home directory, an employer domain) that deliberately never
reach this repository or a CI runner. A green badge means the tests passed and
the repository-side rules found nothing, and nothing more.

The strict scan is an operator requirement rather than something CI can attest:
whoever publishes a change is expected to run `tools/sanitize-lint.py` with a
local secrets file (the pre-commit hook refuses to run without one) and to scan
the history commit by commit before pushing. An outside reader cannot verify
that a process was followed, only that the tool exists and that the tests pass.
