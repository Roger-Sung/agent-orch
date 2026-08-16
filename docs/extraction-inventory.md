# Extraction inventory

Per-file record of what came out of the private deployment this code grew up
in, what stayed behind, and what had to be rewritten on the way. Written
before the code was moved, so that the extraction is checkable rather than
remembered.

Legend:

- **take** — copied unchanged (modulo the repo-wide environment-variable
  rename described below)
- **rewrite** — copied, then edited to remove deployment-specific content
- **leave** — stays in the private deployment; either it is configuration for
  that deployment, or it encodes knowledge that is not mine to publish
- **new** — did not exist upstream

A repo-wide rename applies to everything taken: environment variables moved
from the deployment's `AIOS_*` namespace to `ORCH_*`
(`AIOS_ORCH_CLAUDE_COMMAND` → `ORCH_CLAUDE_COMMAND`, `AIOS_CONTAINMENT*` →
`ORCH_CONTAINMENT*`, `AIOS_FAKE_AGENT_*` → `ORCH_FAKE_AGENT_*`). The upstream
prefix named a private system and would have been meaningless here.

> **Migration note — this rename is a breaking change for the original
> deployment.** The moment that deployment stops carrying its own copy of the
> code and consumes this repository as an upstream dependency, its service
> launcher and its launchd plist still export the old `AIOS_*` names. Nothing
> reads them any more, so the provider commands fall back to their defaults
> and the daemon fails at the first stage — quietly, because a missing
> optional environment variable is not an error. Rename them in the same
> change that switches to the dependency; do not stage it as a follow-up.
>
> Checklist for that switch:
> - `run-daemon.sh` (or equivalent launcher): `AIOS_ORCH_CLAUDE_COMMAND`,
>   `AIOS_ORCH_CODEX_COMMAND`, `AIOS_ORCH_CLAUDE_MODEL`, `AIOS_ORCH_CODEX_MODEL`
> - launchd plist: any `AIOS_ORCH_*` entry in `EnvironmentVariables`
> - shell profiles, cron entries, or wrappers that export `AIOS_ORCH_HOME`,
>   `AIOS_ORCH_POLL_INTERVAL`, `AIOS_ORCH_WAIT_TIMEOUT`
> - anything asserting on `AIOS_CONTAINMENT` / `AIOS_CONTAINMENT_WORKSPACE`
>
> Quick check after the switch: `env | grep AIOS_` should be empty, and a
> provider smoke task should reach `done`.

## Engine

| Upstream path | Disposition | Notes |
|---|---|---|
| `orchestrator/__init__.py` | rewrite | docstring named the private system |
| `orchestrator/__main__.py` | take | |
| `orchestrator/cli.py` | rewrite | parser description; env var rename |
| `orchestrator/controller.py` | rewrite | stage prompt preamble named the private system |
| `orchestrator/daemon.py` | take | |
| `orchestrator/db.py` | take | |
| `orchestrator/demo.py` | take | env var rename only |
| `orchestrator/ipc.py` | take | |
| `orchestrator/profile.py` | take | strict YAML subset parser for profiles |
| `orchestrator/runner.py` | rewrite | containment hook message, default git identity, env var rename |
| `orchestrator/start.py` | rewrite | risk vocabulary trimmed to generic terms, see below |

### `start.py` risk vocabulary

Upstream `HIGH_RISK_KEYWORDS` listed four private artefacts by name (a
personal profile document, a personality document, a memory index, and a
dispatch database). Those are the deployment's vocabulary, not the engine's,
and publishing them would leak the shape of a private system for no benefit.

They are removed here. The generic terms (`orchestrator/`, `router/`,
`scheduler/`, `daemon/`, `lock/`, `memory/`, `dispatch/`, `persistent-state`)
remain as built-in defaults. A deployment supplies its own vocabulary through
`ORCH_RISK_RULES`, pointing at a file in the format documented in
`risk-rules.yaml` at the repository root (which is an example, not an active
ruleset). The loader lives in `orchestrator/risk_rules.py`: an unset variable
means an empty ruleset, a configured-but-missing file warns and continues
empty, and a malformed file refuses to load rather than silently classifying
every task as low risk. External rules can only raise an assessment, never
lower one.

