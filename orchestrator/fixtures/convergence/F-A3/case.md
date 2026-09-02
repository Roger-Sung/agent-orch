# F-A3 — one original correction alongside three invented-mechanism contradictions

Scenario: a single review round produces four blocking findings. One names a real
risk in the surface being specified. Three name contradictions among counters,
windows and attestations that the spec itself introduced.

## Blocking findings

```
Source: runtime_evidence
Source reference: <ORCH_HOME>/tasks/<TASK_ID>/reports/propose-scratch/review-notes.md (H2)
Failure scenario: write-mode connect keeps create semantics, so a wrong path yields a silent empty DB.
Material consequence: silent data loss.
Why spec must decide now: it is the spec's own durability contract.
Simplest sufficient correction: no-create URI plus fail-closed identity check.
Finding origin: original_surface
```

```
Source: runtime_evidence
Source reference: same review-notes.md (H4)
Failure scenario: three serial steps are scheduled 15 seconds apart while the spec's own estimate puts one of them at 20 seconds, so the window can never be met.
Material consequence: the timed window is unexecutable; any run reports a deadline miss.
Why spec must decide now: the schedule is spec-fixed.
Simplest sufficient correction: delete the second-level choreography.
Finding origin: newly_added_spec_mechanism
```

```
Source: runtime_evidence
Source reference: same review-notes.md (H5)
Failure scenario: the disposition gate accepts resumable statuses, so a workspace can be removed while the task can still be resumed.
Material consequence: a resumable task loses its workspace.
Why spec must decide now: the gate predicate is spec-fixed.
Simplest sufficient correction: reuse the existing terminal-status set instead of inventing a predicate.
Finding origin: newly_added_spec_mechanism
```

```
Source: runtime_evidence
Source reference: same review-notes.md (H6)
Failure scenario: a completion-evidence equation counts a file created by SQLite backup as if mkdir created it, so no legal success path satisfies it.
Material consequence: the task cannot unlock its successor.
Why spec must decide now: the equation is the task's completion evidence.
Simplest sufficient correction: delete the call-count equation.
Finding origin: newly_added_spec_mechanism
```

## Why this routes to simplification, and what must survive

Rule 1 of the outcome decision applies: any `newly_added_spec_mechanism` blocking
finding routes to `simplify`, even when a correction is present in the same round.
The correction is not lost — the simplify output must carry the no-create open
contract in its "original risk to post-simplification owner" table.
