# F-A2 — a spec-invented probe contradicts its own ledger

Scenario: the reviewed spec added an optional containment probe plus a per-attempt
artifact index to record the probe's output. The index schema has four columns and
no start-time column, yet a later section requires deciding, from that same file,
whether a probe started after the third round. When the probe reports nothing, the
provenance columns cannot be produced at all.

## Blocking finding

```
Source: runtime_evidence
Source reference: <ORCH_HOME>/tasks/<TASK_ID>/reports/propose-scratch/review-notes.md (H3)
Failure scenario: the probe returns an invalid report; the index row cannot be appended because two of its four required columns do not exist, so the "exactly one row per started attempt" invariant fails on a legal success path.
Material consequence: the completion evidence for the probe is unsatisfiable, so the task can never be shown complete.
Why spec must decide now: the contradiction is between two sections of the spec itself; no downstream stage can resolve it.
Simplest sufficient correction: delete the probe and its index; the underlying risk is already observable from the existing runs/*.log and evidence.json.
Finding origin: newly_added_spec_mechanism
```

## Why this routes to simplification

The contradiction exists only because the spec added a probe and a ledger to
observe itself. Patching the ledger schema adds a third mechanism to reconcile
the first two. The deletion list must name the probe and the artifact index.
