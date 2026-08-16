# agent-orch

A stateful dispatcher for agent tasks: SQLite-backed state machine, a
single-writer daemon, per-stage caps, cross-provider stop gates, and a sealed
evidence trail. Not a prompt wrapper — the point is what happens when a run
goes wrong.

> **Status: extraction in progress.** This repository is being lifted out of a
> private system it grew up in. The engine and its tests are here; the
> containment work described below is not finished yet, and this README is a
> stub that will be replaced with the real thing (architecture and lifecycle
> diagrams, a recorded demo) before publication. See
> `docs/extraction-inventory.md` for exactly what moved and what did not.

## Why a service and not a loop

A shell loop that calls an agent repeatedly is fine until something fails
halfway. Then you need to know which stage was running, how many attempts it
already burned, whether its output was ever reviewed, and whether it is safe
to resume. That state has to live somewhere durable, and exactly one writer
has to own it. That is what this is.

- **Typed outcomes, not parsing.** A stage ends by printing one typed outcome;
  the profile maps outcomes to the next stage. Anything else is a failure.
- **Caps everywhere.** Per-stage attempt caps and per-edge transition caps, so
  a disagreeing pair of agents cannot ping-pong forever.
- **Cross-provider stop gate.** The reviewer of a stop gate comes from a
  different provider family than the executor, because a model is a poor judge
  of its own output.
- **Evidence.** Every stage writes a log and a record; a task's history is
  reconstructible after the fact.

## Containment, honestly

Three layers, and it is worth being precise about what each one does.

**Git.** Stages run inside a worktree with credentials stripped, `GIT_ASKPASS`
and `GIT_SSH_COMMAND` pointed at `/usr/bin/false`, and a `pre-push` hook that
rejects unconditionally. A result cannot leave through git. Commits still work
— the constraint is on exfiltration, not on doing the job — and they are
attributed to a synthetic identity unless a real one is explicitly requested
(`docs/decisions/0001-git-identity.md`).

**L1, prevention.** The stage's child process runs under `sandbox-exec` with a
write allowlist: its workspace, its artifact directory, the temporary
directories, and the provider CLI's own state directories. Everything else is
read-only to it. If the host cannot provide a sandbox, a mutating stage
refuses to run rather than quietly running unconfined; `--allow-unsandboxed`
is how you say you accept that, and it has to be said out loud.

**L2, detection.** Before and after each stage, a sentinel snapshot of the
declared protected roots (`ORCH_PROTECTED_ROOTS`) is compared. Anything that
changed outside the workspace blocks the task, quarantines it, and records the
offending paths as evidence — including when the stage itself reported
success, which is the case that actually matters.

**What is still missing.** The agent runs as the same UNIX user and has
unrestricted network access. A separate executor identity and an egress
allowlist would close that; neither is implemented, and `sandbox-exec` itself
is deprecated by Apple, which is why L2 exists independently of L1 rather than
as a formality.

## Try it

```sh
python3 -m orchestrator.demo          # synthetic task, fake agent, no provider calls
python3 -m unittest discover -s orchestrator/tests
python3 -m unittest discover -s tools/tests
```

## Layout

```
orchestrator/        engine: controller, daemon, db, ipc, profile, runner, cli, start
orchestrator/profiles/   stage machines (propose, review, apply, stop gate, smoke)
orchestrator/examples/   fake agent and a demo profile
tools/               sanitization scanner and the pre-commit hook wrapping it
packaging/           service templates (placeholders, not machine paths)
docs/                decisions and the extraction inventory
```

## License

All rights reserved. Source-available for reading and evaluation only — see
[LICENSE](LICENSE). This is not open source, and no use, redistribution, or
derivative work is permitted without written permission.
