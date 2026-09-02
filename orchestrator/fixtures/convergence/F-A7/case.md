# F-A7 — a second scheduler proposed to supervise the first

Scenario: the reviewed spec worries that the existing scheduler may miss a run. Its
remedy is a second scheduler that watches the first, plus a liveness counter and a
recovery branch for the case where the supervisor itself stalls.

## Blocking finding

```
Source: established_invariant
Source reference: convergence policy, burden of proof question 5
Failure scenario: the supervisor stalls. Nothing watches it, so the spec has moved the original failure one level up and added the supervisor's own failure modes (its lease, its counter, its recovery branch) on top.
Material consequence: two control planes with overlapping ownership of the same runs; a stall is now harder to diagnose, not easier.
Why spec must decide now: the supervisor is persistent infrastructure; adding it is not reversible by apply.
Simplest sufficient correction: delete the supervisor. Observe missed runs directly from the existing evidence.json and runs/*.log, which already record every transition.
Finding origin: newly_added_spec_mechanism
```

## Why this routes to simplification

Burden-of-proof question 5 fails outright: the new mechanism is more complex than
the risk it controls, and question 4 also fails because existing evidence already
makes the missed run observable. The deletion list must name the supervisor, its
counter and its recovery branch.
