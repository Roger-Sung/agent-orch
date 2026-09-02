# F-A4 — a persistent-state decision that no design can eliminate

Scenario: the reviewed spec must place a durable SQLite database somewhere. Two
candidate roots exist and they differ in consequences the spec cannot rank on its
own: one is inside a protected root and would make every touching stage produce
`protected_root_drift`; the other is outside the user's backup scope. Every design
that satisfies the brief still has to name one root.

## Blocking finding

```
Source: user_requirement
Source reference: task immutable input, "durable state must have a single canonical path"
Failure scenario: the drafter picks a root on its own; the operator later discovers the database is outside their backup scope and the choice has already been baked into a helper module constant and every reader.
Material consequence: persistent state location, backup coverage and containment behaviour all change with the answer; reverting after cutover means a second migration.
Why spec must decide now: the canonical path is a module constant with no env override, so apply cannot defer it and stop-gate cannot hold it.
Simplest sufficient correction: none available - the choice is irreducible; the drafter must record the question with a recommended answer and let review hold.
Finding origin: original_surface
```

## Why this holds instead of routing

There is no design that satisfies the brief without naming a root. This is the
boundary of decision rule 3: the decision is not an artifact of a mechanism the
spec invented, so it cannot be removed by simplifying. The task must stop at
`waiting_user` with `stop_reason=user_decision_required`.

## Expected engine behaviour

- `tasks.status = waiting_user`, `tasks.stop_reason = user_decision_required`
- `current_stage = draft`, `owner = claude` (the CAS in the success branch runs
  unchanged, so the hold lands already pointed at the next stage)
- `tasks.input_hash` identical before the hold, during the hold, and after resume
- after `orch resume`, `claim_stage` returns `draft` with no `--rerun-stage`