One deviation from the original contract draft: rules are a mapping keyed by
rule id rather than a sequence, because the engine has exactly one YAML subset
parser and it is mapping-only. Adding a second parser to gain list syntax
would have been a worse trade than keying by an id that has to be unique
anyway.

## Profiles

| Upstream path | Disposition | Notes |
|---|---|---|
| `profiles/propose.yaml` | rewrite | prompts were in Chinese and embedded an absolute scratch path; translated and made workspace-relative |
| `profiles/spec_review.yaml` | rewrite | named the private system; report paths were deployment-absolute |
| `profiles/claude_apply_codex_review.yaml` | rewrite | same, plus an absolute worktree default |
| `profiles/codex_implement_claude_review.yaml` | rewrite | same |
| `profiles/stop_gate_claude.yaml` | rewrite | named the private system |
| `profiles/stop_gate_codex.yaml` | rewrite | named the private system |
| `profiles/provider_smoke.yaml` | take | already generic |
| `profiles/provider_smoke_gated.yaml` | take | already generic |
| `profiles/b4_memory_validation.yaml` | rewrite → `artifact_validation.yaml` | structure is a useful generic validation harness; the subject matter (a private memory subsystem) is not. Renamed, prompts generalised, profile type `b4-validation` → `artifact-validation` |

Two rewrites deserve calling out because they changed meaning, not just words:

1. **Review axis name.** Upstream reviews judged "AI-OS constraints" — the
   private platform's rules. Here the axis is "Platform constraints", i.e.
   the rules of whatever platform the task runs on.
2. **Report paths.** Upstream prompts wrote reports to an absolute path inside
   the deployment. Here they are relative to the stage's working directory,
   which is the task workspace. The specification called for a `${WORKSPACE}`
   placeholder; there is no substitution mechanism in the prompt builder
   today, so a literal placeholder would have been decoration. Relative paths
   work with the current code. Adding real substitution is a reasonable
   follow-up.

## Tests and examples

| Upstream path | Disposition | Notes |
|---|---|---|
| `tests/__init__.py` | take | |
| `tests/test_orchestrator.py` | rewrite | env var rename; profile fixture renamed; daemon-script test retargeted at `packaging/run-daemon.sh` |
| `tests/test_containment.py` | rewrite | asserted on the containment message that named the private system |
| `examples/fake_agent.py` | take | env var rename |
| `examples/demo-input.md` | take | |
| `examples/demo-loop.yaml` | take | |

## Deployment configuration — left behind

| Upstream path | Disposition | Notes |
|---|---|---|
| `com.user.aios-orchestrator.plist` | leave → template | absolute paths for one machine; `packaging/agent-orch.plist.template` ships placeholders instead |
| `run-daemon.sh` | leave → template | loaded a credential file and hard-coded one machine's layout; `packaging/run-daemon.sh` is a generic equivalent |
| `run-propose.sh` | leave | a one-line convenience wrapper around one deployment's paths |
| `HANDOFF.md` | leave | working notes between two agents in that deployment, including a credential file reference |
| `README.md` | leave → new | upstream README documents the deployment's operation; this repository needs its own |

## New in this repository

| Path | Notes |
|---|---|
| `LICENSE` | all rights reserved, source-available for evaluation |
| `risk-patterns.yaml` | sanitization rules (rules only, never values) |
| `risk-rules.yaml` | contract draft for externalised risk vocabulary |
| `tools/sanitize-lint.py` | forbidden-pattern scanner |
| `tools/pre-commit` | fail-closed pre-commit hook wrapping the scanner |
| `tools/tests/` | scanner tests and fixtures |
| `packaging/` | service templates with placeholders |
| `docs/decisions/0001-git-identity.md` | commit identity inside containment |
| `docs/extraction-inventory.md` | this file |

## Not yet done

Tracked so that the gap is visible rather than discovered later:

- `docs/threat-model.md`, including the scanner's line-by-line matching limit
  and the residual gaps L3 would close (separate UNIX identity, network egress
  allowlist).
- A recorded end-to-end run under L1 to confirm the provider CLI write
  allowlist is complete in practice; today it comes from an observed-write
  probe, not from a full task under the sandbox.
- README, architecture and lifecycle diagrams, recorded demo.
