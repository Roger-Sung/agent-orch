# F-A4b — looks like a missing decision, but a decision-free design exists

Distilled from how decision D-A6 of the convergence policy was actually reached.

Scenario: the reviewed spec needs a stage outcome that stops a task at
`waiting_user`. Its draft proposes a new per-stage `holds` profile field and then
asks the user to decide whether to widen the profile schema for it, on the grounds
that the change touches routing and persistent state.

## Blocking finding

```
Source: established_invariant
Source reference: orchestrator/profile.py (_exact_keys for non-terminal stages) and Profile.to_dict
Failure scenario: the drafter escalates "should we widen the shared profile schema" to the user; the lifecycle stops on a question nobody needs answered, because a design exists that reaches the same stop semantics with zero schema change - an engine-reserved outcome name, which is already carried in the snapshot and needs no new field.
Material consequence: a hold that costs an operator round-trip and produces no decision; the cheaper design is never surfaced.
Why spec must decide now: the reviewer must choose between holding and requiring the decision-free design in this same round.
Simplest sufficient correction: require the reserved-outcome-name design, which passes the burden-of-proof questions, and drop the schema-widening proposal along with the question it created.
Finding origin: original_surface
```

## Why this routes to correction rather than holding

Decision rule 3 applies only when **every** design that satisfies the brief must
make the decision. Here one does not. The reviewer's correct move is to require
that design, so the finding routes to `draft`. Escalating a removable decision to
a hold parks the lifecycle on a question with no audience.
