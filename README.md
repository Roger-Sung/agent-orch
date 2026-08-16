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

Stages run inside a git worktree with credentials stripped and push disabled
by a hook, so a result cannot leave through git. That is real, and it is also
not a sandbox: the agent is the same UNIX user and can still write outside the
worktree. That gap is why this extraction exists — the write allowlist and
out-of-workspace write detection are the next piece of work, and until they
land the honest description is the one above.

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
